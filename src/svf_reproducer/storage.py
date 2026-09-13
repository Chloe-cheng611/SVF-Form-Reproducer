"""不変世代の保存と実行台帳。

候補の書込・同期・manifest確認を終えてから世代を確定し、最後にcurrentを切り替える。
期限切れやPID消滅を理由に自動解除・再投入はしない。結果不明はUNKNOWNとして残す。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import socket
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .config import json_bytes, sha256_bytes, write_json
from .locking import fsync_directory, write_bytes_durable
from .locking import file_lock as _locked
from .models import Issue, RuntimeResult

REVISION_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
RUNNING_STATES = {"RUNNING"}

Artifacts = dict[str, "bytes | str | dict | list"]


def now_text() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _encode(value: bytes | str | dict | list) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8")
    return json_bytes(value)


def save_revision(workspace: str | Path, revision: str, artifacts: Artifacts) -> Path:
    root = Path(workspace)
    with _locked(root / "revision.lock"):
        return _save_revision_unlocked(root, revision, artifacts)


def _write_tree(directory: Path, artifacts: Artifacts) -> dict[str, str]:
    manifest: dict[str, str] = {}
    base = directory.resolve()
    for relative, value in artifacts.items():
        target = (directory / relative).resolve()
        if base not in target.parents:
            raise ValueError(f"artifact path escapes revision: {relative}")
        body = _encode(value)
        write_bytes_durable(target, body)
        if target.read_bytes() != body:
            raise OSError(f"read-back mismatch: {relative}")
        manifest[relative] = sha256_bytes(body)
    return manifest


def _save_revision_unlocked(root: Path, revision: str, artifacts: Artifacts) -> Path:
    if not REVISION_NAME.fullmatch(revision) or revision.endswith(".tmp"):
        raise ValueError("revisionは英数字で始まる128文字以内の安全な名前が必要です")
    revisions = root / "revisions"
    revisions.mkdir(parents=True, exist_ok=True)
    final = revisions / revision
    temporary = revisions / f"{revision}.tmp"
    if final.exists():
        raise FileExistsError(f"revision already exists: {revision}")
    if temporary.exists():
        raise FileExistsError(f"incomplete revision exists: {temporary}")
    temporary.mkdir()
    manifest = _write_tree(temporary, artifacts)
    manifest_body = json_bytes({"revision": revision, "files": manifest})
    write_bytes_durable(temporary / "manifest.json", manifest_body)
    for directory in sorted({(temporary / relative).parent for relative in manifest}, key=lambda x: len(x.parts), reverse=True):
        fsync_directory(directory)
    fsync_directory(temporary)
    os.replace(temporary, final)
    fsync_directory(revisions)
    _verify_tree(final, json.loads(manifest_body))
    write_json(root / "current.json", {"revision": revision, "manifest_hash": sha256_bytes(manifest_body), "committed_at": now_text()})
    return final


def save_diagnostic(workspace: str | Path, name: str, artifacts: Artifacts) -> Path:
    """検査に失敗した候補を診断領域へ保存する。currentと旧世代は変えない。"""
    if not REVISION_NAME.fullmatch(name):
        raise ValueError("診断名は英数字で始まる128文字以内の安全な名前が必要です")
    root = Path(workspace) / "diagnostics"
    with _locked(Path(workspace) / "revision.lock"):
        target = root / f"{name}-{dt.datetime.now().strftime('%Y%m%dT%H%M%S')}"
        target.mkdir(parents=True, exist_ok=False)
        _write_tree(target, artifacts)
        fsync_directory(target)
        return target


def _verify_tree(path: Path, manifest: dict[str, Any]) -> None:
    for relative, expected in manifest.get("files", {}).items():
        artifact = path / relative
        if not artifact.is_file() or sha256_bytes(artifact.read_bytes()) != expected:
            raise OSError(f"revision artifact hash mismatch: {relative}")


def revision_manifest_hash(path: str | Path) -> str:
    return sha256_bytes(Path(path, "manifest.json").read_bytes())


def load_revision(workspace: str | Path, revision: str) -> Path:
    root = Path(workspace)
    path = root / "revisions" / revision
    if not REVISION_NAME.fullmatch(revision) or revision.endswith(".tmp") or not path.is_dir():
        raise FileNotFoundError(f"revision not found: {revision}")
    manifest_body = (path / "manifest.json").read_bytes()
    _verify_tree(path, json.loads(manifest_body))
    return path


def load_current(workspace: str | Path) -> Path:
    root = Path(workspace)
    with (root / "current.json").open("r", encoding="utf-8") as stream:
        current = json.load(stream)
    path = root / "revisions" / current["revision"]
    if not path.is_dir() or path.name.endswith(".tmp"):
        raise FileNotFoundError("current revision is incomplete or missing")
    manifest_body = (path / "manifest.json").read_bytes()
    if sha256_bytes(manifest_body) != current.get("manifest_hash"):
        raise OSError("current manifest hash mismatch")
    _verify_tree(path, json.loads(manifest_body))
    return path


@contextmanager
def execution_slot(workspace: str | Path) -> Iterator[None]:
    """予約から結果記録まで、ワークスペース全体の実行枠を保持する。"""
    with _locked(Path(workspace) / "runtime-slot.lock"):
        yield


class JobLedger:
    def __init__(self, workspace: str | Path):
        self.workspace = Path(workspace)
        self.path = self.workspace / "runtime-ledger.json"
        self.lock_path = self.workspace / "runtime-ledger.lock"

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"schema": "svf-runtime-ledger/1.1", "jobs": {}}
        with self.path.open("r", encoding="utf-8") as stream:
            value = json.load(stream)
        value.setdefault("jobs", {})
        return value

    def entries(self) -> dict[str, Any]:
        with _locked(self.lock_path):
            return self._read()["jobs"]

    @staticmethod
    def _replay(old: dict[str, Any], input_hash: str, *, allow_empty: bool) -> RuntimeResult | list[Issue] | None:
        if old["input_hash"] != input_hash:
            return [Issue("JOB_ID_CONFLICT", "同じjob_idに異なる入力が指定されました", stage="runtime")]
        if old.get("state") in RUNNING_STATES:
            return [Issue("JOB_ALREADY_RUNNING", "同じjob_idの実行中記録があります。statusとreconcileで確認してください", stage="runtime")]
        result = old.get("result")
        if not result:
            return None
        if result["status"] == "SUCCEEDED" or (allow_empty and result["status"] == "EMPTY"):
            return RuntimeResult.from_dict(result)
        if result["status"] == "UNKNOWN":
            return [Issue("JOB_RESULT_UNKNOWN", "照会または手動確認まで再投入できません", stage="runtime")]
        return None

    def before_run(self, job_id: str, input_hash: str) -> RuntimeResult | list[Issue] | None:
        with _locked(self.lock_path):
            old = self._read()["jobs"].get(job_id)
            return None if old is None else self._replay(old, input_hash, allow_empty=False)

    def claim(
        self,
        job_id: str,
        input_hash: str,
        *,
        input_manifest_hash: str | None = None,
        runtime_job_id: str | None = None,
    ) -> RuntimeResult | list[Issue] | None:
        with _locked(self.lock_path):
            ledger = self._read()
            old = ledger["jobs"].get(job_id)
            if old:
                replay = self._replay(old, input_hash, allow_empty=True)
                if replay is not None:
                    return replay
            running = sorted(key for key, value in ledger["jobs"].items() if value.get("state") in RUNNING_STATES and key != job_id)
            if running:
                return [Issue("WORKSPACE_BUSY", "他のjob_idが実行中です: " + ", ".join(running), stage="runtime")]
            ledger["jobs"][job_id] = {
                "input_hash": input_hash,
                "input_manifest_hash": input_manifest_hash,
                "state": "RUNNING",
                "runtime_job_id": runtime_job_id,
                "started_at": now_text(),
                "host": socket.gethostname(),
                "pid": os.getpid(),
            }
            write_json(self.path, ledger)
            return None

    def record(self, job_id: str, input_hash: str, result: RuntimeResult) -> None:
        with _locked(self.lock_path):
            ledger = self._read()
            existing = ledger["jobs"].get(job_id, {})
            if existing and existing.get("input_hash") != input_hash:
                raise ValueError("job_id conflict")
            ledger["jobs"][job_id] = {
                "input_hash": input_hash,
                "input_manifest_hash": result.input_manifest_hash or existing.get("input_manifest_hash"),
                "state": "FINISHED",
                "runtime_job_id": result.runtime_job_id,
                "started_at": result.started_at or existing.get("started_at"),
                "finished_at": result.finished_at or now_text(),
                "host": result.host or existing.get("host"),
                "pid": result.pid if result.pid is not None else existing.get("pid"),
                "result": result.to_dict(),
            }
            write_json(self.path, ledger)

    def reconcile(
        self,
        job_id: str,
        status: str,
        *,
        operator: str,
        evidence: str,
        checked_at: str | None = None,
    ) -> dict[str, Any]:
        """外部ジョブ照会または未送信の根拠に基づき、UNKNOWNから状態を変更する。"""
        if status not in {"SUCCEEDED", "FAILED", "EMPTY", "UNKNOWN"}:
            raise ValueError("statusはSUCCEEDED、FAILED、EMPTY、UNKNOWNのいずれかが必要です")
        if not operator.strip() or not evidence.strip():
            raise ValueError("確認者と外部照会結果または未送信の根拠が必要です")
        with _locked(self.lock_path):
            ledger = self._read()
            entry = ledger["jobs"].get(job_id)
            if entry is None:
                raise KeyError(job_id)
            result = entry.get("result") or {
                "status": "UNKNOWN",
                "runtime_job_id": entry.get("runtime_job_id") or job_id,
                "data_count": 0,
                "artifact_paths": [],
                "log_paths": [],
                "error_code": "RUNTIME_INTERRUPTED",
                "issues": [],
                "input_manifest_hash": entry.get("input_manifest_hash"),
                "artifacts": [],
                "started_at": entry.get("started_at"),
                "finished_at": None,
                "host": entry.get("host"),
                "pid": entry.get("pid"),
            }
            result["status"] = status
            entry["result"] = result
            entry["state"] = "FINISHED"
            entry.setdefault("reconciliations", []).append(
                {"status": status, "operator": operator, "evidence": evidence, "checked_at": checked_at or now_text()}
            )
            ledger["jobs"][job_id] = entry
            write_json(self.path, ledger)
            return entry
