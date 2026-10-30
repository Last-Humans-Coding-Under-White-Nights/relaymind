"""Private no-follow source I/O primitives (RM34/AC25).

Platform code owns only opened objects. Staging, parsers and publication live in
sources.py. Windows system libraries are loaded only by its native factory.
"""

import os
import stat
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class CaptureIoControl(Protocol):
    deadline: float
    monotonic: Callable[[], float]
    cancelled: Callable[[], bool]


class NativeIoAborted(OSError):
    """A safe native cancellation/deadline fence, translated by the reader."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class SourceInfo:
    identity: tuple[int, int]
    size: int
    modified: int
    changed: int
    is_directory: bool
    is_regular: bool
    is_reparse: bool

    @property
    def fingerprint(self) -> tuple[int, int, int, int, int]:
        return (*self.identity, self.size, self.modified, self.changed)


class SourceFile(Protocol):
    def read(self, count: int) -> bytes: ...
    def info(self) -> SourceInfo: ...
    def close(self) -> None: ...


class SourceTree(Protocol):
    def info(self, relative: str) -> SourceInfo: ...
    def open_file(self, relative: str) -> SourceFile: ...
    def names(self) -> Iterator[str]: ...
    def verify_root(self) -> None: ...
    def close(self) -> None: ...


def _info(value: os.stat_result) -> SourceInfo:
    return SourceInfo(
        (value.st_dev, value.st_ino),
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
        stat.S_ISDIR(value.st_mode),
        stat.S_ISREG(value.st_mode),
        stat.S_ISLNK(value.st_mode),
    )


def _component(part: str) -> str:
    if part in ("", ".", "..") or "\x00" in part:
        raise OSError("invalid_component")
    return part


def _walk(descriptor: int, parts: list[str]) -> int:
    """Open each directory part below descriptor without following links.

    Owns descriptor: on success the returned one replaces it, on failure it is closed.
    """
    try:
        for part in parts:
            child = os.open(
                _component(part), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
            )
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_directory(path: Path) -> int:
    # Spec 14.5: only the configured root itself must not be a symlink. Its
    # ancestors (such as /tmp on macOS) are operator setup, so they are resolved;
    # the root and everything below it are opened without following links.
    absolute = path.absolute()
    parent = Path(os.path.realpath(absolute.parent))
    parts = [*parent.parts[1:], _component(absolute.name)]
    return _walk(os.open(parent.anchor, os.O_RDONLY | os.O_DIRECTORY), parts)


def _parent(root_fd: int, relative: str) -> tuple[int, str]:
    parts = [_component(part) for part in relative.split("/")]
    return _walk(os.dup(root_fd), parts[:-1]), parts[-1]


class _PosixFile:
    def __init__(self, fd: int) -> None:
        self.fd = fd

    def read(self, count: int) -> bytes:
        return os.read(self.fd, count)

    def info(self) -> SourceInfo:
        return _info(os.fstat(self.fd))

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1


class _PosixTree:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.fd = _open_directory(root)
        self.identity = _info(os.fstat(self.fd)).identity

    def info(self, relative: str) -> SourceInfo:
        descriptor, name = _parent(self.fd, relative)
        try:
            return _info(os.stat(name, dir_fd=descriptor, follow_symlinks=False))
        finally:
            os.close(descriptor)

    def open_file(self, relative: str) -> SourceFile:
        descriptor, name = _parent(self.fd, relative)
        try:
            return _PosixFile(
                os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
            )
        finally:
            os.close(descriptor)

    def names(self) -> Iterator[str]:
        with os.scandir(self.fd) as entries:
            for entry in entries:
                yield entry.name

    def verify_root(self) -> None:
        current_fd = _open_directory(self.root)
        try:
            if _info(os.fstat(current_fd)).identity != self.identity:
                raise OSError("source_root_changed")
        finally:
            os.close(current_fd)

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1


def open_source_tree(root: Path, *, control: CaptureIoControl | None = None) -> SourceTree:
    if os.name == "nt":
        from relaymind.assets._windows_source_io import WindowsSourceTree

        return WindowsSourceTree(root, control=control)
    return _PosixTree(root)
