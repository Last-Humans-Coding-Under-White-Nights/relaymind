"""RM38/RM41 AC29/AC32: exact reviewed activation before any heavy loading."""

import asyncio
import json
import time

import pytest

from test_embedding_config import hybrid
from test_embedding_manifest import fixture


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


def test_file_swap_inside_backend_loading_is_fenced(tmp_path):
    from relaymind.assets.embedding_qualification import qualification_key
    from relaymind.assets.embeddings import EmbeddingError
    from test_embedding_backend import NativeDouble

    config = configured(tmp_path)
    key = qualification_key(config.manifest, config.digest)

    def load(artifact):
        (tmp_path / "weights.bin").rename(tmp_path / "old")
        (tmp_path / "weights.bin").write_bytes(b"fixture weights")
        return NativeDouble()

    entry = api().BackendRegistration(
        lambda: dict(config.manifest.data["runtime"]), lambda: "cpu", load
    )
    with pytest.raises(EmbeddingError, match="artifact_changed"):
        api().EmbeddingLoader(qualifications={key}, backends={"fixture": entry}).load(config)
