"""RM34/AC25: stage ordinary local packages and closed imports without authority."""

import hashlib
import json
import os
from datetime import UTC, datetime

import pytest

from test_asset_catalog import record


def readers():
    from relaymind.assets import sources
    from relaymind.assets.settings import SourceConfig

    return sources, SourceConfig


def capture(path, kind="local_directory", *, source_id="example", previous=None, **context):
    s, config = readers()
    return s.capture_source(
        config(source_id, kind, path),
        previous=previous or s.SourceInventory.empty(source_id),
        context=s.CaptureContext(datetime(2026, 10, 29, tzinfo=UTC), **context),
    )


def skill(
    path, *, name="Hello World", description="Greeting skill", metadata="", body="Say hello."
):
    path.mkdir(parents=True, exist_ok=True)
    (path / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n{metadata}---\n{body}",
        encoding="utf-8",
        newline="\n",
    )
    return path / "SKILL.md"


def imported(path, *, records=None, complete=False, removed=(), **changes):
    path.mkdir(parents=True, exist_ok=True)
    original = path / "hello.bin"
    original.write_bytes(b"hello")
    value = record()
    for key in ("schema_version", "source_id", "availability", "stale"):
        value.pop(key)
    data = dict(
        schema_version=1,
        source_id="example",
        source_version=None,
        captured_at="2026-10-29T00:00:00Z",
        complete=complete,
        records=records
        if records is not None
        else [{"record": value, "original_path": "hello.bin"}],
        removed_asset_ids=list(removed),
    )
    data.update(changes)
    manifest = path / "import.json"
    manifest.write_text(json.dumps(data), encoding="utf-8")
    return manifest, data


def test_local_ordinary_folder_identity_original_hash_and_manifest(tmp_path):
    s, _ = readers()
    root = tmp_path / "skills"
    source_file = skill(
        root / "Zed", name="中文 Name", metadata="metadata:\n  dependencies: [other:x]\n"
    )
    (root / "Zed" / "skill.manifest.json").write_text(
        '{"version":"2.0", "languages":["zh-CN"], "resources":["/never-read"]}'
    )
    skill(root / "Alpha")
    got = capture(root)
    assert got.complete
    assert [d.record.identity["package_id"] for d in got.documents] == ["Hello World", "中文 Name"]
    actual = got.documents[1]
    assert (
        actual.record.asset_id
        == "skill:d3911d6f6f0ae5f57dca39fb3a1f1d8d94e7bed9dfabdf5ace9f79865ce8948a"
    )
    # Compare an independent original-byte hash; source reads must never normalize before hashing.
    assert actual.record.content_hash == hashlib.sha256(source_file.read_bytes()).hexdigest()
    assert actual.record.identity["package_version"] == "2.0"
    assert actual.record.language_tags == ("zh-CN",)
    assert actual.record.dependencies == ("other:x",)
    assert actual.body == "Say hello."
    assert all(d.record.availability == "unknown" for d in got.documents)
    assert len(got.files) == 3
    assert capture(root).source_digest == got.source_digest
    old_id = actual.record.asset_id
    skill(root / "Zed", name="Renamed")
    assert capture(root).documents[1].record.asset_id == old_id
    assert s.SourceInventory.empty("example").records == ()


def test_root_entry_prevents_child_fallback_even_when_invalid(tmp_path):
    skill(tmp_path / "child")
    (tmp_path / "SKILL.md").write_text("invalid")
    s, _ = readers()
    with pytest.raises(s.SourceFailure) as caught:
        capture(tmp_path)
    assert caught.value.reason == "invalid_data"
    assert caught.value.rejected_count == 1
    skill(tmp_path)
    assert len(capture(tmp_path).documents) == 1


