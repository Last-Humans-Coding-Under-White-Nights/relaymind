"""RM34/AC25: stage ordinary local packages and closed imports without authority."""

import hashlib
import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from relaymind.assets import sources
from relaymind.assets.settings import SourceConfig
from relaymind.canonical import canonical_digest
from test_asset_catalog import record


def capture(path, kind="local_directory", *, source_id="example", previous=None, **context):
    return sources.capture_source(
        SourceConfig(source_id, kind, path),
        previous=previous or sources.SourceInventory.empty(source_id),
        context=sources.CaptureContext(datetime(2026, 10, 29, tzinfo=UTC), **context),
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
    assert sources.SourceInventory.empty("example").records == ()


def package_id(package, source_id="example"):
    return "skill:" + canonical_digest({"source_id": source_id, "relative_package_path": package})


def test_root_entry_prevents_child_fallback_even_when_invalid(tmp_path):
    """RM34/AC25, spec 0.66 section 4.6.1: an invalid root is a rejected candidate."""
    skill(tmp_path / "child")
    (tmp_path / "SKILL.md").write_text("invalid")
    got = capture(tmp_path)
    assert got.documents == ()
    assert not got.complete
    assert got.rejected_count == 1
    assert got.exclusions == ({"asset_id": package_id("."), "reason": "invalid_data"},)
    skill(tmp_path)
    assert len(capture(tmp_path).documents) == 1


def test_invalid_root_after_a_child_generation_is_a_partial_generation(tmp_path):
    """RM34/AC25, spec 0.66 section 4.6.1: a rejected root never aborts the scan.

    The prior child record is omitted because the root takes precedence; it
    stays (stale) in a partial generation and is never removed.
    """
    skill(tmp_path / "child")
    first = capture(tmp_path)
    previous = sources.SourceInventory(
        "example",
        1,
        tuple(
            sources.InventoryAsset(d.record, first.package_paths[d.record.asset_id])
            for d in first.documents
        ),
        owners={d.record.asset_id: "example" for d in first.documents},
    )
    (tmp_path / "SKILL.md").write_text("invalid")
    got = capture(tmp_path, previous=previous)
    assert got.documents == ()
    assert not got.complete
    assert got.rejected_count == 1
    assert got.exclusions == ({"asset_id": package_id("."), "reason": "invalid_data"},)
    assert got.removed_asset_ids == frozenset()


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ("---\nname: a\nname: b\ndescription: x\n---\nbody", "invalid_data"),
        ("---\nname: &a hello\ndescription: *a\n---\nbody", "invalid_data"),
        ("---\nname: !!str hello\ndescription: x\n---\nbody", "invalid_data"),
        ("---\nname: a\ndescription: x\nmetadata: []\n---\nbody", "invalid_data"),
        ("---\nname: a\ndescription: x\nmetadata:\n  version: 1\n---\nbody", "invalid_data"),
        (
            "---\nname: a\ndescription: x\nmetadata:\n  languages: [en, en]\n---\nbody",
            "invalid_data",
        ),
        ("---\nname: a\ndescription: x\n---\n", "invalid_data"),
        ("---\nname: a\ndescription: <script>unsafe\n---\nbody", "invalid_data"),
        ("---\nname: <b>a</b>\ndescription: x\n---\nbody", "invalid_data"),
        ("---\nname: a\ndescription: x\n---\nbad \x01 control", "invalid_data"),
        ("---\nname: a\ndescription: x\n---\n" + "x" * (256 * 1024 + 1), "size_limit"),
        (
            "---\nname: a\ndescription: x\nextra: " + "[" * 17 + "0" + "]" * 17 + "\n---\nbody",
            "invalid_data",
        ),
    ],
)
def test_unsafe_or_malformed_candidate_is_rejected_on_its_own(tmp_path, payload, reason):
    """RM34/AC25, spec 0.66 section 4.6.1: a rejected file never hides its siblings."""
    (tmp_path / "bad").mkdir()
    (tmp_path / "bad" / "SKILL.md").write_text(payload, newline="\n")
    skill(tmp_path / "good")
    got = capture(tmp_path)
    assert [d.record.asset_id for d in got.documents] == [package_id("good")]
    assert not got.complete
    assert got.rejected_count == 1
    assert got.exclusions == ({"asset_id": package_id("bad"), "reason": reason},)
    assert got.removed_asset_ids == frozenset()
    assert "<script>" not in json.dumps([dict(e) for e in got.exclusions])


