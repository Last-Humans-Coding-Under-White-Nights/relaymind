"""Closed asset contracts and safe searchable projections (RM34, AC25).

Catalog data never conveys execution authority or proves host availability.
"""

import copy
import math
import re
import unicodedata
from collections import Counter
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any

from jsonschema import SchemaError
from jsonschema.protocols import Validator

from relaymind.canonical import canonical_digest, canonical_json
from relaymind.redaction import Redactor, redact_text, scrub
from relaymind.schema_util import StrictValidator, freeze_json, load_packaged_schema
from relaymind.tools.registry import check_parameters_schema

# Spec 14.5 offer caps: one offered projection, in records and canonical bytes.
MAX_OFFER_RECORDS = 1000
MAX_OFFER_BYTES = 32 * 1024 * 1024
# Acquisition limits (spec 14.5). Capture enforces these and queued jobs record them.
READ_BUFFER_BYTES = 1024 * 1024
MAX_JOB_RECORDS = 1000
MAX_JOB_RECEIVED_BYTES = 100 * 1024 * 1024
MAX_ASSET_BYTES = 10 * READ_BUFFER_BYTES
MAX_METADATA_BYTES = 4 * READ_BUFFER_BYTES
JOB_SECONDS = 60


def acquisition_budgets() -> dict[str, Any]:
    """The budgets a refresh job records: the same values capture enforces (RM03)."""
    return dict(
        max_requests=1000,
        max_records=MAX_JOB_RECORDS,
        max_received_bytes=MAX_JOB_RECEIVED_BYTES,
        max_asset_bytes=MAX_ASSET_BYTES,
        max_metadata_bytes=MAX_METADATA_BYTES,
        read_buffer_bytes=READ_BUFFER_BYTES,
        exchange_seconds=10,
        job_seconds=JOB_SECONDS,
        workers=1,
        cpu_batch_ms=50,
        background_gpu_with_llama=False,
    )


class CatalogUnavailable(RuntimeError):
    """Committed catalog data is corrupt; never translate this to healthy empty."""


class AssetValidationError(ValueError):
    """A bounded safe reason, never source text or parser exception text."""


# Built once. Strict integers and const/enum: 1.0 and true are not 1 (RM09 rule).
_RECORD_VALIDATOR = StrictValidator(load_packaged_schema("relaymind.store", "asset_record.v1.json"))
_SNAPSHOT_VALIDATOR = StrictValidator(
    load_packaged_schema("relaymind.store", "asset_catalog_snapshot.v1.json")
)
LANGUAGE_TAG = re.compile(r"[A-Za-z0-9-]{1,64}")
_MARKUP = re.compile(r"<[A-Za-z!?/]")
_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)$")


def _fail(reason: str) -> None:
    raise AssetValidationError(reason)


def text(value: object, limit: int, *, empty: bool = False, body: bool = False) -> str:
    """Validate projected text with UTF-8 byte, control and repetition bounds.

    A skill instruction body (``body=True``) is internal searchable data, never
    offered, rendered or ranked: it keeps the encoding, NFC, control and byte
    bounds, and skips the markup and repeated-line rules (spec 0.66 section 4.6).
    """
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
    if body:
        return normalized
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


def validate_document(value: object, validator: Validator) -> dict[str, Any]:
    """A plain-JSON object that passes one compiled closed schema, as a deep copy."""
    try:
        _json(value)
        if not isinstance(value, Mapping):
            _fail("invalid_schema")
        if next(validator.iter_errors(value), None) is not None:
            _fail("invalid_schema")
        return copy.deepcopy(dict(value))
    except (RecursionError, UnicodeError, TypeError, OverflowError):
        raise AssetValidationError("invalid_json") from None


def check_language_tags(value: object) -> None:
    """At most 32 unique whole-string tags, the asset_record language_tags rule."""
    if (
        not isinstance(value, list)
        or len(value) > 32
        or any(not isinstance(v, str) or not LANGUAGE_TAG.fullmatch(v) for v in value)
        or len(set(value)) != len(value)
    ):
        _fail("invalid_language")


def timestamp(value: object, *, nullable: bool = False) -> None:
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
    # The schema's anchored patterns already fix the hash and language tag forms.
    raw = validate_document(value, _RECORD_VALIDATOR)
    timestamp(raw["captured_at"])
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
    # Internal committed bytes; never accepted from an untrusted wire snapshot.
    record_bytes: tuple[bytes, ...] = field(default=(), repr=False, compare=False)

    def to_json_object(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "catalog_revision": self.catalog_revision,
            "published_at": self.published_at,
            "sources": copy.deepcopy(list(self.sources)),
            "assets": [a.to_json_object() for a in self.assets],
        }