@pytest.mark.parametrize(
    "payload",
    [
        "---\nname: a\nname: b\ndescription: x\n---\nbody",
        "---\nname: &a hello\ndescription: *a\n---\nbody",
        "---\nname: !!str hello\ndescription: x\n---\nbody",
        "---\nname: a\ndescription: x\nmetadata: []\n---\nbody",
        "---\nname: a\ndescription: x\nmetadata:\n  version: 1\n---\nbody",
        "---\nname: a\ndescription: x\nmetadata:\n  languages: [en, en]\n---\nbody",
        "---\nname: a\ndescription: x\n---\n",
        "---\nname: a\ndescription: x\n---\n<script>unsafe",
        "---\nname: a\ndescription: x\nextra: " + "[" * 17 + "0" + "]" * 17 + "\n---\nbody",
    ],
)
def test_unsafe_or_malformed_local_input_fails_whole_scan(tmp_path, payload):
    s, _ = readers()
    (tmp_path / "SKILL.md").write_text(payload)
    with pytest.raises(s.SourceFailure) as caught:
        capture(tmp_path)
    assert "<script>" not in str(caught.value)


def test_source_secret_values_redacted_but_schema_declarations_untouched(tmp_path):
    skill(tmp_path, description="Use credential-987", metadata="token: credential-987\n")
    got = capture(tmp_path)
    assert got.documents[0].record.description == "Use [redacted]"
    assert "credential-987" not in json.dumps(got.documents[0].to_json_object())
    assert (
        got.documents[0].record.content_hash
        == hashlib.sha256((tmp_path / "SKILL.md").read_bytes()).hexdigest()
    )


def symlink(link, target, *, directory=False):
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError as exc:
        if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
            pytest.skip("native Windows runner lacks symlink creation privilege")
        raise


@pytest.mark.parametrize("target", ["root", "skill", "manifest", "child", "original"])
def test_symlinks_never_followed(tmp_path, target):
    s, _ = readers()
    real = tmp_path / "real"
    skill(real)
    assert capture(real).documents
    root = real
    kind = "local_directory"
    if target == "root":
        root = tmp_path / "link"
        symlink(root, real, directory=True)
    elif target == "skill":
        outside = skill(tmp_path / "outside-package")
        assert capture(outside.parent).documents
        (real / "SKILL.md").unlink()
        symlink(real / "SKILL.md", outside)
    elif target == "manifest":
        outside = tmp_path / "outside-manifest.json"
        outside.write_text('{"version":"valid","languages":["en"]}')
        (real / "skill.manifest.json").write_bytes(outside.read_bytes())
        assert capture(real).documents[0].record.source_version == "valid"
        (real / "skill.manifest.json").unlink()
        symlink(real / "skill.manifest.json", outside)
    elif target == "child":
        root = tmp_path / "container"
        root.mkdir()
        symlink(root / "child", real, directory=True)
    else:
        root, _ = imported(tmp_path / "import")
        assert capture(root, "metadata_import").documents
        outside = tmp_path / "outside-original.bin"
        outside.write_bytes(b"hello")
        (root.parent / "hello.bin").unlink()
        symlink(root.parent / "hello.bin", outside)
        kind = "metadata_import"
    with pytest.raises(s.SourceFailure):
        capture(root, kind)


def test_import_closed_consistent_and_no_body_or_availability(tmp_path):
    manifest, data = imported(tmp_path)
    got = capture(manifest, "metadata_import")
    assert got.documents[0].record.availability == "unknown"
    assert got.documents[0].body is None
    assert got.source_digest == hashlib.sha256(manifest.read_bytes()).hexdigest()
    assert got.source_version is None
    for field, value in (
        ("availability", "available"),
        ("stale", False),
        ("catalog_revision", 0),
        ("host_snapshot", {}),
        ("source_id", "example"),
    ):
        changed = json.loads(json.dumps(data))
        changed["records"][0]["record"][field] = value
        manifest.write_text(json.dumps(changed))
        s, _ = readers()
        with pytest.raises(s.SourceFailure):
            capture(manifest, "metadata_import")


