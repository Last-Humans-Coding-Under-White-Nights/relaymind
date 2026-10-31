"""Native Windows, handle-relative, non-reparse source reads (RM34/AC25).

NtCreateFile opens exactly one component under a pinned parent handle. Every
accepted directory remains open without delete sharing; read handles remain
open without write/delete sharing through staging. No pathname stat fallback.
"""

import ctypes as C
import errno
import re
import struct
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path, PureWindowsPath
from typing import Any

from relaymind.assets.source_io import CaptureIoControl, NativeIoAborted, SourceInfo

_U32 = C.c_uint32
_U16 = C.c_uint16
_I64 = C.c_int64
_HANDLE = C.c_void_p
_FILE_COMPLETE_IF_OPLOCKED = 0x00000100
_STATUS_SUCCESS = 0
_STATUS_PENDING = 0x00000103
_STATUS_OPLOCK_BREAK_IN_PROGRESS = 0x00000108


class _UnicodeString(C.Structure):
    _fields_ = [("Length", _U16), ("MaximumLength", _U16), ("Buffer", C.c_void_p)]


class _ObjectAttributes(C.Structure):
    _fields_ = [
        ("Length", _U32),
        ("RootDirectory", _HANDLE),
        ("ObjectName", C.POINTER(_UnicodeString)),
        ("Attributes", _U32),
        ("SecurityDescriptor", C.c_void_p),
        ("SecurityQualityOfService", C.c_void_p),
    ]


class _Status(C.Union):
    _fields_ = [("Status", C.c_int32), ("Pointer", C.c_void_p)]


class _IoStatus(C.Structure):
    _fields_ = [("Result", _Status), ("Information", C.c_size_t)]


class _BasicInfo(C.Structure):
    _fields_ = [
        ("CreationTime", _I64),
        ("LastAccessTime", _I64),
        ("LastWriteTime", _I64),
        ("ChangeTime", _I64),
        ("FileAttributes", _U32),
    ]


class _StandardInfo(C.Structure):
    _fields_ = [
        ("AllocationSize", _I64),
        ("EndOfFile", _I64),
        ("NumberOfLinks", _U32),
        ("DeletePending", C.c_ubyte),
        ("Directory", C.c_ubyte),
    ]


class _IdInfo(C.Structure):
    _fields_ = [("VolumeSerialNumber", C.c_uint64), ("FileId", C.c_ubyte * 16)]


class _DirectoryEntry(C.Structure):
    _fields_ = [
        ("NextEntryOffset", _U32),
        ("FileIndex", _U32),
        ("CreationTime", _I64),
        ("LastAccessTime", _I64),
        ("LastWriteTime", _I64),
        ("ChangeTime", _I64),
        ("EndOfFile", _I64),
        ("AllocationSize", _I64),
        ("FileAttributes", _U32),
        ("FileNameLength", _U32),
        ("EaSize", _U32),
        ("FileName", _U16 * 1),
    ]


def _directory_names(raw: bytes) -> Iterator[str]:
    offset = 0
    header = _DirectoryEntry.FileName.offset
    while True:
        if offset + header > len(raw):
            raise OSError(errno.EIO, "invalid_directory_buffer")
        next_offset = struct.unpack_from("<I", raw, offset)[0]
        length = struct.unpack_from("<I", raw, offset + 60)[0]
        if (
            not length
            or length % 2
            or offset + header + length > len(raw)
            or (
                next_offset
                and (
                    next_offset % 8
                    or next_offset < header + length
                    or offset + next_offset >= len(raw)
                )
            )
        ):
            raise OSError(errno.EIO, "invalid_directory_buffer")
        try:
            name = raw[offset + header : offset + header + length].decode("utf-16-le")
        except UnicodeError:
            raise OSError(errno.EIO, "invalid_directory_encoding") from None
        if name not in (".", ".."):
            yield name
        if not next_offset:
            return
        offset += next_offset


def _component(value: str) -> None:
    if (
        not value
        or value in (".", "..")
        or any(c in value for c in "\\/:\x00")
        or value.endswith((" ", "."))
        or re.fullmatch(r"(?i:CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\..*)?", value)
    ):
        raise OSError(errno.EINVAL, "invalid_component")
    if len(value.encode("utf-16-le")) > 65532:
        raise OSError(errno.ENAMETOOLONG, "invalid_component")


