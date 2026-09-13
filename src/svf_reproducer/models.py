"""読込境界で型・enum・範囲を検査するデータ契約。"""
from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Literal


Severity = Literal["error", "warning"]
Compatibility = Literal["edit_supported", "preserve_only", "unsupported"]

_INTEGER = re.compile(r"^[+-]?\d+$")
_FULLWIDTH = str.maketrans("０１２３４５６７８９", "0123456789")


def parse_positive_int(value: Any, label: str) -> int:
    """boolと小数を拒否し、整数表記の文字列だけを受理して正規化値を返す。"""
    if isinstance(value, bool):
        raise ValueError(f"{label}に真偽値は指定できません")
    if isinstance(value, int):
        number = value
    elif isinstance(value, float):
        raise ValueError(f"{label}に小数は指定できません: {value!r}")
    elif isinstance(value, str):
        text = value.strip().translate(_FULLWIDTH)
        if not _INTEGER.fullmatch(text):
            raise ValueError(f"{label}は整数表記が必要です: {value!r}")
        number = int(text)
    else:
        raise ValueError(f"{label}の型が不正です: {type(value).__name__}")
    if number <= 0:
        raise ValueError(f"{label}は正の整数が必要です: {number}")
    return number


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _finite(value: Any, label: str, *, allow_none: bool = True) -> float | None:
    if value is None:
        _check(allow_none, f"{label}は必須です")
        return None
    _check(not isinstance(value, bool) and isinstance(value, (int, float)), f"{label}は数値が必要です: {value!r}")
    number = float(value)
    _check(math.isfinite(number), f"{label}は有限の数値が必要です: {value!r}")
    return number


def _enum(value: Any, label: str, allowed: set[str]) -> str:
    _check(isinstance(value, str) and value in allowed, f"{label}は{sorted(allowed)}のいずれかが必要です: {value!r}")
    return value


def _string_list(value: Any, label: str) -> list[str]:
    _check(isinstance(value, list) and all(isinstance(x, str) for x in value), f"{label}は文字列配列が必要です")
    return list(value)


def _known_keys(cls: Any, value: dict[str, Any], label: str) -> dict[str, Any]:
    _check(isinstance(value, dict), f"{label}はJSON objectが必要です")
    known = {x.name for x in fields(cls)}
    unknown = sorted(set(value) - known)
    _check(not unknown, f"{label}の未知キー: " + ", ".join(unknown))
    return dict(value)