def test_body_markup_and_repeated_fences_stay_internal_data(tmp_path):
    """RM34/AC25, spec 0.66 sections 4.6 and 14.5: body text is bounded, not markup-checked."""
    body = "Run `tool <id>`.\n" + "```bash\nx\n```\n" * 5 + "<script>data</script>\n"
    skill(tmp_path, body=body)
    got = capture(tmp_path)
    assert got.complete
    assert got.rejected_count == 0
    assert got.documents[0].body == body
    # The offered fields keep the projection rules: repetition in a description rejects.
    skill(tmp_path, description='"a\\na\\na\\na"')
    assert capture(tmp_path).exclusions[0]["reason"] == "invalid_data"


@pytest.mark.parametrize("with_manifest", [False, True])
def test_invalid_utf8_skill_rejects_only_its_candidate(tmp_path, with_manifest):
    """RM34/AC25: invalid UTF-8 is a candidate rejection, not a source failure."""
    (tmp_path / "bad").mkdir()
    (tmp_path / "bad" / "SKILL.md").write_bytes(b"\xff\xfe not utf-8")
    if with_manifest:
        (tmp_path / "bad" / "skill.manifest.json").write_text('{"version":"1"}')
    skill(tmp_path / "good")

    got = capture(tmp_path)

    assert [d.record.asset_id for d in got.documents] == [package_id("good")]
    assert not got.complete
    assert got.rejected_count == 1
    assert got.exclusions == ({"asset_id": package_id("bad"), "reason": "invalid_data"},)
    assert got.removed_asset_ids == frozenset()


# Verbatim copies of OpenClaw 2026.10.7 bundled skills (package `openclaw`, MIT).
BUNDLED = Path(__file__).parent / "fixtures" / "openclaw-bundled-skills"
BUNDLED_SHA256 = {
    "diagram-maker": "6195e03fcb04a1a68c4af72165e0c85efcd2719af79380d82e42996ea89ad74a",
    "gemini": "1aad57691db927cf26b489e15bab4eb909e390ebca361c128f98ab5bcf4bdf43",
    "sherpa-onnx-tts": "f3a4d30d0e7e14e76dcb5245e197e3dcf490e3f9e8298466e86e4f33847f532d",
    "tmux": "8aeaa6f3dfe06d58e09721778b8c9e654bcbd47590f26f442c4dc87c9349c572",
    "weather": "62ab4821aa873949d1c1091836be1659a42b32caadce4bd145f5505a1ceaeec1",
}


def bundled(root):
    shutil.copytree(BUNDLED, root, ignore=shutil.ignore_patterns("LICENSE"))
    return root


def test_openclaw_bundled_skills_are_admitted(tmp_path):
    """RM34/AC25 (#268): placeholders, HTML examples and repeated fences in real bodies."""
    root = bundled(tmp_path / "skills")
    got = capture(root)
    assert got.complete
    assert got.rejected_count == 0
    by_name = {d.record.identity["package_id"]: d for d in got.documents}
    assert sorted(by_name) == sorted(BUNDLED_SHA256)
    for name, digest in BUNDLED_SHA256.items():
        assert by_name[name].record.content_hash == digest
        assert by_name[name].record.availability == "unknown"
        assert by_name[name].record.asset_id == package_id(name)
    assert "gemini skills <command>" in by_name["gemini"].body
    assert "<!-- SVG -->" in by_name["diagram-maker"].body
    assert by_name["tmux"].body.count("```bash\n") > 3


def test_one_bad_file_admits_the_good_ones_and_retains_its_prior_record(tmp_path):
    """RM34/AC25 (#268): partial local scan; no removal while any candidate is rejected."""
    root = bundled(tmp_path / "skills")
    first = capture(root)
    previous = sources.SourceInventory(
        "example",
        1,
        tuple(
            sources.InventoryAsset(d.record, first.package_paths[d.record.asset_id])
            for d in first.documents
        ),
        owners={d.record.asset_id: "example" for d in first.documents},
    )
    (root / "gemini" / "SKILL.md").write_text("---\nname: gemini\n---\n", newline="\n")
    shutil.rmtree(root / "weather")
    (root / "not-a-skill").mkdir()
    got = capture(root, previous=previous)
    assert not got.complete
    assert sorted(d.record.identity["package_id"] for d in got.documents) == [
        "diagram-maker",
        "sherpa-onnx-tts",
        "tmux",
    ]
    assert got.rejected_count == 1
    assert got.exclusions == ({"asset_id": package_id("gemini"), "reason": "invalid_data"},)
    # A partial generation never removes: weather stays (stale) until a complete scan.
    assert got.removed_asset_ids == frozenset()
    (root / "gemini" / "SKILL.md").write_bytes((BUNDLED / "gemini" / "SKILL.md").read_bytes())
    healed = capture(root, previous=previous)
    assert healed.complete
    assert healed.removed_asset_ids == frozenset({package_id("weather")})


