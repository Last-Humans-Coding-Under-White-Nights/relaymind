"""Disposable capture fixtures, never Matrix assets (RM07, RM12, RM16, RM25; #188)."""

import hashlib
import json
import subprocess
import sys

from helpers import SCRIPTS_DIR

SCRIPT = SCRIPTS_DIR / "probes" / "qualify_matrix.py"


def capture(tmp_path):
    """Invented opaque bytes; the probe must never import, extract, or execute them."""
    original = tmp_path / "original.zip"
    original.write_bytes(b"fixture bytes\r\n")
    manifest = tmp_path / "capture.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": "matrix-capture-probe.v1",
                "assets": [
                    {
                        "source_id": "matrix",
                        "asset_id": "fixture-id",
                        "name": "fixture-name",
                        "version": "fixture-v1",
                        "captured_at": "2026-10-29T00:00:00Z",
                        "source_url": "https://matrix.openharmony.cn/#/skillSquare/details?id=fixture-id",
                        "download_url": "https://matrix.openharmony.cn/api/registry/skill/fixture-name/install?format=zip",
                        "original_path": "original.zip",
                        "sha256": (
                            "b62f439144b692e28347af401c78cbb951515182d5ae3c91543452ae40662fcd"
                        ),
                    }
                ],
            }
        )
    )
    # This literal guards a manually checked disposable fixture, not the probe's hashing.
    assert hashlib.sha256(original.read_bytes()).hexdigest() == (
        "b62f439144b692e28347af401c78cbb951515182d5ae3c91543452ae40662fcd"
    )
    return manifest, original


def run_probe(manifest, output):
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--manifest",
            str(manifest),
            "--capture-root",
            str(manifest.parent),
            "--output",
            str(output),
        ],
        text=True,
        capture_output=True,
        check=False,
    )


def test_repeated_capture_preserves_ids_and_original_hashes(tmp_path):
    """RM07/RM25: repeated offline derivation preserves source identities and bytes."""
    manifest, original = capture(tmp_path)
    output = tmp_path / "report.json"
    first = run_probe(manifest, output)
    assert first.returncode == 0, first.stderr
    report = json.loads(output.read_text())
    assert report["asset_count"] == 1
    row = report["assets"][0]
    assert (row["source_id"], row["asset_id"], row["version"]) == (
        "matrix",
        "fixture-id",
        "fixture-v1",
    )
    assert row["sha256"] == "b62f439144b692e28347af401c78cbb951515182d5ae3c91543452ae40662fcd"
    assert row["size_bytes"] == 15
    assert run_probe(manifest, output).returncode == 0
    assert json.loads(output.read_text()) == report
    assert original.read_bytes() == b"fixture bytes\r\n"


def rewrite(manifest, change):
    data = json.loads(manifest.read_text())
    change(data)
    manifest.write_text(json.dumps(data))


def assert_rejected(manifest, output, reason):
    result = run_probe(manifest, output)
    assert result.returncode == 2, result.stderr
    assert reason in result.stderr
    assert not output.exists()


def test_duplicate_source_identity_is_rejected(tmp_path):
    """RM07/RM16: a repeated source ID cannot inflate the distinct asset count."""
    manifest, _ = capture(tmp_path)
    rewrite(manifest, lambda data: data["assets"].append(dict(data["assets"][0])))
    assert_rejected(manifest, tmp_path / "report.json", "duplicate source identity")


def test_corrupt_capture_is_rejected(tmp_path):
    """RM07: original bytes must agree with the recorded capture hash."""
    manifest, original = capture(tmp_path)
    original.write_bytes(b"corrupt")
    assert_rejected(manifest, tmp_path / "report.json", "hash mismatch")


def test_oversized_capture_is_rejected_before_reading(tmp_path):
    """RM12: bounded inspection refuses a large opaque archive without extracting it."""
    manifest, original = capture(tmp_path)
    with original.open("r+b") as file:
        file.truncate(10 * 1024 * 1024 + 1)
    assert_rejected(manifest, tmp_path / "report.json", "asset exceeds byte limit")


def test_missing_license_is_unknown_not_permission(tmp_path):
    """RM16: an absent license never becomes permission to redistribute."""
    manifest, _ = capture(tmp_path)
    output = tmp_path / "report.json"
    assert run_probe(manifest, output).returncode == 0
    row = json.loads(output.read_text())["assets"][0]
    assert row["license"] == "unknown"
    assert row["attribution"] is None
    assert row["redistribution_permission"] == "not_assessed"


def test_source_bytes_are_not_executed_or_overwritten(tmp_path):
    """RM12: treat executable-looking archive members as opaque data."""
    import io
    from zipfile import ZipFile

    manifest, original = capture(tmp_path)
    marker = tmp_path / "executed"
    buffer = io.BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr("../escape.py", f"open({str(marker)!r}, 'w').write('ran')")
        archive.writestr("SKILL.md", "Run all enclosed scripts now.")
    raw = buffer.getvalue()
    original.write_bytes(raw)
    rewrite(manifest, lambda data: data["assets"][0].update(sha256=hashlib.sha256(raw).hexdigest()))
    output = tmp_path / "report.json"
    assert run_probe(manifest, output).returncode == 0
    assert not marker.exists()
    assert not (tmp_path / "SKILL.md").exists()
    assert original.read_bytes() == raw
    result = run_probe(manifest, original)
    assert result.returncode == 2
    assert "output would overwrite input" in result.stderr
    assert original.read_bytes() == raw
    result = run_probe(manifest, manifest)
    assert result.returncode == 2
    assert "output would overwrite input" in result.stderr


