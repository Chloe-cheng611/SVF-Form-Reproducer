"""OS別のファイルロックと永続化順序。

fcntlはUnix向けであり、Windowsには存在しない。実装の選択と読込は最初の利用時に
行い、モジュールの読込自体がOS固有モジュールに依存しないようにする。WindowsとPOSIX
の薄い実装を同じ排他契約で提供し、POSIXのディレクトリ同期手順をWindowsへ移さない。
"""
from __future__ import annotations

import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

LOCK_TIMEOUT_SECONDS = 60.0
LOCK_RETRY_SECONDS = 0.05

IS_WINDOWS = sys.platform.startswith("win")

_backend: dict[str, Callable[..., Any]] | None = None


class LockUnavailable(TimeoutError):
    """既定時間内に排他を取得できなかった。"""


def _windows_backend() -> dict[str, Callable[..., Any]]:
    import msvcrt

    def try_acquire(stream) -> bool:
        try:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def release(stream) -> None:
        try:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass

    def sync_directory(_path: str | Path) -> None:
        return None

    return {"try_acquire": try_acquire, "release": release, "sync_directory": sync_directory}


def _posix_backend() -> dict[str, Callable[..., Any]]:
    import fcntl

    def try_acquire(stream) -> bool:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def release(stream) -> None:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass

    def sync_directory(path: str | Path) -> None:
        descriptor = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(descriptor)
        except OSError:
            # 一部のファイルシステムはディレクトリ同期を実装しない。
            pass
        finally:
            os.close(descriptor)

    return {"try_acquire": try_acquire, "release": release, "sync_directory": sync_directory}


def backend() -> dict[str, Callable[..., Any]]:
    global _backend
    if _backend is None:
        _backend = _windows_backend() if IS_WINDOWS else _posix_backend()
    return _backend


def reset_backend() -> None:
    """試験でOS実装を切り替えるために使う。"""
    global _backend
    _backend = None


def fsync_directory(path: str | Path) -> None:
    backend()["sync_directory"](path)


@contextmanager
def file_lock(path: str | Path, timeout: float = LOCK_TIMEOUT_SECONDS) -> Iterator[None]:
    implementation = backend()
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    with target.open("a+b") as stream:
        while not implementation["try_acquire"](stream):
            if time.monotonic() >= deadline:
                raise LockUnavailable(f"ロックを取得できません: {target}")
            time.sleep(LOCK_RETRY_SECONDS)
        try:
            yield
        finally:
            implementation["release"](stream)


def fsync_path(path: str | Path) -> None:
    with Path(path).open("rb+") as stream:
        stream.flush()
        os.fsync(stream.fileno())


def write_bytes_durable(path: str | Path, data: bytes) -> None:
    """内容を書き、fsyncしてから返す。親ディレクトリの確定は呼出側が行う。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def replace_durable(source: str | Path, destination: str | Path) -> None:
    """同一ファイルシステム上での置換とディレクトリ確定。"""
    os.replace(str(source), str(destination))
    fsync_directory(Path(destination).parent)
