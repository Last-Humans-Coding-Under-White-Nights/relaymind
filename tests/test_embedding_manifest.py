"""RM38/RM41, AC29/AC32: closed pinned local artifacts, never authority by config."""

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest


def api():
    from relaymind.assets import embedding_manifest

    return embedding_manifest


def fixture(tmp_path):
    files = []
    for name, role in [
        ("weights.bin", "weights"),
        ("tokenizer.json", "tokenizer"),
        ("LICENSE", "license"),
    ]:
        data = ("fixture " + role).encode()
        (tmp_path / name).write_bytes(data)
        files.append(
            dict(path=name, roles=[role], bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
        )
    value = dict(
        schema_version=1,
        model=dict(name="fixture", version="1"),
        source=dict(url="https://example.test/fixture", revision="1"),
        quantization="fixture-f32",
        license="MIT",
        runtime=dict(backend="fixture", version="1", build="1"),
        device="cpu",
        dimensions=2,
        files=files,
    )
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(value))
    return path, value


def simulate_win32_sharing_violation(monkeypatch, source, destination):
    """Exercise Windows' held-handle rename refusal on other test hosts."""
    original = Path.rename
    state = {"blocked": True}

    def rename(path, target):
        if state["blocked"] and path == source and Path(target) == destination:
            error = PermissionError(13, "sharing violation", str(path))
            error.winerror = 32
            raise error
        return original(path, target)

    monkeypatch.setattr(Path, "rename", rename)
    return state


def inject_changed_artifact_fingerprint(verified, name="weights.bin"):
    """Change one checked reader observation without mutating the artifact."""
    original = verified.tree.info

    def changed(relative):
        info = original(relative)
        return replace(info, changed=info.changed + 1) if relative == name else info

    verified.tree.info = changed


def test_manifest_ordering_is_canonical_and_files_remain_original(tmp_path):
    path, value = fixture(tmp_path)
    first = api().load_embedding_manifest(path)
    value["files"].reverse()
    path.write_text(json.dumps(value, indent=2))
    second = api().load_embedding_manifest(path)
    assert first.artifact_hash == second.artifact_hash
    assert first.manifest_digest == second.manifest_digest
    value["device"] = "different"
    path.write_text(json.dumps(value))
    third = api().load_embedding_manifest(path)
    assert third.artifact_hash == first.artifact_hash
    assert third.manifest_digest != first.manifest_digest
    with api().verify_embedding_files(first) as verified:
        assert verified.read_bytes("weights.bin", limit=100) == b"fixture weights"
        with pytest.raises(api().EmbeddingError):
            verified.read_bytes("weights.bin", limit=2)


@pytest.mark.parametrize(
    "change",
    [
        dict(schema_version=True),
        dict(dimensions=0),
        dict(dimensions=4097),
        dict(dimensions=True),
        dict(extra=True),
        dict(
            runtime={"backend": "os.system(boom)", "version": "1", "build": "1", "command": "boom"}
        ),
        dict(files=[]),
        dict(license="界" * 100),
    ],
)
def test_manifest_is_closed_and_bounded(tmp_path, change):
    path, value = fixture(tmp_path)
    value.update(change)
    path.write_text(json.dumps(value))
    with pytest.raises(api().EmbeddingError):
        api().load_embedding_manifest(path)


@pytest.mark.parametrize(
    "name",
    [
        "../weights.bin",
        "/weights.bin",
        "C:/weights.bin",
        "a\\b",
        "a//b",
        "./weights.bin",
        "a/../b",
        "a/./b",
    ],
)
def test_unsafe_artifact_paths_are_refused_before_open(tmp_path, name):
    path, value = fixture(tmp_path)
    value["files"][0]["path"] = name
    path.write_text(json.dumps(value))
    with pytest.raises(api().EmbeddingError):
        api().load_embedding_manifest(path)


