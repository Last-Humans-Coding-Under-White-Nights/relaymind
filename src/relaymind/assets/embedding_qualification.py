"""Reviewed exact-identity activation authority; no production entries yet (RM41)."""

from relaymind.assets.embedding_manifest import EmbeddingManifest
from relaymind.assets.embeddings import EmbeddingError

# Actual rights/development/runtime/device evidence is reviewed with each future
# source entry. A config/environment/local JSON cannot populate this registry.
QUALIFIED_IDENTITIES: frozenset[tuple] = frozenset()


def qualification_key(manifest: EmbeddingManifest, semantic_digest: str) -> tuple:
    raw = manifest.data
    return (
        manifest.manifest_digest,
        semantic_digest,
        raw["runtime"]["backend"],
        raw["runtime"]["version"],
        raw["runtime"]["build"],
        raw["device"],
    )


def require_qualified(manifest: EmbeddingManifest, semantic_digest: str) -> None:
    if qualification_key(manifest, semantic_digest) not in QUALIFIED_IDENTITIES:
        raise EmbeddingError("unqualified_artifact")
