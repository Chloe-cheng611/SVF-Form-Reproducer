from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .locking import fsync_directory, write_bytes_durable
from .models import MappingConfig, TargetProfile


def read_json(path: str | Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError(f"JSON object expected: {path}")
    return value


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def write_json(path: str | Path, value: Any) -> None:
    """内容をfsyncしてから置換し、親ディレクトリを確定する。"""
    target = Path(path)
    temporary = target.with_suffix(target.suffix + ".tmp")
    write_bytes_durable(temporary, json_bytes(value))
    temporary.replace(target)
    fsync_directory(target.parent)


def load_profile(path: str | Path) -> TargetProfile:
    source = Path(path).resolve()
    profile = TargetProfile.from_dict(read_json(source))
    if profile.base_xml and not Path(profile.base_xml).expanduser().is_absolute():
        profile.base_xml = str((source.parent / profile.base_xml).resolve())
    profile.assets = {
        name: str((source.parent / value).resolve()) if not Path(value).expanduser().is_absolute() else str(Path(value).expanduser())
        for name, value in profile.assets.items()
    }
    return profile


def load_mapping(path: str | Path) -> MappingConfig:
    return MappingConfig.from_dict(read_json(path))


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def stable_json_hash(value: Any) -> str:
    body = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256_bytes(body)


def same_file(first: str | Path, second: str | Path) -> bool:
    """同一・シンボリックリンク・ハードリンクのいずれかで同じ実体かを判定する。"""
    left, right = Path(first), Path(second)
    try:
        if left.resolve() == right.resolve():
            return True
    except OSError:
        pass
    try:
        return left.exists() and right.exists() and left.samefile(right)
    except OSError:
        return False