@pytest.mark.parametrize(
    "bad_skill",
    [
        b"\xff\xfe not utf-8",
        b"---\nname: a\n---\nmissing description\n",
        b"---\nname: a\ndescription: x\n---\n" + b"x" * (10 * 1024 * 1024 + 1),
    ],
)
def test_symlinked_manifest_beside_a_rejected_skill_fails_the_whole_scan(tmp_path, bad_skill):
    """RM34/AC25, spec 0.66 section 4.6.1: path safety is checked before content admission.

    A malformed SKILL.md must not hide a symlinked optional manifest and turn a
    path-safety failure into a partial generation.
    """
    root = tmp_path / "skills"
    skill(root / "good")
    (root / "bad").mkdir()
    (root / "bad" / "SKILL.md").write_bytes(bad_skill)
    outside = tmp_path / "outside-manifest.json"
    outside.write_text('{"version":"1"}')
    symlink(root / "bad" / "skill.manifest.json", outside)
    with pytest.raises(sources.SourceFailure) as caught:
        capture(root)
    assert caught.value.reason == "invalid_data"
    assert not isinstance(caught.value, sources.CandidateRejected)


def test_scan_failure_after_a_rejected_candidate_keeps_its_rejection(tmp_path):
    """RM34/AC25, spec 0.66 section 4.6.1: a failed scan keeps its rejection evidence."""
    from relaymind.store import RunStore
    from relaymind.store.asset_catalog import AssetCatalogStore

    root = tmp_path / "skills"
    (root / "bad").mkdir(parents=True)
    (root / "bad" / "SKILL.md").write_bytes(b"\xff\xfe not utf-8")
    skill(root / "good")
    outside = tmp_path / "outside-manifest.json"
    outside.write_text('{"version":"1"}')
    symlink(root / "good" / "skill.manifest.json", outside)
    with pytest.raises(sources.SourceFailure) as caught:
        capture(root)
    failure = caught.value
    assert failure.reason == "invalid_data"
    assert not isinstance(failure, sources.CandidateRejected)
    assert failure.rejected_count == 2
    assert failure.exclusions == (
        {"asset_id": package_id("bad"), "reason": "invalid_data"},
        {"asset_id": None, "reason": "invalid_data"},
    )
    with RunStore() as store:
        catalog = AssetCatalogStore(store)
        catalog.record_failure(
            "example", rejected_count=failure.rejected_count, exclusions=failure.exclusions
        )
        source = catalog.snapshot().sources[0]
        assert source["rejected_count"] == 2
        assert source["exclusions"][0] == {"asset_id": package_id("bad"), "reason": "invalid_data"}


def test_scan_level_failures_still_fail_the_whole_scan(tmp_path):
    """RM34/AC25: path safety, budgets and a missing root are not per-file rejections."""
    root = bundled(tmp_path / "skills")
    for index in range(1000):
        (root / f"extra-{index:04d}").mkdir()
    with pytest.raises(sources.SourceFailure) as caught:
        capture(root)
    assert caught.value.reason == "record_limit"
    with pytest.raises(sources.SourceFailure) as caught:
        capture(tmp_path / "missing")
    assert caught.value.reason in ("source_unavailable", "invalid_data")


def test_oversized_original_is_a_rejected_candidate_but_job_bytes_stay_whole(tmp_path):
    """RM34/AC25: the 10 MiB SKILL.md cap is per file; the 100 MiB job cap is per scan."""
    root = bundled(tmp_path / "skills")
    (root / "huge").mkdir()
    with (root / "huge" / "SKILL.md").open("wb") as handle:
        handle.truncate(10 * 1024 * 1024 + 1)
    got = capture(root)
    assert got.exclusions == ({"asset_id": package_id("huge"), "reason": "size_limit"},)
    assert len(got.documents) == len(BUNDLED_SHA256)


