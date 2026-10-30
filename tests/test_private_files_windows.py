"""RM37/AC28 private-file boundary: portable wrappers and native Windows ACLs."""

import ctypes as C
import importlib
import os
from pathlib import Path

import pytest

from relaymind.assets._windows_source_io import NativeIoControl, _Api, _ObjectAttributes
from relaymind.recommendations.private_files import PrivateFileError


def test_private_native_create_applies_security_at_exclusive_open():
    """RM37/AC28: bytes cannot precede the protected descriptor at FILE_CREATE."""
    api = object.__new__(_Api)
    api.close = lambda handle: None
    api.current_thread_handle = lambda: 777
    api.cancel_synchronous = lambda handle: False

    def native_open(out, access, attributes, status, allocation, flags, share, disposition, *rest):
        attrs = C.cast(attributes, C.POINTER(_ObjectAttributes)).contents
        assert attrs.SecurityDescriptor == 12345
        assert access == 0xC0000000
        assert share == 1
        assert disposition == 2
        C.cast(out, C.POINTER(C.c_void_p))[0] = 999
        return 0

    api.open = native_open
    api.control = NativeIoControl(api)
    try:
        assert (
            api.create(
                "key",
                1,
                read=True,
                directory=False,
                access=0xC0000000,
                disposition=2,
                security_descriptor=12345,
            )
            == 999
        )
    finally:
        api.control.close()


@pytest.mark.parametrize("code, collision", [(80, True), (183, True), (5, False), (2, False)])
def test_private_native_create_collision_is_distinct_from_other_failures(code, collision):
    """RM37/AC28: only an existing-entry collision permits trying the existing key."""
    api = object.__new__(_Api)
    api.close = lambda handle: None
    api.current_thread_handle = lambda: 777
    api.cancel_synchronous = lambda handle: False
    api.error_code = lambda status: code
    api.open = lambda *args: -1
    api.control = NativeIoControl(api)
    try:
        with pytest.raises(FileExistsError if collision else OSError) as caught:
            api.create("key", 1, read=True, directory=False, disposition=2)
        assert isinstance(caught.value, FileExistsError) == collision
    finally:
        api.control.close()


def test_private_windows_helper_import_is_portable():
    """RM37/AC28: importing the boundary never loads a Windows DLL on POSIX."""
    helper = importlib.import_module("relaymind.recommendations._windows_private_files")
    assert callable(helper.open_windows_private_file)
    assert callable(helper.create_windows_private_file)


native = pytest.mark.skipif(os.name != "nt", reason="requires real native Windows ACL/file APIs")


@native
def test_native_private_creation_collision_and_bounded_reopen(tmp_path):
    """RM37/AC28: protected creation/reopen is valid; collision never overwrites."""
    from relaymind.recommendations._windows_private_files import (
        create_windows_private_file,
        open_windows_private_file,
    )

    path = tmp_path / "私钥𐐀"
    assert create_windows_private_file(path, b"k" * 32)
    assert not create_windows_private_file(path, b"changed")
    with open_windows_private_file(path, maximum=32) as lease:
        assert lease.data == b"k" * 32
        assert len(lease.identity) == 2
        assert "kkkk" not in repr(lease)
        lease.verify()
        for operation in (
            lambda: path.write_bytes(b"changed"),
            path.unlink,
            lambda: path.rename(path.with_name("replacement")),
        ):
            with pytest.raises(OSError):
                operation()
    with pytest.raises(PrivateFileError):
        with open_windows_private_file(path, maximum=31):
            pass


@native
@pytest.mark.parametrize("suffix", [":secret", "::$DATA"])
def test_native_private_ads_rejected(tmp_path, suffix):
    """RM37/AC28: a valid private target cannot make an ADS path acceptable."""
    from relaymind.recommendations._windows_private_files import (
        create_windows_private_file,
        open_windows_private_file,
    )

    path = tmp_path / "key"
    create_windows_private_file(path, b"valid")
    with pytest.raises(PrivateFileError):
        with open_windows_private_file(Path(str(path) + suffix)):
            pass


