from __future__ import annotations

from dataclasses import dataclass


MM_PER_INCH = 25.4
POINTS_PER_INCH = 72.0


def px_to_mm(value: float, source_dpi: float) -> float:
    if source_dpi <= 0:
        raise ValueError("source_dpi must be positive")
    return value * MM_PER_INCH / source_dpi


def point_to_mm(value: float, user_unit: float = 1.0) -> float:
    if user_unit <= 0:
        raise ValueError("PDF UserUnit must be positive")
    return value * user_unit * MM_PER_INCH / POINTS_PER_INCH


def mm_to_dot(value: float, coordinate_dpi: float) -> float:
    if coordinate_dpi <= 0:
        raise ValueError("coordinate_dpi must be positive")
    return value * coordinate_dpi / MM_PER_INCH


def dot_to_mm(value: float, coordinate_dpi: float) -> float:
    if coordinate_dpi <= 0:
        raise ValueError("coordinate_dpi must be positive")
    return value * MM_PER_INCH / coordinate_dpi


@dataclass(frozen=True, slots=True)
class Affine:
    a: float = 1.0
    b: float = 0.0
    c: float = 0.0
    d: float = 1.0
    e: float = 0.0
    f: float = 0.0

    def apply(self, x: float, y: float) -> tuple[float, float]:
        return self.a * x + self.c * y + self.e, self.b * x + self.d * y + self.f

    def inverse(self) -> "Affine":
        determinant = self.a * self.d - self.b * self.c
        if determinant == 0:
            raise ValueError("affine transform is not invertible")
        return Affine(
            self.d / determinant,
            -self.b / determinant,
            -self.c / determinant,
            self.a / determinant,
            (self.c * self.f - self.d * self.e) / determinant,
            (self.b * self.e - self.a * self.f) / determinant,
        )

    def compose(self, inner: "Affine") -> "Affine":
        """innerを先に適用してからselfを適用する合成変換。"""
        return Affine(
            self.a * inner.a + self.c * inner.b,
            self.b * inner.a + self.d * inner.b,
            self.a * inner.c + self.c * inner.d,
            self.b * inner.c + self.d * inner.d,
            self.a * inner.e + self.c * inner.f + self.e,
            self.b * inner.e + self.d * inner.f + self.f,
        )

    def to_list(self) -> list[float]:
        return [self.a, self.b, self.c, self.d, self.e, self.f]


def pdf_page_to_top_left_mm(width_pt: float, height_pt: float, rotation: int = 0, user_unit: float = 1.0) -> Affine:
    scale = user_unit * MM_PER_INCH / POINTS_PER_INCH
    normalized = rotation % 360
    if normalized == 0:
        return Affine(scale, 0, 0, -scale, 0, height_pt * scale)
    if normalized == 90:
        return Affine(0, scale, scale, 0, 0, 0)
    if normalized == 180:
        return Affine(-scale, 0, 0, scale, width_pt * scale, 0)
    if normalized == 270:
        return Affine(0, -scale, -scale, 0, height_pt * scale, width_pt * scale)
    raise ValueError("PDF rotation must be 0, 90, 180, or 270")



def pdf_crop_to_top_left_mm(
    crop_x0: float,
    crop_y0: float,
    crop_width_pt: float,
    crop_height_pt: float,
    rotation: int = 0,
    user_unit: float = 1.0,
) -> Affine:
    """CropBoxの原点、回転、UserUnitを合成してページ左上原点のmmへ移す。"""
    page = pdf_page_to_top_left_mm(crop_width_pt, crop_height_pt, rotation, user_unit)
    return page.compose(Affine(1.0, 0.0, 0.0, 1.0, -crop_x0, -crop_y0))