def grow_while_reading(monkeypatch, target, size):
    """Extend target past size after its first chunk is read (after the size check)."""
    actual = os.read
    inode = target.stat().st_ino
    grown = [False]

    def read(fd, amount):
        raw = actual(fd, amount)
        if raw and not grown[0] and os.fstat(fd).st_ino == inode:
            grown[0] = True
            with target.open("r+b") as handle:
                handle.truncate(size + 1)
        return raw

    monkeypatch.setattr(os, "read", read)
    return grown


@pytest.mark.skipif(os.name == "nt", reason="POSIX read path")
def test_growth_past_the_limit_during_reading_fails_the_whole_scan(tmp_path, monkeypatch):
    """RM34/AC25, spec 0.66 section 4.6.1: a change during capture is not a rejection."""
    root = tmp_path / "skills"
    skill(root / "good")
    growing = skill(root / "growing")
    grown = grow_while_reading(monkeypatch, growing, 10 * 1024 * 1024)
    with pytest.raises(sources.SourceFailure) as caught:
        capture(root)
    assert grown[0]
    assert not isinstance(caught.value, sources.CandidateRejected)
    assert caught.value.reason == "source_unavailable"


def test_a_changed_oversized_candidate_fails_the_whole_scan(tmp_path, monkeypatch):
    """RM34/AC25, spec 0.66 section 4.6.1: a rejected file is still verified as unchanged."""
    root = tmp_path / "skills"
    skill(root / "good")
    (root / "huge").mkdir()
    huge = root / "huge" / "SKILL.md"
    with huge.open("wb") as handle:
        handle.truncate(10 * 1024 * 1024 + 1)
    real = sources._Stage.verify

    def changed(self):
        with huge.open("r+b") as handle:
            handle.truncate(10 * 1024 * 1024 + 2)
        return real(self)

    monkeypatch.setattr(sources._Stage, "verify", changed)
    with pytest.raises(sources.SourceFailure) as caught:
        capture(root)
    assert caught.value.reason == "source_unavailable"


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
    with pytest.raises(sources.SourceFailure):
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
        with pytest.raises(sources.SourceFailure):
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
    with pytest.raises(sources.SourceFailure):
        capture(manifest, "metadata_import")


def test_local_limits_cancel_and_deadline_abort_before_publication(tmp_path):
    skill(tmp_path, body="x" * (256 * 1024 + 1))
    # Spec 0.66 section 4.6.1: a per-file bound rejects that candidate only.
    oversized = capture(tmp_path)
    assert oversized.documents == () and not oversized.complete
    assert oversized.exclusions[0]["reason"] == "size_limit"
    skill(tmp_path)
    with pytest.raises(sources.SourceFailure) as caught:
        capture(tmp_path, cancelled=lambda: True)
    assert caught.value.reason == "cancelled"
    ticks = iter([0.0, 61.0])
    with pytest.raises(sources.SourceFailure) as caught:
        capture(tmp_path, monotonic=lambda: next(ticks, 61.0))
    assert caught.value.reason == "deadline"
    (tmp_path / "SKILL.md").unlink()
    for index in range(1001):
        (tmp_path / str(index)).touch()
    with pytest.raises(sources.SourceFailure) as caught:
        capture(tmp_path)
    assert caught.value.reason == "record_limit"


def test_reused_original_is_hashed_once_but_changed_capture_rejected(tmp_path, monkeypatch):
    manifest, data = imported(tmp_path)
    second = json.loads(json.dumps(data["records"][0]))
    second["record"]["asset_id"] = "example:second"
    data["records"].append(second)
    manifest.write_text(json.dumps(data))
    got = capture(manifest, "metadata_import")
    assert got.received_bytes == manifest.stat().st_size + 5
    assert len(got.files) == 2
    real = sources._Stage.read
    calls = [0]

    def changed(self, relative, *args, **kwargs):
        if relative == "hello.bin":
            calls[0] += 1
            if calls[0] == 2:
                (tmp_path / relative).write_bytes(b"other")
        return real(self, relative, *args, **kwargs)

    monkeypatch.setattr(sources._Stage, "read", changed)
    with pytest.raises(sources.SourceFailure):
        capture(manifest, "metadata_import")


