"""備考の管理と決定的な解釈。

明確な変更文は新しい値を採用する。数字が複数あって変更先が曖昧な場合は、最初や
最後の数字を勝手に採らずIssueへ戻す。取消済み備考は解決処理に入れない。
"""
from __future__ import annotations

import re
import uuid
from dataclasses import asdict, dataclass
from typing import Any

from .models import Issue, parse_positive_int

_FULLWIDTH = str.maketrans(
    "０１２３４５６７８９（）：＝，、．",
    "0123456789():=,,.",
)

# 「AからBへ変更」は新しい値Bを採る。
CHANGE_PATTERNS = (
    re.compile(r"\d+\s*行?\s*(?:から|より)\s*(\d+)\s*行?\s*(?:に|へ|と)\s*(?:変更|変え|する|し|修正)"),
    re.compile(r"(\d+)\s*行\s*(?:に|へ)\s*(?:変更|変え|する|し|修正)"),
)
DISPLAY_KEYWORD = re.compile(r"表示|displayLineCount", re.IGNORECASE)
DISPLAY_PATTERNS = (
    re.compile(r"(\d+)\s*行"),
    re.compile(r"displayLineCount\s*(?:=|:)\s*(\d+)", re.IGNORECASE),
)


def normalize_text(text: str) -> str:
    return text.translate(_FULLWIDTH)


def extract_display_line_count(text: str) -> tuple[int | None, Issue | None]:
    """備考1件から表示行数の新しい値を求める。"""
    body = normalize_text(text)
    for pattern in CHANGE_PATTERNS:
        match = pattern.search(body)
        if match:
            try:
                return parse_positive_int(match.group(1), "displayLineCount"), None
            except ValueError as exc:
                return None, Issue("DISPLAY_LINE_COUNT_INVALID", str(exc), stage="annotation")
    if not DISPLAY_KEYWORD.search(body):
        return None, None
    found: list[int] = []
    for pattern in DISPLAY_PATTERNS:
        for match in pattern.finditer(body):
            try:
                found.append(parse_positive_int(match.group(1), "displayLineCount"))
            except ValueError as exc:
                return None, Issue("DISPLAY_LINE_COUNT_INVALID", str(exc), stage="annotation")
    if not found:
        return None, None
    if len(set(found)) > 1:
        return None, Issue("COMMENT_AMBIGUOUS", f"表示行数の候補が複数あります: {sorted(set(found))}", stage="annotation")
    return found[0], None


@dataclass(slots=True)
class Annotation:
    id: str
    target_ids: list[str]
    text: str
    revision: int
    active: bool
    source_hash: str

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Annotation":
        return cls(**value)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AnnotationSet:
    def __init__(self, annotations: list[Annotation] | None = None):
        self.annotations = annotations or []

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "AnnotationSet":
        return cls([Annotation.from_dict(x) for x in value.get("annotations", [])])

    def to_dict(self) -> dict[str, Any]:
        return {"schema": "svf-annotations/1.0", "annotations": [x.to_dict() for x in self.annotations]}

    def add(self, target_ids: list[str], text: str, source_hash: str) -> Annotation:
        if not target_ids or not all(isinstance(x, str) and x for x in target_ids):
            raise ValueError("備考には1件以上の対象IDが必要です")
        note = Annotation(str(uuid.uuid4()), list(target_ids), text, 1, True, source_hash)
        self.annotations.append(note)
        return note

    def revise(self, annotation_id: str, text: str, source_hash: str, target_ids: list[str] | None = None) -> Annotation:
        note = self._find(annotation_id)
        note.text = text
        note.source_hash = source_hash
        if target_ids:
            note.target_ids = list(target_ids)
        note.revision += 1
        note.active = True
        return note

    def cancel(self, annotation_id: str) -> Annotation:
        note = self._find(annotation_id)
        note.active = False
        note.revision += 1
        return note

    def active_revisions(self) -> dict[str, int]:
        return {x.id: x.revision for x in self.annotations if x.active}

    def targets_of(self, note_ids: list[str]) -> set[str]:
        return {target for note_id in note_ids for target in self._find(note_id).target_ids}

    def display_line_changes(self) -> tuple[list[dict[str, Any]], list[Issue]]:
        values: dict[str, list[tuple[int, str]]] = {}
        issues: list[Issue] = []
        for note in self.annotations:
            if not note.active:
                continue
            found, issue = extract_display_line_count(note.text)
            if issue is not None:
                for target_id in note.target_ids:
                    issues.append(Issue(issue.code, issue.reason, issue.severity, issue.stage, target_id))
                continue
            if found is not None:
                for target_id in note.target_ids:
                    values.setdefault(target_id, []).append((found, note.id))
        changes: list[dict[str, Any]] = []
        for target_id, entries in values.items():
            unique = {value for value, _ in entries}
            if len(unique) > 1:
                issues.append(Issue("COMMENT_CONFLICT", "表示行数の指定が競合しています", target_id=target_id, stage="annotation"))
                continue
            changes.append(
                {
                    "target_id": target_id,
                    "op": "set",
                    "property": "displayLineCount",
                    "value": entries[-1][0],
                    "note_ids": [note_id for _, note_id in entries],
                    "example_ids": [],
                }
            )
        return changes, issues

    def _find(self, annotation_id: str) -> Annotation:
        for note in self.annotations:
            if note.id == annotation_id:
                return note
        raise KeyError(annotation_id)
