"""Recordの行送りと罫線周期。

heightUnitとheightを対象版で確認した対応表から解釈し、有効ピッチを求める。
未登録の単位ではピッチと容量を未確定のままにし、y2-y1を無条件の行送りにしない。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .geometry import MM_PER_INCH, POINTS_PER_INCH, dot_to_mm
from .models import DocumentIR, Issue, TargetProfile
from .xmlcore import estimate_capacity

UNIT_TO_MM = {
    "mm": lambda value, dpi: value,
    "inch": lambda value, dpi: value * MM_PER_INCH,
    "point": lambda value, dpi: value * MM_PER_INCH / POINTS_PER_INCH,
    "dot": lambda value, dpi: dot_to_mm(value, dpi) if dpi else None,
}


@dataclass(slots=True)
class RecordLayout:
    record_id: str
    direction: str
    display_line_count: int | None
    capacity: int | None
    capacity_reason: str | None
    line_period: int | None
    fixed_frame: bool
    pitch_mm: float | None = None
    pitch_source: str = "unresolved"

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "direction": self.direction,
            "display_line_count": self.display_line_count,
            "capacity": self.capacity,
            "capacity_reason": self.capacity_reason,
            "line_period": self.line_period,
            "fixed_frame": self.fixed_frame,
            "pitch_mm": self.pitch_mm,
            "pitch_source": self.pitch_source,
        }


def _number(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def record_pitch_mm(record: Any, profile: TargetProfile, coordinate_dpi: float | None) -> tuple[float | None, str]:
    """確認済みのheightUnitとheightから有効ピッチを求める。"""
    raw_unit = record.attributes.get("heightUnit")
    height = _number(record.attributes.get("height"))
    unit = profile.enum_maps.get("record.heightUnit", {}).get(str(raw_unit), "")
    if raw_unit is not None and unit and height is not None and height > 0:
        convert = UNIT_TO_MM.get(unit)
        if convert is not None:
            value = convert(height, coordinate_dpi)
            if value is not None and value > 0:
                return value, f"height({unit})"
    if raw_unit is not None:
        return None, "height_unit_unconfirmed"
    return None, "height_absent"


def analyze_record_layouts(ir: DocumentIR, profile: TargetProfile, settings: dict[str, Any] | None = None) -> tuple[list[RecordLayout], list[Issue]]:
    settings = settings or {}
    by_id = {x.id: x for x in ir.elements}
    records = [x for x in ir.elements if x.kind == "Record"]
    per_subform: dict[str | None, int] = {}
    for record in records:
        per_subform[record.parent_id] = per_subform.get(record.parent_id, 0) + 1
    layouts: list[RecordLayout] = []
    issues: list[Issue] = []
    for record in records:
        parent = by_id.get(record.parent_id or "")
        raw_direction = parent.attributes.get("direction") if parent else None
        direction = profile.enum_maps.get("subform.direction", {}).get(str(raw_direction), "unknown")
        if direction == "unknown":
            issues.append(
                Issue("DIRECTION_UNCONFIRMED", f"SubFormのdirection {raw_direction!r}が対象版の対応表に未登録です", "warning", "layout", record.id)
            )
        g = record.geometry_mm
        pg = parent.geometry_mm if parent else {}
        pitch, pitch_source = record_pitch_mm(record, profile, ir.coordinate_dpi)
        if pitch is None:
            frame = (g.get("y2", 0.0) - g.get("y1", 0.0)) if direction == "vertical" else (g.get("x2", 0.0) - g.get("x1", 0.0))
            if frame > 0:
                pitch, pitch_source = frame, f"record_frame({pitch_source})"
                issues.append(
                    Issue("PITCH_UNCONFIRMED", "行送りをRecord枠から仮置きしました。対象版でheightUnitを確認するまで容量は未確定です", "warning", "layout", record.id)
                )
        available = (pg.get("y2", 0.0) - g.get("y1", 0.0)) if direction == "vertical" else (pg.get("x2", 0.0) - g.get("x1", 0.0))
        if pitch is None or pitch_source.startswith("record_frame"):
            capacity, reason = None, "pitch_unconfirmed"
        else:
            try:
                capacity, reason = estimate_capacity(
                    available,
                    pitch,
                    direction=direction,
                    record_count=per_subform.get(record.parent_id, 1),
                    variable_height=bool(settings.get("variable_height", False)),
                    has_suppression=record.attributes.get("groupSuppress", "false") == "true",
                    has_page_link=bool(parent and parent.attributes.get("linkName")),
                )
            except ValueError as exc:
                capacity, reason = None, "invalid_geometry"
                issues.append(Issue("LAYOUT_INVALID", str(exc), target_id=record.id, stage="layout"))
        record_settings = settings.get("records", {}).get(record.id, {})
        period = record_settings.get("line_period")
        if period is not None and (isinstance(period, bool) or not isinstance(period, int) or period <= 0):
            issues.append(Issue("LINE_PERIOD_INVALID", "罫線周期は正の整数が必要", target_id=record.id, stage="layout"))
            period = None
        layouts.append(
            RecordLayout(
                record.id,
                direction,
                record.display_line_count,
                capacity,
                reason,
                period,
                bool(record_settings.get("fixed_frame", False)),
                pitch,
                pitch_source,
            )
        )
    return layouts, issues


def line_schedule(instance_count: int, *, period: int = 1, blank_after: set[int] | None = None) -> list[bool]:
    if instance_count < 0 or period <= 0:
        raise ValueError("instance_count must be non-negative and period positive")
    blank_after = blank_after or set()
    return [((index + 1) % period == 0) and (index + 1 not in blank_after) for index in range(instance_count)]
