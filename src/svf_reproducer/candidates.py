"""固定部と明細候補の切り分け。

列の整列、行間隔、内容をあわせて候補化し、確定できないものは未確定に残す。
幅・高さを補って候補生成を通すことはしない。確定は備考または元XMLで行う。
"""
from __future__ import annotations

import statistics
from typing import Any

from .models import Issue

BBOX_KEYS = ("x", "y", "width", "height")


def _usable(item: dict[str, Any]) -> dict[str, float] | None:
    box = item.get("layout_bbox")
    if not isinstance(box, dict) or item.get("bbox_source") not in {"measured", "estimated"}:
        return None
    if not all(isinstance(box.get(key), (int, float)) and not isinstance(box.get(key), bool) for key in BBOX_KEYS):
        return None
    values = {key: float(box[key]) for key in BBOX_KEYS}
    return values if values["width"] > 0 and values["height"] > 0 else None


def build_candidates(source_document: dict[str, Any], tolerance: float = 0.5) -> tuple[dict[str, Any], list[Issue]]:
    objects = source_document.get("objects", [])
    issues: list[Issue] = []
    cells: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    for item in objects:
        box = _usable(item)
        if box is None:
            unresolved.append(
                {
                    "source_id": item.get("id"),
                    "reason": item.get("unresolved_reason") or f"bboxが幅・高さを持ちません(bbox_source={item.get('bbox_source')})",
                }
            )
            continue
        cells.append({"source_id": item["id"], "page": item.get("page", 1), "text": item.get("text"), "bbox": box})
    if unresolved:
        issues.append(Issue("SOURCE_BBOX_UNRESOLVED", f"幅・高さが確定しない抽出結果が{len(unresolved)}件あります", "warning", "candidate"))
    cells.sort(key=lambda x: (x["page"], x["bbox"]["y"], x["bbox"]["x"]))
    rows: list[dict[str, Any]] = []
    for cell in cells:
        band = max(tolerance, cell["bbox"]["height"] / 2)
        current = rows[-1] if rows else None
        if current and current["page"] == cell["page"] and abs(cell["bbox"]["y"] - current["y"]) <= band:
            current["cells"].append(cell)
            current["y"] = min(current["y"], cell["bbox"]["y"])
        else:
            rows.append({"page": cell["page"], "y": cell["bbox"]["y"], "cells": [cell]})
    for row in rows:
        row["cells"].sort(key=lambda x: x["bbox"]["x"])
        row["columns"] = [round(x["bbox"]["x"] / max(tolerance, 1e-9)) for x in row["cells"]]
    groups: list[dict[str, Any]] = []
    for row in rows:
        signature = (row["page"], tuple(row["columns"]))
        if groups and groups[-1]["signature"] == signature:
            groups[-1]["rows"].append(row)
        else:
            groups.append({"signature": signature, "rows": [row]})
    templates: list[dict[str, Any]] = []
    fixed: list[dict[str, Any]] = []
    for group in groups:
        members = group["rows"]
        if len(members) < 2:
            fixed.extend({"source_id": cell["source_id"], "page": cell["page"], "bbox": cell["bbox"], "text": cell["text"]} for row in members for cell in row["cells"])
            continue
        deltas = [round(members[index + 1]["y"] - members[index]["y"], 4) for index in range(len(members) - 1)]
        pitch = statistics.median(deltas) if deltas else None
        spread = (max(deltas) - min(deltas)) if deltas else 0.0
        candidate = {
            "candidate_id": f"candidate-{len(templates) + 1:03d}",
            "page": group["signature"][0],
            "role": "unresolved",
            "column_x_mm": [row_cell["bbox"]["x"] for row_cell in members[0]["cells"]],
            "row_pitch_mm": pitch,
            "row_pitch_spread_mm": spread,
            "instances": [
                {"row": index + 1, "cells": [{"source_id": cell["source_id"], "bbox": cell["bbox"], "text": cell["text"]} for cell in row["cells"]]}
                for index, row in enumerate(members)
            ],
        }
        if pitch is None or spread > tolerance:
            candidate["role"] = "unresolved"
            issues.append(
                Issue("DETAIL_PITCH_UNSTABLE", f"{candidate['candidate_id']}の行間隔が一定ではありません", "warning", "candidate", candidate["candidate_id"])
            )
        templates.append(candidate)
    if cells and not templates:
        issues.append(Issue("DETAIL_CANDIDATE_UNRESOLVED", "反復明細を幾何情報だけでは確定できません。備考または元XMLが必要です", "warning", "candidate"))
    if not any(x.get("kind") == "line" for x in objects):
        issues.append(Issue("RULE_LINES_UNRESOLVED", "罫線の抽出に未対応のため、罫線を含む候補化は元XMLか備考で確定します", "warning", "candidate"))
    return (
        {
            "schema": "svf-candidates/1.1",
            "unit": source_document.get("unit", "mm"),
            "record_templates": templates,
            "fixed_objects": fixed,
            "unresolved_objects": unresolved,
        },
        issues,
    )
