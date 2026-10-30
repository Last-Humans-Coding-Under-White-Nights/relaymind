"""Same-file ownership checks and bounded private leases (RM37/AC28)."""

import os
import secrets
import stat
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from relaymind.recommendations.limits import MAX_PRIVATE_FILE_BYTES


class PrivateFileError(ValueError):
    """Safe local reason/path only; no bytes from a private file."""


@dataclass(repr=False)
class PrivateFileLease:
    data: bytes = field(repr=False)
    fingerprint: tuple
    _verify: Callable[[], None] = field(repr=False)

    @property
    def identity(self) -> tuple:
        """The file's device and inode (or volume and file ID on Windows)."""
        return self.fingerprint[:2]

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


def resolve_private_path(path: Path) -> Path:
    """Resolve links in a configured path's ancestors, never in the file itself.

    The private-file checks open every component without following links, so
    an operator path through an ancestor link (such as macOS /tmp) is resolved
    once, where it is configured. A link that appears later is still refused.
    """
    absolute = path.absolute()
    return Path(os.path.realpath(absolute.parent)) / absolute.name


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
def open_private_file(
    path: Path, *, maximum: int = MAX_PRIVATE_FILE_BYTES
) -> Iterator[PrivateFileLease]:
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

                lease = PrivateFileLease(b"".join(chunks), fingerprint, verify)
                lease.verify()
                yield lease
            finally:
                os.close(descriptor)
    except FileNotFoundError:
        raise
    except OSError:
        raise PrivateFileError(f"cannot open private file: {path}") from None


def read_private_file(path: Path, *, maximum: int = MAX_PRIVATE_FILE_BYTES) -> bytes:
    try:
        with open_private_file(path, maximum=maximum) as lease:
            return lease.data
    except FileNotFoundError:
        raise PrivateFileError(f"cannot open private file: {path}") from None


def _write_all(descriptor: int, data: bytes) -> None:
    pending = memoryview(data)
    while pending:
        written = os.write(descriptor, pending)
        if written <= 0:
            raise PrivateFileError("private file write failed")
        pending = pending[written:]


def create_private_file_exclusive(path: Path, data: bytes) -> bool:
    """Create a complete private file, or report that one already exists.

    The bytes are written and synced under a temporary name in the same
    directory, then hard-linked to the final name, which never replaces an
    existing entry. A crash or a failed write therefore never leaves a
    truncated file under the final name; leftover temporary files from an
    earlier crash are removed here and are never read.
    """
    if os.name == "nt":
        from relaymind.recommendations._windows_private_files import create_windows_private_file

        return create_windows_private_file(path, data)
    try:
        with _parent(path) as (_, parent, name, _):
            prefix = f".{name}.partial-"
            for entry in os.listdir(parent):
                if entry.startswith(prefix):
                    os.unlink(entry, dir_fd=parent)
            temporary = prefix + secrets.token_hex(8)
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent,
            )
            try:
                try:
                    _private(os.fstat(descriptor))
                    _write_all(descriptor, data)
                    os.fsync(descriptor)
                    _private(os.fstat(descriptor))
                finally:
                    os.close(descriptor)
                try:
                    os.link(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
                    created = True
                except FileExistsError:
                    created = False
            finally:
                os.unlink(temporary, dir_fd=parent)
            os.fsync(parent)
            return created
    except OSError:
        raise PrivateFileError(f"cannot create private file: {path}") from None
