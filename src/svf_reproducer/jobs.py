"""実行入力の固定、項目契約の検査、外部処理の呼出と結果記録。"""
from __future__ import annotations

import csv
import datetime as dt
import decimal
import io
import os
import re
import socket
import subprocess
import uuid
from pathlib import Path
from typing import Any

from .config import json_bytes, sha256_file, stable_json_hash
from .locking import write_bytes_durable
from .models import Issue, Job, MappingConfig, MappingField, RuntimeResult, TargetProfile
from .storage import now_text

DECIMAL_TEXT = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$")
DATE_TEXT = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def load_job(path: str | Path) -> Job:
    source = Path(path)
    with source.open("r", encoding="utf-8") as stream:
        import json

        value = json.load(stream)
    job = Job.from_dict(value)
    data_file = Path(job.data_file)
    if not data_file.is_absolute():
        job.data_file = str((source.parent / data_file).resolve())
    return job


def _validate_value(value: str, field: MappingField, row_number: int, profile: TargetProfile) -> list[Issue]:
    issues: list[Issue] = []
    target = f"{field.source_key}@{row_number}"
    is_null = field.null_policy == "null_token" and field.null_token is not None and value == field.null_token
    if value == "" and field.required:
        issues.append(Issue("VALUE_REQUIRED", "必須値が空です", target_id=target, stage="job"))
    elif value == "" and field.null_policy == "error":
        issues.append(Issue("NULL_NOT_ALLOWED", "空値を許可しない項目です", target_id=target, stage="job"))
    if is_null and field.required:
        issues.append(Issue("NULL_NOT_ALLOWED", "必須値にNULLトークンが指定されています", target_id=target, stage="job"))
    if value and not is_null:
        if field.value_type == "decimal":
            if not DECIMAL_TEXT.fullmatch(value):
                issues.append(Issue("VALUE_TYPE_INVALID", "採用する十進数の字句規則に一致しません", target_id=target, stage="job"))
            else:
                try:
                    if not decimal.Decimal(value).is_finite():
                        raise decimal.InvalidOperation
                except decimal.InvalidOperation:
                    issues.append(Issue("VALUE_TYPE_INVALID", "十進数として解釈できません", target_id=target, stage="job"))
        elif field.value_type == "date":
            if not DATE_TEXT.fullmatch(value):
                issues.append(Issue("VALUE_TYPE_INVALID", "日付はYYYY-MM-DD表記が必要です", target_id=target, stage="job"))
            else:
                try:
                    dt.date.fromisoformat(value)
                except ValueError:
                    issues.append(Issue("VALUE_TYPE_INVALID", "存在しない日付です", target_id=target, stage="job"))
        if field.max_chars is not None and len(value) > field.max_chars:
            issues.append(Issue("VALUE_OVERFLOW", f"{field.max_chars}文字を超えています", target_id=target, stage="job"))
        if ("\n" in value or "\r" in value) and not profile.supports_embedded_newlines:
            issues.append(Issue("NEWLINE_UNSUPPORTED", "出力経路が埋込改行に未対応です", target_id=target, stage="job"))
    return issues


def _scan_line_endings(text: str, profile: TargetProfile) -> list[Issue]:
    """引用内の改行とレコード区切りを分けて検査する。"""
    issues: list[Issue] = []
    inside = False
    index = 0
    length = len(text)
    while index < length:
        character = text[index]
        if character == '"':
            if inside and index + 1 < length and text[index + 1] == '"':
                index += 2
                continue
            inside = not inside
        elif character in "\r\n":
            if inside:
                if not profile.supports_embedded_newlines:
                    issues.append(Issue("NEWLINE_UNSUPPORTED", "引用内の改行に出力経路が未対応です", stage="job"))
                    break
            elif not (character == "\r" and index + 1 < length and text[index + 1] == "\n"):
                issues.append(Issue("CSV_LINE_ENDING_INVALID", "共通CSVのレコード区切りはCRLFが必要です", stage="job"))
                break
            if character == "\r" and index + 1 < length and text[index + 1] == "\n":
                index += 1
        index += 1
    return issues