def _root_parts(path: Path) -> tuple[str, tuple[str, ...]]:
    value = str(path.absolute())
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    if value.startswith("\\\\.\\"):
        raise OSError(errno.EINVAL, "invalid_namespace")
    parsed = PureWindowsPath(value)
    if not parsed.is_absolute():
        raise OSError(errno.EINVAL, "invalid_root")
    if parsed.drive.startswith("\\\\"):
        anchor = "\\??\\UNC\\" + parsed.drive[2:] + "\\"
        # The UNC anchor alone may contain these two separators; untrusted
        # descendants still pass only one validated component per native call.
        if any(part in ("", ".", "..") for part in parsed.drive[2:].split("\\")):
            raise OSError(errno.EINVAL, "invalid_root")
    elif re.fullmatch(r"[A-Za-z]:", parsed.drive):
        anchor = "\\??\\" + parsed.drive + "\\"
    else:
        raise OSError(errno.EINVAL, "invalid_namespace")
    parts = parsed.parts[1:]
    for part in parts:
        _component(part)
    return anchor, parts


class NativeIoControl:
    """Own cancellation until the actual capture-thread native call completes.

    A driver may refuse cancellation. In that case the operation and its buffers
    remain owned/fenced; no completed drain or replacement worker is invented.
    The non-daemon monitor is joined before this capture returns to a thread pool.
    """

    def __init__(self, api: "_Api", context: CaptureIoControl | None = None) -> None:
        self.api = api
        self._deadline = time.monotonic() + 60 if context is None else context.deadline
        self._clock = time.monotonic if context is None else context.monotonic
        self._cancelled = (lambda: False) if context is None else context.cancelled
        self._lock = threading.Lock()
        self._done = threading.Event()
        self._active: int | None = None
        self._generation = 0
        self._reason: str | None = None
        self._closed = False
        self._thread_handle = api.current_thread_handle()
        self._monitor = threading.Thread(target=self._watch, name="asset-native-cancel")
        try:
            self._monitor.start()
        except BaseException:
            api.close(self._thread_handle)
            raise

    def _check_fence(self) -> str | None:
        reason = (
            "cancelled"
            if self._cancelled()
            else ("deadline" if self._clock() >= self._deadline else None)
        )
        with self._lock:
            if self._reason is None and reason is not None:
                self._reason = reason
            return self._reason

    @property
    def reason(self) -> str | None:
        return self._check_fence()

    def _watch(self) -> None:
        while not self._done.wait(0.01):
            if self._check_fence() is None:
                continue
            with self._lock:
                active = self._active
            if active is not None:
                # ERROR_NOT_FOUND can precede native request registration. Keep
                # the fence and retry until this registered operation exits.
                self.api.cancel_synchronous(self._thread_handle)

    def invoke(
        self,
        function: Callable[..., Any],
        *args: object,
        discard: Callable[[Any], None] | None = None,
        cleanup: bool = False,
    ) -> Any:
        reason = self._check_fence()
        if reason is not None and not cleanup:
            raise NativeIoAborted(reason)
        with self._lock:
            if self._closed:
                raise OSError(errno.EIO, "native_control_closed")
            self._generation += 1
            generation = self._generation
            self._active = generation
        try:
            result = function(*args)
            reason = self._check_fence()
            if reason is not None and not cleanup:
                if discard is not None:
                    discard(result)
                raise NativeIoAborted(reason)
            return result
        except OSError:
            reason = self._check_fence()
            if reason is not None and not cleanup:
                raise NativeIoAborted(reason) from None
            raise
        finally:
            with self._lock:
                if self._active == generation:
                    self._active = None

    def close(self) -> None:
        if self._closed:
            return
        with self._lock:
            if self._active is not None:
                raise RuntimeError("native_operation_still_owned")
            self._closed = True
        self._done.set()
        self._monitor.join()
        self.api.close(self._thread_handle)


