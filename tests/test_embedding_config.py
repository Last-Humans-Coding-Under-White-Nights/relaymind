"""RM38/RM41/RM42, AC29/AC32/AC33: no implicit calibration settings."""

import json

import pytest

from test_embedding_manifest import fixture


def api():
    from relaymind.assets import embedding_config

    return embedding_config


def hybrid(manifest):
    return dict(
        schema_version=1,
        identity=dict(name="fixture_hybrid", version="1"),
        manifest_digest=manifest.manifest_digest,
        representation=dict(
            version="asset_fields.v1",
            normalization="NFC",
            query_prefix="",
            document_prefix="",
            max_tokens=100,
            overflow="unavailable",
        ),
        index=dict(version="exact_cosine.v1", batch_size=25),
        fusion=dict(
            version="weighted_rrf.v1", lexical_weight=0.4, semantic_weight=0.6, k=60, min_cosine=0.7
        ),
        confidence=dict(min_score=0.01, min_margin=0.001),
    )


def test_semantic_configs_bind_complete_values_and_real_manifest(tmp_path):
    from relaymind.assets.embedding_manifest import load_embedding_manifest

    path, _ = fixture(tmp_path)
    manifest = load_embedding_manifest(path)
    value = hybrid(manifest)
    config = tmp_path / "hybrid.json"
    config.write_text(json.dumps(value))
    first = api().load_hybrid_config(config, manifest)
    value["confidence"]["min_margin"] = 0.002
    config.write_text(json.dumps(value))
    second = api().load_hybrid_config(config, manifest)
    assert first.digest != second.digest
    value["manifest_digest"] = "0" * 64
    config.write_text(json.dumps(value))
    with pytest.raises(api().EmbeddingError):
        api().load_hybrid_config(config, manifest)


@pytest.mark.parametrize(
    "field,value",
    [
        ("batch_size", 0),
        ("batch_size", 26),
        ("batch_size", True),
        ("k", 0),
        ("k", 1001),
        ("lexical_weight", 0),
        ("lexical_weight", 0.9),
        ("min_cosine", 1.1),
        ("min_margin", -1),
        ("max_tokens", 8193),
        ("query_prefix", "界" * 86),
    ],
)
def test_unsafe_or_missing_calibration_is_refused(tmp_path, field, value):
    from relaymind.assets.embedding_manifest import load_embedding_manifest

    path, _ = fixture(tmp_path)
    manifest = load_embedding_manifest(path)
    config = hybrid(manifest)
    table = next(table for table in config.values() if isinstance(table, dict) and field in table)
    table[field] = value
    target = tmp_path / "config.json"
    target.write_text(json.dumps(config))
    with pytest.raises(api().EmbeddingError):
        api().load_hybrid_config(target, manifest)


def test_adaptation_reuses_manifest_and_has_no_hidden_ranking_settings(tmp_path):
    from relaymind.assets.embedding_manifest import load_embedding_manifest

    path, _ = fixture(tmp_path)
    manifest = load_embedding_manifest(path)
    value = dict(
        schema_version=1,
        identity=dict(name="fixture_adaptation", version="1"),
        manifest="manifest.json",
        manifest_digest=manifest.manifest_digest,
        representation=dict(
            version="task_text.v1",
            normalization="NFC",
            query_prefix="",
            max_tokens=100,
            overflow="unavailable",
        ),
        similarity=dict(version="exact_cosine.v1", threshold=0.9),
        ranking_version="bounded_votes.v1",
    )
    target = tmp_path / "adaptation.json"
    target.write_text(json.dumps(value))
    parsed = api().load_adaptation_config(target)
    assert parsed.manifest.manifest_digest == manifest.manifest_digest
    value["max_votes"] = 30
    target.write_text(json.dumps(value))
    with pytest.raises(api().EmbeddingError):
        api().load_adaptation_config(target)


def test_weights_inside_sum_tolerance_are_preserved_without_renormalizing(tmp_path):
    from relaymind.assets.embedding_manifest import load_embedding_manifest

    path, _ = fixture(tmp_path)
    manifest = load_embedding_manifest(path)
    value = hybrid(manifest)
    value["fusion"]["lexical_weight"] = 1.0000000000005
    value["fusion"]["semantic_weight"] = 1e-15
    config = tmp_path / "hybrid.json"
    config.write_text(json.dumps(value))
    assert (
        api().load_hybrid_config(config, manifest).data["fusion"]["lexical_weight"]
        == 1.0000000000005
    )