@pytest.mark.parametrize(
    "change",
    [
        "source",
        "hash",
        "duplicate",
        "unknown_remove",
        "partial_remove",
        "collision",
        "path_escape",
        "alias",
        "nonfinite",
        "duplicate_json",
    ],
)
def test_invalid_import_rejects_entire_document(tmp_path, change):
    s, _ = readers()
    manifest, data = imported(tmp_path)
    entry = data["records"][0]
    if change == "source":
        data["source_id"] = "wrong"
    elif change == "hash":
        entry["record"]["content_hash"] = "0" * 64
    elif change == "duplicate":
        data["records"].append(entry)
    elif change == "unknown_remove":
        data["complete"] = True
        data["removed_asset_ids"] = ["unknown"]
    elif change == "partial_remove":
        data["removed_asset_ids"] = ["example:hello"]
    elif change == "collision":
        data["complete"] = True
        data["removed_asset_ids"] = [entry["record"]["asset_id"]]
    elif change == "path_escape":
        entry["original_path"] = "../hello.bin"
    elif change == "alias":
        entry["original_path"] = "import.json"
    elif change == "nonfinite":
        entry["record"]["identity"]["package_version"] = float("nan")
    if change == "duplicate_json":
        manifest.write_text('{"schema_version":1,"schema_version":1}')
    else:
        manifest.write_text(json.dumps(data))
    with pytest.raises(s.SourceFailure):
        capture(manifest, "metadata_import")


def test_local_limits_cancel_and_deadline_abort_before_publication(tmp_path):
    s, _ = readers()
    skill(tmp_path, body="x" * (256 * 1024 + 1))
    with pytest.raises(s.SourceFailure) as caught:
        capture(tmp_path)
    assert caught.value.reason == "size_limit"
    skill(tmp_path)
    with pytest.raises(s.SourceFailure) as caught:
        capture(tmp_path, cancelled=lambda: True)
    assert caught.value.reason == "cancelled"
    ticks = iter([0.0, 61.0])
    with pytest.raises(s.SourceFailure) as caught:
        capture(tmp_path, monotonic=lambda: next(ticks, 61.0))
    assert caught.value.reason == "deadline"
    (tmp_path / "SKILL.md").unlink()
    for index in range(1001):
        (tmp_path / str(index)).touch()
    with pytest.raises(s.SourceFailure) as caught:
        capture(tmp_path)
    assert caught.value.reason == "record_limit"


def test_reused_original_is_hashed_once_but_changed_capture_rejected(tmp_path, monkeypatch):
    s, _ = readers()
    manifest, data = imported(tmp_path)
    second = json.loads(json.dumps(data["records"][0]))
    second["record"]["asset_id"] = "example:second"
    data["records"].append(second)
    manifest.write_text(json.dumps(data))
    got = capture(manifest, "metadata_import")
    assert got.received_bytes == manifest.stat().st_size + 5
    assert len(got.files) == 2
    real = s._Stage.read
    calls = [0]

    def changed(self, relative, *args, **kwargs):
        if relative == "hello.bin":
            calls[0] += 1
            if calls[0] == 2:
                (tmp_path / relative).write_bytes(b"other")
        return real(self, relative, *args, **kwargs)

    monkeypatch.setattr(s._Stage, "read", changed)
    with pytest.raises(s.SourceFailure):
        capture(manifest, "metadata_import")


@pytest.mark.skipif(os.name == "nt", reason="Windows pins opened files against rename")
def test_path_swap_during_read_and_vanished_root_fail_without_absence(tmp_path, monkeypatch):
    s, _ = readers()
    root = tmp_path / "root"
    original = skill(root)
    import os

    actual = os.read
    changed = [False]

    def swap(fd, amount):
        raw = actual(fd, amount)
        if raw and not changed[0]:
            changed[0] = True
            original.rename(root / "old")
            original.write_text("new bytes")
        return raw

    monkeypatch.setattr(os, "read", swap)
    with pytest.raises(s.SourceFailure):
        capture(root)
    monkeypatch.setattr(os, "read", actual)
    original.unlink()
    (root / "old").rename(original)
    stage_read = s._Stage.read

    def vanish(self, *args, **kwargs):
        got = stage_read(self, *args, **kwargs)
        root.rename(tmp_path / "gone")
        return got

    monkeypatch.setattr(s._Stage, "read", vanish)
    with pytest.raises(s.SourceFailure):
        capture(root)