@pytest.mark.skipif(os.name == "nt", reason="Windows pins opened files against rename")
def test_path_swap_during_read_and_vanished_root_fail_without_absence(tmp_path, monkeypatch):
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
    with pytest.raises(sources.SourceFailure):
        capture(root)
    monkeypatch.setattr(os, "read", actual)
    original.unlink()
    (root / "old").rename(original)
    stage_read = sources._Stage.read

    def vanish(self, *args, **kwargs):
        got = stage_read(self, *args, **kwargs)
        root.rename(tmp_path / "gone")
        return got

    monkeypatch.setattr(sources._Stage, "read", vanish)
    with pytest.raises(sources.SourceFailure):
        capture(root)


def test_sparse_oversized_original_and_manifest_rejected_before_read(tmp_path):
    manifest, data = imported(tmp_path)
    with (tmp_path / "hello.bin").open("wb") as file:
        file.truncate(10 * 1024 * 1024 + 1)
    with pytest.raises(sources.SourceFailure) as caught:
        capture(manifest, "metadata_import")
    assert caught.value.reason == "size_limit"
    assert caught.value.received_bytes == manifest.stat().st_size
    with manifest.open("wb") as file:
        file.truncate(4 * 1024 * 1024 + 1)
    with pytest.raises(sources.SourceFailure) as caught:
        capture(manifest, "metadata_import")
    assert caught.value.received_bytes == 0


def test_import_total_bytes_and_combined_operation_limits(tmp_path):
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
    with pytest.raises(sources.SourceFailure) as caught:
        capture(manifest, "metadata_import")
    assert caught.value.reason == "size_limit"
    assert caught.value.received_bytes > 100 * 1024 * 1024
    data["records"] = [template] * 501
    data["removed_asset_ids"] = [f"example:removed{i}" for i in range(500)]
    manifest.write_text(json.dumps(data))
    with pytest.raises(sources.SourceFailure) as caught:
        capture(manifest, "metadata_import")
    assert caught.value.reason == "record_limit"


def test_frontmatter_precedence_manifest_validation_and_no_resource_reads(tmp_path):
    skill(
        tmp_path,
        metadata="metadata:\n  version: front\n  languages: [zh]\nresources: [/never-read]\n",
    )
    manifest = tmp_path / "skill.manifest.json"
    manifest.write_text('{"version":"fallback","languages":["en"],"resources":["/never-read"]}')
    got = capture(tmp_path)
    assert got.documents[0].record.source_version == "front"
    assert got.documents[0].record.language_tags == ("zh",)
    # Spec 0.66 section 4.6.1: an invalid optional manifest rejects its package.
    for invalid in ('{"version":true}', '{"version":"a","version":"b"}'):
        manifest.write_text(invalid)
        got = capture(tmp_path)
        assert got.documents == () and not got.complete
        assert got.exclusions == ({"asset_id": package_id("."), "reason": "invalid_data"},)


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        pytest.param(b"[]", "invalid_data", id="non-object"),
        pytest.param(b"\xff\xfe", "invalid_data", id="invalid-utf8"),
        pytest.param(None, "size_limit", id="oversized"),
    ],
)
def test_invalid_optional_manifest_rejects_only_its_candidate(tmp_path, payload, reason):
    """RM34/AC25: optional manifest JSON, encoding and size checks are per package."""
    skill(tmp_path / "bad")
    skill(tmp_path / "good")
    manifest = tmp_path / "bad" / "skill.manifest.json"
    if payload is None:
        with manifest.open("wb") as handle:
            handle.truncate(4 * 1024 * 1024 + 1)
    else:
        manifest.write_bytes(payload)

    got = capture(tmp_path)

    assert [d.record.asset_id for d in got.documents] == [package_id("good")]
    assert not got.complete
    assert got.rejected_count == 1
    assert got.exclusions == ({"asset_id": package_id("bad"), "reason": reason},)
    assert got.removed_asset_ids == frozenset()


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

    skill(tmp_path, metadata="metadata:\n  version: valid\n")
    with RunStore() as store:
        catalog = AssetCatalogStore(store)
        catalog.publish(capture(tmp_path), expected_revision=0)
        first = catalog.snapshot()
        skill(tmp_path, metadata="metadata:\n  version: null\n")
        partial = capture(tmp_path, previous=catalog.inventory("example"))
        assert partial.documents == () and not partial.complete
        catalog.publish(partial, expected_revision=first.catalog_revision)
        snapshot = catalog.snapshot()
        # Spec 0.66 section 4.6.1: the rejected package's prior record is kept, stale.
        assert snapshot.catalog_revision == first.catalog_revision + 1
        assert snapshot.assets[0].content_hash == first.assets[0].content_hash
        assert snapshot.assets[0].source_version == "valid"
        assert snapshot.assets[0].stale
        source = snapshot.sources[0]
        assert not source["complete"] and not source["stale"]
        assert source["rejected_count"] == 1
        assert source["exclusions"] == [{"asset_id": package_id("."), "reason": "invalid_data"}]
        assert source["accepted_count"] == 1
        # Repairing the file restores one complete generation without the exclusion.
        skill(tmp_path, metadata="metadata:\n  version: valid\n")
        catalog.publish(
            capture(tmp_path, previous=catalog.inventory("example")),
            expected_revision=snapshot.catalog_revision,
        )
        repaired = catalog.snapshot()
        assert repaired.sources[0]["complete"] and repaired.sources[0]["exclusions"] == []
        assert repaired.sources[0]["rejected_count"] == 0
        assert not repaired.assets[0].stale