def validate_catalog_snapshot(value: object) -> CatalogSnapshot:
    raw = validate_document(value, _SNAPSHOT_VALIDATOR)
    timestamp(raw["published_at"])
    source_ids = {s["source_id"] for s in raw["sources"]}
    if len(source_ids) != len(raw["sources"]):
        _fail("duplicate_source")
    for source in raw["sources"]:
        text(source["source_id"], 256)
        for key in ("captured_at", "last_success_at"):
            timestamp(source[key], nullable=True)
        if source["source_version"] is not None:
            text(source["source_version"], 256)
        if source["complete"] and source["exclusions"]:
            _fail("incomplete_coverage")
        for exclusion in source["exclusions"]:
            text(exclusion["reason"], 256)
            if exclusion["asset_id"] is not None:
                text(exclusion["asset_id"], 256)
    assets = tuple(validate_asset_record(a) for a in raw["assets"])
    if len({a.asset_id for a in assets}) != len(assets):
        _fail("duplicate_asset")
    if any(a.source_id not in source_ids for a in assets):
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


def _safe_free_text(value: str, limit: int, secrets: Collection[str], *, body: bool = False) -> str:
    # Preserve the pre-normalization byte cap, then scrub at both sides of NFC.
    try:
        if len(value.encode("utf-8")) > limit:
            _fail("size_limit")
    except UnicodeError:
        raise AssetValidationError("invalid_encoding") from None
    normalized = text(redact_text(value, secrets), limit, body=body)
    return text(redact_text(normalized, secrets), limit, body=body)


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
    # The record is already validated; only its description changes here, and
    # _safe_free_text applies the same text bounds the record validator does.
    projected = replace(record, description=_safe_free_text(raw["description"], 8192, secrets))
    normalized_body = None if body is None else _safe_free_text(body, 262144, secrets, body=True)
    return SearchDocument(projected, normalized_body, projection_digest(projected, normalized_body))


def projection_digest(record: AssetRecord, body: str | None) -> str:
    """The content identity of one projection; observation times and staleness excluded."""
    value = record.to_json_object()
    value.pop("captured_at")
    value.pop("stale")
    return canonical_digest({"record": value, "body": body})


def discovery_records(
    snapshot: CatalogSnapshot, requested_kinds: frozenset[str]
) -> tuple[AssetRecord, ...]:
    if not requested_kinds or not requested_kinds <= {"skill", "mcp_tool"}:
        _fail("invalid_kinds")
    offered = tuple(a for a in snapshot.assets if a.kind in requested_kinds)
    if (
        len(offered) > MAX_OFFER_RECORDS
        or len(canonical_json([a.to_json_object() for a in offered]).encode("utf-8"))
        > MAX_OFFER_BYTES
    ):
        _fail("offer_limit")
    return offered


def failure_exclusions(reason: str, asset_id: str | None = None) -> tuple[Mapping[str, Any], ...]:
    """The one recorded exclusion of a failed acquisition: a safe reason, no content."""
    return (freeze_json({"asset_id": asset_id, "reason": reason}),)


# Plain capture data shared by the staging reader and the store; the store never
# depends on the reader (AGENTS.md ownership boundaries).


@dataclass(frozen=True)
class InventoryAsset:
    record: AssetRecord
    relative_package_path: str | None


@dataclass(frozen=True)
class SourceInventory:
    source_id: str
    revision: int = 0
    records: tuple[InventoryAsset, ...] = ()
    tombstones: frozenset[str] = frozenset()
    owners: Mapping[str, str] = field(default_factory=lambda: freeze_json({}))

    @classmethod
    def empty(cls, source_id: str) -> "SourceInventory":
        return cls(source_id)


@dataclass(frozen=True)
class StagedFile:
    relative_path: str
    sha256: str
    size: int


@dataclass(frozen=True)
class SourceCapture:
    source_id: str
    config_digest: str
    source_digest: str
    source_version: str | None
    captured_at: str
    documents: tuple[SearchDocument, ...]
    files: tuple[StagedFile, ...]
    complete: bool
    removed_asset_ids: frozenset[str]
    package_paths: Mapping[str, str]
    received_bytes: int
    batch_durations_ms: tuple[float, ...] = ()
    # Spec 0.66 section 4.6.1: candidates a partial local scan rejected on their own.
    rejected_count: int = 0
    exclusions: tuple[Mapping[str, Any], ...] = ()
