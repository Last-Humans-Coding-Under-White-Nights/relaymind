"""Native Windows owner-only private leases and exclusive creation (RM37/AC28).

Ancestors are pinned ordinary directories; privacy is checked on the exact
final read handle. Only ordinary user allow ACEs and validated deny ACEs are
accepted. Privileged administrators and code running as the user are outside
this owner-only file boundary.
"""

import ctypes as C
import struct
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from relaymind.assets._windows_source_io import (
    NativeIoControl,
    WindowsSourceTree,
    _Api,
    _component,
    _WindowsFile,
)
from relaymind.recommendations.limits import MAX_PRIVATE_FILE_BYTES
from relaymind.recommendations.private_files import PrivateFileError, PrivateFileLease

_PTR = C.c_void_p
_U32 = C.c_uint32
_BOOL = C.c_int32
_MAX_SECURITY = 65536
_READ_PRIVATE = 0x00120081


class _SidAndAttributes(C.Structure):
    _fields_ = [("Sid", _PTR), ("Attributes", _U32)]


class _TokenUser(C.Structure):
    _fields_ = [("User", _SidAndAttributes)]


class _AclSize(C.Structure):
    _fields_ = [("AceCount", _U32), ("AclBytesInUse", _U32), ("AclBytesFree", _U32)]


def _sid_size(raw: bytes) -> int:
    if len(raw) < 8 or raw[0] != 1 or raw[1] > 15:
        raise PrivateFileError("invalid private file SID")
    size = 8 + 4 * raw[1]
    if size > len(raw):
        raise PrivateFileError("invalid private file SID")
    return size


def _validate_ace(raw: bytes, user: bytes, equal: Callable[[bytes, bytes], bool]) -> bool:
    """Validate bounded ordinary ACE bytes; return whether it grants the user."""
    if len(raw) < 16:
        raise PrivateFileError("invalid private file ACE")
    kind, flags, size, mask = struct.unpack_from("<BBHI", raw)
    if size != len(raw) or kind not in (0, 1) or flags & ~0x17:
        raise PrivateFileError("unsupported private file ACE")
    sid = raw[8:]
    if _sid_size(sid) != len(sid):
        raise PrivateFileError("invalid private file ACE")
    if kind == 1:
        return False
    if not equal(sid, user):
        raise PrivateFileError("private file grants another identity")
    return bool(mask)


