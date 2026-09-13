from __future__ import annotations

from typing import Any


def create_comparison_plan(capacity: int | None = None) -> dict[str, Any]:
    counts = [0, 1, 2]
    if capacity is not None and capacity > 0:
        counts.extend([capacity - 1, capacity, capacity + 1, 2 * capacity + 1])
    counts = sorted(set(x for x in counts if x >= 0))
    return {
        "schema": "svf-comparison-plan/1.0",
        "principle": "D、N、配置設定は一度に一種類だけ変更する",
        "cases": [
            {"case_id": "D-only", "change": ["display_line_count"], "hold": ["data_count", "geometry"], "expected": {"designer": "record", "runtime": "record"}, "status": "not_run"},
            {"case_id": "N-only", "change": ["data_count"], "values": counts, "hold": ["xml", "display_line_count", "geometry"], "expected": {"runtime": "record"}, "status": "not_run"},
            {"case_id": "geometry-only", "change": ["subform_bounds", "record_height"], "hold": ["data_count", "display_line_count"], "expected": {"designer": "record", "runtime": "record"}, "status": "not_run"},
            {"case_id": "vertical-minimum", "settings": {"direction": "vertical"}, "status": "not_run"},
            {"case_id": "horizontal-minimum", "settings": {"direction": "horizontal"}, "status": "not_run"},
            {"case_id": "linked-subform-minimum", "settings": {"link": True, "linked_record_required": False}, "status": "not_run"},
        ],
    }