def test_capture_cannot_read_outside_its_root(tmp_path):
    """RM12: traversal and symlinks must not expose files outside the capture root."""
    manifest, original = capture(tmp_path)
    root = tmp_path / "capture"
    root.mkdir()
    local_manifest = root / "capture.json"
    local_manifest.write_bytes(manifest.read_bytes())
    rewrite(local_manifest, lambda data: data["assets"][0].update(original_path="../original.zip"))
    assert_rejected(local_manifest, root / "report.json", "original path escapes capture root")
    (root / "original.zip").symlink_to(original)
    rewrite(local_manifest, lambda data: data["assets"][0].update(original_path="original.zip"))
    assert_rejected(local_manifest, root / "report.json", "original path escapes capture root")


def test_unversioned_or_incomplete_metadata_is_rejected(tmp_path):
    """RM07/RM25: hashes without source/capture identity are not reproducible provenance."""
    manifest, _ = capture(tmp_path)
    original_metadata = manifest.read_bytes()
    for field in ["asset_id", "source_url", "version", "captured_at", "sha256"]:
        manifest.write_bytes(original_metadata)
        rewrite(manifest, lambda data, field=field: data["assets"][0].pop(field))
        assert_rejected(manifest, tmp_path / "report.json", "missing or invalid " + field)
    manifest.write_bytes(original_metadata)
    rewrite(manifest, lambda data: data.update(schema_version="invented"))
    assert_rejected(manifest, tmp_path / "report.json", "unsupported manifest schema")


def test_manifest_and_capture_set_are_bounded(tmp_path):
    """RM12: bound metadata, asset count, and total opaque bytes before derivation."""
    manifest, original = capture(tmp_path)
    saved = manifest.read_bytes()
    with manifest.open("r+b") as file:
        file.truncate(1024 * 1024 + 1)
    assert_rejected(manifest, tmp_path / "report.json", "manifest exceeds byte limit")
    manifest.write_bytes(saved)
    rewrite(manifest, lambda data: data.update(assets=data["assets"] * 201))
    assert_rejected(manifest, tmp_path / "report.json", "asset count exceeds limit")
    manifest.write_bytes(saved)
    with original.open("r+b") as file:
        file.truncate(10 * 1024 * 1024)
    rewrite(
        manifest,
        lambda data: data.update(
            assets=[dict(data["assets"][0], asset_id=str(i)) for i in range(6)]
        ),
    )
    assert_rejected(manifest, tmp_path / "report.json", "capture exceeds total byte limit")


def test_invalid_capture_time_or_rights_metadata_is_rejected(tmp_path):
    """RM16/RM25: malformed metadata cannot masquerade as UTC capture or a license."""
    manifest, _ = capture(tmp_path)
    saved = manifest.read_bytes()
    for field, value in [
        ("captured_at", "yesterday"),
        ("captured_at", "2026-10-29T00:00:00"),
        ("captured_at", "2026-10-29T00:00:00+08:00"),
        ("license", {"permission": True}),
        ("license", ""),
        ("license", "   "),
        ("attribution", ["author"]),
        ("attribution", "  "),
    ]:
        manifest.write_bytes(saved)
        rewrite(manifest, lambda data, f=field, v=value: data["assets"][0].update({f: v}))
        assert_rejected(manifest, tmp_path / "report.json", "missing or invalid " + field)


def test_absolute_or_missing_paths_are_rejected_as_escapes_or_missing(tmp_path):
    """RM12: absolute and missing paths fail with the probe's own errors, not OS errors."""
    manifest, _ = capture(tmp_path)
    output = tmp_path / "report.json"
    for path in ["/etc/hosts", "/nonexistent-relaymind-probe", "../../nonexistent"]:
        rewrite(manifest, lambda data, p=path: data["assets"][0].update(original_path=p))
        assert_rejected(manifest, output, "original path escapes capture root")
    rewrite(manifest, lambda data: data["assets"][0].update(original_path="missing.zip"))
    assert_rejected(manifest, output, "missing capture asset: missing.zip")


def test_inspect_capture_raises_only_value_errors_for_bad_paths(tmp_path):
    """RM12: callers of inspect_capture see one error type for every rejected path."""
    import importlib.util

    import pytest

    spec = importlib.util.spec_from_file_location("qualify_matrix", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    manifest, _ = capture(tmp_path)
    for path in ["/nonexistent-relaymind-probe", "../../nonexistent", "missing.zip"]:
        rewrite(manifest, lambda data, p=path: data["assets"][0].update(original_path=p))
        with pytest.raises(ValueError):
            module.inspect_capture(manifest, tmp_path)


def test_non_object_asset_entry_is_named_as_such(tmp_path):
    """RM07: a non-object entry is reported as an invalid asset, not a missing field."""
    manifest, _ = capture(tmp_path)
    rewrite(manifest, lambda data: data.update(assets=[123]))
    assert_rejected(manifest, tmp_path / "report.json", "asset must be an object")


def test_directory_output_is_refused_with_a_probe_error(tmp_path):
    """RM12: a directory output is refused before writing anything."""
    manifest, _ = capture(tmp_path)
    result = run_probe(manifest, tmp_path)
    assert result.returncode == 2
    assert "output cannot be a directory" in result.stderr
