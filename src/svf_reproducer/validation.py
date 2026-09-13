"""検証記録と受入判定。

reportは不変のform_revisionと入力ハッシュに結び付ける。旧世代の合格は履歴として
残すが、現在の出力対象と一致する場合だけ現在の受入として扱う。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from xml.etree import ElementTree as ET

from .config import read_json, sha256_file, stable_json_hash, write_json
from .models import Issue, MappingConfig, TargetProfile
from .storage import now_text
from .xmlcore import validate_xml

CheckKind = Literal["structure", "preview", "runtime", "designer"]
KINDS: tuple[str, ...] = ("structure", "preview", "runtime", "designer")
DESIGNER_KEYS = ("product", "version", "operator", "result")


@dataclass(slots=True)
class ValidationCheck:
    kind: CheckKind
    status: Literal["pass", "fail", "not_run"]
    input_hashes: dict[str, str]
    evidence: list[Any]
    issues: list[Issue]

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "status": self.status,
            "input_hashes": self.input_hashes,
            "evidence": self.evidence,
            "issues": [x.to_dict() for x in self.issues],
        }


def structural_report(
    xml_path: str | Path,
    profile: TargetProfile,
    mapping: MappingConfig | None = None,
    *,
    form_revision: str | None = None,
) -> dict[str, Any]:
    issues = validate_xml(xml_path, profile, mapping)
    input_hashes = {
        "form": sha256_file(xml_path),
        "profile": stable_json_hash(profile.to_dict()),
        "mapping": stable_json_hash(mapping.to_dict()) if mapping else "not-supplied",
    }
    check = ValidationCheck(
        "structure",
        "fail" if any(x.severity == "error" for x in issues) else "pass",
        input_hashes,
        [str(Path(xml_path).resolve())],
        issues,
    )
    return {
        "schema": "svf-validation/1.1",
        "form": {"path": str(Path(xml_path).resolve()), "sha256": input_hashes["form"]},
        "form_revision": form_revision,
        "input_hashes": input_hashes,
        "checks": [check.to_dict()] + [ValidationCheck(kind, "not_run", input_hashes, [], []).to_dict() for kind in KINDS[1:]],
    }


def _check_of(report: dict[str, Any], kind: str) -> dict[str, Any] | None:
    matches = [x for x in report.get("checks", []) if x.get("kind") == kind]
    if len(matches) != 1:
        return None
    return matches[0]


def acceptance_issues(report: dict[str, Any], required: set[str]) -> list[Issue]:
    """現在の受入として使える検証記録かを調べる。空の証拠や旧入力は受け入れない。"""
    issues: list[Issue] = []
    expected = report.get("input_hashes", {})
    for kind in sorted(required):
        check = _check_of(report, kind)
        if check is None:
            issues.append(Issue("CHECK_MISSING", f"必須検査{kind}が一意に存在しません", stage="acceptance"))
            continue
        if check.get("status") != "pass":
            issues.append(Issue("CHECK_NOT_PASSED", f"必須検査{kind}がpassではありません", stage="acceptance"))
            continue
        if not check.get("evidence"):
            issues.append(Issue("EVIDENCE_MISSING", f"{kind}の証拠がありません", stage="acceptance"))
        if expected and check.get("input_hashes") != expected:
            issues.append(Issue("EVIDENCE_STALE", f"{kind}の証拠が現在の入力と一致しません", stage="acceptance"))
        if any(x.get("severity") == "error" for x in check.get("issues", [])):
            issues.append(Issue("CHECK_HAS_ERROR", f"{kind}にerrorが残っています", stage="acceptance"))
    return issues


def accepted(report: dict[str, Any], required: set[str]) -> bool:
    return not acceptance_issues(report, required)


def current_input_issues(report: dict[str, Any]) -> list[Issue]:
    """検証記録が現在のXMLと一致するかを確認する。"""
    form = report.get("form", {})
    path = Path(form.get("path", ""))
    if not path.is_file():
        return [Issue("FORM_MISSING", f"検証記録の対象XMLがありません: {path}", stage="acceptance")]
    if sha256_file(path) != form.get("sha256"):
        return [Issue("FORM_CHANGED", "検証記録の作成後に対象XMLが変わりました。新しい世代で検証をやり直してください", stage="acceptance")]
    return []


def record_check(
    report_path: str | Path,
    kind: CheckKind,
    status: Literal["pass", "fail", "not_run"],
    evidence_paths: list[str | Path],
    issues: list[Issue] | None = None,
) -> dict[str, Any]:
    report = read_json(report_path)
    check = _check_of(report, kind)
    if check is None:
        raise ValueError(f"validation check not found or duplicated: {kind}")
    evidence = []
    for value in evidence_paths:
        path = Path(value)
        if not path.is_file():
            raise FileNotFoundError(path)
        evidence.append({"path": str(path.resolve()), "sha256": sha256_file(path)})
    if status == "pass":
        stale = current_input_issues(report)
        if stale:
            raise ValueError(stale[0].reason)
        if any(x.severity == "error" for x in (issues or [])):
            raise ValueError("errorを伴う検査はpassにできません")
        if kind == "structure" and any(x.get("severity") == "error" for x in check.get("issues", [])):
            raise ValueError("構造検査にerrorが残っています")
        manifests = []
        for item in evidence:
            candidate = Path(item["path"])
            if candidate.suffix.lower() != ".json":
                continue
            try:
                value = read_json(candidate)
            except (OSError, ValueError):
                continue
            if value.get("schema") == "svf-evidence/1.0":
                manifests.append(value)
        if not manifests:
            raise ValueError("passにはsvf-evidence/1.0の証拠manifestが必要です")
        for manifest in manifests:
            if manifest.get("kind") != kind or manifest.get("input_hashes") != check.get("input_hashes"):
                raise ValueError("証拠manifestの種別または入力ハッシュが検証記録と一致しません")
            artifacts = manifest.get("artifacts", [])
            if not artifacts:
                raise ValueError("証拠manifestに成果物がありません")
            for artifact in artifacts:
                artifact_path = Path(artifact["path"])
                if not artifact_path.is_file() or sha256_file(artifact_path) != artifact.get("sha256"):
                    raise ValueError(f"証拠成果物のハッシュが一致しません: {artifact_path}")
    check["status"] = status
    check["evidence"] = evidence
    check["issues"] = [x.to_dict() for x in (issues or [])]
    check["recorded_at"] = now_text()
    write_json(report_path, report)
    return report


def create_evidence_manifest(
    report_path: str | Path,
    kind: CheckKind,
    artifact_paths: list[str | Path],
    output_path: str | Path,
    note: str = "",
    *,
    metadata: dict[str, Any] | None = None,
    runtime_result_path: str | Path | None = None,
) -> dict[str, Any]:
    report = read_json(report_path)
    check = _check_of(report, kind)
    if check is None:
        raise ValueError(f"validation check not found or duplicated: {kind}")
    artifacts = []
    for value in artifact_paths:
        path = Path(value)
        if not path.is_file():
            raise FileNotFoundError(path)
        if path.stat().st_size == 0:
            raise ValueError(f"0バイトの成果物は証拠になりません: {path}")
        artifacts.append({"path": str(path.resolve()), "sha256": sha256_file(path)})
    if not artifacts:
        raise ValueError("証拠成果物が必要です")
    expected = check.get("input_hashes", {})
    body: dict[str, Any] = {}
    if kind == "preview":
        svg_paths = [Path(x["path"]) for x in artifacts if Path(x["path"]).suffix.lower() == ".svg"]
        if not svg_paths:
            raise ValueError("previewのpass証拠には参考SVGが必要です")
        for svg_path in svg_paths:
            svg = ET.parse(svg_path).getroot()
            if svg.attrib.get("data-source-hash") != expected.get("form") or svg.attrib.get("data-profile-hash") != expected.get("profile"):
                raise ValueError("参考SVGの原本またはprofileハッシュが検証記録と一致しません")
    elif kind == "runtime":
        if runtime_result_path is None:
            raise ValueError("runtimeの証拠にはRuntimeResultのJSONが必要です")
        result = read_json(runtime_result_path)
        if result.get("status") != "SUCCEEDED":
            raise ValueError(f"RuntimeResultがSUCCEEDEDではありません: {result.get('status')}")
        if not result.get("input_manifest_hash"):
            raise ValueError("RuntimeResultに実行manifestのハッシュがありません")
        recorded = {Path(x).resolve() for x in result.get("artifact_paths", [])}
        supplied = {Path(x["path"]) for x in artifacts}
        if not recorded or not recorded.issubset(supplied):
            raise ValueError("RuntimeResultの成果物がすべて証拠に含まれていません")
        body["runtime"] = {
            "runtime_job_id": result.get("runtime_job_id"),
            "input_manifest_hash": result["input_manifest_hash"],
            "data_count": result.get("data_count"),
            "artifacts": result.get("artifacts", []),
        }
    elif kind == "designer":
        missing = [key for key in DESIGNER_KEYS if not str((metadata or {}).get(key, "")).strip()]
        if missing:
            raise ValueError("Designer証拠には次の項目が必要です: " + ", ".join(missing))
        if str(metadata.get("form_sha256", expected.get("form"))) != expected.get("form"):
            raise ValueError("Designer証拠の対象XMLが検証記録と一致しません")
        body["designer"] = {**{key: metadata[key] for key in DESIGNER_KEYS}, "form_sha256": expected.get("form")}
    manifest = {
        "schema": "svf-evidence/1.0",
        "kind": kind,
        "input_hashes": expected,
        "form_revision": report.get("form_revision"),
        "artifacts": artifacts,
        "note": note,
        "created_at": now_text(),
        **body,
    }
    write_json(output_path, manifest)
    return manifest
