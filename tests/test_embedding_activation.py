"""RM38/RM41 AC29/AC32: exact reviewed activation before any heavy loading."""

import asyncio
import json
import time

import pytest

from test_embedding_config import hybrid
from test_embedding_manifest import (
    fixture,
    inject_changed_artifact_fingerprint,
    simulate_win32_sharing_violation,
)


def api():
    from relaymind.assets import embedding_activation

    return embedding_activation


def configured(tmp_path):
    from relaymind.assets.embedding_config import load_hybrid_config
    from relaymind.assets.embedding_manifest import load_embedding_manifest

    path, _ = fixture(tmp_path)
    manifest = load_embedding_manifest(path)
    config_path = tmp_path / "hybrid.json"
    config_path.write_text(json.dumps(hybrid(manifest)))
    return load_hybrid_config(config_path, manifest)


def test_unqualified_activation_refuses_before_backend_or_weight_access(tmp_path):
    from relaymind.assets.embeddings import EmbeddingError

    config = configured(tmp_path)
    (tmp_path / "weights.bin").unlink()
    with pytest.raises(EmbeddingError, match="unqualified_artifact"):
        api().EmbeddingLoader().load(config)


def test_runtime_mismatch_and_changed_files_refuse_before_activation(tmp_path):
    from relaymind.assets.embedding_qualification import qualification_key
    from relaymind.assets.embeddings import EmbeddingError

    config = configured(tmp_path)
    key = qualification_key(config.manifest, config.digest)
    entry = api().BackendRegistration(
        lambda: {"backend": "fixture", "version": "different", "build": "1"},
        lambda: "cpu",
        lambda artifact: None,
    )
    loader = api().EmbeddingLoader(qualifications={key}, backends={"fixture": entry})
    with pytest.raises(EmbeddingError, match="embedding_runtime_mismatch"):
        loader.load(config)
    entry = api().BackendRegistration(
        lambda: dict(config.manifest.data["runtime"]), lambda: "cpu", lambda artifact: None
    )
    loader = api().EmbeddingLoader(qualifications={key}, backends={"fixture": entry})
    (tmp_path / "weights.bin").write_bytes(b"changed")
    with pytest.raises(EmbeddingError):
        loader.load(config)


def test_explicit_fixture_constructor_injection_uses_real_checked_engine(tmp_path):
    from relaymind.assets.embedding_qualification import qualification_key
    from relaymind.assets.embeddings import represent_query
    from test_embedding_backend import NativeDouble

    config = configured(tmp_path)
    key = qualification_key(config.manifest, config.digest)
    registration = api().BackendRegistration(
        lambda: dict(config.manifest.data["runtime"]),
        lambda: "cpu",
        lambda artifact: NativeDouble(),
    )
    loader = api().EmbeddingLoader(qualifications={key}, backends={"fixture": registration})
    loader.validate(config)
    engine = loader.load(config)
    batch = asyncio.run(
        engine.embed([represent_query("Hello", "")], max_tokens=20, deadline=time.monotonic() + 1)
    )
    assert batch.vectors == ((0.6, 0.8),)
    assert batch.observation["cost_observations"][0]["value"]["calls"] == 1


def _exercise_file_swap_inside_backend_loading(tmp_path, monkeypatch, simulate_lock):
    from relaymind.assets.embedding_qualification import qualification_key
    from relaymind.assets.embeddings import EmbeddingError
    from test_embedding_backend import NativeDouble

    config = configured(tmp_path)
    key = qualification_key(config.manifest, config.digest)
    source, old = tmp_path / "weights.bin", tmp_path / "old"
    original = source.read_bytes()
    if simulate_lock:
        simulate_win32_sharing_violation(monkeypatch, source, old)
    attempted = {"error": None, "renamed": False, "native_created": False}

    def load(artifact):
        try:
            source.rename(old)
        except PermissionError as failure:
            attempted["error"] = failure
            raise
        attempted["renamed"] = True
        source.write_bytes(original)
        attempted["native_created"] = True
        return NativeDouble()

    entry = api().BackendRegistration(
        lambda: dict(config.manifest.data["runtime"]), lambda: "cpu", load
    )
    engine = None
    with pytest.raises(EmbeddingError) as failure:
        engine = (
            api().EmbeddingLoader(qualifications={key}, backends={"fixture": entry}).load(config)
        )
    assert engine is None
    if attempted["error"] is not None:
        assert getattr(attempted["error"], "winerror", None) == 32
        assert str(failure.value) == "embedding_model_unavailable"
        assert source.read_bytes() == original and not old.exists()
        assert not attempted["renamed"] and not attempted["native_created"]
    else:
        assert attempted["renamed"] and attempted["native_created"]
        assert str(failure.value) == "artifact_changed"
        assert source.read_bytes() == old.read_bytes() == original


def test_file_swap_inside_backend_loading_is_fenced(tmp_path, monkeypatch):
    """RM41/AC32: an actual OS swap is blocked or detected before activation."""
    _exercise_file_swap_inside_backend_loading(tmp_path, monkeypatch, False)


def test_simulated_win32_sharing_lock_blocks_backend_swap(tmp_path, monkeypatch):
    """RM41/AC32: the WinError 32 branch refuses without changing source bytes."""
    _exercise_file_swap_inside_backend_loading(tmp_path, monkeypatch, True)


def test_backend_load_rechecks_changed_reader_fingerprint(tmp_path):
    """RM41/AC32: swap detection remains exercised when the OS blocks rename."""
    from relaymind.assets.embedding_qualification import qualification_key
    from relaymind.assets.embeddings import EmbeddingError
    from test_embedding_backend import NativeDouble

    config = configured(tmp_path)
    key = qualification_key(config.manifest, config.digest)
    original = (tmp_path / "weights.bin").read_bytes()

    def load(artifact):
        inject_changed_artifact_fingerprint(artifact)
        return NativeDouble()

    entry = api().BackendRegistration(
        lambda: dict(config.manifest.data["runtime"]), lambda: "cpu", load
    )
    with pytest.raises(EmbeddingError, match="artifact_changed"):
        api().EmbeddingLoader(qualifications={key}, backends={"fixture": entry}).load(config)
    assert (tmp_path / "weights.bin").read_bytes() == original
