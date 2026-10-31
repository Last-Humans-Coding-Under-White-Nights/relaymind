"""Immutable asset source configuration (RM34, section 14.5)."""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from relaymind.assets.catalog import text
from relaymind.selection.http import validate_url


@dataclass(frozen=True)
class SourceConfig:
    source_id: str
    kind: str
    path: Path | None = None
    adapter_contract: str | None = None
    secret_ref: str | None = None


@dataclass(frozen=True)
class RefreshSettings:
    local_scan_seconds: int = 30
    remote_seconds: int = 3600
    backoff_initial_seconds: int = 60
    backoff_max_seconds: int = 3600


# The closed key sets of each table, shared with the launcher's table check.
ASSET_FIELDS = frozenset({"sources"})
REFRESH_FIELDS = frozenset(RefreshSettings.__dataclass_fields__)
HYBRID_FIELDS = frozenset({"manifest", "config"})
ADAPTATION_FIELDS = frozenset({"enabled", "config"})
SOURCE_FIELDS = frozenset({"source_id", "kind", "path", "adapter_contract", "secret_ref"})


@dataclass(frozen=True)
class AssetSettings:
    sources: tuple[SourceConfig, ...] = ()
    refresh: RefreshSettings = RefreshSettings()


@dataclass(frozen=True)
class RecommendationSettings:
    enabled: bool = False
    user_id: str | None = None
    workspace_id: str | None = None
    total_ms: int = 500
    primary_ms: int = 150
    primary: str = "lexical"
    name: str | None = None
    version: str | None = None
    url: str | None = None
    fallback: bool = True


RECOMMENDATION_FIELDS = frozenset(RecommendationSettings.__dataclass_fields__)


def _closed(value: object, fields: frozenset[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) - fields:
        raise ValueError(f"{label}: unknown fields or invalid table")
    return value


def _positive(value: object, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def parse_asset_settings(
    assets: Mapping[str, object], refresh: Mapping[str, object], *, config_dir: Path
) -> AssetSettings:
    # parse_* stay closed on their own: they are a public contract, not only the launcher's.
    _closed(assets, ASSET_FIELDS, "assets")
    _closed(refresh, REFRESH_FIELDS, "asset_refresh")
    defaults = RefreshSettings()
    timings = RefreshSettings(
        **{key: _positive(refresh.get(key, getattr(defaults, key)), key) for key in REFRESH_FIELDS}
    )
    if timings.backoff_max_seconds < timings.backoff_initial_seconds:
        raise ValueError("asset_refresh backoff_max_seconds must be at least initial")
    raw_sources = assets.get("sources", [])
    if not isinstance(raw_sources, list):
        raise ValueError("assets.sources must be an array")
    sources = []
    for raw in raw_sources:
        raw = _closed(raw, SOURCE_FIELDS, "source")
        source_id = text(raw.get("source_id"), 256)
        kind = raw.get("kind")
        if kind == "qualified_remote":
            from relaymind.assets.remote_contracts import (
                resolve_remote_profile,
                validate_secret_ref,
            )

            profile = resolve_remote_profile(raw.get("adapter_contract"))
            if raw.get("path") is not None:
                raise ValueError("qualified remote path must be null")
            validate_secret_ref(raw.get("secret_ref"))
            if timings.remote_seconds < profile.minimum_poll_seconds:
                raise ValueError("remote interval below installed profile minimum")
            if any(s.adapter_contract == profile.profile_id for s in sources):
                raise ValueError("duplicate installed remote profile")
            sources.append(
                SourceConfig(source_id, kind, None, profile.profile_id, raw.get("secret_ref"))
            )
            continue
        if kind not in ("local_directory", "metadata_import"):
            raise ValueError("assets.sources kind must be local_directory or metadata_import")
        if raw.get("adapter_contract") is not None or raw.get("secret_ref") is not None:
            raise ValueError("local/import sources require null adapter_contract and secret_ref")
        path = raw.get("path")
        if not isinstance(path, str) or not path.strip():
            raise ValueError("local/import sources require a nonempty path")
        path = Path(os.path.abspath(config_dir / path))
        sources.append(SourceConfig(source_id, kind, path))
    if len({s.source_id for s in sources}) != len(sources):
        raise ValueError("assets.sources source_id must be unique")
    return AssetSettings(tuple(sources), timings)


def _bounded_path(value: object, label: str) -> None:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 2048:
        raise ValueError(f"{label} must be a bounded path")


def parse_recommendation_settings(
    value: Mapping[str, object],
    *,
    source_count: int,
    hybrid: Mapping[str, object] | None = None,
    adaptation: Mapping[str, object] | None = None,
) -> RecommendationSettings:
    _closed(value, RECOMMENDATION_FIELDS, "recommendations")
    hybrid = _closed(hybrid or {}, HYBRID_FIELDS, "asset_hybrid")
    adaptation = _closed(adaptation or {}, ADAPTATION_FIELDS, "asset_adaptation")
    if hybrid and set(hybrid) != HYBRID_FIELDS:
        raise ValueError("asset_hybrid requires manifest and config")
    for key, val in hybrid.items():
        _bounded_path(val, f"asset_hybrid {key}")
    if "enabled" in adaptation and type(adaptation["enabled"]) is not bool:
        raise ValueError("asset_adaptation enabled must be boolean")
    if adaptation.get("enabled") and "config" not in adaptation:
        raise ValueError("asset_adaptation enabled requires config")
    if "config" in adaptation:
        _bounded_path(adaptation["config"], "asset_adaptation config")
    raw = {**vars(RecommendationSettings()), **value}
    for key in ("enabled", "fallback"):
        if type(raw[key]) is not bool:
            raise ValueError(f"recommendations {key} must be boolean")
    for key in ("total_ms", "primary_ms"):
        _positive(raw[key], f"recommendations {key}")
    if raw["primary_ms"] > raw["total_ms"]:
        raise ValueError("recommendations primary_ms must not exceed total_ms")
    if raw["primary"] not in ("lexical", "hybrid", "http"):
        raise ValueError("recommendations primary must be lexical, hybrid or http")
    if raw["primary"] == "hybrid" and not hybrid:
        raise ValueError("hybrid artifacts are not qualified; manifest/config are required")
    for key in ("user_id", "workspace_id", "name", "version"):
        if raw[key] is not None:
            text(raw[key], 256)
    if raw["enabled"] and (not raw["user_id"] or not raw["workspace_id"] or not source_count):
        raise ValueError(
            "enabled recommendations require user_id, workspace_id and explicit sources"
        )
    if raw["primary"] == "http" and (not raw["name"] or not raw["version"] or not raw["url"]):
        raise ValueError("recommendations http requires name, version and url")
    if raw["url"] is not None:
        validate_url(raw["url"])
    return RecommendationSettings(**raw)