@native
def test_native_private_absence_and_directory(tmp_path):
    """RM37/AC28: actual absence differs from an unsafe existing object."""
    from relaymind.recommendations._windows_private_files import open_windows_private_file

    with pytest.raises(FileNotFoundError):
        with open_windows_private_file(tmp_path / "absent"):
            pass
    with pytest.raises(PrivateFileError):
        with open_windows_private_file(tmp_path):
            pass


@pytest.mark.parametrize(
    "kind, flags, mask, trustee, accepted",
    [
        (0, 0, 0x120081, "user", True),
        (1, 0, 0x120081, "other", False),
        (0, 0, 0x120081, "other", None),
        (0, 0x10, 1, "other", None),
        (0, 8, 1, "user", None),
        (9, 0, 1, "user", None),
        (5, 0, 1, "user", None),
    ],
)
def test_private_ace_rejects_broad_inherited_and_unsupported_grants(
    kind, flags, mask, trustee, accepted
):
    """RM37/AC28: deny/inheritance never excuses a broad or unknown granting ACE."""
    import struct

    from relaymind.recommendations import _windows_private_files as helper

    user = b"\x01\x01\x00\x00\x00\x00\x00\x05\x15\x00\x00\x00"
    other = b"\x01\x01\x00\x00\x00\x00\x00\x01\x00\x00\x00\x00"
    sid = user if trustee == "user" else other
    ace = struct.pack("<BBHI", kind, flags, 8 + len(sid), mask) + sid
    if accepted is None:
        with pytest.raises(PrivateFileError):
            helper._validate_ace(ace, user, lambda a, b: a == b)
    else:
        assert helper._validate_ace(ace, user, lambda a, b: a == b) is accepted


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"\x00" * 7,
        b"\x00\x00\x08\x00" + b"\x00" * 4,
        b"\x00\x00\x14\x00" + b"\x00" * 4 + b"\x01\x0f" + b"\x00" * 10,
    ],
)
def test_private_ace_bounds_before_sid_dereference(raw):
    """RM37/AC28: truncated ACEs/SIDs fail without out-of-buffer dereferences."""
    from relaymind.recommendations import _windows_private_files as helper

    with pytest.raises(PrivateFileError):
        helper._validate_ace(raw, b"user", lambda a, b: True)


def _set_native_acl(path, sddl):
    """Disposable fixture only; use real Windows SD conversion/security writes."""
    advapi = C.WinDLL("advapi32", use_last_error=True)
    kernel = C.WinDLL("kernel32", use_last_error=True)
    convert = advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [C.c_wchar_p, C.c_uint32, C.POINTER(C.c_void_p), C.POINTER(C.c_uint32)]
    convert.restype = C.c_int32
    apply = advapi.SetFileSecurityW
    apply.argtypes, apply.restype = [C.c_wchar_p, C.c_uint32, C.c_void_p], C.c_int32
    free = kernel.LocalFree
    free.argtypes, free.restype = [C.c_void_p], C.c_void_p
    descriptor = C.c_void_p()
    assert convert(sddl, 1, C.byref(descriptor), None), "fixture SDDL conversion failed"
    try:
        assert apply(str(path), 0x80000004, descriptor), "fixture ACL update failed"
    finally:
        free(descriptor)


@native
@pytest.mark.parametrize("acl", ["D:P(A;;FA;;;WD)", "D:P(A;;FR;;;BU)", "D:NO_ACCESS_CONTROL"])
def test_native_private_broad_or_null_dacl_refused(tmp_path, acl):
    """RM37/AC28: same-owner files with Everyone/Users/null access are unsafe."""
    from relaymind.recommendations._windows_private_files import (
        create_windows_private_file,
        open_windows_private_file,
    )

    path = tmp_path / "key"
    create_windows_private_file(path, b"valid private bytes")
    _set_native_acl(path, acl)
    with pytest.raises(PrivateFileError):
        with open_windows_private_file(path):
            pass


