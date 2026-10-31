"""Closed explicit hybrid/adaptation settings; no hidden calibration (RM41/RM42)."""

from dataclasses import dataclass
from functools import cache
from pathlib import Path

from relaymind.assets.embedding_manifest import (
    EmbeddingManifest,
    check_strings,
    load_embedding_manifest,
    read_semantic_json,
)
from relaymind.assets.embeddings import EmbeddingError
from relaymind.canonical import canonical_digest
from relaymind.schema_util import StrictValidator, freeze_json, load_packaged_schema


@dataclass(frozen=True)
class SemanticConfig:
    data: object
    digest: str
    manifest: EmbeddingManifest


@cache
def _validator(name):
    return StrictValidator(load_packaged_schema("relaymind.assets", name))


def _read(path, name):
    raw = read_semantic_json(path)
    if not _validator(name).is_valid(raw):
        raise EmbeddingError("invalid_semantic_config")
    try:
        check_strings(raw)
    except UnicodeError:
        raise EmbeddingError("invalid_semantic_config") from None
    return raw


def _bind(raw, manifest):
    if raw["manifest_digest"] != manifest.manifest_digest:
        raise EmbeddingError("manifest_digest_mismatch")
    return SemanticConfig(freeze_json(raw), canonical_digest(raw), manifest)


def load_hybrid_config(path: Path, manifest: EmbeddingManifest) -> SemanticConfig:
    raw = _read(path, "asset_hybrid_config.v1.json")
    fusion = raw["fusion"]
    if abs(fusion["lexical_weight"] + fusion["semantic_weight"] - 1.0) > 1e-12:
        raise EmbeddingError("invalid_fusion_weights")
    return _bind(raw, manifest)


def load_adaptation_config(path: Path) -> SemanticConfig:
    raw = _read(path, "asset_adaptation_config.v1.json")
    manifest = load_embedding_manifest(path.parent / raw["manifest"])
    return _bind(raw, manifest)