def test_metadata_import_language_tag_must_match_whole_string(tmp_path):
    """RM34/AC25: ASCII language tags cannot acquire a trailing newline via imports."""
    manifest, data = imported(tmp_path)
    data["records"][0]["record"]["language_tags"] = ["en\n"]
    manifest.write_text(json.dumps(data))
    with pytest.raises(sources.SourceFailure):
        capture(manifest, "metadata_import")


@pytest.mark.parametrize("deadline", [False, True])
def test_native_io_fence_keeps_unresponsive_operation_owned_until_completion(deadline):
    """RM34/AC25: cancellation requests cannot masquerade as completed native I/O."""
    import threading
    import time

    from relaymind.assets._windows_source_io import NativeIoAborted, NativeIoControl

    cancel, entered, release, requested = (threading.Event() for _ in range(4))
    closed, outcomes = [], []
    started_at = None

    def in_flight_clock():
        return 0.0 if started_at is None else time.monotonic() - started_at

    class Api:
        def current_thread_handle(self):
            if deadline:
                # Exercise a cold handle/monitor setup beyond the in-flight budget.
                time.sleep(0.05)
            return 777

        def cancel_synchronous(self, handle):
            assert handle == 777
            requested.set()
            return False  # ERROR_NOT_FOUND can race actual native request registration.

        def close(self, handle):
            closed.append(handle)

    def worker():
        context = sources.CaptureContext(
            datetime(2026, 10, 29, tzinfo=UTC),
            monotonic=in_flight_clock if deadline else time.monotonic,
            cancelled=cancel.is_set,
        )
        if deadline:
            context.deadline = 0.03
        controller = NativeIoControl(Api(), context)

        def unresponsive_driver():
            nonlocal started_at
            started_at = time.monotonic()
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