def read_csv(raw: bytes, profile: TargetProfile) -> tuple[list[str], list[list[str]], list[Issue]]:
    issues: list[Issue] = []
    if raw.startswith(b"\xef\xbb\xbf"):
        issues.append(Issue("CSV_BOM_FORBIDDEN", "共通CSVはUTF-8 BOMなしです", stage="job"))
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        return [], [], issues + [Issue("CSV_ENCODING_INVALID", str(exc), stage="job")]
    issues.extend(_scan_line_endings(text, profile))
    try:
        records = list(csv.reader(io.StringIO(text, newline="")))
    except csv.Error as exc:
        return [], [], issues + [Issue("CSV_INVALID", str(exc), stage="job")]
    if records and records[-1] == []:
        records.pop()
    if not records:
        return [], [], issues + [Issue("CSV_HEADER_MISSING", "0件でもCSVヘッダー行が必要です", stage="job")]
    header = records[0]
    duplicates = sorted({name for name in header if header.count(name) > 1})
    if duplicates:
        issues.append(Issue("CSV_HEADER_DUPLICATE", "ヘッダー列が重複しています: " + ", ".join(duplicates), stage="job"))
    if any(name.strip() == "" for name in header):
        issues.append(Issue("CSV_HEADER_INVALID", "空のヘッダー列があります", stage="job"))
    return header, records[1:], issues


def validate_job(job: Job, mapping: MappingConfig, profile: TargetProfile) -> tuple[list[dict[str, str]], list[Issue]]:
    issues: list[Issue] = []
    if job.empty_policy == "emit_form" and not profile.supports_empty_form:
        issues.append(Issue("EMPTY_FORM_UNSUPPORTED", "emit_formは対象経路で未確認です", stage="job"))
    if job.output_profile != profile.profile_id:
        issues.append(Issue("PROFILE_MISMATCH", "jobとprofileのIDが一致しません", stage="job"))
    expected_mapping_hash = stable_json_hash(mapping.to_dict())
    if job.mapping_hash != expected_mapping_hash:
        issues.append(Issue("MAPPING_HASH_MISMATCH", "jobのmapping_hashが現在の対応表と一致しません", stage="job"))
    try:
        raw = Path(job.data_file).read_bytes()
    except OSError as exc:
        return [], issues + [Issue("DATA_FILE_MISSING", str(exc), stage="job")]
    header, records, csv_issues = read_csv(raw, profile)
    issues.extend(csv_issues)
    if not header:
        return [], issues
    detail_fields = [x for x in mapping.fields if x.scope == "detail"]
    header_fields = [x for x in mapping.fields if x.scope == "header"]
    seen_targets: set[tuple[str, str]] = set()
    for field in mapping.fields:
        key = (field.scope, field.svf_field)
        if key in seen_targets:
            issues.append(Issue("MAPPING_DUPLICATE_TARGET", f"{field.scope}のField {field.svf_field!r}が重複しています", target_id=field.svf_field, stage="job"))
        seen_targets.add(key)
        if field.scope == "detail" and len(profile.record_group) > 1 and not field.record_id:
            issues.append(Issue("MAPPING_RECORD_REQUIRED", "複数Recordではrecord_idが必要です", target_id=field.source_key, stage="job"))
    required_columns = {x.source_key for x in detail_fields}
    for field in detail_fields:
        if field.source_key not in header:
            issues.append(Issue("CSV_COLUMN_MISSING", f"列{field.source_key!r}がありません", target_id=field.source_key, stage="job"))
    extra = [name for name in header if name not in required_columns]
    if extra:
        issues.append(Issue("CSV_COLUMN_UNEXPECTED", "対応表にない列があります: " + ", ".join(sorted(set(extra))), stage="job"))
    rows: list[dict[str, str]] = []
    for number, record in enumerate(records, 1):
        if len(record) < len(header):
            issues.append(Issue("CSV_CELL_MISSING", f"{number}行目のセルが{len(header) - len(record)}個不足しています", target_id=f"row@{number}", stage="job"))
        elif len(record) > len(header):
            issues.append(Issue("CSV_CELL_EXTRA", f"{number}行目のセルが{len(record) - len(header)}個多くあります", target_id=f"row@{number}", stage="job"))
        rows.append({name: record[index] for index, name in enumerate(header) if index < len(record)})
    if len(rows) != job.data_count:
        issues.append(Issue("DATA_COUNT_MISMATCH", f"CSV={len(rows)}、job={job.data_count}", stage="job"))
    for field in header_fields:
        if field.source_key not in job.header:
            if field.required:
                issues.append(Issue("HEADER_VALUE_MISSING", f"共通値{field.source_key!r}がありません", target_id=field.source_key, stage="job"))
            continue
        issues.extend(_validate_value(str(job.header[field.source_key]), field, 0, profile))
    for number, row in enumerate(rows, 1):
        for field in detail_fields:
            if field.source_key not in header:
                continue
            value = row.get(field.source_key)
            if value is None:
                issues.append(Issue("VALUE_MISSING", f"必要な列{field.source_key!r}の値がありません", target_id=f"{field.source_key}@{number}", stage="job"))
                continue
            issues.extend(_validate_value(value, field, number, profile))
    return rows, issues


