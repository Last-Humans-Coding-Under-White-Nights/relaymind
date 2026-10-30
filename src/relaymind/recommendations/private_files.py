"""Same-file ownership checks and bounded private leases (RM37/AC28)."""

import os
import stat
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path


class PrivateFileError(ValueError):
    """Safe local reason/path only; no bytes from a private file."""


@dataclass(repr=False)
class PrivateFileLease:
    data: bytes = field(repr=False)
    identity: tuple[int, int]
    fingerprint: tuple
    _verify: Callable[[], None] = field(repr=False)

    def verify(self) -> None:
        self._verify()


def _private(info: os.stat_result) -> tuple:
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise PrivateFileError("unsafe private file")
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_mode,
        info.st_uid,
    )


@contextmanager
def _parent(path: Path):
    absolute = path.absolute()
    if any(part in (".", "..") or "\x00" in part for part in absolute.parts[1:]):
        raise PrivateFileError("unsafe private path")
    with ExitStack() as stack:
        parent = os.open(absolute.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        stack.callback(os.close, parent)
        pinned = [(parent, os.fstat(parent))]
        for component in absolute.parts[1:-1]:
            parent = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            stack.callback(os.close, parent)
            pinned.append((parent, os.fstat(parent)))
        yield absolute, parent, absolute.name, pinned


@contextmanager
def open_private_file(path: Path, *, maximum: int = 1024 * 1024) -> Iterator[PrivateFileLease]:
    """Hold checked final/parent descriptors through caller work and verification."""
    if os.name == "nt":
        from relaymind.recommendations._windows_private_files import open_windows_private_file

        with open_windows_private_file(path, maximum=maximum) as lease:
            yield lease
        return
    try:
        with _parent(path) as (absolute, parent, name, pinned):
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            try:
                fingerprint = _private(os.fstat(descriptor))
                if fingerprint[2] > maximum:
                    raise PrivateFileError("private file too large")
                chunks, size = [], 0
                while chunk := os.read(descriptor, min(65536, maximum + 1 - size)):
                    size += len(chunk)
                    if size > maximum:
                        raise PrivateFileError("private file too large")
                    chunks.append(chunk)

                def verify():
                    try:
                        if _private(os.fstat(descriptor)) != fingerprint:
                            raise PrivateFileError("private file changed")
                        with _parent(absolute) as (_, current_parent, current_name, current_pinned):
                            if [(s.st_dev, s.st_ino) for _, s in pinned] != [
                                (s.st_dev, s.st_ino) for _, s in current_pinned
                            ]:
                                raise PrivateFileError("private path changed")
                            current = os.open(
                                current_name,
                                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                dir_fd=current_parent,
                            )
                            try:
                                if _private(os.fstat(current)) != fingerprint:
                                    raise PrivateFileError("private path changed")
                            finally:
                                os.close(current)
                    except OSError:
                        raise PrivateFileError(f"private file changed: {path}") from None

                lease = PrivateFileLease(b"".join(chunks), fingerprint[:2], fingerprint, verify)
                lease.verify()
                yield lease
            finally:
                os.close(descriptor)
    except FileNotFoundError:
        raise
    except OSError:
        raise PrivateFileError(f"cannot open private file: {path}") from None


def read_private_snapshot(path: Path, *, maximum: int = 1024 * 1024) -> PrivateFileLease:
    with open_private_file(path, maximum=maximum) as lease:
        lease.verify()
        return lease


def read_private_file(path: Path, *, maximum: int = 1024 * 1024) -> bytes:
    try:
        return read_private_snapshot(path, maximum=maximum).data
    except FileNotFoundError:
        raise PrivateFileError(f"cannot open private file: {path}") from None


def create_private_file_exclusive(path: Path, data: bytes) -> bool:
    """Private before writing; collision never overwrites an existing entry."""
    if os.name == "nt":
        from relaymind.recommendations._windows_private_files import create_windows_private_file

        return create_windows_private_file(path, data)
    try:
        with _parent(path) as (_, parent, name, _):
            try:
                descriptor = os.open(
                    name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent
                )
            except FileExistsError:
                return False
            try:
                _private(os.fstat(descriptor))
                pending = memoryview(data)
                while pending:
                    written = os.write(descriptor, pending)
                    if written <= 0:
                        raise PrivateFileError("private file write failed")
                    pending = pending[written:]
                os.fsync(descriptor)
                _private(os.fstat(descriptor))
            finally:
                os.close(descriptor)
        return True
    except OSError:
        raise PrivateFileError(f"cannot create private file: {path}") from None