@dataclass(slots=True)
class Issue:
    code: str
    reason: str
    severity: Severity = "error"
    stage: str = "validation"
    target_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class TargetProfile:
    profile_id: str
    product: str | None = None
    version: str | None = None
    service_pack: str | None = None
    output_device: str | None = None
    host_os: str | None = None
    fonts: list[str] = field(default_factory=list)
    coordinate_dpi: float | None = None
    output_device_dpi: float | None = None
    base_xml: str | None = None
    runtime_kind: Literal["command", "none"] = "none"
    runtime_command: list[str] = field(default_factory=list)
    runtime_timeout_seconds: float = 300
    runtime_artifact_globs: list[str] = field(default_factory=lambda: ["*.pdf"])
    ocr_command: list[str] = field(default_factory=list)
    pdf_render_command: list[str] = field(default_factory=list)
    ocr_timeout_seconds: float = 120
    ocr_max_log_bytes: int = 1_000_000
    proposal_command: list[str] = field(default_factory=list)
    proposal_timeout_seconds: float = 120
    supports_empty_form: bool = False
    supports_embedded_newlines: bool = False
    compatibility: dict[str, Compatibility] = field(default_factory=dict)
    editable_attributes: dict[str, list[str]] = field(default_factory=dict)
    element_templates: dict[str, str] = field(default_factory=dict)
    allow_reparent: bool = False
    enum_maps: dict[str, dict[str, str]] = field(default_factory=dict)
    record_group: list[str] = field(default_factory=list)
    assets: dict[str, str] = field(default_factory=dict)
    supported_formdata_versions: list[str] = field(default_factory=list)
    source_notes: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        _check(isinstance(self.profile_id, str) and self.profile_id != "", "profile_idは非空の文字列が必要です")
        for key in ("product", "version", "service_pack", "output_device", "host_os", "base_xml"):
            value = getattr(self, key)
            _check(value is None or isinstance(value, str), f"{key}は文字列が必要です")
        self.coordinate_dpi = _finite(self.coordinate_dpi, "coordinate_dpi")
        self.output_device_dpi = _finite(self.output_device_dpi, "output_device_dpi")
        for key in ("runtime_timeout_seconds", "ocr_timeout_seconds", "proposal_timeout_seconds"):
            value = _finite(getattr(self, key), key, allow_none=False)
            _check(value > 0, f"{key}は正の数が必要です")
            setattr(self, key, value)
        _check(not isinstance(self.ocr_max_log_bytes, bool) and isinstance(self.ocr_max_log_bytes, int) and self.ocr_max_log_bytes > 0, "ocr_max_log_bytesは正の整数が必要です")
        self.runtime_kind = _enum(self.runtime_kind, "runtime_kind", {"command", "none"})
        for key in ("fonts", "runtime_command", "runtime_artifact_globs", "ocr_command", "pdf_render_command", "proposal_command", "record_group", "supported_formdata_versions", "source_notes"):
            setattr(self, key, _string_list(getattr(self, key), key))
        for key in ("supports_empty_form", "supports_embedded_newlines", "allow_reparent"):
            _check(isinstance(getattr(self, key), bool), f"{key}は真偽値が必要です")
        _check(isinstance(self.compatibility, dict), "compatibilityはobjectが必要です")
        for tag, value in self.compatibility.items():
            _check(isinstance(tag, str), "compatibilityのキーは文字列が必要です")
            _enum(value, f"compatibility[{tag}]", {"edit_supported", "preserve_only", "unsupported"})
        _check(isinstance(self.editable_attributes, dict), "editable_attributesはobjectが必要です")
        for tag, value in self.editable_attributes.items():
            _string_list(value, f"editable_attributes[{tag}]")
        for key in ("element_templates", "assets"):
            value = getattr(self, key)
            _check(isinstance(value, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()), f"{key}は文字列のobjectが必要です")
        _check(isinstance(self.enum_maps, dict), "enum_mapsはobjectが必要です")
        for name, value in self.enum_maps.items():
            _check(isinstance(value, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()), f"enum_maps[{name}]は文字列のobjectが必要です")

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "TargetProfile":
        return cls(**_known_keys(cls, value, "profile"))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def readiness_issues(self, require_runtime: bool = False) -> list[Issue]:
        missing = []
        for key in ("product", "version", "output_device", "host_os", "coordinate_dpi", "base_xml"):
            if getattr(self, key) in (None, ""):
                missing.append(key)
        if self.base_xml and not Path(self.base_xml).expanduser().is_file():
            missing.append("base_xml(existing file)")
        if require_runtime and (self.runtime_kind != "command" or not self.runtime_command):
            missing.append("runtime_command")
        if missing:
            return [Issue("PROFILE_UNREADY", "未設定または不正: " + ", ".join(missing), stage="profile")]
        return []


@dataclass(slots=True)
class MappingField:
    source_key: str
    scope: Literal["header", "detail"]
    svf_field: str
    record_id: str | None = None
    value_type: Literal["string", "decimal", "date"] = "string"
    required: bool = False
    null_policy: Literal["empty", "null_token", "error"] = "empty"
    null_token: str | None = None
    max_chars: int | None = None
    overflow_policy: Literal["error"] = "error"

    def __post_init__(self) -> None:
        for key in ("source_key", "svf_field"):
            value = getattr(self, key)
            _check(isinstance(value, str) and value != "", f"{key}は非空の文字列が必要です")
        _check(self.record_id is None or isinstance(self.record_id, str), "record_idは文字列が必要です")
        _check(self.null_token is None or isinstance(self.null_token, str), "null_tokenは文字列が必要です")
        self.scope = _enum(self.scope, "scope", {"header", "detail"})
        self.value_type = _enum(self.value_type, "value_type", {"string", "decimal", "date"})
        self.null_policy = _enum(self.null_policy, "null_policy", {"empty", "null_token", "error"})
        self.overflow_policy = _enum(self.overflow_policy, "overflow_policy", {"error"})
        _check(isinstance(self.required, bool), "requiredは真偽値が必要です")
        if self.max_chars is not None:
            self.max_chars = parse_positive_int(self.max_chars, "max_chars")
        _check(self.null_policy != "null_token" or self.null_token is not None, "null_policy=null_tokenにはnull_tokenが必要です")

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "MappingField":
        return cls(**_known_keys(cls, value, "mapping.fields[]"))


