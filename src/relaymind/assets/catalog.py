"""Closed asset contracts and safe searchable projections (RM34, AC25).

Catalog data never conveys execution authority or proves host availability.
"""

import copy
import math
import re
import unicodedata
from collections import Counter
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from jsonschema import Draft202012Validator, SchemaError

from relaymind.canonical import canonical_digest, canonical_json
from relaymind.redaction import Redactor, redact_text, scrub
from relaymind.schema_util import freeze_json, load_packaged_schema
from relaymind.tools.registry import check_parameters_schema


class AssetValidationError(ValueError):
    """A bounded safe reason, never source text or parser exception text."""


_RECORD_SCHEMA = load_packaged_schema("relaymind.store", "asset_record.v1.json")
_SNAPSHOT_SCHEMA = load_packaged_schema("relaymind.store", "asset_catalog_snapshot.v1.json")
_HASH = re.compile(r"[a-f0-9]{64}")
_LANGUAGE = re.compile(r"[A-Za-z0-9-]{1,64}")
_MARKUP = re.compile(r"<[A-Za-z!?/]")
_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)$")


def _fail(reason: str) -> None:
    raise AssetValidationError(reason)


def text(value: object, limit: int, *, empty: bool = False) -> str:
    """Validate projected text with UTF-8 byte, control and repetition bounds."""
    if not isinstance(value, str) or (not empty and not value.strip()):
        _fail("invalid_text")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError:
        _fail("invalid_encoding")
    if size > limit:
        _fail("size_limit")
    normalized = unicodedata.normalize("NFC", value)
    if len(normalized.encode("utf-8")) > limit:
        _fail("size_limit")
    if any((ord(c) < 32 and c not in "\t\r\n") or 127 <= ord(c) <= 159 for c in normalized):
        _fail("invalid_control")
    if _MARKUP.search(normalized):
        _fail("invalid_markup")
    if any(
        count > 3
        for count in Counter(
            line.strip() for line in normalized.splitlines() if line.strip()
        ).values()
    ):
        _fail("repeated_line")
    return normalized


def _json(value: object) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                _fail("invalid_json")
            _json(item)
    elif isinstance(value, list | tuple):
        for item in value:
            _json(item)
    elif isinstance(value, float):
        if not math.isfinite(value):
            _fail("invalid_number")
    elif value is not None and type(value) not in (str, int, bool):
        _fail("invalid_json")


def _validate(value: object, schema: dict[str, Any]) -> dict[str, Any]:
    try:
        _json(value)
        if not isinstance(value, Mapping) or type(value.get("schema_version")) is not int:
            _fail("invalid_schema")
        if next(Draft202012Validator(schema).iter_errors(value), None) is not None:
            _fail("invalid_schema")
        return copy.deepcopy(dict(value))
    except (RecursionError, UnicodeError, TypeError, OverflowError):
        raise AssetValidationError("invalid_json") from None


def _timestamp(value: object, *, nullable: bool = False) -> None:
    if nullable and value is None:
        return
    if not isinstance(value, str) or not _UTC.fullmatch(value):
        _fail("invalid_timestamp")
    try:
        moment = datetime.fromisoformat(value)
        if moment.utcoffset() != timedelta(0):
            _fail("invalid_timestamp")
    except ValueError:
        raise AssetValidationError("invalid_timestamp") from None


@dataclass(frozen=True)
class AssetRecord:
    schema_version: int
    asset_id: str
    kind: str
    source_id: str
    source_locator: str
    source_version: str | None
    captured_at: str
    content_hash: str
    description: str
    language_tags: tuple[str, ...]
    dependencies: tuple[str, ...]
    availability: str
    stale: bool
    identity: Mapping[str, Any]

    def to_json_object(self) -> dict[str, Any]:
        return {
            **vars(self),
            "identity": copy.deepcopy(self.identity),
            "language_tags": list(self.language_tags),
            "dependencies": list(self.dependencies),
        }


def validate_asset_record(value: object) -> AssetRecord:
    raw = _validate(value, _RECORD_SCHEMA)
    _timestamp(raw["captured_at"])
    if not _HASH.fullmatch(raw["content_hash"]):
        _fail("invalid_hash")
    if any(not _LANGUAGE.fullmatch(tag) for tag in raw["language_tags"]):
        _fail("invalid_language")
    for name in ("asset_id", "source_id"):
        text(raw[name], 256)
    text(raw["source_locator"], 2048)
    text(raw["description"], 8192)
    if raw["source_version"] is not None:
        text(raw["source_version"], 256)
    for dependency in raw["dependencies"]:
        text(dependency, 256)
    identity = raw["identity"]
    for key, item in identity.items():
        if key not in ("input_schema", "schema_hash") and item is not None:
            text(item, 256)
    if raw["kind"] == "mcp_tool":
        schema = identity["input_schema"]
        try:
            if len(canonical_json(schema).encode("utf-8")) > 65536:
                _fail("size_limit")
            check_parameters_schema(schema)
            if canonical_digest(schema) != identity["schema_hash"]:
                _fail("schema_hash_mismatch")
        except (SchemaError, ValueError, TypeError, RecursionError):
            raise AssetValidationError("invalid_input_schema") from None
    raw["language_tags"] = tuple(raw["language_tags"])
    raw["dependencies"] = tuple(raw["dependencies"])
    raw["identity"] = freeze_json(identity)
    return AssetRecord(**raw)