def runtime_input_manifest(form_path: str | Path, job: Job, mapping: MappingConfig, profile: TargetProfile) -> dict[str, Any]:
    """XML、CSV、mapping、profile、実行器、素材の識別値を記録する。"""
    job_body = job.to_dict()
    job_body["data_file"] = Path(job.data_file).name
    try:
        csv_digest = sha256_file(job.data_file)
        csv_size = Path(job.data_file).stat().st_size
    except OSError as exc:
        csv_digest, csv_size = f"unavailable:{exc.errno}", -1
    assets = {}
    for name, value in sorted(profile.assets.items()):
        try:
            assets[name] = sha256_file(value)
        except OSError:
            assets[name] = "missing"
    return {
        "schema": "svf-runtime-input/1.0",
        "form": sha256_file(form_path),
        "data_csv": {"sha256": csv_digest, "bytes": csv_size},
        "job": job_body,
        "mapping": mapping.to_dict(),
        "profile": profile.to_dict(),
        "runtime_command": profile.runtime_command,
        "fonts": profile.fonts,
        "assets": assets,
    }


def runtime_input_hash(form_path: str | Path, job: Job, mapping: MappingConfig, profile: TargetProfile) -> str:
    return stable_json_hash(runtime_input_manifest(form_path, job, mapping, profile))


def fixate_inputs(form_path: str | Path, job: Job, mapping: MappingConfig, profile: TargetProfile, run_dir: Path) -> tuple[dict[str, Path], dict[str, Any]]:
    """実行用入力を専用ディレクトリへ固定し、そのコピーを検査・実行する。"""
    directory = run_dir / "input"
    directory.mkdir(parents=True, exist_ok=True)
    form_copy = directory / "form.xml"
    csv_copy = directory / "data.csv"
    write_bytes_durable(form_copy, Path(form_path).read_bytes())
    write_bytes_durable(csv_copy, Path(job.data_file).read_bytes())
    fixed_job = Job.from_dict({**job.to_dict(), "data_file": str(csv_copy.resolve())})
    paths = {
        "form": form_copy,
        "csv": csv_copy,
        "job": directory / "job.json",
        "mapping": directory / "mapping.json",
        "profile": directory / "profile.json",
    }
    write_bytes_durable(paths["job"], json_bytes(fixed_job.to_dict()))
    write_bytes_durable(paths["mapping"], json_bytes(mapping.to_dict()))
    write_bytes_durable(paths["profile"], json_bytes(profile.to_dict()))
    manifest = runtime_input_manifest(form_copy, fixed_job, mapping, profile)
    write_bytes_durable(run_dir / "input-manifest.json", json_bytes(manifest))
    return paths, manifest


