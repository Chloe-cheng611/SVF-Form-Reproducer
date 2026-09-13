"""GUIとCLIが共有する作業単位。

入力は開始時にバイト列として固定する。検査を通った候補だけを不変世代へ確定し、
最後にcurrentを切り替える。検査に失敗した候補は診断領域にだけ残す。
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from .annotations import AnnotationSet
from .config import (
    load_mapping,
    load_profile,
    read_json,
    same_file,
    sha256_bytes,
    sha256_file,
    stable_json_hash,
    write_json,
)
from .jobs import load_job, run_runtime, runtime_input_manifest
from .layout import analyze_record_layouts
from .locking import write_bytes_durable
from .models import Issue, RuntimeResult
from .preview import render_svg
from .proposals import apply_proposal, build_proposal_payload, request_proposal
from .storage import JobLedger, execution_slot, load_revision, save_diagnostic, save_revision
from .validation import structural_report
from .xmlcore import compile_xml, extract_ir


class SamePathError(ValueError):
    pass


def _reject_same_path(source: str | Path, output: str | Path) -> None:
    if same_file(source, output):
        raise SamePathError("入力XMLと出力XMLに同じ実体を指定できません。原本を保持するため中止します")


def import_form(xml_path: str | Path, profile_path: str | Path, workspace: str | Path, revision: str | None = None) -> Path:
    profile = load_profile(profile_path)
    payload = Path(xml_path).read_bytes()
    ir = extract_ir(xml_path, profile, data=payload)
    revision = revision or ir.source_hash[:12]
    report = structural_report(xml_path, profile, form_revision=revision)
    return save_revision(
        workspace,
        revision,
        {
            "source.xml": payload,
            "ir.json": ir.to_dict(),
            "profile.json": profile.to_dict(),
            "validation.json": report,
        },
    )


def generate_form(
    xml_path: str | Path,
    profile_path: str | Path,
    changes: list[dict[str, Any]],
    output_path: str | Path,
    *,
    workspace: str | Path | None = None,
    revision: str | None = None,
    mapping_path: str | Path | None = None,
) -> dict[str, Any]:
    profile = load_profile(profile_path)
    mapping = load_mapping(mapping_path) if mapping_path else None
    source = Path(xml_path)
    output = Path(output_path)
    _reject_same_path(source, output)
    payload = source.read_bytes()
    source_hash = sha256_bytes(payload)
    body = compile_xml(None, changes, profile, data=payload)
    with tempfile.TemporaryDirectory(prefix="svf-candidate-") as staging:
        candidate = Path(staging) / "candidate.xml"
        candidate.write_bytes(body)
        report = structural_report(candidate, profile, mapping, form_revision=revision)
        report["source_hash"] = source_hash
        report["changes_hash"] = stable_json_hash({"changes": changes})
        failed = report["checks"][0]["status"] != "pass"
        artifacts: dict[str, Any] = {
            "source.xml": payload,
            "generated.xml": body,
            "changes.json": {"changes": changes, "source_hash": source_hash},
            "profile.json": profile.to_dict(),
            "validation.json": report,
        }
        if mapping is not None:
            artifacts["mapping.json"] = mapping.to_dict()
        if failed:
            report["committed"] = False
            report["output_written"] = False
            if workspace is not None:
                report["diagnostic_path"] = str(save_diagnostic(workspace, revision or source_hash[:12], artifacts))
            return report
        if workspace is not None:
            revision = revision or stable_json_hash({"source": source_hash, "changes": changes})[:12]
            report["form_revision"] = revision
            report["form"] = {"path": str((Path(workspace) / "revisions" / revision / "generated.xml").resolve()), "sha256": sha256_bytes(body)}
            artifacts["validation.json"] = report
            revision_path = save_revision(workspace, revision, artifacts)
            report["revision_path"] = str(revision_path)
            report["committed"] = True
        else:
            report["form"] = {"path": str(output.resolve()), "sha256": sha256_bytes(body)}
            report["committed"] = False
    temporary = output.with_suffix(output.suffix + ".tmp")
    try:
        write_bytes_durable(temporary, body)
        temporary.replace(output)
        report["output_written"] = True
        report["output_path"] = str(output.resolve())
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        report["output_written"] = False
        report["output_error"] = str(exc)
        report["checks"][0].setdefault("issues", []).append(
            Issue("OUTPUT_WRITE_FAILED", f"確定世代からの書き出しに失敗しました: {exc}", stage="generate").to_dict()
        )
    return report


def render_reference(
    xml_path: str | Path,
    profile_path: str | Path,
    output_path: str | Path,
    *,
    mode: str = "design",
    data_count: int | None = None,
) -> list[Issue]:
    profile = load_profile(profile_path)
    ir = extract_ir(xml_path, profile)
    layouts, layout_issues = analyze_record_layouts(ir, profile)
    svg, issues = render_svg(ir, profile, mode=mode, data_count=data_count, layouts=layouts)
    Path(output_path).write_text(svg, encoding="utf-8")
    return ir.issues + layout_issues + issues


def open_acceptance(workspace: str | Path, revision: str) -> Path:
    """確定世代の検証記録を受入領域へ複製する。

    世代ディレクトリは不変のままにし、以降の証拠登録は複製へ書く。
    """
    generation = load_revision(workspace, revision)
    target = Path(workspace) / "acceptance" / revision / "validation.json"
    if not target.exists():
        source = generation / "validation.json"
        if not source.is_file():
            raise FileNotFoundError(f"確定世代{revision!r}に検証記録がありません")
        write_json(target, read_json(source))
    return target


def verify_form_revision(workspace: str | Path, revision: str, form_path: str | Path) -> list[Issue]:
    """Jobのform_revisionが実際の確定世代と一致するかを確認する。"""
    try:
        path = load_revision(workspace, revision)
    except FileNotFoundError as exc:
        return [Issue("FORM_REVISION_MISSING", f"確定世代{revision!r}がありません: {exc}", stage="runtime")]
    except OSError as exc:
        return [Issue("FORM_REVISION_CORRUPT", f"確定世代{revision!r}の成果物がmanifestと一致しません: {exc}", stage="runtime")]
    digest = sha256_file(form_path)
    for name in ("generated.xml", "source.xml"):
        candidate = path / name
        if candidate.is_file() and sha256_file(candidate) == digest:
            return []
    return [Issue("FORM_REVISION_MISMATCH", f"実行対象XMLが確定世代{revision!r}の成果物と一致しません", stage="runtime")]


def execute_job(
    form_path: str | Path,
    job_path: str | Path,
    mapping_path: str | Path,
    profile_path: str | Path,
    workspace: str | Path,
    output_dir: str | Path,
) -> RuntimeResult:
    job = load_job(job_path)
    mapping = load_mapping(mapping_path)
    profile = load_profile(profile_path)
    if job.output_profile != profile.profile_id:
        issue = Issue("PROFILE_MISMATCH", "jobとprofileのIDが一致しません", stage="runtime")
        return RuntimeResult("FAILED", job.job_id, job.data_count, error_code="PROFILE_MISMATCH", issues=[issue])
    revision_issues = verify_form_revision(workspace, job.form_revision, form_path)
    if revision_issues:
        return RuntimeResult("FAILED", job.job_id, job.data_count, error_code=revision_issues[0].code, issues=revision_issues)
    manifest = runtime_input_manifest(form_path, job, mapping, profile)
    digest = stable_json_hash(manifest)
    ledger = JobLedger(workspace)
    with execution_slot(workspace):
        prior = ledger.claim(job.job_id, digest, input_manifest_hash=digest)
        if isinstance(prior, RuntimeResult):
            return prior
        if isinstance(prior, list):
            return RuntimeResult("FAILED", job.job_id, job.data_count, error_code=prior[0].code, issues=prior)
        try:
            result = run_runtime(form_path, job, mapping, profile, output_dir, input_manifest_hash=digest)
        except BaseException as exc:  # 中断でもRUNNINGのまま残さない
            unknown = RuntimeResult(
                "UNKNOWN",
                job.job_id,
                job.data_count,
                error_code="RUNTIME_INTERRUPTED",
                issues=[Issue("RUNTIME_INTERRUPTED", f"実行中に中断しました。未送信と証明できません: {exc!r}", stage="runtime")],
                input_manifest_hash=digest,
            )
            ledger.record(job.job_id, digest, unknown)
            raise
        ledger.record(job.job_id, digest, result)
    return result


def write_runtime_result(result: RuntimeResult, output_path: str | Path) -> Path:
    target = Path(output_path)
    write_json(target, result.to_dict())
    return target


def mapping_hash(mapping_path: str | Path) -> str:
    return stable_json_hash(load_mapping(mapping_path).to_dict())


# --- 原稿から生成までの最小経路 -------------------------------------------


def load_annotations(path: str | Path) -> AnnotationSet:
    file = Path(path)
    return AnnotationSet.from_dict(read_json(file)) if file.exists() else AnnotationSet()


def save_annotations(path: str | Path, notes: AnnotationSet) -> None:
    write_json(path, notes.to_dict())


def list_targets(xml_path: str | Path, profile_path: str | Path, kinds: set[str] | None = None) -> list[dict[str, Any]]:
    ir = extract_ir(xml_path, load_profile(profile_path))
    return [
        {
            "id": x.id,
            "kind": x.kind,
            "name": x.name,
            "parent_id": x.parent_id,
            "display_line_count": x.display_line_count,
            "text": x.text,
        }
        for x in ir.elements
        if kinds is None or x.kind in kinds
    ]


def resolve_changes(xml_path: str | Path, profile_path: str | Path, annotations: AnnotationSet) -> tuple[list[dict[str, Any]], list[Issue]]:
    """備考から決定的に解釈できる変更を求める。取消済み備考は対象にしない。"""
    profile = load_profile(profile_path)
    ir = extract_ir(xml_path, profile)
    known = {x.id for x in ir.elements}
    changes, issues = annotations.display_line_changes()
    resolved: list[dict[str, Any]] = []
    for change in changes:
        if change["target_id"] not in known:
            issues.append(Issue("TARGET_MISSING", "備考の対象が現在のXMLにありません", target_id=change["target_id"], stage="annotation"))
            continue
        resolved.append(change)
    return resolved, issues


def propose_changes(
    xml_path: str | Path,
    profile_path: str | Path,
    annotations: AnnotationSet,
    *,
    fewshot_index: dict[str, Any] | None = None,
    target_ids: list[str] | None = None,
) -> dict[str, Any]:
    """確定した対象ID、備考版、原本ハッシュ、必要な記法例だけをLLMへ渡す。"""
    profile = load_profile(profile_path)
    ir = extract_ir(xml_path, profile)
    payload = build_proposal_payload(ir, profile, annotations, fewshot_index=fewshot_index, target_ids=target_ids)
    proposal = request_proposal(profile, payload)
    return proposal


def apply_and_generate(
    xml_path: str | Path,
    profile_path: str | Path,
    proposal: dict[str, Any],
    annotations: AnnotationSet,
    output_path: str | Path,
    *,
    workspace: str | Path | None = None,
    revision: str | None = None,
    mapping_path: str | Path | None = None,
) -> dict[str, Any]:
    profile = load_profile(profile_path)
    payload = Path(xml_path).read_bytes()
    ir = extract_ir(xml_path, profile, data=payload)
    changes = apply_proposal(
        xml_path,
        proposal,
        source_hash=ir.source_hash,
        revision=ir.revision,
        annotations=annotations,
        profile=profile,
        data=payload,
        changes_only=True,
    )
    report = generate_form(xml_path, profile_path, changes, output_path, workspace=workspace, revision=revision, mapping_path=mapping_path)
    report["applied_changes"] = changes
    return report
