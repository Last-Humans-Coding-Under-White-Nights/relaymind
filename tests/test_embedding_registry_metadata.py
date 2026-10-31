"""RM41/AC32: reviewed local activation is exact, never operator asserted."""

import json
from pathlib import Path

import pytest

from relaymind.assets.embedding_config import validate_adaptation_config, validate_hybrid_config
from relaymind.assets.embedding_manifest import validate_embedding_manifest
from relaymind.assets.embedding_qualification import qualification_key, require_qualified
from relaymind.assets.embeddings import EmbeddingError

ROOT = Path(__file__).resolve().parents[1] / "models" / "embeddings"
PREFIX = "multilingual-e5-small-linux-x86_64"


@pytest.mark.parametrize("kind", ["hybrid", "adaptation"])
def test_reviewed_local_identity_requires_exact_artifact_config_and_runtime(kind):
    manifest_raw = json.loads((ROOT / f"{PREFIX}.manifest.json").read_bytes())
    manifest = validate_embedding_manifest(manifest_raw)
    config_raw = json.loads((ROOT / f"{PREFIX}.{kind}.json").read_bytes())
    validate = validate_hybrid_config if kind == "hybrid" else validate_adaptation_config
    config = validate(config_raw, manifest)
    require_qualified(manifest, config.digest)
    assert qualification_key(manifest, config.digest)[0] == config_raw["manifest_digest"]
    with pytest.raises(EmbeddingError, match="unqualified_artifact"):
        require_qualified(manifest, "0" * 64)
    for field, value in [("device", "cpu:unreviewed"), ("model", "unreviewed")]:
        changed = json.loads(json.dumps(manifest_raw))
        if field == "model":
            changed["runtime"]["build"] = "0" * 64
        else:
            changed[field] = value
        with pytest.raises(EmbeddingError, match="unqualified_artifact"):
            require_qualified(validate_embedding_manifest(changed), config.digest)