class _PrivateApi(_Api):
    def __init__(self) -> None:
        super().__init__()
        self.security = C.WinDLL("advapi32", use_last_error=True)

        def bind(library, name, args, result):
            function = getattr(library, name)
            function.argtypes, function.restype = args, result
            return function

        self.process = bind(self.kernel, "GetCurrentProcess", [], _PTR)
        self.open_token = bind(
            self.security, "OpenProcessToken", [_PTR, _U32, C.POINTER(_PTR)], _BOOL
        )
        self.token_info = bind(
            self.security, "GetTokenInformation", [_PTR, _U32, _PTR, _U32, C.POINTER(_U32)], _BOOL
        )
        self.valid_sid = bind(self.security, "IsValidSid", [_PTR], _BOOL)
        self.sid_length = bind(self.security, "GetLengthSid", [_PTR], _U32)
        self.equal_sid = bind(self.security, "EqualSid", [_PTR, _PTR], _BOOL)
        self.get_security = bind(
            self.security,
            "GetSecurityInfo",
            [
                _PTR,
                _U32,
                _U32,
                C.POINTER(_PTR),
                C.POINTER(_PTR),
                C.POINTER(_PTR),
                C.POINTER(_PTR),
                C.POINTER(_PTR),
            ],
            _U32,
        )
        self.valid_descriptor = bind(self.security, "IsValidSecurityDescriptor", [_PTR], _BOOL)
        self.descriptor_length = bind(self.security, "GetSecurityDescriptorLength", [_PTR], _U32)
        self.descriptor_dacl = bind(
            self.security,
            "GetSecurityDescriptorDacl",
            [_PTR, C.POINTER(_BOOL), C.POINTER(_PTR), C.POINTER(_BOOL)],
            _BOOL,
        )
        self.descriptor_control = bind(
            self.security,
            "GetSecurityDescriptorControl",
            [_PTR, C.POINTER(C.c_uint16), C.POINTER(_U32)],
            _BOOL,
        )
        self.valid_acl = bind(self.security, "IsValidAcl", [_PTR], _BOOL)
        self.acl_info = bind(self.security, "GetAclInformation", [_PTR, _PTR, _U32, _U32], _BOOL)
        self.get_ace = bind(self.security, "GetAce", [_PTR, _U32, C.POINTER(_PTR)], _BOOL)
        self.sid_string = bind(
            self.security, "ConvertSidToStringSidW", [_PTR, C.POINTER(_PTR)], _BOOL
        )
        self.convert_descriptor = bind(
            self.security,
            "ConvertStringSecurityDescriptorToSecurityDescriptorW",
            [C.c_wchar_p, _U32, C.POINTER(_PTR), C.POINTER(_U32)],
            _BOOL,
        )
        self.free = bind(self.kernel, "LocalFree", [_PTR], _PTR)
        self.write = bind(
            self.kernel, "WriteFile", [_PTR, _PTR, _U32, C.POINTER(_U32), _PTR], _BOOL
        )
        self.flush = bind(self.kernel, "FlushFileBuffers", [_PTR], _BOOL)

    def _sid(self, address: int, start: int, length: int) -> bytes:
        if not address or address < start or address + 8 > start + length:
            raise PrivateFileError("invalid private file SID")
        size = _sid_size(C.string_at(address, min(68, start + length - address)))
        if address + size > start + length or not self.valid_sid(address):
            raise PrivateFileError("invalid private file SID")
        if self.sid_length(address) != size:
            raise PrivateFileError("invalid private file SID")
        return C.string_at(address, size)

    def user_sid(self) -> bytes:
        def query() -> bytes:
            token = _PTR()
            if not self.open_token(self.process(), 8, C.byref(token)):
                raise PrivateFileError("cannot query private file identity")
            try:
                needed = _U32()
                if self.token_info(token, 1, None, 0, C.byref(needed)) or C.get_last_error() != 122:
                    raise PrivateFileError("cannot query private file identity")
                if not C.sizeof(_TokenUser) <= needed.value <= _MAX_SECURITY:
                    raise PrivateFileError("invalid private file identity")
                buffer = C.create_string_buffer(needed.value)
                actual = _U32()
                if not self.token_info(token, 1, buffer, len(buffer), C.byref(actual)):
                    raise PrivateFileError("cannot query private file identity")
                if not C.sizeof(_TokenUser) <= actual.value <= len(buffer):
                    raise PrivateFileError("invalid private file identity")
                user = C.cast(buffer, C.POINTER(_TokenUser)).contents
                return self._sid(user.User.Sid, C.addressof(buffer), actual.value)
            finally:
                self.close(token)

        return self.control.invoke(query)

    def same_sid(self, left: bytes, right: bytes) -> bool:
        return bool(self.equal_sid(C.create_string_buffer(left), C.create_string_buffer(right)))

    def private_security(self, handle: int, user: bytes) -> tuple:
        def query() -> tuple:
            owner, dacl, descriptor = _PTR(), _PTR(), _PTR()
            try:
                if (
                    self.get_security(
                        handle, 1, 5, C.byref(owner), None, C.byref(dacl), None, C.byref(descriptor)
                    )
                    != 0
                ):
                    raise PrivateFileError("cannot query private file security")
                if not descriptor.value or not self.valid_descriptor(descriptor):
                    raise PrivateFileError("invalid private file security")
                length = self.descriptor_length(descriptor)
                if not 20 <= length <= _MAX_SECURITY:
                    raise PrivateFileError("invalid private file security")
                owner_sid = self._sid(owner.value, descriptor.value, length)
                if not self.same_sid(owner_sid, user):
                    raise PrivateFileError("private file has another owner")
                present, defaulted, checked = _BOOL(), _BOOL(), _PTR()
                if not self.descriptor_dacl(
                    descriptor, C.byref(present), C.byref(checked), C.byref(defaulted)
                ):
                    raise PrivateFileError("cannot query private file DACL")
                if not present.value or not checked.value or checked.value != dacl.value:
                    raise PrivateFileError("private file has no restrictive DACL")
                start = dacl.value
                if start < descriptor.value or start + 8 > descriptor.value + length:
                    raise PrivateFileError("invalid private file ACL")
                acl_length = struct.unpack_from("<H", C.string_at(start, 8), 2)[0]
                if (
                    acl_length < 8
                    or start + acl_length > descriptor.value + length
                    or not self.valid_acl(dacl)
                ):
                    raise PrivateFileError("invalid private file ACL")
                sizes = _AclSize()
                if not self.acl_info(dacl, C.byref(sizes), C.sizeof(sizes), 2):
                    raise PrivateFileError("cannot query private file ACL")
                if (
                    not 8 <= sizes.AclBytesInUse <= acl_length
                    or sizes.AceCount > (sizes.AclBytesInUse - 8) // 16
                ):
                    raise PrivateFileError("invalid private file ACL")
                granted = False
                end = start + sizes.AclBytesInUse
                previous = start + 8
                for index in range(sizes.AceCount):
                    ace = _PTR()
                    if not self.get_ace(dacl, index, C.byref(ace)) or not ace.value:
                        raise PrivateFileError("cannot query private file ACE")
                    if ace.value != previous or ace.value + 4 > end:
                        raise PrivateFileError("invalid private file ACE")
                    ace_size = struct.unpack_from("<H", C.string_at(ace.value, 4), 2)[0]
                    if ace_size < 16 or ace.value + ace_size > end:
                        raise PrivateFileError("invalid private file ACE")
                    self._sid(ace.value + 8, ace.value, ace_size)
                    granted = (
                        _validate_ace(C.string_at(ace.value, ace_size), user, self.same_sid)
                        or granted
                    )
                    previous += ace_size
                if not granted or previous != end:
                    raise PrivateFileError("private file has no user grant")
                control, revision = C.c_uint16(), _U32()
                if not self.descriptor_control(descriptor, C.byref(control), C.byref(revision)):
                    raise PrivateFileError("cannot query private file security")
                return owner_sid, control.value, C.string_at(start, acl_length)
            finally:
                if descriptor.value:
                    self.free(descriptor)

        return self.control.invoke(query)

    @contextmanager
    def protected_descriptor(self, user: bytes) -> Iterator[int]:
        def allocate() -> int:
            spelling, descriptor = _PTR(), _PTR()
            try:
                if not self.sid_string(C.create_string_buffer(user), C.byref(spelling)):
                    raise PrivateFileError("cannot create private file security")
                sid = C.wstring_at(spelling)
                if not self.convert_descriptor(
                    f"O:{sid}D:P(A;;FA;;;{sid})", 1, C.byref(descriptor), None
                ):
                    raise PrivateFileError("cannot create private file security")
                if not descriptor.value:
                    raise PrivateFileError("cannot create private file security")
                result, descriptor.value = descriptor.value, None
                return result
            finally:
                if spelling.value:
                    self.free(spelling)
                if descriptor.value:
                    self.free(descriptor)

        descriptor = self.control.invoke(allocate, discard=self.free)
        try:
            yield descriptor
        finally:
            self.control.invoke(self.free, descriptor, cleanup=True)