@native
def test_native_private_ancestor_junction_refused_with_valid_target(tmp_path):
    """RM37/AC28: opening a junction never follows a valid protected target."""
    import subprocess

    from relaymind.recommendations._windows_private_files import (
        create_windows_private_file,
        open_windows_private_file,
    )

    real = tmp_path / "real"
    real.mkdir()
    path = real / "key"
    create_windows_private_file(path, b"valid")
    with open_windows_private_file(path) as lease:
        assert lease.data == b"valid"
    alias = tmp_path / "alias"
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(alias), str(real)], capture_output=True, check=False
    )
    assert result.returncode == 0, "could not create disposable native junction"
    with pytest.raises(PrivateFileError):
        with open_windows_private_file(alias / "key"):
            pass


@native
def test_native_private_final_symlink_refused_with_valid_target(tmp_path):
    """RM37/AC28: symlink rejection has a valid same-directory success control."""
    from relaymind.recommendations._windows_private_files import (
        create_windows_private_file,
        open_windows_private_file,
    )

    target, link = tmp_path / "target", tmp_path / "link"
    create_windows_private_file(target, b"valid")
    with open_windows_private_file(target) as lease:
        assert lease.data == b"valid"
    try:
        link.symlink_to(target)
    except OSError as exc:
        if exc.winerror == 1314:
            pytest.skip("native fixture needs SeCreateSymbolicLinkPrivilege or Developer Mode")
        raise
    with pytest.raises(PrivateFileError):
        with open_windows_private_file(link):
            pass


@pytest.mark.parametrize("flags", [0x20, 0x40, 0x80])
def test_private_allow_ace_unknown_flags_fail_closed(flags):
    """RM37/AC28: undefined/audit ACE flags cannot add semantics to a private allow."""
    import struct

    from relaymind.recommendations import _windows_private_files as helper

    sid = b"\x01\x01\x00\x00\x00\x00\x00\x05\x15\x00\x00\x00"
    with pytest.raises(PrivateFileError):
        helper._validate_ace(struct.pack("<BBHI", 0, flags, 20, 1) + sid, sid, lambda a, b: a == b)


@pytest.mark.parametrize("failure", ["api", "descriptor", "owner", "null", "ace", "none"])
def test_private_security_descriptor_is_freed_on_every_query_outcome(failure):
    """RM37/AC28: borrowed owner/ACL pointers never own or outlive their descriptor."""
    import struct

    from relaymind.recommendations._windows_private_files import _PrivateApi

    user = b"\x01\x01\x00\x00\x00\x00\x00\x05\x15\x00\x00\x00"
    ace = struct.pack("<BBHI", 0, 0, 20, 0x1F01FF) + user
    raw = bytes(20) + user + struct.pack("<BBHHH", 2, 0, 28, 1, 0) + ace
    descriptor = C.create_string_buffer(raw)
    base = C.addressof(descriptor)
    freed, closed = [], []
    api = object.__new__(_PrivateApi)
    api.close = closed.append
    api.current_thread_handle = lambda: 777
    api.cancel_synchronous = lambda handle: False
    api.free = lambda pointer: freed.append(pointer.value)

    def out(pointer, value):
        C.cast(pointer, C.POINTER(C.c_void_p))[0] = value

    def get_security(handle, kind, flags, owner, group, acl, sacl, result):
        out(result, base)
        out(owner, base + 20)
        out(acl, base + 32)
        return 5 if failure == "api" else 0

    api.get_security = get_security
    api.valid_descriptor = lambda pointer: failure != "descriptor"
    api.descriptor_length = lambda pointer: len(raw)
    api.valid_sid = lambda pointer: True
    api.sid_length = lambda pointer: 12
    api.equal_sid = lambda left, right: failure != "owner"

    def get_dacl(descriptor, present, checked, defaulted):
        C.cast(present, C.POINTER(C.c_int32))[0] = int(failure != "null")
        out(checked, base + 32)
        return 1

    api.descriptor_dacl = get_dacl
    api.valid_acl = lambda pointer: True

    def acl_info(pointer, result, size, kind):
        values = C.cast(result, C.POINTER(C.c_uint32))
        values[0], values[1], values[2] = 1, 28, 0
        return 1

    api.acl_info = acl_info
    api.get_ace = lambda acl, index, result: (
        out(result, base + (59 if failure == "ace" else 40)) or 1
    )
    api.descriptor_control = lambda descriptor, control, revision: 1
    api.control = NativeIoControl(api)
    try:
        if failure == "none":
            assert api.private_security(999, user)[0] == user
        else:
            with pytest.raises(PrivateFileError):
                api.private_security(999, user)
    finally:
        api.control.close()
    assert freed == [base]
    assert closed == [777]