def test_sparse_oversized_original_and_manifest_rejected_before_read(tmp_path):
    s, _ = readers()
    manifest, data = imported(tmp_path)
    with (tmp_path / "hello.bin").open("wb") as file:
        file.truncate(10 * 1024 * 1024 + 1)
    with pytest.raises(s.SourceFailure) as caught:
        capture(manifest, "metadata_import")
    assert caught.value.reason == "size_limit"
    assert caught.value.received_bytes == manifest.stat().st_size
    with manifest.open("wb") as file:
        file.truncate(4 * 1024 * 1024 + 1)
    with pytest.raises(s.SourceFailure) as caught:
        capture(manifest, "metadata_import")
    assert caught.value.received_bytes == 0


def test_import_total_bytes_and_combined_operation_limits(tmp_path):
    s, _ = readers()
    manifest, data = imported(tmp_path)
    empty_hash = hashlib.sha256(bytes(10 * 1024 * 1024)).hexdigest()
    template = data["records"][0]
    data["records"] = []
    for index in range(11):
        entry = json.loads(json.dumps(template))
        entry["record"]["asset_id"] = f"example:{index}"
        entry["record"]["content_hash"] = empty_hash
        entry["original_path"] = f"file{index}"
        with (tmp_path / entry["original_path"]).open("wb") as file:
            file.truncate(10 * 1024 * 1024)
        data["records"].append(entry)
    manifest.write_text(json.dumps(data))
    with pytest.raises(s.SourceFailure) as caught:
        capture(manifest, "metadata_import")
    assert caught.value.reason == "size_limit"
    assert caught.value.received_bytes > 100 * 1024 * 1024
    data["records"] = [template] * 501
    data["removed_asset_ids"] = [f"example:removed{i}" for i in range(500)]
    manifest.write_text(json.dumps(data))
    with pytest.raises(s.SourceFailure) as caught:
        capture(manifest, "metadata_import")
    assert caught.value.reason == "record_limit"


def test_frontmatter_precedence_manifest_validation_and_no_resource_reads(tmp_path):
    s, _ = readers()
    skill(
        tmp_path,
        metadata="metadata:\n  version: front\n  languages: [zh]\nresources: [/never-read]\n",
    )
    manifest = tmp_path / "skill.manifest.json"
    manifest.write_text('{"version":"fallback","languages":["en"],"resources":["/never-read"]}')
    got = capture(tmp_path)
    assert got.documents[0].record.source_version == "front"
    assert got.documents[0].record.language_tags == ("zh",)
    manifest.write_text('{"version":true}')
    with pytest.raises(s.SourceFailure):
        capture(tmp_path)
    manifest.write_text('{"version":"a","version":"b"}')
    with pytest.raises(s.SourceFailure):
        capture(tmp_path)