def test_native_io_deadline_expired_before_invoke_skips_driver():
    """RM34/AC25: an expired source deadline aborts before native I/O begins."""
    from relaymind.assets._windows_source_io import NativeIoAborted, NativeIoControl

    closed = []

    class Api:
        def current_thread_handle(self):
            return 777

        def cancel_synchronous(self, handle):
            raise AssertionError("no native request was registered")

        def close(self, handle):
            closed.append(handle)

    context = sources.CaptureContext(datetime(2026, 10, 29, tzinfo=UTC))
    context.deadline = context.monotonic() - 1
    controller = NativeIoControl(Api(), context)
    try:
        with pytest.raises(NativeIoAborted, match="deadline"):
            controller.invoke(lambda: pytest.fail("expired operation entered the driver"))
    finally:
        controller.close()
    assert closed == [777]


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
    context = sources.CaptureContext(datetime(2026, 10, 29, tzinfo=UTC), cancelled=cancelled.is_set)
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
    manifest, data = imported(tmp_path)
    os.link(tmp_path / "hello.bin", tmp_path / "hello-alias.bin")
    second = json.loads(json.dumps(data["records"][0]))
    second["record"]["asset_id"] = "example:second"
    second["original_path"] = "hello-alias.bin"
    data["records"].append(second)
    manifest.write_text(json.dumps(data))
    original_sha256 = sources.hashlib.sha256
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

    monkeypatch.setattr(sources.hashlib, "sha256", ObservedHash)
    captured = capture(manifest, "metadata_import")
    assert captured.received_bytes == manifest.stat().st_size + 5
    assert sum(value == b"hello" for value in observed_inputs) == 1
    assert {d.record.content_hash for d in captured.documents} == {
        "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    }


@pytest.mark.skipif(os.name == "nt", reason="POSIX ancestor symlink semantics")
def test_symlinked_ancestor_is_resolved_but_a_symlinked_root_is_refused(tmp_path):
    """RM34/AC25, spec 14.5: only the configured root itself must not be a symlink.

    A configured path under a symlinked folder (such as /tmp on macOS) is scanned;
    the root and everything below it are still opened without following links.
    """
    real = tmp_path / "real"
    skill(real / "skills")
    alias = tmp_path / "alias"
    symlink(alias, real, directory=True)
    assert capture(alias / "skills").documents
    symlink(real / "root-link", real / "skills", directory=True)
    with pytest.raises(sources.SourceFailure):
        capture(alias / "root-link")


def test_json_manifest_rejects_non_finite_numbers():
    """RM34/AC25: 1e999 parses to infinity; a staged manifest must not carry it."""
    with pytest.raises(sources.SourceFailure):
        sources._json(b'{"version": 1e999}')
    with pytest.raises(sources.SourceFailure):
        sources._json(b'{"version": NaN}')


def test_searchable_text_is_nfc_but_identifiers_and_paths_stay_exact(tmp_path):
    """RM34/AC25: the searchable projection is NFC (spec 4.6); identities are exact.

    Asset/source IDs are opaque identities and original_path must open the exact
    staged file, so neither is normalized: NFD and NFC names can be distinct files.
    """
    import unicodedata

    nfd = unicodedata.normalize("NFD", "Café")
    nfc = unicodedata.normalize("NFC", "Café")
    assert nfd != nfc

    # Local folder: description and instruction body are projected as NFC.
    skill(tmp_path / "local", description=f"{nfd} greeting", body=f"Say {nfd}.")
    document = capture(tmp_path / "local").documents[0]
    assert document.record.description == f"{nfc} greeting"
    assert document.body == f"Say {nfc}."

    # Import: an NFD asset_id, package_id and original_path are kept exactly.
    root = tmp_path / "import"
    root.mkdir()
    (root / f"{nfd}.bin").write_bytes(b"hello")
    value = record(asset_id=f"example:{nfd}", description=f"{nfd} tool")
    value["identity"]["package_id"] = nfd
    for key in ("schema_version", "source_id", "availability", "stale"):
        value.pop(key)
    manifest, _ = imported(root, records=[{"record": value, "original_path": f"{nfd}.bin"}])
    got = capture(manifest, "metadata_import")
    document = got.documents[0]
    assert document.record.asset_id == f"example:{nfd}"
    assert document.record.identity["package_id"] == nfd
    assert document.record.description == f"{nfc} tool"
    paths = [f.relative_path for f in got.files]
    assert f"{nfd}.bin" in paths and f"{nfc}.bin" not in paths


@pytest.mark.parametrize("reason,rejected_count", [("cancelled", 0), ("deadline", 1)])
def test_native_abort_preserves_honest_rejection_count(
    tmp_path, monkeypatch, reason, rejected_count
):
    """RM16, RM34/AC25: native cancellation retains content without rejecting an asset."""
    from relaymind.assets.source_io import NativeIoAborted
    from relaymind.store import RunStore
    from relaymind.store.asset_catalog import AssetCatalogStore

    skill(tmp_path)
    with RunStore() as store:
        catalog = AssetCatalogStore(store)
        catalog.publish(capture(tmp_path), expected_revision=0)
        original = catalog.snapshot()

        def aborted(*args, **kwargs):
            raise NativeIoAborted(reason)

        monkeypatch.setattr(sources, "open_source_tree", aborted)
        with pytest.raises(sources.SourceFailure) as caught:
            capture(tmp_path, previous=catalog.inventory("example"), received_bytes=17)
        failure = caught.value
        assert failure.reason == reason
        assert failure.received_bytes == 17
        catalog.record_failure(
            "example", rejected_count=failure.rejected_count, exclusions=failure.exclusions
        )
        observed = catalog.snapshot()
        assert observed.assets[0].content_hash == original.assets[0].content_hash
        assert observed.assets[0].stale
        assert observed.sources[0]["rejected_count"] == rejected_count