def _stream_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def inspect_artifacts(paths: list[str]) -> tuple[list[dict[str, Any]], list[Issue]]:
    """成果物の非空・解析可能・ページ数を確認する。ハッシュ一致は改変検知にすぎない。"""
    report: list[dict[str, Any]] = []
    issues: list[Issue] = []
    for value in paths:
        path = Path(value)
        size = path.stat().st_size if path.is_file() else 0
        entry: dict[str, Any] = {"path": str(path), "bytes": size, "sha256": sha256_file(path) if size else None, "pages": None}
        if size == 0:
            issues.append(Issue("ARTIFACT_EMPTY", f"成果物が0バイトです: {path.name}", stage="runtime"))
            report.append(entry)
            continue
        if path.suffix.lower() == ".pdf":
            head = path.read_bytes()[:1024]
            tail = path.read_bytes()[-2048:]
            if not head.startswith(b"%PDF-"):
                issues.append(Issue("ARTIFACT_INVALID", f"PDFの署名がありません: {path.name}", stage="runtime"))
            elif b"%%EOF" not in tail:
                issues.append(Issue("ARTIFACT_INVALID", f"PDFの終端がありません: {path.name}", stage="runtime"))
            else:
                try:
                    from pypdf import PdfReader  # type: ignore

                    entry["pages"] = len(PdfReader(str(path)).pages)
                    if entry["pages"] < 1:
                        issues.append(Issue("ARTIFACT_INVALID", f"PDFのページがありません: {path.name}", stage="runtime"))
                except ImportError:
                    issues.append(Issue("ARTIFACT_PAGES_UNVERIFIED", "pypdf未導入のためページ数を確認できません", "warning", "runtime"))
                except Exception as exc:  # pypdfは多様な例外を投げる
                    issues.append(Issue("ARTIFACT_INVALID", f"PDFを解析できません: {path.name}: {exc}", stage="runtime"))
        report.append(entry)
    return report, issues