@pytest.mark.skipif(os.name == "nt", reason="colon filenames are POSIX-only")
def test_posix_original_name_may_contain_non_drive_colon(tmp_path):
    manifest, data = imported(tmp_path)
    (tmp_path / "hello.bin").rename(tmp_path / "hello:bytes")
    data["records"][0]["original_path"] = "hello:bytes"
    manifest.write_text(json.dumps(data))
    assert capture(manifest, "metadata_import").documents[0].record.content_hash == (
        "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    )


def test_import_records_are_processed_in_measured_batches_of_25(tmp_path):
    manifest, data = imported(tmp_path)
    template = data["records"][0]
    data["records"] = []
    for index in range(26):
        entry = json.loads(json.dumps(template))
        entry["record"]["asset_id"] = f"example:{index}"
        data["records"].append(entry)
    manifest.write_text(json.dumps(data))
    got = capture(manifest, "metadata_import")
    assert len(got.batch_durations_ms) == 2
    assert all(value >= 0 for value in got.batch_durations_ms)


def test_private_source_tree_reads_opened_files_and_keeps_root_identity(tmp_path):
    from relaymind.assets.source_io import open_source_tree

    (tmp_path / "hello").write_bytes(b"hello")
    tree = open_source_tree(tmp_path)
    try:
        assert list(tree.names()) == ["hello"]
        info = tree.info("hello")
        assert info.is_regular and not info.is_reparse and info.size == 5
        source_file = tree.open_file("hello")
        try:
            assert source_file.info().identity == info.identity
            assert source_file.read(2) == b"he"
            assert source_file.read(3) == b"llo"
        finally:
            source_file.close()
        tree.verify_root()
    finally:
        tree.close()


def test_windows_directory_buffer_bounds_and_native_layout_without_loading_dlls():
    import ctypes
    import struct

    from relaymind.assets._windows_source_io import _directory_names, _DirectoryEntry

    assert _DirectoryEntry.FileName.offset == 68
    name = "中文𐐀".encode("utf-16-le")
    data = bytearray(68 + len(name))
    struct.pack_into("<I", data, 60, len(name))
    data[68:] = name
    assert list(_directory_names(bytes(data))) == ["中文𐐀"]
    for bad in (bytes(10), bytes(data[:69]), struct.pack("<I", 1) + bytes(data[4:])):
        with pytest.raises(OSError):
            list(_directory_names(bad))
    assert ctypes.sizeof(_DirectoryEntry) >= 70


def test_explicit_null_frontmatter_version_retains_previous_generation(tmp_path):
    """RM34/AC25: only absent metadata version means unknown; explicit null is invalid."""
    from relaymind.store import RunStore
    from relaymind.store.asset_catalog import AssetCatalogStore

    s, _ = readers()
    skill(tmp_path, metadata="metadata:\n  version: valid\n")
    with RunStore() as store:
        catalog = AssetCatalogStore(store)
        catalog.publish(capture(tmp_path), expected_revision=0)
        first = catalog.snapshot()
        skill(tmp_path, metadata="metadata:\n  version: null\n")
        with pytest.raises(s.SourceFailure) as caught:
            capture(tmp_path, previous=catalog.inventory("example"))
        catalog.record_failure("example", caught.value)
        snapshot = catalog.snapshot()
        assert snapshot.catalog_revision == first.catalog_revision
        assert snapshot.assets[0].content_hash == first.assets[0].content_hash
        assert snapshot.assets[0].source_version == "valid"
        assert snapshot.assets[0].stale


def test_metadata_import_language_tag_must_match_whole_string(tmp_path):
    """RM34/AC25: ASCII language tags cannot acquire a trailing newline via imports."""
    s, _ = readers()
    manifest, data = imported(tmp_path)
    data["records"][0]["record"]["language_tags"] = ["en\n"]
    manifest.write_text(json.dumps(data))
    with pytest.raises(s.SourceFailure):
        capture(manifest, "metadata_import")


@pytest.mark.parametrize("deadline", [False, True])
def test_native_io_fence_keeps_unresponsive_operation_owned_until_completion(deadline):
    """RM34/AC25: cancellation requests cannot masquerade as completed native I/O."""
    import threading
    import time

    from relaymind.assets._windows_source_io import NativeIoAborted, NativeIoControl

    s, _ = readers()
    cancel, entered, release, requested = (threading.Event() for _ in range(4))
    closed, outcomes = [], []

    class Api:
        def current_thread_handle(self):
            return 777

        def cancel_synchronous(self, handle):
            assert handle == 777
            requested.set()
            return False  # ERROR_NOT_FOUND can race actual native request registration.

        def close(self, handle):
            closed.append(handle)

    def worker():
        context = s.CaptureContext(datetime(2026, 10, 29, tzinfo=UTC), cancelled=cancel.is_set)
        if deadline:
            context.deadline = time.monotonic() + 0.03
        controller = NativeIoControl(Api(), context)

        def unresponsive_driver():
            entered.set()
            release.wait(timeout=2)
            return 999

        try:
            controller.invoke(unresponsive_driver, discard=closed.append)
        except NativeIoAborted as exc:
            outcomes.append(exc.reason)
        finally:
            controller.close()

    owned = threading.Thread(target=worker)
    owned.start()
    assert entered.wait(1)
    if not deadline:
        cancel.set()
    assert requested.wait(1)
    assert owned.is_alive()
    assert not outcomes
    release.set()
    owned.join(timeout=1)
    assert not owned.is_alive()
    assert outcomes == ["deadline" if deadline else "cancelled"]
    assert closed.count(999) == 1
    assert closed.count(777) == 1
    assert not any(t.name == "asset-native-cancel" and t.is_alive() for t in threading.enumerate())


@pytest.mark.parametrize(
    "status,race_cancel,owned_result",
    [
        (0x108, False, True),
        (-1073741790, False, False),
        (0, True, True),
    ],
)
def test_native_create_rejects_alternate_or_cancelled_success_with_exact_handle_ownership(
    status, race_cancel, owned_result
):
    """RM34/AC25: informational/late success never becomes a usable source handle."""
    import ctypes
    import threading

    from relaymind.assets._windows_source_io import NativeIoControl, _Api

    s, _ = readers()
    cancelled = threading.Event()
    closed = []
    api = object.__new__(_Api)
    api.close = closed.append
    api.current_thread_handle = lambda: 777
    api.cancel_synchronous = lambda handle: False
    api.error_code = lambda value: 5

    def native_open(out_handle, *args):
        ctypes.cast(out_handle, ctypes.POINTER(ctypes.c_void_p))[0] = 999
        if race_cancel:
            cancelled.set()
        return status

    api.open = native_open
    context = s.CaptureContext(datetime(2026, 10, 29, tzinfo=UTC), cancelled=cancelled.is_set)
    api.control = NativeIoControl(api, context)
    try:
        with pytest.raises(OSError):
            api.create("SKILL.md", 1, read=True, directory=False)
    finally:
        api.control.close()
    assert closed.count(999) == int(owned_result)
    assert closed.count(777) == 1


def test_reused_physical_original_is_hashed_once_for_all_declared_records(tmp_path, monkeypatch):
    """RM34/AC25: one staged physical original produces one byte hash, not one per alias."""
    s, _ = readers()
    manifest, data = imported(tmp_path)
    os.link(tmp_path / "hello.bin", tmp_path / "hello-alias.bin")
    second = json.loads(json.dumps(data["records"][0]))
    second["record"]["asset_id"] = "example:second"
    second["original_path"] = "hello-alias.bin"
    data["records"].append(second)
    manifest.write_text(json.dumps(data))
    original_sha256 = s.hashlib.sha256
    observed_inputs = []

    class ObservedHash:
        def __init__(self, initial=b""):
            self.hash = original_sha256(initial)
            self.input = bytearray(initial)
            observed_inputs.append(self.input)

        def update(self, chunk):
            self.input.extend(chunk)
            self.hash.update(chunk)

        def hexdigest(self):
            return self.hash.hexdigest()

    monkeypatch.setattr(s.hashlib, "sha256", ObservedHash)
    captured = capture(manifest, "metadata_import")
    assert captured.received_bytes == manifest.stat().st_size + 5
    assert sum(value == b"hello" for value in observed_inputs) == 1
    assert {d.record.content_hash for d in captured.documents} == {
        "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    }