@pytest.mark.parametrize("size", [0, 7, 65537])
def test_private_token_query_bounds_and_token_ownership(size, monkeypatch):
    """RM37/AC28: abnormal token sizes fail before allocating and close the owned token."""
    from relaymind.recommendations._windows_private_files import _PrivateApi

    closed = []
    api = object.__new__(_PrivateApi)
    api.close = lambda handle: closed.append(
        handle.value if isinstance(handle, C.c_void_p) else handle
    )
    api.current_thread_handle = lambda: 777
    api.cancel_synchronous = lambda handle: False
    api.process = lambda: -1

    def open_token(process, access, result):
        C.cast(result, C.POINTER(C.c_void_p))[0] = 555
        return 1

    def token_info(token, kind, buffer, length, result):
        assert buffer is None and length == 0
        C.cast(result, C.POINTER(C.c_uint32))[0] = size
        return 0

    api.open_token, api.token_info = open_token, token_info
    monkeypatch.setattr(C, "get_last_error", lambda: 122, raising=False)
    api.control = NativeIoControl(api)
    try:
        with pytest.raises(PrivateFileError):
            api.user_sid()
    finally:
        api.control.close()
    assert closed == [555, 777]


@native
def test_native_private_active_lease_rejects_dacl_broadening_and_restores_acl(tmp_path):
    """RM37/AC28: an owner can edit the DACL while leased, but verification must fail."""
    from relaymind.recommendations._windows_private_files import (
        _PrivateApi,
        create_windows_private_file,
        open_windows_private_file,
    )

    path = tmp_path / "key"
    assert create_windows_private_file(path, b"valid private bytes")
    with open_windows_private_file(path) as lease:
        lease.verify()
        api, spelling = _PrivateApi(), C.c_void_p()
        assert api.sid_string(C.create_string_buffer(lease.fingerprint[-1][0]), C.byref(spelling))
        try:
            sid = C.wstring_at(spelling)
        finally:
            api.free(spelling)
        safe_acl = f"D:P(A;;FA;;;{sid})"
        try:
            _set_native_acl(path, f"{safe_acl}(A;;FR;;;WD)")
            with pytest.raises(PrivateFileError):
                lease.verify()
        finally:
            _set_native_acl(path, safe_acl)
    with open_windows_private_file(path) as restored:
        assert restored.data == b"valid private bytes"
        restored.verify()