@pytest.mark.parametrize(
    "raw",
    [
        b'{"schema_version":1,"schema_version":1}',
        b'{"x":NaN}',
        b"[[[[[0]]]]]",
        b"{} {}",
        b"\xff",
        b" " * 65537,
    ],
)
def test_semantic_parser_refuses_duplicate_nonfinite_depth_encoding_and_growth(tmp_path, raw):
    path = tmp_path / "manifest.json"
    path.write_bytes(raw)
    with pytest.raises(api().EmbeddingError):
        api().load_embedding_manifest(path)


def _exercise_artifact_hash_mutation_and_replacement(tmp_path, monkeypatch, simulate_lock):
    path, _ = fixture(tmp_path)
    manifest = api().load_embedding_manifest(path)
    source, old = tmp_path / "weights.bin", tmp_path / "old"
    original = source.read_bytes()
    lock = simulate_win32_sharing_violation(monkeypatch, source, old) if simulate_lock else None
    blocked = False
    with api().verify_embedding_files(manifest) as verified:
        try:
            source.rename(old)
        except PermissionError as failure:
            assert getattr(failure, "winerror", None) == 32
            blocked = True
            assert source.read_bytes() == original
            assert not old.exists()
            assert verified.read_bytes("weights.bin", limit=100) == original
            verified.guard()
        else:
            source.write_bytes(original)
            with pytest.raises(api().EmbeddingError, match="artifact_changed"):
                verified.guard()
    if lock is not None:
        lock["blocked"] = False
    if blocked:
        source.rename(old)
    assert old.read_bytes() == original
    source.write_bytes(b"x" * len(original))
    with pytest.raises(api().EmbeddingError, match="artifact_hash_mismatch"):
        api().verify_embedding_files(manifest)


def test_artifact_hash_mutation_and_replacement_are_refused(tmp_path, monkeypatch):
    """RM41/AC32: actual replacement is blocked or detected, then hash is checked."""
    _exercise_artifact_hash_mutation_and_replacement(tmp_path, monkeypatch, False)


def test_simulated_win32_sharing_lock_preserves_artifact_hash_guard(tmp_path, monkeypatch):
    """RM41/AC32: source handle release permits a later bad-content check."""
    _exercise_artifact_hash_mutation_and_replacement(tmp_path, monkeypatch, True)


def test_checked_artifact_identity_change_is_fenced_without_rename(tmp_path):
    """RM41/AC32: detection remains exercised when OS handles prevent swaps."""
    path, _ = fixture(tmp_path)
    manifest = api().load_embedding_manifest(path)
    with api().verify_embedding_files(manifest) as verified:
        inject_changed_artifact_fingerprint(verified)
        with pytest.raises(api().EmbeddingError, match="artifact_changed"):
            verified.guard()


def test_symlinks_and_physical_aliases_are_refused(tmp_path):
    path, value = fixture(tmp_path)
    (tmp_path / "weights.bin").rename(tmp_path / "actual")
    (tmp_path / "weights.bin").symlink_to(tmp_path / "actual")
    with pytest.raises(api().EmbeddingError):
        api().verify_embedding_files(api().load_embedding_manifest(path))
    (tmp_path / "weights.bin").unlink()
    (tmp_path / "actual").rename(tmp_path / "weights.bin")
    (tmp_path / "alias").hardlink_to(tmp_path / "weights.bin")
    value["files"].append(dict(value["files"][0], path="alias"))
    path.write_text(json.dumps(value))
    with pytest.raises(api().EmbeddingError):
        api().verify_embedding_files(api().load_embedding_manifest(path))


def test_unqualified_artifact_never_reaches_backend_loading(tmp_path):
    path, _ = fixture(tmp_path)
    manifest = api().load_embedding_manifest(path)
    from relaymind.assets import embedding_qualification

    with pytest.raises(api().EmbeddingError, match="unqualified_artifact"):
        embedding_qualification.require_qualified(manifest, "0" * 64)
