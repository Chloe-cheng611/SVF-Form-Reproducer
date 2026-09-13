from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable
from xml.etree import ElementTree as ET

from .config import sha256_file
from .xmlcore import local_name, parse_xml


PURPOSES = {
    "Field": "可変項目の属性記法",
    "Text": "固定文字の属性記法",
    "Line": "線の属性記法",
    "Box": "枠と塗りの属性記法",
    "SubForm": "サブフォームの属性と親文脈",
    "Record": "レコードの属性と親文脈",
    "Bitmap": "画像部品の属性記法",
    "Barcode": "バーコードの属性記法",
    "QRCode": "QRコードの属性記法",
    "Paper": "用紙属性の記法",
}


@dataclass(slots=True)
class FewShotExample:
    example_id: str
    tag: str
    purpose: str
    parent_context: list[str]
    source: str
    source_version: str | None
    source_designer: str | None
    attribute_names: list[str]
    fragment: str

    def to_dict(self) -> dict:
        return asdict(self)


def build_index(path: str | Path) -> dict:
    source = Path(path)
    root = parse_xml(source.read_bytes())
    version = root.attrib.get("version")
    designer = root.attrib.get("designer")
    examples: list[FewShotExample] = []

    def visit(node: ET.Element, parents: list[str]) -> None:
        tag = local_name(node.tag)
        if tag in PURPOSES:
            examples.append(
                FewShotExample(
                    example_id=f"example-{len(examples) + 1:03d}",
                    tag=tag,
                    purpose=PURPOSES[tag],
                    parent_context=parents,
                    source=source.name,
                    source_version=version,
                    source_designer=designer,
                    attribute_names=list(node.attrib),
                    fragment=ET.tostring(node, encoding="unicode", short_empty_elements=True),
                )
            )
        for child in list(node):
            if isinstance(child.tag, str):
                visit(child, parents + [tag])

    visit(root, [])
    return {
        "schema": "svf-fewshot-index/1.0",
        "source": str(source),
        "source_hash": sha256_file(source),
        "examples": [x.to_dict() for x in examples],
    }


def select_examples(index: dict, tags: Iterable[str], limit: int = 8) -> list[dict]:
    wanted = set(tags)
    selected = [x for x in index.get("examples", []) if x.get("tag") in wanted]
    return selected[: max(0, limit)]

