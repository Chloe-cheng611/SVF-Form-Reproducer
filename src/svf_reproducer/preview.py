"""参考表示。

設計表示の反復数は表示行数D、データ参考表示の反復数は件数Nから求める。N=0を1へ
補正しない。Recordの子要素群を相対配置で反復し、固定文字・Field・罫線を一緒に
表示する。実際の描画件数が確定してから打切りを通知する。
"""
from __future__ import annotations

import html
from typing import Any, Iterable, Literal

from .config import stable_json_hash
from .models import DocumentIR, ElementIR, Issue, TargetProfile

MAX_INSTANCES = 200


def _line_style(profile: TargetProfile, element: ElementIR) -> str:
    kind = profile.enum_maps.get("line.lineType", {}).get(str(element.attributes.get("lineType", "")), "")
    color = profile.enum_maps.get("color", {}).get(str(element.attributes.get("color", "")), "")
    dash = {"dashed": ' stroke-dasharray="2 1"', "dotted": ' stroke-dasharray="0.5 1"'}.get(kind, "")
    stroke = f' stroke="{html.escape(color)}"' if color else ""
    return dash + stroke


def _shape(element: ElementIR, profile: TargetProfile, dx: float, dy: float, extra: str) -> list[str]:
    g = element.geometry_mm
    attrs = f'data-object-id="{html.escape(element.id)}"{extra}'
    if element.kind == "Line" and all(k in g for k in ("x1", "y1", "x2", "y2")):
        return [f'<line {attrs} x1="{g["x1"]+dx}" y1="{g["y1"]+dy}" x2="{g["x2"]+dx}" y2="{g["y2"]+dy}"{_line_style(profile, element)}/>']
    if element.kind in {"Box", "SubForm"} and all(k in g for k in ("x1", "y1", "x2", "y2")):
        fill = profile.enum_maps.get("box.fill", {}).get(str(element.attributes.get("fillStyle", "")), "")
        body = f' fill="{html.escape(fill)}"' if fill else ""
        return [f'<rect {attrs} x="{g["x1"]+dx}" y="{g["y1"]+dy}" width="{g["x2"]-g["x1"]}" height="{g["y2"]-g["y1"]}"{body}/>']
    if element.kind == "Text" and element.text is not None:
        return [
            f'<text {attrs} x="{g.get("x", 0.0)+dx}" y="{g.get("y", 0.0)+dy}" fill="black" stroke="none" font-size="3.5">{html.escape(element.text)}</text>'
        ]
    if element.kind == "Field":
        x, y = g.get("x", 0.0) + dx, g.get("y", 0.0) + dy
        label = f'{{{element.name}}}' if element.name else "{Field}"
        return [f'<text {attrs} x="{x}" y="{y}" fill="#0878c9" stroke="none" font-size="3.5" font-style="italic">{html.escape(label)}</text>']
    if element.kind == "Bitmap" and all(k in g for k in ("x1", "y1", "x2", "y2")):
        return [
            f'<rect {attrs} x="{g["x1"]+dx}" y="{g["y1"]+dy}" width="{g["x2"]-g["x1"]}" height="{g["y2"]-g["y1"]}" stroke-dasharray="1 1"/>'
        ]
    return []


