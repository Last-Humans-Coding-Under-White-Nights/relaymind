"""Reproduce source catalogs from pinned acquisition recipes, not completed labels."""

import json
import re
from collections.abc import Mapping
from pathlib import Path

from relaymind.evaluation.asset_corpus_io import MIB, AssetCorpusError, matches_original, read_local
from relaymind.evaluation.asset_source_catalog import (
    SourceCatalogResult,
    SourceOriginal,
    build_source_catalog,
)


def build_recipe(recipe: Mapping, cache_root: Path, output: Path) -> SourceCatalogResult:
    if recipe.get("schema_version") != 1 or type(recipe.get("schema_version")) is not int:
        raise AssetCorpusError("invalid_source_recipe")
    files = {}
    for row in recipe.get("files", []):
        if (
            not isinstance(row, Mapping)
            or not isinstance(row.get("id"), str)
            or not row["id"]
            or row["id"] in files
            or not isinstance(row.get("sha256"), str)
            or not re.fullmatch("[0-9a-f]{64}", row["sha256"])
            or type(row.get("bytes")) is not int
            or row["bytes"] < 0
        ):
            raise AssetCorpusError("invalid_source_recipe")
        files[row["id"]] = row
    if len(files) > 1000 or len(recipe.get("assets", [])) > 1000:
        raise AssetCorpusError("source_limit")
    retained = {}

    def cached(file_id):
        if file_id not in files:
            raise AssetCorpusError("unknown_source_file")
        row = files[file_id]
        if file_id not in retained:
            payload = read_local(
                cache_root,
                row["sha256"] + ".original",
                4 * MIB if row.get("kind") == "metadata" else 10 * MIB,
            )
            if not matches_original(payload, row["sha256"], row["bytes"]):
                raise AssetCorpusError("source_digest_mismatch")
            retained[file_id] = payload
            if sum(len(v) for v in retained.values()) > 100 * MIB:
                raise AssetCorpusError("source_limit")
        return retained[file_id]

    originals = []
    required_asset_keys = {
        "file_id",
        "asset_id",
        "source_id",
        "source_url",
        "revision",
        "captured_at",
        "kind",
        "name",
        "format",
    }
    allowed_asset_keys = required_asset_keys | {
        "version",
        "metadata_file_id",
        "license_file_id",
    }
    for raw in recipe.get("assets", []):
        if (
            not isinstance(raw, Mapping)
            or not required_asset_keys <= set(raw)
            or not set(raw) <= allowed_asset_keys
        ):
            raise AssetCorpusError("invalid_source_recipe")
        row = dict(raw)
        file_id = row.pop("file_id")
        metadata_id = row.pop("metadata_file_id", None)
        license_id = row.pop("license_file_id", None)
        version = row.pop("version", None)
        if (
            (metadata_id is not None and not isinstance(metadata_id, str))
            or (license_id is not None and not isinstance(license_id, str))
            or (version is not None and not isinstance(version, str))
        ):
            raise AssetCorpusError("invalid_source_recipe")
        if file_id not in files:
            raise AssetCorpusError("unknown_source_file")
        originals.append(
            SourceOriginal(
                **row,
                version=version,
                payload=cached(file_id),
                sha256=files[file_id]["sha256"],
                metadata_bytes=cached(metadata_id) if metadata_id else None,
                license_bytes=cached(license_id) if license_id else None,
            )
        )
    extra = []
    for row in recipe.get("evidence_files", []):
        if not isinstance(row, Mapping) or not {"kind", "file_id"} <= set(row):
            raise AssetCorpusError("invalid_source_recipe")
        extra.append((row["kind"], cached(row["file_id"])))
    dataset_id = recipe.get("dataset_id")
    if not isinstance(dataset_id, str) or not dataset_id:
        raise AssetCorpusError("invalid_source_recipe")
    return build_source_catalog(
        originals,
        output,
        dataset_id=dataset_id,
        exclusions=recipe.get("exclusions", []),
        additional_evidence=extra,
    )


def write_example_configs(result: SourceCatalogResult) -> dict[str, Path]:
    """Existing production TOML, with actual generic metadata-import paths."""
    sources = ",".join(
        "{source_id="
        + json.dumps(source_id)
        + ',kind="metadata_import",path='
        + json.dumps(path.name)
        + "}"
        for source_id, path in sorted(result.import_paths.items())
    )
    base = (
        """[external_model]
base_url="https://unused.example.invalid/v1"
model="unused-by-retrieval"
context_limit=4096
[assets]
sources=["""
        + sources
        + """]
[recommendations]
enabled=true
user_id="source-benchmark"
workspace_id="disposable"
total_ms=500
primary_ms=150
fallback=true
"""
    )
    paths = {}
    for arm, suffix in [
        ("builtin", 'primary="lexical"\n'),
        (
            "http",
            'primary="http"\nname="example_lexical"\nversion="1"\nurl="http://127.0.0.1:8913/select"\n',
        ),
    ]:
        path = result.catalog_path.parent / (arm + "-production.toml")
        path.write_bytes((base + suffix).encode("utf-8"))
        paths[arm] = path
    return paths
