"""Disposable native Windows I/O/watchdog scenarios for RM34/AC25 tests.

Every holder and blocked capture is a child process of the test supervisor.
Nothing here runs outside the sandbox path passed by the native pytest case.
"""

import ctypes as C
import json
import subprocess
import sys
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from relaymind.assets._windows_source_io import (
    _HANDLE,
    _U32,
    NativeIoAborted,
    NativeIoControl,
    _Api,
    _IoStatus,
    _ObjectAttributes,
    _UnicodeString,
)
from relaymind.assets.sources import CaptureContext, SourceFailure
from relaymind.store import RunStore
from relaymind.store.asset_catalog import AssetCatalogStore
from test_asset_sources import capture, skill


class Overlapped(C.Structure):
    _fields_ = [
        ("Internal", C.c_size_t),
        ("InternalHigh", C.c_size_t),
        ("Offset", _U32),
        ("OffsetHigh", _U32),
        ("hEvent", _HANDLE),
    ]


def kernel():
    api = C.WinDLL("kernel32", use_last_error=True)
    definitions = {
        "CreateFileW": ([C.c_wchar_p, _U32, _U32, C.c_void_p, _U32, _U32, _HANDLE], _HANDLE),
        "CreateEventW": ([C.c_void_p, C.c_int, C.c_int, C.c_wchar_p], _HANDLE),
        "DeviceIoControl": (
            [
                _HANDLE,
                _U32,
                C.c_void_p,
                _U32,
                C.c_void_p,
                _U32,
                C.POINTER(_U32),
                C.POINTER(Overlapped),
            ],
            C.c_int,
        ),
        "WaitForSingleObject": ([_HANDLE, _U32], _U32),
        "CloseHandle": ([_HANDLE], C.c_int),
        "CreateNamedPipeW": (
            [C.c_wchar_p, _U32, _U32, _U32, _U32, _U32, _U32, C.c_void_p],
            _HANDLE,
        ),
        "ConnectNamedPipe": ([_HANDLE, C.c_void_p], C.c_int),
    }
    for name, (arguments, result) in definitions.items():
        function = getattr(api, name)
        function.argtypes, function.restype = arguments, result
    return api


def valid(handle):
    assert handle not in (None, C.c_void_p(-1).value), (
        f"native fixture open failed: {C.get_last_error()}"
    )
    return handle


def holder(path):
    api = kernel()
    handle = valid(
        api.CreateFileW(str(path), 0x80000000 | 0x40000000, 7, None, 3, 0x40000000, None)
    )
    event = valid(api.CreateEventW(None, True, False, None))
    overlapped, returned = Overlapped(hEvent=event), _U32()
    try:
        # A real batch oplock remains outstanding until its break. Do not issue
        # FSCTL_OPLOCK_BREAK_ACKNOWLEDGE after the event signals.
        result = api.DeviceIoControl(
            handle, 0x00090008, None, 0, None, 0, C.byref(returned), C.byref(overlapped)
        )
        assert not result and C.get_last_error() == 997, "batch oplock was not granted/pending"
        print("granted", flush=True)
        assert api.WaitForSingleObject(event, 5000) == 0, "no real oplock break observed"
        print("break-observed", flush=True)
        assert sys.stdin.readline().strip() == "release"
    finally:
        api.CloseHandle(handle)
        api.CloseHandle(event)


def ordinary_open(api, path, *, control=None, statuses=None):
    name = "\\??\\" + str(path)
    encoded = name.encode("utf-16-le")
    buffer = C.create_string_buffer(encoded + b"\x00\x00")
    string = _UnicodeString(len(encoded), len(encoded) + 2, C.cast(buffer, C.c_void_p))
    attributes = _ObjectAttributes(
        C.sizeof(_ObjectAttributes), None, C.pointer(string), 0x40, None, None
    )
    handle, status = _HANDLE(), _IoStatus()

    completed_status = []

    def work():
        print("opening", flush=True)
        # Intentionally omit FILE_COMPLETE_IF_OPLOCKED for the pending-call
        # control and cancellation test. Production always supplies that flag.
        value = api.open(
            C.byref(handle),
            0x00100000 | 0x80 | 1,
            C.byref(attributes),
            C.byref(status),
            None,
            0,
            1,
            1,
            0x00200000 | 0x20,
            None,
            0,
        )
        completed_status.append(value)
        if statuses is not None:
            statuses.append(value)
        return value

    def discard(value):
        if value >= 0 and handle.value:
            api.close(handle.value)
            handle.value = None

    try:
        return work() if control is None else control.invoke(work, discard=discard)
    finally:
        if handle.value and completed_status and completed_status[0] >= 0:
            api.close(handle.value)


def pending_open(path):
    api, cancelled = _Api(), threading.Event()
    context = CaptureContext(datetime.now(UTC), cancelled=cancelled.is_set)
    controller = NativeIoControl(api, context)
    api.control = controller
    timer = threading.Timer(0.15, cancelled.set)
    statuses = []
    started = time.monotonic()
    timer.start()
    try:
        ordinary_open(api, path, control=controller, statuses=statuses)
        raise AssertionError("pending ordinary create unexpectedly escaped its cancellation fence")
    except NativeIoAborted as exc:
        assert time.monotonic() - started >= 0.1
        return dict(
            completed=True,
            reason=exc.reason,
            native_call_was_pending=True,
            elapsed_seconds=time.monotonic() - started,
            native_statuses=statuses,
        )
    finally:
        timer.cancel()
        timer.join()
        controller.close()