def _snapshot(api: _PrivateApi, handle: int, user: bytes, maximum: int) -> tuple:
    info = api.information(handle)
    if info.is_reparse or not info.is_regular or info.is_directory:
        raise PrivateFileError("private file is not ordinary")
    if not 0 <= info.size <= maximum:
        raise PrivateFileError("private file too large")
    security = api.private_security(handle, user)
    if api.information(handle) != info:
        raise PrivateFileError("private file changed")
    return (*info.fingerprint, security)


@contextmanager
def _parent(path: Path) -> Iterator[tuple[WindowsSourceTree, str, bytes]]:
    if any(part == ".." for part in path.parts):
        raise PrivateFileError("unsafe private path")
    _component(path.name)
    api = _PrivateApi()
    control = NativeIoControl(api)
    api.control = control
    try:
        user = api.user_sid()
        tree = WindowsSourceTree(path.absolute().parent, native=control)
        try:
            yield tree, path.name, user
        finally:
            tree.close()
    finally:
        control.close()


@contextmanager
def open_windows_private_file(
    path: Path, *, maximum: int = MAX_PRIVATE_FILE_BYTES
) -> Iterator[PrivateFileLease]:
    """Own read/parent handles until exit; every verification checks current path."""
    if not 0 <= maximum <= MAX_PRIVATE_FILE_BYTES:
        raise PrivateFileError("invalid private file size limit")
    try:
        with _parent(path) as (tree, name, user):
            api = tree.api
            parent = tree._directories[()]
            handle = api.create(name, parent, read=True, directory=False, access=_READ_PRIVATE)
            try:
                fingerprint = _snapshot(api, handle, user, maximum)
                source = _WindowsFile(api, handle)
                data = bytearray()
                while chunk := source.read(min(65536, maximum + 1 - len(data))):
                    data.extend(chunk)
                    if len(data) > maximum:
                        raise PrivateFileError("private file too large")
                if len(data) != fingerprint[2]:
                    raise PrivateFileError("private file changed")

                def verify() -> None:
                    try:
                        if _snapshot(api, handle, user, maximum) != fingerprint:
                            raise PrivateFileError("private file changed")
                        tree.verify_root()
                        current = api.create(
                            name, parent, read=True, directory=False, access=_READ_PRIVATE
                        )
                        try:
                            if _snapshot(api, current, user, maximum) != fingerprint:
                                raise PrivateFileError("private path changed")
                        finally:
                            api.control.invoke(api.close, current, cleanup=True)
                    except OSError:
                        raise PrivateFileError(f"private file changed: {path}") from None

                lease = PrivateFileLease(bytes(data), fingerprint, verify)
                lease.verify()
                yield lease
            finally:
                api.control.invoke(api.close, handle, cleanup=True)
    except FileNotFoundError:
        raise
    except OSError:
        raise PrivateFileError(f"cannot open private file: {path}") from None


