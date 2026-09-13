"""元XMLを正本とするIR構築、許可差分の適用、構造検査。

編集バッチ中は元の木から作ったIDとElementの対応を固定する。位置からの再採番は
行わない。削除した要素と子孫への後続操作はバッチ全体を未適用にする。
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable
from xml.etree import ElementTree as ET

from .config import sha256_bytes
from .geometry import dot_to_mm
from .models import DocumentIR, ElementIR, Issue, MappingConfig, TargetProfile, parse_positive_int
from .xmldoc import (
    MAX_XML_BYTES,
    Splice,
    XmlDocument,
    XmlEncodingError,
    XmlPreservationError,
    XmlSecurityError,
    apply_splices,
    indent_for_child,
    insert_child_splice,
    load_document,
    local_name,
    parse_xml,
    remove_splice,
    semantic_signature,
    set_attribute_splice,
)

GEOMETRY_KEYS = ("x", "y", "x1", "y1", "x2", "y2", "width", "height")
ROOT_TAG = "FormData"
MOVE_MARKER = "\x00move:"

__all__ = [
    "CompilationError",
    "ElementIndex",
    "GEOMETRY_KEYS",
    "MAX_XML_BYTES",
    "XmlSecurityError",
    "XmlEncodingError",
    "XmlPreservationError",
    "compile_xml",
    "estimate_capacity",
    "extract_ir",
    "index_tree",
    "local_name",
    "parse_xml",
    "resolve_display_line_count",
    "validate_xml",
]


class CompilationError(ValueError):
    def __init__(self, issues: list[Issue]):
        super().__init__("; ".join(f"{x.code}: {x.reason}" for x in issues))
        self.issues = issues


def _stable_id(path: str, tag: str, name: str | None) -> str:
    digest = hashlib.sha256(path.encode("utf-8")).hexdigest()[:10]
    label = (name or tag).replace(" ", "_")[:40]
    return f"svf:{tag}:{label}:{digest}"


def _segment(node: ET.Element) -> str:
    tag = local_name(node.tag)
    for key in ("name", "Number", "id"):
        if key in node.attrib:
            safe = node.attrib[key].replace("/", "%2F").replace("[", "%5B").replace("]", "%5D")
            return f"{tag}[@{key}={safe}]"
    return tag


def _walk(root: ET.Element) -> Iterable[tuple[ET.Element, str, str | None]]:
    def visit(node: ET.Element, path: str, parent_id: str | None):
        if not isinstance(node.tag, str):
            return
        node_id = _stable_id(path, local_name(node.tag), node.attrib.get("name"))
        yield node, path, parent_id
        counts: dict[str, int] = {}
        for child in list(node):
            if not isinstance(child.tag, str):
                continue
            child_segment = _segment(child)
            counts[child_segment] = counts.get(child_segment, 0) + 1
            yield from visit(child, f"{path}/{child_segment}[{counts[child_segment]}]", node_id)

    yield from visit(root, f"/{_segment(root)}[1]", None)


def index_tree(root: ET.Element) -> tuple[dict[str, ET.Element], dict[int, str], dict[str, str | None]]:
    by_id: dict[str, ET.Element] = {}
    ids_by_object: dict[int, str] = {}
    parents: dict[str, str | None] = {}
    for node, path, parent_id in _walk(root):
        node_id = _stable_id(path, local_name(node.tag), node.attrib.get("name"))
        by_id[node_id] = node
        ids_by_object[id(node)] = node_id
        parents[node_id] = parent_id
    return by_id, ids_by_object, parents


class TargetInvalid(LookupError):
    def __init__(self, code: str, reason: str, target_id: str | None):
        super().__init__(reason)
        self.issue = Issue(code, reason, target_id=target_id)


class ElementIndex:
    """1編集バッチ中だけ有効なIDとElementの対応。位置からの再採番を行わない。"""

    def __init__(self, root: ET.Element):
        self.root = root
        self.by_id, self.ids_by_object, self.parents = index_tree(root)
        self.removed: set[str] = set()
        self.children: dict[str, list[str]] = {}
        for node_id, parent_id in self.parents.items():
            if parent_id is not None:
                self.children.setdefault(parent_id, []).append(node_id)
        self._new_count = 0

    def id_of(self, element: ET.Element) -> str | None:
        return self.ids_by_object.get(id(element))

    def resolve(self, node_id: Any) -> ET.Element:
        if not isinstance(node_id, str) or not node_id:
            raise TargetInvalid("TARGET_MISSING", "target_idが指定されていません", None)
        if node_id in self.removed:
            raise TargetInvalid("TARGET_REMOVED", "削除済みまたは削除された親の子孫は編集できません", node_id)
        node = self.by_id.get(node_id)
        if node is None:
            raise TargetInvalid("TARGET_MISSING", "元XMLに存在しないtarget_idです", node_id)
        return node

    def descendants(self, node_id: str) -> list[str]:
        found: list[str] = []
        stack = list(self.children.get(node_id, []))
        while stack:
            current = stack.pop()
            found.append(current)
            stack.extend(self.children.get(current, []))
        return found

    def mark_removed(self, node_id: str) -> None:
        self.removed.add(node_id)
        self.removed.update(self.descendants(node_id))

    def register_new(self, parent_id: str, element: ET.Element) -> str:
        self._new_count += 1
        digest = hashlib.sha256(f"{parent_id}:{self._new_count}".encode("utf-8")).hexdigest()[:10]
        new_id = f"svf:new:{local_name(element.tag)}:{self._new_count}:{digest}"
        self.by_id[new_id] = element
        self.ids_by_object[id(element)] = new_id
        self.parents[new_id] = parent_id
        self.children.setdefault(parent_id, []).append(new_id)
        return new_id

    def set_parent(self, node_id: str, parent_id: str) -> None:
        old = self.parents.get(node_id)
        if old is not None and node_id in self.children.get(old, []):
            self.children[old].remove(node_id)
        self.parents[node_id] = parent_id
        self.children.setdefault(parent_id, []).append(node_id)

    def is_descendant(self, node_id: str, ancestor_id: str) -> bool:
        current = self.parents.get(node_id)
        while current is not None:
            if current == ancestor_id:
                return True
            current = self.parents.get(current)
        return False


def _number(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def resolve_coordinate_dpi(document: XmlDocument, profile: TargetProfile) -> tuple[float | None, list[Issue]]:
    """原稿DPI・XML座標DPI・出力機器DPIを分け、XML属性とprofileの一致を確認する。"""
    issues: list[Issue] = []
    declared: float | None = None
    paper = next((node for node in document.root.iter() if local_name(node.tag) == "Paper"), None)
    if paper is not None and "resolution" in paper.attrib:
        declared = _number(paper.attrib["resolution"])
        if declared is None or declared <= 0:
            issues.append(Issue("COORDINATE_DPI_INVALID", f"Paper/@resolutionが有限の正数ではありません: {paper.attrib['resolution']!r}", stage="import"))
            declared = None
    profile_dpi = profile.coordinate_dpi
    if profile_dpi is not None and (not math.isfinite(profile_dpi) or profile_dpi <= 0):
        issues.append(Issue("PROFILE_DPI_INVALID", "profileのcoordinate_dpiが有限の正数ではありません", stage="profile"))
        profile_dpi = None
    if declared is not None and profile_dpi is not None and abs(declared - profile_dpi) > 1e-9:
        issues.append(
            Issue(
                "COORDINATE_DPI_MISMATCH",
                f"XMLのPaper/@resolution {declared:g}とprofileのcoordinate_dpi {profile_dpi:g}が一致しません",
                stage="import",
            )
        )
        return None, issues
    dpi = profile_dpi if profile_dpi is not None else declared
    if dpi is None:
        issues.append(Issue("COORDINATE_DPI_UNRESOLVED", "座標DPIが未確認です。尺度未確定のまま座標を換算しません", stage="import"))
    return dpi, issues


def extract_ir(path: str | Path, profile: TargetProfile, *, data: bytes | None = None) -> DocumentIR:
    source = Path(path)
    payload = source.read_bytes() if data is None else data
    document = load_document(payload)
    issues = profile.readiness_issues(require_runtime=False)
    if not document.lexical_safe:
        issues.append(Issue("XML_LEXICAL_UNSAFE", document.preservation_reason or "字句保全ができません", "warning", "import"))
    dpi, dpi_issues = resolve_coordinate_dpi(document, profile)
    issues.extend(dpi_issues)
    index = ElementIndex(document.root)
    page_width = page_height = None
    elements: list[ElementIR] = []
    for node, source_path, _ in _walk(document.root):
        tag = local_name(node.tag)
        node_id = index.id_of(node)
        if tag == "Paper":
            page_width = _number(node.attrib.get("freeWidth"))
            page_height = _number(node.attrib.get("freeHeight"))
        raw = {key: node.attrib[key] for key in GEOMETRY_KEYS if key in node.attrib}
        geometry: dict[str, float] = {}
        for key, value in raw.items():
            number = _number(value)
            if number is None:
                issues.append(Issue("GEOMETRY_UNRESOLVED", f"{key}={value!r}は有限の数値ではありません", stage="import", target_id=node_id))
                continue
            if dpi is not None:
                geometry[key] = dot_to_mm(number, dpi)
        display_raw = node.attrib.get("displayLineCount") if tag == "Record" else None
        display = None
        if display_raw is not None:
            try:
                display = parse_positive_int(display_raw, "displayLineCount")
            except ValueError as exc:
                issues.append(Issue("DISPLAY_LINE_COUNT_UNRESOLVED", f"元値{display_raw!r}を保持します: {exc}", "warning", "import", node_id))
        elements.append(
            ElementIR(
                id=node_id,
                kind=tag,
                name=node.attrib.get("name"),
                parent_id=index.parents[node_id],
                page_id="page:1",
                source_path=source_path,
                attributes=dict(node.attrib),
                geometry_mm=geometry,
                raw_geometry=raw,
                text=node.attrib.get("strText") if tag == "Text" else None,
                display_line_count=display,
                display_line_count_raw=display_raw,
            )
        )
    return DocumentIR(
        schema="svf-ir/1.2",
        source_path=str(source.resolve()),
        source_hash=sha256_bytes(payload),
        revision=1,
        coordinate_dpi=dpi,
        page_width_mm=page_width,
        page_height_mm=page_height,
        elements=elements,
        issues=issues,
        encoding=document.encoding,
        declared_encoding=document.declared_encoding,
    )


def resolve_display_line_count(
    explicit: int | None,
    original: int | None,
    capacity: int | None,
    template_value: int | None,
) -> tuple[int | None, str]:
    for value, reason in (
        (explicit, "annotation"),
        (original, "original_xml"),
        (capacity, "simple_capacity"),
        (template_value, "profile_template"),
    ):
        if value is not None:
            return parse_positive_int(value, "displayLineCount"), reason
    return None, "unresolved"


def estimate_capacity(
    available_height_mm: float,
    pitch_mm: float,
    *,
    direction: str,
    record_count: int = 1,
    variable_height: bool = False,
    has_suppression: bool = False,
    has_page_link: bool = False,
) -> tuple[int | None, str | None]:
    complex_reasons = []
    if direction != "vertical":
        complex_reasons.append("direction")
    if record_count != 1:
        complex_reasons.append("multiple_records")
    if variable_height:
        complex_reasons.append("variable_height")
    if has_suppression:
        complex_reasons.append("suppression")
    if has_page_link:
        complex_reasons.append("page_link")
    if complex_reasons:
        return None, ",".join(complex_reasons)
    if not math.isfinite(available_height_mm) or not math.isfinite(pitch_mm):
        raise ValueError("available height and pitch must be finite")
    if available_height_mm <= 0 or pitch_mm <= 0:
        raise ValueError("available height and pitch must be positive")
    capacity = math.floor(available_height_mm / pitch_mm)
    if capacity <= 0:
        raise ValueError("calculated capacity is zero")
    return capacity, None


def _allowed(profile: TargetProfile, tag: str, attribute: str) -> bool:
    return attribute in profile.editable_attributes.get(tag, [])


def _parent_allowed(child_tag: str, parent_tag: str) -> bool:
    rules = {
        "SubForm": {"FormData"},
        "Record": {"SubForm"},
        "Field": {"FormData", "Record"},
        "Text": {"FormData", "Record"},
        "Line": {"FormData", "Record"},
        "Box": {"FormData", "Record"},
        "Bitmap": {"FormData", "Record"},
    }
    return parent_tag in rules.get(child_tag, set())


def _clone(element: ET.Element, mapping: dict[int, ET.Element]) -> ET.Element:
    copy = ET.Element(element.tag, dict(element.attrib)) if isinstance(element.tag, str) else ET.Element(element.tag)
    copy.text, copy.tail = element.text, element.tail
    mapping[id(element)] = copy
    for child in list(element):
        copy.append(_clone(child, mapping))
    return copy


def _attribute_text(value: Any, attribute: str) -> str:
    """属性値を字句へ変換する。切り捨てや丸めは行わない。"""
    if isinstance(value, bool):
        raise ValueError(f"{attribute}に真偽値は指定できません")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{attribute}に有限でない数値は指定できません: {value!r}")
        return repr(value)
    if isinstance(value, (int, str)):
        return str(value)
    raise ValueError(f"{attribute}の値の型が不正です: {type(value).__name__}")


@dataclass(slots=True)
class _Plan:
    splices: list[Splice] = field(default_factory=list)
    inserts: dict[int, tuple[ET.Element, list[str]]] = field(default_factory=dict)
    moved: list[tuple[ET.Element, int, int]] = field(default_factory=list)
    attribute_only: bool = True


def compile_xml(
    source: str | Path | None,
    changes: list[dict[str, Any]],
    profile: TargetProfile,
    *,
    data: bytes | None = None,
) -> bytes:
    """許可差分を固定IDで適用し、再解析と意味差分で保全を確認したバイト列を返す。"""
    payload = Path(source).read_bytes() if data is None else data
    document = load_document(payload)
    if not changes:
        return payload
    if not document.lexical_safe:
        raise CompilationError([Issue("XML_LEXICAL_UNSAFE", document.preservation_reason or "字句保全ができないため編集を中止します", stage="compile")])
    index = ElementIndex(document.root)
    expected_map: dict[int, ET.Element] = {}
    expected_root = _clone(document.root, expected_map)
    expected_parents: dict[int, ET.Element] = {}
    for parent in expected_root.iter():
        for child in list(parent):
            expected_parents[id(child)] = parent
    issues: list[Issue] = []
    plan = _Plan()
    touched: set[tuple[str, str]] = set()

    def expected_of(element: ET.Element) -> ET.Element:
        return expected_map[id(element)]

    for number, change in enumerate(changes):
        operation = change.get("op")
        target_id = change.get("target_id")
        try:
            node = index.resolve(target_id)
        except TargetInvalid as exc:
            exc.issue.reason = f"changes[{number}]: {exc.issue.reason}"
            issues.append(exc.issue)
            continue
        tag = local_name(node.tag)
        if operation != "add" and profile.compatibility.get(tag, "preserve_only") != "edit_supported":
            issues.append(Issue("PROFILE_UNSUPPORTED", f"{tag}は編集対応として登録されていません", target_id=target_id))
            continue
        if operation == "set":
            attribute = change.get("property")
            if not isinstance(attribute, str) or not _allowed(profile, tag, attribute):
                issues.append(Issue("PROPERTY_NOT_ALLOWED", f"{tag}.{attribute}は変更不可", target_id=target_id))
                continue
            if (target_id, attribute) in touched:
                issues.append(Issue("CHANGE_CONFLICT", f"{tag}.{attribute}に複数の値が指定されています", target_id=target_id))
                continue
            touched.add((target_id, attribute))
            try:
                if attribute == "displayLineCount":
                    text_value = str(parse_positive_int(change.get("value"), "displayLineCount"))
                else:
                    text_value = _attribute_text(change.get("value"), attribute)
            except ValueError as exc:
                code = "DISPLAY_LINE_COUNT_INVALID" if attribute == "displayLineCount" else "VALUE_TYPE_INVALID"
                issues.append(Issue(code, str(exc), target_id=target_id))
                continue
            plan.splices.append(set_attribute_splice(document, node, attribute, text_value))
            expected_of(node).set(attribute, text_value)
        elif operation == "remove":
            if index.parents.get(target_id) is None:
                issues.append(Issue("REMOVE_ROOT_FORBIDDEN", "ルートは削除できません", target_id=target_id))
                continue
            plan.splices.append(remove_splice(document, node))
            plan.attribute_only = False
            index.mark_removed(target_id)
            copy = expected_of(node)
            expected_parents[id(copy)].remove(copy)
        elif operation == "add":
            value = change.get("value")
            template_name = value.get("template") if isinstance(value, dict) else None
            fragment_text = profile.element_templates.get(template_name or "")
            if fragment_text is None:
                issues.append(Issue("TEMPLATE_UNCONFIRMED", "profileに登録された対象版ひな形が必要", target_id=target_id))
                continue
            try:
                child = parse_xml(fragment_text.encode("utf-8"))
            except (ET.ParseError, XmlSecurityError, XmlEncodingError) as exc:
                issues.append(Issue("TEMPLATE_INVALID", str(exc), target_id=target_id))
                continue
            child_tag = local_name(child.tag)
            unsupported = sorted(
                {
                    local_name(item.tag)
                    for item in child.iter()
                    if isinstance(item.tag, str) and profile.compatibility.get(local_name(item.tag), "preserve_only") == "unsupported"
                }
            )
            if profile.compatibility.get(child_tag, "preserve_only") != "edit_supported":
                issues.append(Issue("PROFILE_UNSUPPORTED", f"{child_tag}の新規生成は未対応", target_id=target_id))
                continue
            if unsupported:
                issues.append(Issue("PROFILE_UNSUPPORTED", f"ひな形の子孫が未対応です: {', '.join(unsupported)}", target_id=target_id))
                continue
            if not _parent_allowed(child_tag, tag):
                issues.append(Issue("PARENT_INVALID", f"{child_tag}を{tag}配下に追加できません", target_id=target_id))
                continue
            overrides = value.get("attributes", {}) if isinstance(value, dict) else {}
            if not isinstance(overrides, dict):
                issues.append(Issue("TEMPLATE_OVERRIDE_INVALID", "attributesはobjectが必要", target_id=target_id))
                continue
            invalid = [key for key in overrides if not _allowed(profile, child_tag, key)]
            if invalid:
                issues.append(Issue("PROPERTY_NOT_ALLOWED", f"{child_tag}の変更不可属性: {', '.join(invalid)}", target_id=target_id))
                continue
            try:
                for key, item in overrides.items():
                    child.set(key, str(parse_positive_int(item, key)) if key == "displayLineCount" else _attribute_text(item, key))
            except ValueError as exc:
                issues.append(Issue("VALUE_TYPE_INVALID", str(exc), target_id=target_id))
                continue
            fragment = ET.tostring(child, encoding="unicode", short_empty_elements=True)
            plan.inserts.setdefault(id(node), (node, []))[1].append(fragment)
            plan.attribute_only = False
            index.register_new(target_id, child)
            expected_of(node).append(child)
            expected_parents[id(child)] = expected_of(node)
            for item in child.iter():
                expected_map.setdefault(id(item), item)
        elif operation == "reparent":
            if not profile.allow_reparent:
                issues.append(Issue("REPARENT_UNSUPPORTED", "profileで親移動が許可されていません", target_id=target_id))
                continue
            value = change.get("value")
            new_parent_id = value.get("new_parent_id") if isinstance(value, dict) else None
            try:
                new_parent = index.resolve(new_parent_id)
            except TargetInvalid as exc:
                exc.issue.reason = f"changes[{number}]: 移動先の{exc.issue.reason}"
                issues.append(exc.issue)
                continue
            if new_parent_id == target_id or index.is_descendant(new_parent_id, target_id):
                issues.append(Issue("PARENT_INVALID", "自分自身または子孫へは移動できません", target_id=target_id))
                continue
            if not _parent_allowed(tag, local_name(new_parent.tag)):
                issues.append(Issue("PARENT_INVALID", "移動先の親が不正です", target_id=target_id))
                continue
            span = document.span(node)
            if any(start <= span.start and span.end <= end for _, start, end in plan.moved):
                issues.append(Issue("MOVE_NESTED_UNSUPPORTED", "移動中の要素の内側を同じバッチで移動できません", target_id=target_id))
                continue
            plan.moved.append((node, span.start, span.end))
            plan.splices.append(remove_splice(document, node))
            plan.inserts.setdefault(id(new_parent), (new_parent, []))[1].append(f"{MOVE_MARKER}{id(node)}")
            plan.attribute_only = False
            index.set_parent(target_id, new_parent_id)
            copy = expected_of(node)
            expected_parents[id(copy)].remove(copy)
            expected_of(new_parent).append(copy)
            expected_parents[id(copy)] = expected_of(new_parent)
        else:
            issues.append(Issue("OP_INVALID", f"未対応op: {operation!r}", target_id=target_id))
    if issues:
        raise CompilationError(issues)
    try:
        body = _render(document, plan)
    except XmlPreservationError as exc:
        raise CompilationError([Issue("XML_PRESERVATION_FAILED", str(exc), stage="compile")]) from exc
    _verify(document, body, expected_root, plan)
    return body


def _render(document: XmlDocument, plan: _Plan) -> bytes:
    moved_sources: dict[int, str] = {}
    contained: dict[int, list[Splice]] = {}
    outer: list[Splice] = []
    for splice in plan.splices:
        owner = None
        for node, start, end in plan.moved:
            if start <= splice.start and splice.end <= end and splice.kind == "attribute":
                owner = id(node)
                break
        if owner is None:
            outer.append(splice)
        else:
            contained.setdefault(owner, []).append(splice)
    for node, start, end in plan.moved:
        fragment = document.text[start:end]
        local = [Splice(x.start - start, x.end - start, x.replacement, x.kind) for x in contained.get(id(node), [])]
        moved_sources[id(node)] = apply_splices(fragment, local) if local else fragment
    splices = list(outer)
    for _, (parent, fragments) in plan.inserts.items():
        resolved = [moved_sources[int(x[len(MOVE_MARKER) :])] if x.startswith(MOVE_MARKER) else x for x in fragments]
        splices.append(insert_child_splice(document, parent, _join_fragments(document, parent, resolved)))
    return document.serialize(apply_splices(document.text, splices))


def _join_fragments(document: XmlDocument, parent: ET.Element, fragments: list[str]) -> str:
    separator, _ = indent_for_child(document, parent)
    return separator.join(fragments)


def _verify(document: XmlDocument, body: bytes, expected_root: ET.Element, plan: _Plan) -> None:
    issues: list[Issue] = []
    try:
        result = load_document(body)
    except (ET.ParseError, XmlSecurityError, XmlEncodingError) as exc:
        raise CompilationError([Issue("XML_PRESERVATION_FAILED", f"生成XMLを再解析できません: {exc}", stage="compile")]) from exc
    if semantic_signature(result.root) != semantic_signature(expected_root):
        issues.append(Issue("XML_PRESERVATION_FAILED", "生成XMLが指示した意味差分と一致しません", stage="compile"))
    if plan.attribute_only:
        original = document.text
        updated = result.text
        cursor_old = cursor_new = 0
        offset = 0
        for splice in sorted(plan.splices, key=lambda x: x.start):
            if original[cursor_old : splice.start] != updated[cursor_new : splice.start + offset]:
                issues.append(Issue("XML_PRESERVATION_FAILED", "属性変更で対象外の字句が変わりました", stage="compile"))
                break
            cursor_new = splice.start + offset + len(splice.replacement)
            offset += len(splice.replacement) - (splice.end - splice.start)
            cursor_old = splice.end
        else:
            if original[cursor_old:] != updated[cursor_new:]:
                issues.append(Issue("XML_PRESERVATION_FAILED", "属性変更で末尾の字句が変わりました", stage="compile"))
    if document.encoding != result.encoding or document.bom != result.bom:
        issues.append(Issue("XML_PRESERVATION_FAILED", "文字コードまたはBOMが変わりました", stage="compile"))
    if issues:
        raise CompilationError(issues)


def _link_cycles(subforms: dict[str, ElementIR]) -> list[list[str]]:
    cycles: list[list[str]] = []
    seen: set[str] = set()
    for name in subforms:
        path: list[str] = []
        current: str | None = name
        local: set[str] = set()
        while current and current in subforms and current not in local:
            local.add(current)
            path.append(current)
            current = subforms[current].attributes.get("linkName") or None
        if current and current in local:
            cycle = path[path.index(current) :]
            key = min(cycle)
            if key not in seen:
                seen.add(key)
                cycles.append(cycle)
    return cycles


def validate_xml(path: str | Path, profile: TargetProfile, mapping: MappingConfig | None = None, *, data: bytes | None = None) -> list[Issue]:
    ir = extract_ir(path, profile, data=data)
    issues = list(ir.issues)
    elements = {x.id: x for x in ir.elements}
    root = next((x for x in ir.elements if x.parent_id is None), None)
    if root is None:
        return issues + [Issue("ROOT_INVALID", "根要素がありません", stage="validation")]
    if root.kind != ROOT_TAG:
        issues.append(Issue("ROOT_INVALID", f"根要素は{ROOT_TAG}が必要です。実際は{root.kind!r}です", stage="validation", target_id=root.id))
    if profile.supported_formdata_versions:
        form_version = root.attributes.get("version")
        if form_version not in profile.supported_formdata_versions:
            issues.append(Issue("FORM_VERSION_UNSUPPORTED", f"FormData version {form_version!r}はprofileの対応外です", stage="validation", target_id=root.id))
    else:
        issues.append(Issue("FORM_VERSION_UNVERIFIED", "profileに対応FormData版が登録されていません", "warning", "validation", root.id))
    names: dict[tuple[str, str], list[ElementIR]] = {}
    for element in ir.elements:
        if element.name:
            names.setdefault((element.kind, element.name), []).append(element)
        parent = elements.get(element.parent_id or "")
        if parent is not None and element.kind in {"SubForm", "Record", "Field", "Text", "Line", "Box", "Bitmap"}:
            if not _parent_allowed(element.kind, parent.kind):
                issues.append(Issue("PARENT_INVALID", f"{element.kind}は{parent.kind}配下に置けません", stage="validation", target_id=element.id))
        if element.kind == "Bitmap":
            registered = profile.assets.get(element.name or "")
            if registered:
                if not Path(registered).expanduser().is_file():
                    issues.append(Issue("ASSET_MISSING", f"登録画像がありません: {registered}", stage="validation", target_id=element.id))
            elif element.attributes.get("strFileName"):
                issues.append(Issue("ASSET_UNVERIFIED", "画像パスは元XMLに保持しますが、対象環境の実在確認が未登録です", "warning", "validation", element.id))
        if element.kind == "Record" and element.display_line_count_raw is not None and element.display_line_count is None:
            issues.append(Issue("DISPLAY_LINE_COUNT_INVALID", f"displayLineCount {element.display_line_count_raw!r}は正の整数ではありません", stage="validation", target_id=element.id))
        g = element.geometry_mm
        if element.kind == "Line" and all(k in g for k in ("x1", "y1", "x2", "y2")):
            if g["x1"] == g["x2"] and g["y1"] == g["y2"]:
                issues.append(Issue("GEOMETRY_INVALID", "線の長さが0です", target_id=element.id))
        if element.kind in {"Box", "SubForm", "Record", "SubProperties.Box"} and all(k in g for k in ("x1", "y1", "x2", "y2")):
            if g["x2"] <= g["x1"] or g["y2"] <= g["y1"]:
                issues.append(Issue("GEOMETRY_INVALID", "枠の幅または高さが正ではありません", target_id=element.id))
    ambiguous: set[str] = set()
    for (kind, name), matches in names.items():
        if len(matches) > 1 and kind in {"Field", "Record", "SubForm"}:
            ambiguous.add(f"{kind}:{name}")
            issues.append(Issue("NAME_AMBIGUOUS", f"{kind}名{name!r}が重複しています", "warning", target_id=matches[0].id))
    subforms = {x.name: x for x in ir.elements if x.kind == "SubForm" and x.name}
    for element in subforms.values():
        link = element.attributes.get("linkName", "")
        if link:
            if link not in subforms:
                issues.append(Issue("LINK_INVALID", f"リンク先SubForm {link!r}がありません", target_id=element.id))
            elif link == element.name:
                issues.append(Issue("LINK_INVALID", "SubFormが自己参照しています", target_id=element.id))
    for cycle in _link_cycles(subforms):
        if len(cycle) > 1:
            issues.append(Issue("LINK_CYCLE", "SubFormのリンクが循環しています: " + " -> ".join(cycle), target_id=subforms[cycle[0]].id))
    if mapping:
        fields_by_name: dict[str, list[ElementIR]] = {}
        for element in ir.elements:
            if element.kind == "Field" and element.name:
                fields_by_name.setdefault(element.name, []).append(element)
        for entry in mapping.fields:
            matches = fields_by_name.get(entry.svf_field, [])
            if not matches:
                issues.append(Issue("CONTRACT_CONFLICT", f"Field {entry.svf_field!r}がXMLにありません", target_id=entry.svf_field))
                continue
            if len(matches) > 1:
                issues.append(Issue("NAME_AMBIGUOUS", f"mappingが参照するField {entry.svf_field!r}が重複し参照を確定できません", target_id=matches[0].id))
                continue
            field_ir = matches[0]
            issues.extend(_field_contract_issues(entry, field_ir))
            if entry.scope == "detail":
                ancestor = field_ir.parent_id
                in_record = False
                while ancestor:
                    parent = elements[ancestor]
                    if parent.kind == "Record":
                        in_record = True
                        if entry.record_id and entry.record_id not in {parent.id, parent.name}:
                            issues.append(Issue("CONTRACT_CONFLICT", f"record_id {entry.record_id!r}と所属Recordが一致しません", target_id=field_ir.id))
                        break
                    ancestor = parent.parent_id
                if not in_record:
                    issues.append(Issue("PARENT_INVALID", "detail FieldがRecord配下にありません", target_id=field_ir.id))
    return issues


def _field_contract_issues(entry: Any, field_ir: ElementIR) -> list[Issue]:
    """名前・桁数・式・キー・マスクの保護差分を確認する。"""
    issues: list[Issue] = []
    raw_count = field_ir.attributes.get("charCount")
    if entry.max_chars is not None and raw_count is not None:
        try:
            declared = parse_positive_int(raw_count, "charCount")
        except ValueError:
            issues.append(Issue("CONTRACT_CONFLICT", f"charCount {raw_count!r}が正の整数ではありません", target_id=field_ir.id))
        else:
            if entry.max_chars > declared:
                issues.append(
                    Issue("CONTRACT_CONFLICT", f"mappingのmax_chars {entry.max_chars}がXMLのcharCount {declared}を超えています", target_id=field_ir.id)
                )
    formula = field_ir.attributes.get("strCalcFormula") or ""
    if formula.strip():
        issues.append(Issue("FIELD_FORMULA_PRESENT", "計算式を持つFieldへ外部値を割り当てます。対象環境で優先順位を確認してください", "warning", target_id=field_ir.id))
    return issues