def render_svg(
    ir: DocumentIR,
    profile: TargetProfile,
    *,
    mode: Literal["design", "data"] = "design",
    data_count: int | None = None,
    max_instances: int = MAX_INSTANCES,
    layouts: Iterable[Any] | None = None,
) -> tuple[str, list[Issue]]:
    issues: list[Issue] = []
    by_id = {x.id: x for x in ir.elements}
    pitch_by_record = {x.record_id: getattr(x, "pitch_mm", None) for x in (layouts or [])}
    direction_by_record = {x.record_id: x.direction for x in (layouts or [])}
    width = ir.page_width_mm
    height = ir.page_height_mm
    if width is None or height is None:
        points = [value for element in ir.elements for key, value in element.geometry_mm.items() if key in {"x", "x1", "x2", "y", "y1", "y2"}]
        extent = max(points, default=200.0) + 10.0
        width = height = extent
        issues.append(Issue("PAGE_SIZE_UNRESOLVED", "用紙寸法がないため要素範囲で参考表示", "warning", "preview"))

    def ancestors(element: ElementIR) -> list[str]:
        chain: list[str] = []
        current = element.parent_id
        while current:
            chain.append(current)
            current = by_id[current].parent_id if current in by_id else None
        return chain

    records = {x.id: x for x in ir.elements if x.kind == "Record"}
    inside_record = {x.id: next((a for a in ancestors(x) if a in records), None) for x in ir.elements}
    body = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}mm" height="{height}mm" viewBox="0 0 {width} {height}"'
        f' data-source-hash="{ir.source_hash}" data-profile-hash="{stable_json_hash(profile.to_dict())}" data-mode="{mode}">',
        '<rect width="100%" height="100%" fill="white"/>',
        '<g fill="none" stroke="black" stroke-width="0.25">',
    ]
    for item in ir.elements:
        if item.id in records or inside_record.get(item.id):
            continue
        body.extend(_shape(item, profile, 0.0, 0.0, ""))
    for record in records.values():
        g = record.geometry_mm
        if not all(k in g for k in ("x1", "y1", "x2", "y2")):
            issues.append(Issue("PREVIEW_GEOMETRY_UNRESOLVED", "Record枠の座標が確定していないため反復しません", "warning", "preview", record.id))
            continue
        parent = by_id.get(record.parent_id or "")
        raw_direction = parent.attributes.get("direction") if parent and parent.kind == "SubForm" else None
        direction = direction_by_record.get(record.id) or profile.enum_maps.get("subform.direction", {}).get(str(raw_direction), "unknown")
        requested = record.display_line_count if mode == "design" else data_count
        if requested is None:
            requested = 1 if mode == "design" else 0
            if mode == "design":
                issues.append(Issue("PREVIEW_DISPLAY_UNRESOLVED", "表示行数が未確定のため1件だけ表示", "warning", "preview", record.id))
        if requested < 0:
            issues.append(Issue("PREVIEW_COUNT_INVALID", "反復数が負です", stage="preview", target_id=record.id))
            continue
        rendered = min(requested, max_instances)
        if direction == "unknown" and requested > 1:
            rendered = 1
            issues.append(
                Issue(
                    "PREVIEW_DIRECTION_UNRESOLVED",
                    f"SubFormのdirection {raw_direction!r}がprofile.enum_maps['subform.direction']に未登録のためRecordを1件だけ表示",
                    "warning",
                    "preview",
                    record.id,
                )
            )
        pitch = pitch_by_record.get(record.id)
        if pitch is None:
            pitch = (g["y2"] - g["y1"]) if direction == "vertical" else (g["x2"] - g["x1"])
        pitch_x = pitch if direction == "horizontal" else 0.0
        pitch_y = pitch if direction == "vertical" else 0.0
        children = [x for x in ir.elements if inside_record.get(x.id) == record.id]
        drawn = 0
        for index in range(rendered):
            dx, dy = index * pitch_x, index * pitch_y
            extra = f' data-instance="{index + 1}" data-placeholder="{mode}"'
            body.append(
                f'<rect data-object-id="{html.escape(record.id)}"{extra} x="{g["x1"]+dx}" y="{g["y1"]+dy}"'
                f' width="{g["x2"]-g["x1"]}" height="{g["y2"]-g["y1"]}" stroke="#0878c9"/>'
            )
            for child in children:
                body.extend(_shape(child, profile, dx, dy, extra))
            drawn += 1
        if drawn < requested:
            issues.append(
                Issue("PREVIEW_TRUNCATED", f"{requested}件中{drawn}件を表示しました。XMLの表示行数は変更しません", "warning", "preview", record.id)
            )
    body.extend(["</g>", "</svg>"])
    return "\n".join(body) + "\n", issues