def capture_oplock(root, db):
    statuses = []
    original_init = _Api.__init__

    def record_init(self):
        original_init(self)
        original_open = self.open

        def observed(*args):
            result = original_open(*args)
            statuses.append(result)
            return result

        self.open = observed

    _Api.__init__ = record_init
    started = time.monotonic()
    with RunStore(db) as store:
        catalog = AssetCatalogStore(store)
        before = catalog.snapshot()
        try:
            catalog.publish(
                capture(root, previous=catalog.inventory("example")),
                expected_revision=before.catalog_revision,
            )
        except SourceFailure as exc:
            catalog.record_failure(
                "example", rejected_count=exc.rejected_count, exclusions=exc.exclusions
            )
        else:
            raise AssertionError("held oplock was admitted as an ordinary capture")
        after = catalog.snapshot()
        assert before.catalog_revision == 1
        assert after.catalog_revision == before.catalog_revision + 1
        content_retained = [a.content_hash for a in after.assets] == [
            a.content_hash for a in before.assets
        ]
        assert content_retained
        assert catalog.inventory("example").tombstones == frozenset()
    return dict(
        completed=True,
        elapsed_seconds=time.monotonic() - started,
        previous_catalog_revision=before.catalog_revision,
        observed_catalog_revision=after.catalog_revision,
        content_retained=content_retained,
        native_statuses=statuses,
    )


def supervised_oplock(root, scenario):
    skill(root / "source")
    source, db = root / "source", root / "catalog.sqlite"
    with RunStore(db) as store:
        AssetCatalogStore(store).publish(capture(source), expected_revision=0)
    children = []
    try:
        held = subprocess.Popen(
            [sys.executable, __file__, "holder", str(source / "SKILL.md")],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        children.append(held)
        assert held.stdout.readline().strip() == "granted"
        control = subprocess.Popen(
            [sys.executable, __file__, "ordinary", str(source / "SKILL.md")],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        children.append(control)
        assert control.stdout.readline().strip() == "opening"
        assert held.stdout.readline().strip() == "break-observed"
        time.sleep(0.1)
        assert control.poll() is None, (
            "ordinary open did not remain pending behind the withheld break"
        )
        control.terminate()
        control.communicate(timeout=3)
        mode = "capture" if scenario == "oplock-capture" else "pending-open"
        result = subprocess.run(
            [sys.executable, __file__, mode, str(source), str(db)],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        evidence = json.loads(result.stdout.strip().splitlines()[-1])
        evidence["ordinary_open_pended"] = True
        # The holder never releases until told, so finishing at all proves the
        # capture did not wait on the hold; the bound only allows a loaded runner.
        assert evidence["elapsed_seconds"] < 10
        held.stdin.write("release\n")
        held.stdin.flush()
        stdout, stderr = held.communicate(timeout=3)
        assert held.returncode == 0, stdout + stderr
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
            child.communicate(timeout=3)
    # Completion must release reader sharing guards, not leave orphaned handles.
    (source / "SKILL.md").write_bytes(b"cleanup verified")
    return evidence


def pending_read(scenario):
    api = kernel()
    pipe_name = "\\\\.\\pipe\\relaymind-catalog-test-" + uuid.uuid4().hex
    server = valid(api.CreateNamedPipeW(pipe_name, 1, 0, 1, 4096, 4096, 0, None))
    client_handles = []

    def connect():
        client_handles.append(valid(api.CreateFileW(pipe_name, 0x40000000, 0, None, 3, 0, None)))

    connector = threading.Thread(target=connect)
    connector.start()
    connected = api.ConnectNamedPipe(server, None)
    assert connected or C.get_last_error() == 535
    connector.join(timeout=1)
    assert client_handles
    cancel, entered = threading.Event(), threading.Event()
    outcomes = []

    def worker():
        native = _Api()
        context = CaptureContext(datetime.now(UTC), cancelled=cancel.is_set)
        if scenario.endswith("deadline"):
            context.deadline = time.monotonic() + 0.15
        controller = NativeIoControl(native, context)
        buffer, actual = C.create_string_buffer(16), _U32()
        started, results = time.monotonic(), []

        def read():
            entered.set()
            result = native.read(server, buffer, len(buffer), C.byref(actual), None)
            results.append((result, C.get_last_error()))
            return result

        try:
            controller.invoke(read)
            raise AssertionError("pending read escaped its cancellation fence")
        except NativeIoAborted as exc:
            assert results and (results[0][1] == 995 or results[0][0])
            outcomes.append(
                dict(
                    completed=True,
                    reason=exc.reason,
                    native_call_was_pending=True,
                    elapsed_seconds=time.monotonic() - started,
                    native_results=results,
                )
            )
        finally:
            controller.close()
        assert not controller._monitor.is_alive()

    owner = threading.Thread(target=worker)
    try:
        owner.start()
        assert entered.wait(1)
        time.sleep(0.05)
        assert owner.is_alive(), "the native read was not pending"
        if not scenario.endswith("deadline"):
            cancel.set()
        owner.join(timeout=2)
        assert not owner.is_alive(), "native cancellation did not drain within the observed bound"
        assert len(outcomes) == 1
        return outcomes[0]
    finally:
        api.CloseHandle(client_handles[0])
        api.CloseHandle(server)
        owner.join(timeout=1)


if __name__ == "__main__":
    mode, path = sys.argv[1], Path(sys.argv[2])
    if mode == "holder":
        holder(path)
    elif mode == "ordinary":
        api = _Api()
        ordinary_open(api, path)
    elif mode == "pending-open":
        print(json.dumps(pending_open(path / "SKILL.md")))
    elif mode == "capture":
        print(json.dumps(capture_oplock(path, Path(sys.argv[3]))))
    elif mode.startswith("pending-read"):
        print(json.dumps(pending_read(mode)))
    else:
        print(json.dumps(supervised_oplock(path, mode)))