class _Api:
    def __init__(self) -> None:
        self.nt = C.WinDLL("ntdll", use_last_error=True)
        self.kernel = C.WinDLL("kernel32", use_last_error=True)
        self.open = self.nt.NtCreateFile
        self.open.argtypes = [
            C.POINTER(_HANDLE),
            _U32,
            C.POINTER(_ObjectAttributes),
            C.POINTER(_IoStatus),
            C.c_void_p,
            _U32,
            _U32,
            _U32,
            _U32,
            C.c_void_p,
            _U32,
        ]
        self.open.restype = C.c_int32
        self.error_code = self.nt.RtlNtStatusToDosError
        self.error_code.argtypes, self.error_code.restype = [C.c_int32], _U32
        self.query = self.kernel.GetFileInformationByHandleEx
        self.query.argtypes = [_HANDLE, C.c_int, C.c_void_p, _U32]
        self.query.restype = C.c_int
        self.file_type = self.kernel.GetFileType
        self.file_type.argtypes, self.file_type.restype = [_HANDLE], _U32
        self.read = self.kernel.ReadFile
        self.read.argtypes = [_HANDLE, C.c_void_p, _U32, C.POINTER(_U32), C.c_void_p]
        self.read.restype = C.c_int
        self.close = self.kernel.CloseHandle
        self.close.argtypes, self.close.restype = [_HANDLE], C.c_int
        self._open_thread = self.kernel.OpenThread
        self._open_thread.argtypes, self._open_thread.restype = [_U32, C.c_int, _U32], _HANDLE
        self._thread_id = self.kernel.GetCurrentThreadId
        self._thread_id.argtypes, self._thread_id.restype = [], _U32
        self._cancel_sync = self.kernel.CancelSynchronousIo
        self._cancel_sync.argtypes, self._cancel_sync.restype = [_HANDLE], C.c_int
        self._cancel_io = self.kernel.CancelIoEx
        self._cancel_io.argtypes, self._cancel_io.restype = [_HANDLE, C.c_void_p], C.c_int
        self._wait = self.kernel.WaitForSingleObject
        self._wait.argtypes, self._wait.restype = [_HANDLE, _U32], _U32
        self.control: NativeIoControl

    def current_thread_handle(self) -> int:
        # A durable non-inheritable handle, never the current-thread pseudo-handle.
        handle = self._open_thread(0x0001, False, self._thread_id())
        if not handle:
            self.failure(C.get_last_error())
        return handle

    def cancel_synchronous(self, handle: int) -> bool:
        return bool(self._cancel_sync(handle))

    @staticmethod
    def failure(code: int) -> None:
        if code in (2, 3):
            raise FileNotFoundError(errno.ENOENT, "source_path_absent")
        if code in (267,):
            raise NotADirectoryError(errno.ENOTDIR, "source_not_directory")
        raise OSError(errno.EACCES if code in (5, 32, 33) else errno.EIO, "source_io_failure")

    def create(
        self,
        name: str,
        parent: int | None,
        *,
        read: bool,
        directory: bool,
        access: int | None = None,
        disposition: int = 1,
        security_descriptor: int | None = None,
    ) -> int:
        encoded = name.encode("utf-16-le")
        if len(encoded) > 65532:
            raise OSError(errno.ENAMETOOLONG, "invalid_component")
        buffer = C.create_string_buffer(encoded + b"\x00\x00")
        string = _UnicodeString(len(encoded), len(encoded) + 2, C.cast(buffer, C.c_void_p))
        attributes = _ObjectAttributes(
            C.sizeof(_ObjectAttributes), parent, C.pointer(string), 0x40, security_descriptor, None
        )
        handle, status = _HANDLE(), _IoStatus()
        status.Result.Status = _STATUS_PENDING
        if access is None:
            access = 0x00100000 | 0x80 | (1 if read or directory else 0)
        share = 1 | (2 if directory or not read else 0)

        def opened() -> int:
            result = self.open(
                C.byref(handle),
                access,
                C.byref(attributes),
                C.byref(status),
                None,
                0,
                share,
                disposition,
                0x00200000 | 0x20 | _FILE_COMPLETE_IF_OPLOCKED,
                None,
                0,
            )
            if result == _STATUS_PENDING:
                # Synchronous NtCreateFile should not pend. If it does, retain
                # IOSB/name storage until actual completion; never free in-use
                # buffers or silently admit a pending/informational handle.
                while status.Result.Status == _STATUS_PENDING:
                    if self.control.reason is not None and handle.value:
                        self._cancel_io(handle.value, None)
                    if handle.value:
                        self._wait(handle.value, 10)
                    else:
                        time.sleep(0.01)
                if handle.value:
                    self.close(handle.value)
                    handle.value = None
                raise OSError(errno.EIO, "unexpected_pending_open")
            return result

        def discard(result: int) -> None:
            if result >= 0 and handle.value:
                self.close(handle.value)
                handle.value = None

        result = self.control.invoke(opened, discard=discard)
        if result < 0:
            code = self.error_code(result)
            if disposition == 2 and code in (80, 183):
                raise FileExistsError(errno.EEXIST, "private_file_exists")
            self.failure(code)
        if result == _STATUS_OPLOCK_BREAK_IN_PROGRESS:
            discard(result)
            raise OSError(errno.EIO, "source_oplock_pending")
        if result != _STATUS_SUCCESS or not handle.value:
            # FILE_COMPLETE_IF_OPLOCKED can return 0x108 with an owned handle.
            # An alternate success triggers a break but is never safe admission.
            discard(result)
            raise OSError(errno.EIO, "source_open_incomplete")
        return handle.value

    def information(self, handle: int) -> SourceInfo:
        basic, standard, identity = _BasicInfo(), _StandardInfo(), _IdInfo()
        for info_class, target in ((0, basic), (1, standard), (18, identity)):
            if not self.control.invoke(
                self.query, handle, info_class, C.byref(target), C.sizeof(target)
            ):
                self.failure(C.get_last_error())
        if standard.DeletePending:
            raise OSError(errno.EIO, "source_delete_pending")
        is_directory = bool(standard.Directory)
        return SourceInfo(
            (identity.VolumeSerialNumber, int.from_bytes(bytes(identity.FileId), "little")),
            standard.EndOfFile,
            basic.LastWriteTime,
            basic.ChangeTime,
            is_directory,
            not is_directory and self.control.invoke(self.file_type, handle) == 1,
            bool(basic.FileAttributes & 0x400),
        )