def run_runtime(
    form_path: str | Path,
    job: Job,
    mapping: MappingConfig,
    profile: TargetProfile,
    output_dir: str | Path,
    *,
    run_dir: Path | None = None,
    input_manifest_hash: str | None = None,
) -> RuntimeResult:
    started = now_text()
    host = socket.gethostname()
    rows, issues = validate_job(job, mapping, profile)
    errors = [x for x in issues if x.severity == "error"]
    if errors:
        return RuntimeResult("FAILED", job.job_id, job.data_count, error_code=errors[0].code, issues=issues, started_at=started, host=host, input_manifest_hash=input_manifest_hash)
    if job.data_count == 0 and job.empty_policy == "skip":
        return RuntimeResult("EMPTY", job.job_id, 0, issues=issues, started_at=started, finished_at=now_text(), host=host, input_manifest_hash=input_manifest_hash)
    ready = profile.readiness_issues(require_runtime=True)
    if ready:
        return RuntimeResult("FAILED", job.job_id, job.data_count, error_code="PROFILE_UNREADY", issues=issues + ready, started_at=started, host=host, input_manifest_hash=input_manifest_hash)
    invalid_globs = [pattern for pattern in profile.runtime_artifact_globs if Path(pattern).is_absolute() or ".." in Path(pattern).parts]
    if invalid_globs:
        issues.append(Issue("PROFILE_INVALID", "成果物globに絶対パスまたは..は使用できません", stage="runtime"))
        return RuntimeResult("FAILED", job.job_id, job.data_count, error_code="PROFILE_INVALID", issues=issues, started_at=started, host=host, input_manifest_hash=input_manifest_hash)
    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    safe_job_id = re.sub(r"[^A-Za-z0-9._-]", "_", job.job_id).strip("._")[:80] or "job"
    runtime_job_id = f"{safe_job_id}-{uuid.uuid4().hex[:8]}"
    if run_dir is None:
        run_dir = target / runtime_job_id
        run_dir.mkdir()
    inputs, manifest = fixate_inputs(form_path, job, mapping, profile, run_dir)
    input_manifest_hash = input_manifest_hash or stable_json_hash(manifest)
    replacements = {
        "{form}": str(inputs["form"].resolve()),
        "{data}": str(inputs["csv"].resolve()),
        "{job}": str(inputs["job"].resolve()),
        "{mapping}": str(inputs["mapping"].resolve()),
        "{profile}": str(inputs["profile"].resolve()),
        "{output_dir}": str(run_dir.resolve()),
        "{runtime_job_id}": runtime_job_id,
    }
    command = []
    for part in profile.runtime_command:
        for token, replacement in replacements.items():
            part = part.replace(token, replacement)
        command.append(part)
    log_path = run_dir / "runtime.log"
    env = dict(os.environ)
    env["SVF_RUNTIME_JOB_ID"] = runtime_job_id
    common = {
        "started_at": started,
        "host": host,
        "input_manifest_hash": input_manifest_hash,
    }
    try:
        completed = subprocess.run(command, text=True, capture_output=True, timeout=profile.runtime_timeout_seconds, check=False, env=env)
    except subprocess.TimeoutExpired as exc:
        log_path.write_text(_stream_text(exc.stdout) + "\n" + _stream_text(exc.stderr), encoding="utf-8")
        issues.append(Issue("RUNTIME_TIMEOUT", "時間内に終了しませんでした。送信済みかは外部照会で確認します", stage="runtime"))
        return RuntimeResult("UNKNOWN", runtime_job_id, job.data_count, log_paths=[str(log_path)], error_code="RUNTIME_TIMEOUT", issues=issues, finished_at=now_text(), **common)
    except (FileNotFoundError, PermissionError, NotADirectoryError) as exc:
        log_path.write_text(str(exc), encoding="utf-8")
        issues.append(Issue("RUNTIME_START_FAILED", f"起動に失敗し未送信です: {exc}", stage="runtime"))
        return RuntimeResult("FAILED", runtime_job_id, job.data_count, log_paths=[str(log_path)], error_code="RUNTIME_START_FAILED", issues=issues, finished_at=now_text(), **common)
    except (OSError, KeyboardInterrupt) as exc:
        log_path.write_text(str(exc), encoding="utf-8")
        issues.append(Issue("RUNTIME_INTERRUPTED", f"起動後に中断しました。未出力と証明できません: {exc}", stage="runtime"))
        return RuntimeResult("UNKNOWN", runtime_job_id, job.data_count, log_paths=[str(log_path)], error_code="RUNTIME_INTERRUPTED", issues=issues, finished_at=now_text(), **common)
    log_path.write_text(_stream_text(completed.stdout) + "\n" + _stream_text(completed.stderr), encoding="utf-8")
    run_root = run_dir.resolve()
    artifacts = sorted(
        {
            str(resolved)
            for pattern in profile.runtime_artifact_globs
            for path in run_dir.glob(pattern)
            if path.is_file()
            for resolved in [path.resolve()]
            if run_root in resolved.parents
        }
    )
    if completed.returncode != 0:
        issues.append(Issue("RUNTIME_FAILED", f"実行器が終了コード{completed.returncode}を返しました。送信済みの可能性があるため無条件に再試行しません", stage="runtime"))
        return RuntimeResult("FAILED", runtime_job_id, job.data_count, artifacts, [str(log_path)], "RUNTIME_FAILED", issues, finished_at=now_text(), **common)
    if not artifacts:
        issues.append(Issue("RUNTIME_ARTIFACT_MISSING", "実行は終了しましたが成果物がありません", stage="runtime"))
        return RuntimeResult("FAILED", runtime_job_id, job.data_count, [], [str(log_path)], "RUNTIME_ARTIFACT_MISSING", issues, finished_at=now_text(), **common)
    report, artifact_issues = inspect_artifacts(artifacts)
    issues.extend(artifact_issues)
    if [x for x in artifact_issues if x.severity == "error"]:
        return RuntimeResult("FAILED", runtime_job_id, job.data_count, artifacts, [str(log_path)], "RUNTIME_ARTIFACT_INVALID", issues, artifacts=report, finished_at=now_text(), **common)
    return RuntimeResult("SUCCEEDED", runtime_job_id, job.data_count, artifacts, [str(log_path)], None, issues, artifacts=report, finished_at=now_text(), **common)