@dataclass(frozen=True)
class CatalogSnapshot:
    schema_version: int
    catalog_revision: int
    published_at: str
    sources: tuple[Mapping[str, Any], ...]
    assets: tuple[AssetRecord, ...]

    def to_json_object(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "catalog_revision": self.catalog_revision,
            "published_at": self.published_at,
            "sources": copy.deepcopy(list(self.sources)),
            "assets": [a.to_json_object() for a in self.assets],
        }


def validate_catalog_snapshot(value: object) -> CatalogSnapshot:
    raw = _validate(value, _SNAPSHOT_SCHEMA)
    if type(raw["catalog_revision"]) is not int:
        _fail("invalid_revision")
    _timestamp(raw["published_at"])
    if len({s["source_id"] for s in raw["sources"]}) != len(raw["sources"]):
        _fail("duplicate_source")
    for source in raw["sources"]:
        text(source["source_id"], 256)
        for key in ("captured_at", "last_success_at"):
            _timestamp(source[key], nullable=True)
        if source["content_hash"] is not None and not _HASH.fullmatch(source["content_hash"]):
            _fail("invalid_hash")
        if source["source_version"] is not None:
            text(source["source_version"], 256)
        if source["complete"] and source["exclusions"]:
            _fail("incomplete_coverage")
        for key in ("accepted_count", "rejected_count"):
            if type(source[key]) is not int:
                _fail("invalid_count")
        for exclusion in source["exclusions"]:
            text(exclusion["reason"], 256)
            if exclusion["asset_id"] is not None:
                text(exclusion["asset_id"], 256)
    assets = tuple(validate_asset_record(a) for a in raw["assets"])
    if len({a.asset_id for a in assets}) != len(assets):
        _fail("duplicate_asset")
    if any(a.source_id not in {s["source_id"] for s in raw["sources"]} for a in assets):
        _fail("unknown_source")
    return CatalogSnapshot(
        1,
        raw["catalog_revision"],
        raw["published_at"],
        tuple(freeze_json(s) for s in raw["sources"]),
        assets,
    )


@dataclass(frozen=True)
class SearchDocument:
    record: AssetRecord
    body: str | None
    projection_digest: str

    def to_json_object(self) -> dict[str, Any]:
        return {
            "record": self.record.to_json_object(),
            "body": self.body,
            "projection_digest": self.projection_digest,
        }


def _safe_schema_projection(value: object, secrets: Collection[str]) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            # Dynamic schema keys may themselves repeat acquired secret values.
            if redact_text(key, secrets) != key:
                _fail("identity_contains_secret")
            text(key, 65536, empty=True)
            _safe_schema_projection(item, secrets)
    elif isinstance(value, list | tuple):
        for item in value:
            _safe_schema_projection(item, secrets)
    elif isinstance(value, str):
        text(value, 65536, empty=True)


def _contains_normalized_secret(value: object, secrets: Collection[str]) -> bool:
    if isinstance(value, Mapping):
        return any(
            _contains_normalized_secret(k, secrets) or _contains_normalized_secret(v, secrets)
            for k, v in value.items()
        )
    if isinstance(value, list | tuple):
        return any(_contains_normalized_secret(item, secrets) for item in value)
    if isinstance(value, str):
        normalized = unicodedata.normalize("NFC", value)
        return redact_text(normalized, secrets) != normalized
    return False


def _safe_free_text(value: str, limit: int, secrets: Collection[str]) -> str:
    # Preserve the pre-normalization byte cap, then scrub at both sides of NFC.
    try:
        if len(value.encode("utf-8")) > limit:
            _fail("size_limit")
    except UnicodeError:
        raise AssetValidationError("invalid_encoding") from None
    normalized = text(redact_text(value, secrets), limit)
    return text(redact_text(normalized, secrets), limit)


def project_asset(
    record: AssetRecord, *, body: str | None, redactor: Redactor, secrets: Collection[str]
) -> SearchDocument:
    # Match both original and NFC forms. Normalization must never make a
    # known acquisition secret invisible to subsequent scrubbing.
    secrets = frozenset(secrets) | {unicodedata.normalize("NFC", secret) for secret in secrets}
    raw = record.to_json_object()
    if record.kind == "mcp_tool":
        _safe_schema_projection(record.identity["input_schema"], secrets)
    # Only metadata acquisition fields may teach secret values. A JSON Schema's
    # property named password is a declaration, never an acquisition credential.
    protected = {k: v for k, v in raw.items() if k != "description"}
    if scrub(protected, secrets) != protected or _contains_normalized_secret(protected, secrets):
        _fail("identity_contains_secret")
    raw["description"] = _safe_free_text(raw["description"], 8192, secrets)
    projected = validate_asset_record(raw)
    normalized_body = None if body is None else _safe_free_text(body, 262144, secrets)
    digest_value = projected.to_json_object()
    # Observation times and staleness are not content identity.
    digest_value.pop("captured_at")
    digest_value.pop("stale")
    return SearchDocument(
        projected,
        normalized_body,
        canonical_digest({"record": digest_value, "body": normalized_body}),
    )


def discovery_records(
    snapshot: CatalogSnapshot, requested_kinds: frozenset[str]
) -> tuple[AssetRecord, ...]:
    if not requested_kinds or not requested_kinds <= {"skill", "mcp_tool"}:
        _fail("invalid_kinds")
    offered = tuple(a for a in snapshot.assets if a.kind in requested_kinds)
    if (
        len(offered) > 1000
        or len(canonical_json([a.to_json_object() for a in offered]).encode("utf-8"))
        > 32 * 1024 * 1024
    ):
        _fail("offer_limit")
    return offered
