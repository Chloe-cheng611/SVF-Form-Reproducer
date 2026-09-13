"""原稿PDF・画像からの抽出。

SourceObjectはページ、単位、原文、確定文字、bboxの幅と高さ、基線、未確定理由を
持つ。PDFはtmとcm、CropBoxの原点、回転、UserUnitを合成する。推定したbboxは実測値
と区別する。OCRには時間上限とログ容量上限を設ける。
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

from .geometry import Affine, pdf_crop_to_top_left_mm, px_to_mm
from .models import Issue, SourceObject, TargetProfile

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}


def _id(path: Path, page: int, number: int) -> str:
    seed = f"{path.resolve()}:{page}:{number}".encode()
    return "source:" + hashlib.sha256(seed).hexdigest()[:14]


def _run_ocr(command: list[str], profile: TargetProfile, replacements: dict[str, str]) -> tuple[str, list[Issue]]:
    resolved = []
    for part in command:
        for token, value in replacements.items():
            part = part.replace(token, value)
        resolved.append(part)
    if all("{source}" not in part for part in command):
        resolved.append(replacements["{source}"])
    try:
        result = subprocess.run(resolved, text=True, capture_output=True, timeout=profile.ocr_timeout_seconds, check=False)
    except subprocess.TimeoutExpired:
        return "", [Issue("OCR_TIMEOUT", f"OCRが{profile.ocr_timeout_seconds}秒で終わらないため取り消しました", stage="extract")]
    except OSError as exc:
        return "", [Issue("OCR_FAILED", f"OCRを起動できません: {exc}", stage="extract")]
    if result.returncode != 0:
        return "", [Issue("OCR_FAILED", (result.stderr or "").strip()[:2000] or "OCR command failed", stage="extract")]
    output = result.stdout or ""
    if len(output.encode("utf-8")) > profile.ocr_max_log_bytes:
        return output[: profile.ocr_max_log_bytes], [
            Issue("OCR_OUTPUT_TRUNCATED", f"OCR出力が{profile.ocr_max_log_bytes}バイトを超えたため打ち切りました", "warning", "extract")
        ]
    return output, []


def _structured_ocr(output: str, path: Path, page: int, source_dpi: float | None) -> tuple[list[SourceObject], list[Issue]]:
    """文字と領域を返す構造化結果を読む。対応しない出力は未確定として残す。"""
    try:
        value = json.loads(output)
    except json.JSONDecodeError:
        return (
            [
                SourceObject(
                    _id(path, page, 1),
                    "text",
                    page,
                    raw_text=output,
                    text=output,
                    bbox_source="unresolved",
                    source_dpi=source_dpi,
                    unresolved_reason="OCR出力が構造化JSONではありません",
                )
            ],
            [Issue("OCR_UNSTRUCTURED", "OCRが文字と領域の構造化結果を返しませんでした", "warning", "extract")],
        )
    items = value.get("objects", value) if isinstance(value, dict) else value
    if not isinstance(items, list):
        return [], [Issue("OCR_UNSTRUCTURED", "OCR出力にobjects配列がありません", stage="extract")]
    objects: list[SourceObject] = []
    issues: list[Issue] = []
    for number, item in enumerate(items, 1):
        if not isinstance(item, dict) or not isinstance(item.get("text"), str):
            issues.append(Issue("OCR_UNSTRUCTURED", f"OCR結果[{number}]にtextがありません", "warning", "extract"))
            continue
        box = None
        reason = "OCRが領域を返しませんでした"
        keys = ("x", "y", "width", "height")
        if all(isinstance(item.get(key), (int, float)) and not isinstance(item.get(key), bool) for key in keys):
            raw = {key: float(item[key]) for key in keys}
            if str(item.get("unit", "px")) == "px":
                if source_dpi:
                    box = {key: px_to_mm(number_value, source_dpi) for key, number_value in raw.items()}
                    reason = None
                else:
                    reason = "画像DPIが未確認のためpxをmmへ換算できません"
            else:
                box = raw
                reason = None
        objects.append(
            SourceObject(
                _id(path, page, number),
                "text",
                page,
                raw_text=item.get("raw_text", item["text"]),
                text=item["text"],
                layout_bbox=box,
                bbox_source="measured" if box else "unresolved",
                baseline=float(item["baseline"]) if isinstance(item.get("baseline"), (int, float)) and not isinstance(item.get("baseline"), bool) else None,
                source_dpi=source_dpi,
                unresolved_reason=reason,
            )
        )
    return objects, issues


def _extract_pdf(source: Path, profile: TargetProfile) -> tuple[list[SourceObject], list[dict[str, Any]], list[Issue]]:
    objects: list[SourceObject] = []
    geometry: list[dict[str, Any]] = []
    issues: list[Issue] = []
    try:
        from pypdf import PdfReader  # type: ignore
    except ImportError:
        return [], [], [Issue("PDF_EXTRACTOR_MISSING", "PDF抽出にはpypdfをインストールしてください", stage="extract")]
    reader = PdfReader(source)
    for page_number, page in enumerate(reader.pages, 1):
        crop = [float(x) for x in page.cropbox]
        crop_width = crop[2] - crop[0]
        crop_height = crop[3] - crop[1]
        rotation = int(page.get("/Rotate", 0) or 0)
        user_unit = float(page.get("/UserUnit", 1) or 1)
        try:
            transform = pdf_crop_to_top_left_mm(crop[0], crop[1], crop_width, crop_height, rotation, user_unit)
        except ValueError as exc:
            issues.append(Issue("PDF_PAGE_UNSUPPORTED", f"{page_number}ページ: {exc}", stage="extract"))
            continue
        geometry.append(
            {
                "page": page_number,
                "media_box_pt": [float(x) for x in page.mediabox],
                "crop_box_pt": crop,
                "rotation": rotation,
                "user_unit": user_unit,
                "to_top_left_mm": transform.to_list(),
                "from_top_left_mm": transform.inverse().to_list(),
            }
        )
        before = len(objects)
        scale_mm = abs(transform.a) or abs(transform.b) or 1.0

        def visitor(text, cm, tm, _font, size, _page=page_number, _transform=transform, _scale=scale_mm):
            if not text or not text.strip():
                return
            text_matrix = Affine(*[float(x) for x in tm])
            if cm is not None:
                text_matrix = Affine(*[float(x) for x in cm]).compose(text_matrix)
            x, y = _transform.apply(text_matrix.e, text_matrix.f)
            font_size = float(size or 0) * (abs(float(tm[0])) or 1.0)
            height = font_size * _scale
            width = height * 0.5 * len(text.strip())
            objects.append(
                SourceObject(
                    _id(source, _page, len(objects) + 1),
                    "text",
                    _page,
                    raw_text=text,
                    text=text.strip(),
                    layout_bbox={"x": x, "y": y - height, "width": width, "height": height} if height > 0 else None,
                    bbox_source="estimated" if height > 0 else "unresolved",
                    baseline=y,
                    unresolved_reason=None if height > 0 else "文字サイズを取得できません",
                )
            )

        raw = page.extract_text(visitor_text=visitor) or ""
        if len(objects) == before:
            if raw:
                objects.append(
                    SourceObject(
                        _id(source, page_number, 1),
                        "text",
                        page_number,
                        raw_text=raw,
                        text=raw,
                        bbox_source="unresolved",
                        unresolved_reason="文字の配置情報を取得できませんでした",
                    )
                )
            else:
                issues.extend(_scan_page(source, page_number, profile, objects))
    return objects, geometry, issues


def _scan_page(source: Path, page_number: int, profile: TargetProfile, objects: list[SourceObject]) -> list[Issue]:
    """文字層のないページを画像化してOCRへ接続する。"""
    if not profile.pdf_render_command or not profile.ocr_command:
        return [
            Issue(
                "OCR_REQUIRED",
                f"{page_number}ページに文字層がありません。pdf_render_commandとocr_commandの登録が必要です",
                stage="extract",
            )
        ]
    import tempfile

    with tempfile.TemporaryDirectory(prefix="svf-scan-") as staging:
        image = Path(staging) / f"page-{page_number}.png"
        replacements = {"{source}": str(source.resolve()), "{page}": str(page_number), "{output}": str(image)}
        render = [part.replace("{source}", replacements["{source}"]).replace("{page}", replacements["{page}"]).replace("{output}", replacements["{output}"]) for part in profile.pdf_render_command]
        try:
            result = subprocess.run(render, text=True, capture_output=True, timeout=profile.ocr_timeout_seconds, check=False)
        except subprocess.TimeoutExpired:
            return [Issue("PDF_RENDER_TIMEOUT", f"{page_number}ページの画像化が時間内に終わりませんでした", stage="extract")]
        except OSError as exc:
            return [Issue("PDF_RENDER_FAILED", f"{page_number}ページの画像化を起動できません: {exc}", stage="extract")]
        if result.returncode != 0 or not image.is_file():
            return [Issue("PDF_RENDER_FAILED", (result.stderr or "").strip()[:2000] or f"{page_number}ページを画像化できません", stage="extract")]
        output, issues = _run_ocr(profile.ocr_command, profile, {"{source}": str(image), "{page}": str(page_number)})
        if not output:
            return issues
        found, structured_issues = _structured_ocr(output, source, page_number, None)
        objects.extend(found)
        return issues + structured_issues


def _extract_image(source: Path, profile: TargetProfile, source_dpi: float | None) -> tuple[list[SourceObject], list[Issue]]:
    objects: list[SourceObject] = []
    issues: list[Issue] = []
    if source_dpi is None or source_dpi <= 0:
        issues.append(Issue("SOURCE_DPI_REQUIRED", "画像には既知の物理寸法またはDPIが必要", stage="extract"))
    try:
        from PIL import Image  # type: ignore
    except ImportError:
        issues.append(Issue("IMAGE_EXTRACTOR_MISSING", "画像抽出にはPillowをインストールしてください", stage="extract"))
    else:
        with Image.open(source) as image:
            width, height = image.width, image.height
        box = None
        reason = "画像DPIが未確認のためpxをmmへ換算できません"
        if source_dpi:
            box = {"x": 0.0, "y": 0.0, "width": px_to_mm(width, source_dpi), "height": px_to_mm(height, source_dpi)}
            reason = None
        objects.append(
            SourceObject(
                _id(source, 1, 1),
                "image",
                1,
                layout_bbox=box,
                ink_bbox={"x": 0.0, "y": 0.0, "width": float(width), "height": float(height)},
                bbox_source="measured" if box else "unresolved",
                source_dpi=source_dpi,
                unresolved_reason=reason,
            )
        )
    if profile.ocr_command:
        output, ocr_issues = _run_ocr(profile.ocr_command, profile, {"{source}": str(source.resolve()), "{page}": "1"})
        issues.extend(ocr_issues)
        if output:
            found, structured_issues = _structured_ocr(output, source, 1, source_dpi)
            objects.extend(found)
            issues.extend(structured_issues)
    else:
        issues.append(Issue("OCR_NOT_CONFIGURED", "OCRコマンド未設定。画像領域だけ保留します", "warning", "extract"))
    return objects, issues


def extract_source(path: str | Path, profile: TargetProfile, *, source_dpi: float | None = None) -> tuple[dict[str, Any], list[Issue]]:
    source = Path(path)
    suffix = source.suffix.lower()
    geometry: list[dict[str, Any]] = []
    if suffix == ".pdf":
        objects, geometry, issues = _extract_pdf(source, profile)
    elif suffix in IMAGE_SUFFIXES:
        objects, issues = _extract_image(source, profile, source_dpi)
    else:
        objects, issues = [], [Issue("SOURCE_TYPE_UNSUPPORTED", f"未対応形式: {suffix}", stage="extract")]
    return {
        "schema": "svf-source/1.1",
        "path": str(source.resolve()),
        "unit": "mm",
        "source_dpi": source_dpi,
        "source_geometry": geometry,
        "objects": [x.to_dict() for x in objects],
    }, issues