@dataclass(slots=True)
class MappingConfig:
    mapping_id: str
    fields: list[MappingField]

    def __post_init__(self) -> None:
        _check(isinstance(self.mapping_id, str) and self.mapping_id != "", "mapping_idは非空の文字列が必要です")
        _check(isinstance(self.fields, list) and all(isinstance(x, MappingField) for x in self.fields), "fieldsはMappingFieldの配列が必要です")

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "MappingConfig":
        _check(isinstance(value, dict), "mappingはJSON objectが必要です")
        unknown = sorted(set(value) - {"mapping_id", "fields"})
        _check(not unknown, "mappingの未知キー: " + ", ".join(unknown))
        raw = value.get("fields", [])
        _check(isinstance(raw, list), "fieldsは配列が必要です")
        return cls(value["mapping_id"], [MappingField.from_dict(x) for x in raw])

    def to_dict(self) -> dict[str, Any]:
        return {"mapping_id": self.mapping_id, "fields": [asdict(x) for x in self.fields]}


@dataclass(slots=True)
class Job:
    job_id: str
    form_revision: str
    mapping_hash: str
    header: dict[str, str]
    data_file: str
    data_count: int
    output_profile: str
    empty_policy: Literal["skip", "emit_form"] = "skip"

    def __post_init__(self) -> None:
        for key in ("job_id", "form_revision", "mapping_hash", "data_file", "output_profile"):
            value = getattr(self, key)
            _check(isinstance(value, str) and value != "", f"{key}は非空の文字列が必要です")
        _check(isinstance(self.header, dict) and all(isinstance(k, str) for k in self.header), "headerは文字列キーのobjectが必要です")
        for key, value in self.header.items():
            _check(isinstance(value, (str, int, float)) and not isinstance(value, bool), f"header[{key}]は文字列または数値が必要です")
        _check(not isinstance(self.data_count, bool) and isinstance(self.data_count, int), "data_countは整数が必要です")
        _check(self.data_count >= 0, "data_countは0以上が必要です")
        self.empty_policy = _enum(self.empty_policy, "empty_policy", {"skip", "emit_form"})

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Job":
        return cls(**_known_keys(cls, value, "job"))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class RuntimeResult:
    status: Literal["SUCCEEDED", "FAILED", "UNKNOWN", "EMPTY"]
    runtime_job_id: str
    data_count: int
    artifact_paths: list[str] = field(default_factory=list)
    log_paths: list[str] = field(default_factory=list)
    error_code: str | None = None
    issues: list[Issue] = field(default_factory=list)
    input_manifest_hash: str | None = None
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    started_at: str | None = None
    finished_at: str | None = None
    host: str | None = None
    pid: int | None = None

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["issues"] = [x.to_dict() for x in self.issues]
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RuntimeResult":
        known = {x.name for x in fields(cls)} - {"issues"}
        body = {k: v for k, v in value.items() if k in known}
        return cls(**body, issues=[Issue(**x) for x in value.get("issues", [])])


@dataclass(slots=True)
class SourceObject:
    """原稿抽出の契約。bboxは幅と高さを持ち、推定値は理由を残す。"""

    id: str
    kind: Literal["text", "image", "page"]
    page: int
    unit: Literal["mm", "px"] = "mm"
    raw_text: str | None = None
    text: str | None = None
    ink_bbox: dict[str, float] | None = None
    layout_bbox: dict[str, float] | None = None
    bbox_source: Literal["measured", "estimated", "unresolved"] = "unresolved"
    baseline: float | None = None
    source_dpi: float | None = None
    unresolved_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ElementIR:
    id: str
    kind: str
    name: str | None
    parent_id: str | None
    page_id: str
    source_path: str
    attributes: dict[str, str]
    geometry_mm: dict[str, float]
    raw_geometry: dict[str, str]
    text: str | None = None
    display_line_count: int | None = None
    display_line_count_raw: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class DocumentIR:
    schema: str
    source_path: str
    source_hash: str
    revision: int
    coordinate_dpi: float | None
    page_width_mm: float | None
    page_height_mm: float | None
    elements: list[ElementIR]
    issues: list[Issue] = field(default_factory=list)
    encoding: str = "utf-8"
    declared_encoding: str | None = None

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["issues"] = [x.to_dict() for x in self.issues]
        return value