class _WindowsFile:
    def __init__(self, api: _Api, handle: int) -> None:
        self.api, self.handle = api, handle

    def read(self, count: int) -> bytes:
        if not 0 < count <= 1024 * 1024:
            raise ValueError("invalid_read_size")
        buffer, actual = C.create_string_buffer(count), _U32()
        if not self.api.control.invoke(
            self.api.read, self.handle, buffer, count, C.byref(actual), None
        ):
            self.api.failure(C.get_last_error())
        if actual.value > count:
            raise OSError(errno.EIO, "invalid_read_size")
        return buffer.raw[: actual.value]

    def info(self) -> SourceInfo:
        return self.api.information(self.handle)

    def close(self) -> None:
        # The source tree retains the read handle through final verification.
        pass


class WindowsSourceTree:
    def __init__(
        self,
        root: Path,
        *,
        control: CaptureIoControl | None = None,
        native: NativeIoControl | None = None,
    ) -> None:
        self.root = root
        self.api = _Api() if native is None else native.api
        self.control = NativeIoControl(self.api, control) if native is None else native
        self.api.control = self.control
        self._owns_control = native is None
        self._handles: list[int] = []
        self._directories: dict[tuple[str, ...], int] = {}
        self._closed = False
        try:
            anchor, parts = _root_parts(root)
            handle = self._directory_open(anchor, None)
            for part in parts:
                handle = self._directory_open(part, handle)
            self._directories[()] = handle
            self.identity = self.api.information(handle).identity
        except BaseException:
            self.close()
            raise

    def _directory_open(self, name: str, parent: int | None) -> int:
        handle = self.api.create(name, parent, read=False, directory=True)
        self._handles.append(handle)
        info = self.api.information(handle)
        if info.is_reparse:
            raise OSError(errno.ELOOP, "source_reparse_point")
        if not info.is_directory:
            raise NotADirectoryError(errno.ENOTDIR, "source_not_directory")
        return handle

    def _parent(self, relative: str) -> tuple[int, str]:
        self._check_open()
        parts = tuple(relative.split("/"))
        for part in parts:
            _component(part)
        for index in range(1, len(parts)):
            prefix = parts[:index]
            if prefix not in self._directories:
                self._directories[prefix] = self._directory_open(
                    parts[index - 1], self._directories[prefix[:-1]]
                )
        return self._directories[parts[:-1]], parts[-1]

    def _check_open(self) -> None:
        if self._closed:
            raise OSError(errno.EBADF, "source_tree_closed")

    def info(self, relative: str) -> SourceInfo:
        parent, name = self._parent(relative)
        handle = self.api.create(name, parent, read=False, directory=False)
        try:
            return self.api.information(handle)
        finally:
            self.control.invoke(self.api.close, handle, cleanup=True)

    def open_file(self, relative: str) -> _WindowsFile:
        parent, name = self._parent(relative)
        handle = self.api.create(name, parent, read=True, directory=False)
        try:
            info = self.api.information(handle)
            if info.is_reparse or not info.is_regular:
                raise OSError(errno.ELOOP, "source_not_regular")
            self._handles.append(handle)
            return _WindowsFile(self.api, handle)
        except BaseException:
            self.control.invoke(self.api.close, handle, cleanup=True)
            raise

    def names(self) -> Iterator[str]:
        self._check_open()
        buffer = C.create_string_buffer(65536)
        info_class = 15  # FileFullDirectoryRestartInfo, then FileFullDirectoryInfo.
        while True:
            if not self.control.invoke(
                self.api.query, self._directories[()], info_class, buffer, len(buffer)
            ):
                code = C.get_last_error()
                if code == 18:
                    return
                self.api.failure(code)
            yield from _directory_names(buffer.raw)
            info_class = 14

    def verify_root(self) -> None:
        self._check_open()
        for handle in self._directories.values():
            info = self.api.information(handle)
            if info.is_reparse or not info.is_directory:
                raise OSError(errno.ELOOP, "source_reparse_point")
        checked = WindowsSourceTree(self.root, native=self.control)
        try:
            if checked.identity != self.identity:
                raise OSError(errno.EIO, "source_root_changed")
        finally:
            checked.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for handle in reversed(self._handles):
            self.control.invoke(self.api.close, handle, cleanup=True)
        self._handles.clear()
        self._directories.clear()
        if self._owns_control:
            self.control.close()