def create_windows_private_file(path: Path, data: bytes) -> bool:
    """Supply owner/protected DACL at FILE_CREATE, before any private bytes exist."""
    if len(data) > MAX_PRIVATE_FILE_BYTES:
        raise PrivateFileError("private file too large")
    try:
        with _parent(path) as (tree, name, user):
            api = tree.api
            with api.protected_descriptor(user) as descriptor:
                try:
                    handle = api.create(
                        name,
                        tree._directories[()],
                        read=True,
                        directory=False,
                        access=0xC0000000,
                        disposition=2,
                        security_descriptor=descriptor,
                    )
                except FileExistsError:
                    return False
            try:
                initial = _snapshot(api, handle, user, len(data))
                if initial[2] != 0:
                    raise PrivateFileError("private file was not empty")
                offset = 0
                while offset < len(data):
                    buffer, actual = C.create_string_buffer(data[offset:]), _U32()
                    if not api.control.invoke(
                        api.write, handle, buffer, len(data) - offset, C.byref(actual), None
                    ):
                        raise PrivateFileError("cannot write private file")
                    if not 0 < actual.value <= len(data) - offset:
                        raise PrivateFileError("cannot write private file")
                    offset += actual.value
                if not api.control.invoke(api.flush, handle):
                    raise PrivateFileError("cannot flush private file")
                final = _snapshot(api, handle, user, len(data))
                if final[:2] != initial[:2] or final[2] != len(data) or final[-1] != initial[-1]:
                    raise PrivateFileError("private file changed")
                tree.verify_root()
            finally:
                api.control.invoke(api.close, handle, cleanup=True)
        return True
    except OSError:
        raise PrivateFileError(f"cannot create private file: {path}") from None
