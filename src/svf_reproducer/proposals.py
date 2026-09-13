"""LLM提案の受取と検査。

備考の対象範囲に含まれる変更だけを適用する。example_idsは参照元の追跡に使い、
変更権限の根拠にしない。生XML断片は参考情報として保存し、適用内容は正規化した
差分案で決める。
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .annotations import AnnotationSet
from .fewshot import select_examples
from .models import DocumentIR, Issue, TargetProfile
from .xmlcore import CompilationError, compile_xml

OPS = {"set", "add", "remove", "reparent"}


def validate_proposal_shape(value: Any) -> list[Issue]:
    issues: list[Issue] = []
    if not isinstance(value, dict):
        return [Issue("PROPOSAL_SCHEMA_INVALID", "提案はJSON objectが必要", stage="proposal")]
    for key, expected in (
        ("source_hash", str),
        ("base_revision", int),
        ("annotation_revisions", dict),
        ("changes", list),
        ("xml_fragments", list),
        ("unresolved", list),
    ):
        actual = value.get(key)
        if not isinstance(actual, expected) or (expected is int and isinstance(actual, bool)):
            issues.append(Issue("PROPOSAL_SCHEMA_INVALID", f"{key}の型が不正", stage="proposal"))
    revisions = value.get("annotation_revisions")
    if isinstance(revisions, dict):
        for note_id, revision in revisions.items():
            if not isinstance(note_id, str) or isinstance(revision, bool) or not isinstance(revision, int):
                issues.append(Issue("PROPOSAL_SCHEMA_INVALID", "annotation_revisionsは文字列キーと整数が必要", stage="proposal"))
                break
    for number, change in enumerate(value.get("changes", []) if isinstance(value.get("changes"), list) else []):
        if not isinstance(change, dict):
            issues.append(Issue("PROPOSAL_SCHEMA_INVALID", f"changes[{number}]はobjectが必要", stage="proposal"))
            continue
        if not isinstance(change.get("target_id"), str) or not change.get("target_id"):
            issues.append(Issue("PROPOSAL_SCHEMA_INVALID", f"changes[{number}].target_idが不正", stage="proposal"))
        if change.get("op") not in OPS:
            issues.append(Issue("PROPOSAL_SCHEMA_INVALID", f"changes[{number}].opが不正", stage="proposal"))
        if change.get("op") == "set" and not isinstance(change.get("property"), str):
            issues.append(Issue("PROPOSAL_SCHEMA_INVALID", f"changes[{number}].propertyが不正", stage="proposal"))
        if change.get("op") == "reparent":
            value_body = change.get("value")
            if not isinstance(value_body, dict) or not isinstance(value_body.get("new_parent_id"), str):
                issues.append(Issue("PROPOSAL_SCHEMA_INVALID", f"changes[{number}].value.new_parent_idが不正", stage="proposal"))
        for list_key in ("note_ids", "example_ids"):
            items = change.get(list_key, [])
            if not isinstance(items, list) or not all(isinstance(x, str) for x in items):
                issues.append(Issue("PROPOSAL_SCHEMA_INVALID", f"changes[{number}].{list_key}が不正", stage="proposal"))
    return issues


def scope_issues(changes: list[dict[str, Any]], annotations: AnnotationSet, source_hash: str) -> list[Issue]:
    """各変更が備考の対象範囲に含まれることを要求する。範囲不明なら適用を止める。"""
    by_id = {note.id: note for note in annotations.annotations}
    issues: list[Issue] = []
    for number, change in enumerate(changes):
        note_ids = change.get("note_ids") or []
        target_id = change.get("target_id")
        if not note_ids:
            issues.append(Issue("PROPOSAL_SCOPE_UNKNOWN", f"changes[{number}]に根拠の備考がありません", target_id=target_id, stage="proposal"))
            continue
        allowed: set[str] = set()
        for note_id in note_ids:
            note = by_id.get(note_id)
            if note is None:
                issues.append(Issue("ANNOTATION_UNKNOWN", f"未知のnote_idです: {note_id}", target_id=target_id, stage="proposal"))
                continue
            if not note.active:
                issues.append(Issue("ANNOTATION_CANCELLED", f"取消済みの備考です: {note_id}", target_id=target_id, stage="proposal"))
                continue
            if note.source_hash != source_hash:
                issues.append(Issue("ANNOTATION_STALE", f"備考の原本ハッシュが現在と一致しません: {note_id}", target_id=target_id, stage="proposal"))
                continue
            allowed.update(note.target_ids)
        if not allowed:
            continue
        required = {target_id}
        if change.get("op") == "reparent":
            body = change.get("value")
            if isinstance(body, dict) and isinstance(body.get("new_parent_id"), str):
                required.add(body["new_parent_id"])
        outside = sorted(x for x in required if x not in allowed)
        if outside:
            issues.append(
                Issue("PROPOSAL_OUT_OF_SCOPE", "備考の対象範囲外を変更しようとしています: " + ", ".join(outside), target_id=target_id, stage="proposal")
            )
    return issues


def normalize_changes(proposal: dict[str, Any]) -> list[dict[str, Any]]:
    """実際に適用する内容だけを残す。生XML断片は参考情報として別に保存する。"""
    fragments: dict[int, str] = {}
    for fragment in proposal.get("xml_fragments", []):
        if not isinstance(fragment, dict):
            raise CompilationError([Issue("PROPOSAL_SCHEMA_INVALID", "xml_fragmentsの要素が不正", stage="proposal")])
        number = fragment.get("change_index")
        if not isinstance(number, int) or isinstance(number, bool) or not 0 <= number < len(proposal.get("changes", [])):
            raise CompilationError([Issue("PROPOSAL_SCHEMA_INVALID", "xml_fragments.change_indexが不正", stage="proposal")])
        if proposal["changes"][number].get("op") != "add":
            raise CompilationError([Issue("PROPOSAL_SCHEMA_INVALID", "XML断片はaddだけに対応", stage="proposal")])
        fragments[number] = fragment.get("xml")
    changes: list[dict[str, Any]] = []
    for number, change in enumerate(proposal["changes"]):
        body = {key: value for key, value in change.items() if key in {"op", "target_id", "property", "value", "note_ids", "example_ids"}}
        if number in fragments:
            body["reference_fragment"] = fragments[number]
            body["fragment_is_reference_only"] = True
        changes.append(body)
    return changes


def apply_proposal(
    source: str | Path,
    proposal: dict[str, Any],
    *,
    source_hash: str,
    revision: int,
    annotations: AnnotationSet,
    profile: TargetProfile,
    data: bytes | None = None,
    changes_only: bool = False,
) -> bytes | list[dict[str, Any]]:
    issues = validate_proposal_shape(proposal)
    if proposal.get("source_hash") != source_hash or proposal.get("base_revision") != revision:
        issues.append(Issue("STALE_PROPOSAL", "原本またはIRの版が変わっています", stage="proposal"))
    if proposal.get("annotation_revisions") != annotations.active_revisions():
        issues.append(Issue("STALE_PROPOSAL", "備考の版または有効状態が変わっています", stage="proposal"))
    unresolved = proposal.get("unresolved", [])
    if unresolved:
        issues.append(Issue("PROPOSAL_UNRESOLVED", f"未確定事項が{len(unresolved)}件あります", stage="proposal"))
    if issues:
        raise CompilationError(issues)
    issues = scope_issues(proposal["changes"], annotations, source_hash)
    if issues:
        raise CompilationError(issues)
    changes = normalize_changes(proposal)
    if changes_only:
        return changes
    return compile_xml(source, changes, profile, data=data)


def build_proposal_payload(
    ir: DocumentIR,
    profile: TargetProfile,
    annotations: AnnotationSet,
    *,
    fewshot_index: dict[str, Any] | None = None,
    target_ids: list[str] | None = None,
) -> dict[str, Any]:
    """確定した対象、備考版、原本ハッシュ、必要な記法例だけを渡す。"""
    selected = set(target_ids or [])
    for note in annotations.annotations:
        if note.active:
            selected.update(note.target_ids)
    elements = [x for x in ir.elements if not selected or x.id in selected]
    kinds = sorted({x.kind for x in elements})
    examples = select_examples(fewshot_index, kinds) if fewshot_index else []
    return {
        "schema": "svf-proposal-request/1.0",
        "source_hash": ir.source_hash,
        "base_revision": ir.revision,
        "annotation_revisions": annotations.active_revisions(),
        "annotations": [x.to_dict() for x in annotations.annotations if x.active],
        "targets": [
            {
                "id": x.id,
                "kind": x.kind,
                "name": x.name,
                "parent_id": x.parent_id,
                "attributes": x.attributes,
                "display_line_count": x.display_line_count,
            }
            for x in elements
        ],
        "editable_attributes": profile.editable_attributes,
        "element_templates": sorted(profile.element_templates),
        "allow_reparent": profile.allow_reparent,
        "fewshot_examples": examples,
    }


class ProposalProvider(Protocol):
    def request(self, payload: dict[str, Any], correction: list[dict[str, Any]] | None = None) -> str: ...


@dataclass(slots=True)
class CommandProposalProvider:
    command: list[str]
    timeout_seconds: float = 120

    def request(self, payload: dict[str, Any], correction: list[dict[str, Any]] | None = None) -> str:
        body = dict(payload)
        if correction:
            body["schema_correction"] = correction
        try:
            result = subprocess.run(
                self.command,
                input=json.dumps(body, ensure_ascii=False),
                text=True,
                capture_output=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(f"提案の取得が{self.timeout_seconds}秒で終わりませんでした") from exc
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or f"proposal command returned {result.returncode}")
        return result.stdout


def request_proposal(provider: ProposalProvider | TargetProfile, payload: dict[str, Any]) -> dict[str, Any]:
    if isinstance(provider, TargetProfile):
        if not provider.proposal_command:
            raise ValueError("profileにproposal_commandが登録されていません")
        provider = CommandProposalProvider(provider.proposal_command, provider.proposal_timeout_seconds)
    correction = None
    for attempt in range(2):
        raw = provider.request(payload, correction)
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            correction = [{"code": "PROPOSAL_JSON_INVALID", "reason": str(exc)}]
            if attempt == 0:
                continue
            raise ValueError("モデル応答を2回ともJSONとして解析できません") from exc
        issues = validate_proposal_shape(value)
        if not issues:
            if value.get("source_hash") != payload.get("source_hash") or value.get("annotation_revisions") != payload.get("annotation_revisions"):
                raise ValueError("応答が要求した原本または備考の版と一致しません。古い応答は採用しません")
            return value
        correction = [x.to_dict() for x in issues]
    raise ValueError("モデル応答を2回ともProposal契約に適合させられません")
