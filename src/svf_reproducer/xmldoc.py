"""XML読込経路の統一と字句保全。

原バイト、BOM、宣言を保存し、許可した文字コードで厳密にデコードしてからDTDと
実体を拒否するパーサへ渡す。編集は開始タグの局所置換と要素範囲の切り貼りで行い、
対象外バイトを保持する。局所置換で扱えない構文は明示的に停止する。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable
from xml.etree import ElementTree as ET
from xml.parsers import expat

MAX_XML_BYTES = 100 * 1024 * 1024
MAX_XML_DEPTH = 200
MAX_XML_ELEMENTS = 500_000

_DECLARATION = re.compile(r"<\?xml\s[^>]*?encoding\s*=\s*([\"'])([A-Za-z0-9._\-]+)\1", re.IGNORECASE)

# 宣言名から実際に使うPythonコーデックへの対応。推測での読み替えは行わない。
ALLOWED_ENCODINGS: dict[str, str] = {
    "utf-8": "utf-8",
    "utf8": "utf-8",
    "us-ascii": "ascii",
    "ascii": "ascii",
    "iso-8859-1": "iso-8859-1",
    "latin-1": "iso-8859-1",
    "utf-16": "utf-16",
    "utf-16le": "utf-16-le",
    "utf-16be": "utf-16-be",
    "shift_jis": "shift_jis",
    "sjis": "shift_jis",
    "windows-31j": "cp932",
    "cp932": "cp932",
    "ms_kanji": "cp932",
    "euc-jp": "euc_jp",
    "iso-2022-jp": "iso2022_jp",
}


class XmlSecurityError(ValueError):
    pass


class XmlEncodingError(ValueError):
    pass


class XmlPreservationError(ValueError):
    pass


def local_name(tag: Any) -> str:
    """名前空間URIと接頭辞のどちらの記法でも局所名を返す。"""
    if not isinstance(tag, str):
        return "#comment"
    return tag.rsplit("}", 1)[-1].rsplit(":", 1)[-1]


def qualified_name(tag: Any) -> str:
    return tag if isinstance(tag, str) else "#comment"


@dataclass(slots=True)
class ElementSpan:
    start: int
    start_tag_end: int
    end_tag_start: int
    end: int
    self_closing: bool
    depth: int


def detect_encoding(data: bytes) -> tuple[bytes, str, str | None]:
    """(BOM, 使用コーデック, 宣言された文字コード名) を返す。"""
    if data.startswith(b"\xef\xbb\xbf"):
        bom, codec, body = b"\xef\xbb\xbf", "utf-8", data[3:]
    elif data.startswith(b"\xff\xfe"):
        bom, codec, body = b"\xff\xfe", "utf-16-le", data[2:]
    elif data.startswith(b"\xfe\xff"):
        bom, codec, body = b"\xfe\xff", "utf-16-be", data[2:]
    elif data[:2] == b"<\x00":
        bom, codec, body = b"", "utf-16-le", data
    elif data[:2] == b"\x00<":
        bom, codec, body = b"", "utf-16-be", data
    else:
        bom, codec, body = b"", "utf-8", data
    prefix = body[:512]
    try:
        header = prefix.decode(codec, errors="ignore")
    except (LookupError, UnicodeDecodeError):
        header = ""
    match = _DECLARATION.search(header)
    declared = match.group(2) if match else None
    if declared is None:
        return bom, codec, None
    normalized = declared.strip().lower()
    if normalized not in ALLOWED_ENCODINGS:
        raise XmlEncodingError(f"未対応の文字コード宣言です: {declared}")
    resolved = ALLOWED_ENCODINGS[normalized]
    if codec.startswith("utf-16") and not resolved.startswith("utf-16"):
        raise XmlEncodingError(f"BOMまたはバイト列はUTF-16ですが宣言は{declared}です")
    if resolved.startswith("utf-16") and not codec.startswith("utf-16"):
        raise XmlEncodingError(f"宣言は{declared}ですがUTF-16のバイト列ではありません")
    if codec.startswith("utf-16"):
        return bom, codec, declared
    return bom, resolved, declared


def _scan_start_tag(text: str, start: int) -> tuple[int, bool]:
    index = start + 1
    quote = ""
    length = len(text)
    while index < length:
        character = text[index]
        if quote:
            if character == quote:
                quote = ""
        elif character in "\"'":
            quote = character
        elif character == ">":
            return index + 1, text[index - 1] == "/"
        index += 1
    raise XmlPreservationError("開始タグの終端を特定できません")


def parse_document(text: str) -> tuple[ET.Element, dict[int, ElementSpan]]:
    """DTDと実体を拒否し、要素ごとの文字位置を記録して木を作る。"""
    data = text.encode("utf-8")
    builder = ET.TreeBuilder(insert_comments=True, insert_pis=True)
    parser = expat.ParserCreate(encoding="utf-8")
    parser.buffer_text = True
    spans: dict[int, ElementSpan] = {}
    stack: list[tuple[ET.Element, int, int, bool]] = []
    cursor = {"byte": 0, "char": 0, "count": 0}

    def to_char(offset: int) -> int:
        if offset < cursor["byte"]:
            raise XmlPreservationError("解析位置が逆行しました")
        cursor["char"] += len(data[cursor["byte"] : offset].decode("utf-8"))
        cursor["byte"] = offset
        return cursor["char"]

    def reject_doctype(*_args: Any) -> None:
        raise XmlSecurityError("DTD宣言は許可されていません")

    def reject_entity(*_args: Any) -> None:
        raise XmlSecurityError("実体宣言は許可されていません")

    def reject_external(*_args: Any) -> int:
        raise XmlSecurityError("外部実体参照は許可されていません")

    def start(name: str, attributes: dict[str, str]) -> None:
        cursor["count"] += 1
        if cursor["count"] > MAX_XML_ELEMENTS:
            raise XmlSecurityError(f"要素数が{MAX_XML_ELEMENTS}を超えました")
        if len(stack) >= MAX_XML_DEPTH:
            raise XmlSecurityError(f"入れ子が{MAX_XML_DEPTH}段を超えました")
        offset = to_char(parser.CurrentByteIndex)
        tag_end, self_closing = _scan_start_tag(text, offset)
        element = builder.start(name, dict(attributes))
        stack.append((element, offset, tag_end, self_closing))

    def end(_name: str) -> None:
        element, offset, tag_end, self_closing = stack.pop()
        if self_closing:
            end_tag_start = finish = tag_end
        else:
            end_tag_start = to_char(parser.CurrentByteIndex)
            closing = text.find(">", end_tag_start)
            if closing < 0:
                raise XmlPreservationError("終了タグの終端を特定できません")
            finish = closing + 1
        spans[id(element)] = ElementSpan(offset, tag_end, end_tag_start, finish, self_closing, len(stack))
        builder.end(_name)

    parser.StartDoctypeDeclHandler = reject_doctype
    parser.EntityDeclHandler = reject_entity
    parser.UnparsedEntityDeclHandler = reject_entity
    parser.ExternalEntityRefHandler = reject_external
    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = builder.data
    parser.CommentHandler = builder.comment
    parser.ProcessingInstructionHandler = builder.pi
    try:
        parser.Parse(data, True)
    except expat.ExpatError as exc:
        raise ET.ParseError(str(exc)) from exc
    root = builder.close()
    return root, spans


@dataclass(slots=True)
class XmlDocument:
    raw: bytes
    bom: bytes
    encoding: str
    declared_encoding: str | None
    text: str
    root: ET.Element
    spans: dict[int, ElementSpan]
    lexical_safe: bool
    preservation_reason: str | None = None

    def serialize(self, text: str | None = None) -> bytes:
        body = self.text if text is None else text
        return self.bom + body.encode(self.encoding)

    def span(self, element: ET.Element) -> ElementSpan:
        span = self.spans.get(id(element))
        if span is None:
            raise XmlPreservationError("元XMLに対応しない要素です")
        return span


def load_document(data: bytes) -> XmlDocument:
    if len(data) > MAX_XML_BYTES:
        raise XmlSecurityError(f"XMLが{MAX_XML_BYTES}バイトを超えています")
    bom, codec, declared = detect_encoding(data)
    body = data[len(bom) :]
    try:
        text = body.decode(codec)
    except UnicodeDecodeError as exc:
        raise XmlEncodingError(f"{declared or codec}として復号できません: {exc}") from exc
    root, spans = parse_document(text)
    try:
        lexical_safe = text.encode(codec) == body
        reason = None if lexical_safe else "文字コードの往復でバイト列が一致しません"
    except UnicodeEncodeError as exc:
        lexical_safe = False
        reason = f"{codec}で出力できない文字があります: {exc}"
    return XmlDocument(data, bom, codec, declared, text, root, spans, lexical_safe, reason)


def load_document_from_path(path: Any) -> XmlDocument:
    from pathlib import Path

    return load_document(Path(path).read_bytes())


def parse_xml(data: bytes | str) -> ET.Element:
    """安全側の設定で解析した根要素を返す。"""
    if isinstance(data, str):
        data = data.encode("utf-8")
    return load_document(data).root


# --- 局所置換 -------------------------------------------------------------

_ATTRIBUTE = re.compile(r"([^\s=/>]+)\s*=\s*([\"'])")


def attribute_span(text: str, span: ElementSpan, name: str) -> tuple[int, int, str] | None:
    """開始タグ内の属性値の文字範囲と引用符を返す。"""
    region_end = span.start_tag_end
    index = span.start + 1
    while index < region_end and not text[index].isspace():
        index += 1
    while index < region_end:
        match = _ATTRIBUTE.search(text, index, region_end)
        if match is None:
            return None
        quote = match.group(2)
        value_start = match.end()
        value_end = text.find(quote, value_start)
        if value_end < 0 or value_end >= region_end:
            raise XmlPreservationError("属性値の終端を特定できません")
        if match.group(1) == name:
            return value_start, value_end, quote
        index = value_end + 1
    return None


def escape_attribute(value: str, quote: str) -> str:
    body = value.replace("&", "&amp;").replace("<", "&lt;")
    body = body.replace(quote, "&quot;" if quote == '"' else "&apos;")
    return body.replace("\r", "&#13;").replace("\n", "&#10;").replace("\t", "&#9;")


@dataclass(slots=True)
class Splice:
    start: int
    end: int
    replacement: str
    kind: str = "edit"


def apply_splices(text: str, splices: Iterable[Splice]) -> str:
    ordered = sorted(splices, key=lambda x: (x.start, x.end))
    previous_end = -1
    for item in ordered:
        if item.start < previous_end:
            raise XmlPreservationError("編集範囲が重なっています")
        previous_end = item.end
    result = text
    for item in reversed(ordered):
        result = result[: item.start] + item.replacement + result[item.end :]
    return result


def set_attribute_splice(document: XmlDocument, element: ET.Element, name: str, value: str) -> Splice:
    span = document.span(element)
    found = attribute_span(document.text, span, name)
    if found is not None:
        start, end, quote = found
        return Splice(start, end, escape_attribute(value, quote), "attribute")
    insert_at = span.start_tag_end - (2 if span.self_closing else 1)
    return Splice(insert_at, insert_at, f' {name}="{escape_attribute(value, chr(34))}"', "attribute")


def _leading_whitespace_start(text: str, start: int) -> int:
    index = start
    while index > 0 and text[index - 1] in " \t":
        index -= 1
    if index > 0 and text[index - 1] == "\n":
        index -= 1
        if index > 0 and text[index - 1] == "\r":
            index -= 1
        return index
    return start


def remove_splice(document: XmlDocument, element: ET.Element) -> Splice:
    span = document.span(element)
    return Splice(_leading_whitespace_start(document.text, span.start), span.end, "", "structure")


def element_source(document: XmlDocument, element: ET.Element) -> str:
    span = document.span(element)
    return document.text[span.start : span.end]


def indent_for_child(document: XmlDocument, parent: ET.Element) -> tuple[str, str]:
    """(子要素の前に置く区切り, 終了タグの前に置く区切り) を元XMLから決める。"""
    text = document.text
    span = document.span(parent)
    children = [child for child in list(parent) if id(child) in document.spans]
    if children:
        first = document.spans[id(children[0])]
        separator = text[span.start_tag_end : first.start]
        if separator.strip() == "":
            last = document.spans[id(children[-1])]
            closing = text[last.end : span.end_tag_start]
            return separator, closing if closing.strip() == "" else separator
    line_start = text.rfind("\n", 0, span.start) + 1
    indent = text[line_start : span.start]
    if indent.strip() != "":
        indent = ""
    newline = "\r\n" if "\r\n" in text else "\n"
    return f"{newline}{indent}  ", f"{newline}{indent}"


def insert_child_splice(document: XmlDocument, parent: ET.Element, fragment: str) -> Splice:
    """末尾の子として挿入する。元XMLの字下げを引き継ぎ、既存の子と本文は変えない。"""
    span = document.span(parent)
    separator, closing = indent_for_child(document, parent)
    if span.self_closing:
        head = document.text[span.start : span.start_tag_end]
        opened = head[:-2].rstrip() + ">"
        name_match = re.match(r"[^\s/>]+", document.text[span.start + 1 : span.start_tag_end])
        raw_tag = name_match.group(0) if name_match else local_name(parent.tag)
        return Splice(span.start, span.end, f"{opened}{separator}{fragment}{closing}</{raw_tag}>", "structure")
    children = [child for child in list(parent) if id(child) in document.spans]
    if children:
        tail_start = document.spans[id(children[-1])].end
        if document.text[tail_start : span.end_tag_start].strip() == "":
            return Splice(tail_start, span.end_tag_start, f"{separator}{fragment}{closing}", "structure")
    elif document.text[span.start_tag_end : span.end_tag_start].strip() == "":
        return Splice(span.start_tag_end, span.end_tag_start, f"{separator}{fragment}{closing}", "structure")
    return Splice(span.end_tag_start, span.end_tag_start, f"{separator}{fragment}{closing}", "structure")


def semantic_signature(element: ET.Element) -> Any:
    if not isinstance(element.tag, str):
        return ("#other", (element.text or "").strip())
    return (
        element.tag,
        tuple(sorted(element.attrib.items())),
        (element.text or "").strip(),
        tuple(semantic_signature(child) for child in list(element)),
    )