@pytest.mark.parametrize(
    "scenario",
    [
        "partial",
        "zero",
        "oversized",
        "write-error",
        "flush-error",
        "cancel-descriptor",
        "cancel-create",
        "cancel-write",
        "cancel-flush",
    ],
)
def test_private_creation_write_progress_failures_and_late_cancellation(
    tmp_path, monkeypatch, scenario
):
    """RM37/AC28: real wrapper control flow owns buffers, descriptors and handles through faults.

    Only native endpoints and the directory walker are replaced on POSIX. The
    production parent/descriptor contexts, NtCreateFile wrapper, snapshot checks,
    write loop, cancellation fence and cleanup are exercised together. These
    portable doubles do not qualify native ACL or filesystem enforcement.
    """
    import threading
    import time
    from types import SimpleNamespace

    from relaymind.assets.source_io import SourceInfo
    from relaymind.recommendations import _windows_private_files as helper

    payload = b"0123456789abcdefghijklmnopqrstuv"
    cancelled = threading.Event()
    freed, closed, written = [], [], bytearray()
    sid = b"\x01\x01\x00\x00\x00\x00\x00\x05\x15\x00\x00\x00"
    spelling = C.create_unicode_buffer("S-1-5-21")
    descriptor = C.create_string_buffer(64)
    spelling_address, descriptor_address = C.addressof(spelling), C.addressof(descriptor)
    created, flushed = False, False
    api = object.__new__(helper._PrivateApi)
    api.current_thread_handle = lambda: 777
    api.cancel_synchronous = lambda handle: False
    api.close = closed.append
    api.free = lambda pointer: freed.append(
        pointer.value if isinstance(pointer, C.c_void_p) else pointer
    )
    api.user_sid = lambda: sid

    def sid_string(user, result):
        C.cast(result, C.POINTER(C.c_void_p))[0] = spelling_address
        return 1

    def convert_descriptor(sddl, revision, result, size):
        assert sddl == "O:S-1-5-21D:P(A;;FA;;;S-1-5-21)"
        C.cast(result, C.POINTER(C.c_void_p))[0] = descriptor_address
        if scenario == "cancel-descriptor":
            cancelled.set()
        return 1

    def native_open(out, access, attributes, status, allocation, flags, share, disposition, *rest):
        nonlocal created
        attrs = C.cast(attributes, C.POINTER(_ObjectAttributes)).contents
        assert attrs.SecurityDescriptor == descriptor_address
        assert attrs.RootDirectory == 333
        assert access == 0xC0000000 and share == 1 and disposition == 2
        C.cast(out, C.POINTER(C.c_void_p))[0] = 999
        created = True
        if scenario == "cancel-create":
            cancelled.set()
        return 0

    def information(handle):
        assert handle == 999
        return SourceInfo((12, 34), len(written), 123, 456, False, True, False)

    def native_write(handle, buffer, size, actual, overlapped):
        assert handle == 999 and overlapped is None
        count = min(3, size) if scenario == "partial" else size
        if scenario == "zero":
            count = 0
        elif scenario == "oversized":
            count = size + 1
        if scenario != "write-error" and count <= size:
            written.extend(C.string_at(buffer, count))
        C.cast(actual, C.POINTER(C.c_uint32))[0] = count
        if scenario == "cancel-write":
            cancelled.set()
        return int(scenario != "write-error")

    def native_flush(handle):
        nonlocal flushed
        assert handle == 999
        flushed = True
        if scenario == "cancel-flush":
            cancelled.set()
        return int(scenario != "flush-error")

    api.sid_string, api.convert_descriptor = sid_string, convert_descriptor
    api.open, api.information = native_open, information
    api.private_security = lambda handle, user: (sid, 0x9004, b"private ACL")
    api.write, api.flush = native_write, native_flush

    class ParentTree:
        def __init__(self, root, *, native):
            self.api, self._directories = native.api, {(): 333}

        def verify_root(self):
            assert created and flushed

        def close(self):
            self.api.control.invoke(self.api.close, 333, cleanup=True)

    context = SimpleNamespace(
        deadline=time.monotonic() + 60,
        monotonic=time.monotonic,
        cancelled=cancelled.is_set,
    )
    monkeypatch.setattr(helper, "_PrivateApi", lambda: api)
    monkeypatch.setattr(helper, "WindowsSourceTree", ParentTree)
    monkeypatch.setattr(
        helper, "NativeIoControl", lambda native_api: NativeIoControl(native_api, context)
    )

    if scenario == "partial":
        assert helper.create_windows_private_file(tmp_path / "key", payload)
        assert bytes(written) == b"0123456789abcdefghijklmnopqrstuv"
        assert flushed
    else:
        with pytest.raises(PrivateFileError):
            helper.create_windows_private_file(tmp_path / "key", payload)
    assert freed.count(spelling_address) == 1
    assert freed.count(descriptor_address) == 1
    assert len(freed) == 2
    assert closed.count(999) == int(created)
    assert closed.count(333) == 1
    assert closed.count(777) == 1
    assert len(closed) == 2 + int(created)
    assert not api.control._monitor.is_alive()
    if scenario in (
        "zero",
        "oversized",
        "write-error",
        "cancel-descriptor",
        "cancel-create",
        "cancel-write",
    ):
        assert not flushed
