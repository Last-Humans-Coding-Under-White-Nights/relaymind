"""Closed recommendation-only contracts (RM35/AC26, spec sections 4.6.2/8.1).

No model context, execution run, feedback corpus or private skill body crosses
this boundary. Parsing and offer/current admission are deliberately separate.
"""

import copy
import hashlib
import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any, Protocol

from relaymind.assets.catalog import (
    MAX_OFFER_BYTES,
    AssetValidationError,
    timestamp,
    validate_asset_record,
    validate_document,
)
from relaymind.canonical import canonical_json
from relaymind.schema_util import (
    FrozenJSONObject,
    StrictValidator,
    freeze_json,
    load_packaged_schema,
)

REQUEST_SCHEMA = load_packaged_schema("relaymind.selection", "selection_request.v2.json")
RESULT_SCHEMA = load_packaged_schema("relaymind.selection", "selection_result.v2.json")
RECOMMENDATION_SCHEMA = load_packaged_schema("relaymind.store", "recommendation_result.v1.json")
_ENVELOPE_SCHEMA = copy.deepcopy(REQUEST_SCHEMA)
_ENVELOPE_SCHEMA["required"].remove("catalog")
del _ENVELOPE_SCHEMA["properties"]["catalog"]
# Strict integers and consts: 60.0 is not 60 and true is not 1 at any v2 boundary.
_VALIDATORS = {
    name: StrictValidator(schema)
    for name, schema in (
        ("request", REQUEST_SCHEMA),
        ("result", RESULT_SCHEMA),
        ("recommendation", RECOMMENDATION_SCHEMA),
        ("envelope", _ENVELOPE_SCHEMA),
    )
}
_HASH_FIELDS = {"content_hash", "schema_hash", "artifact_hash"}
_TEXT_EXCEPTIONS = {
    "task_text",
    "text",
    "description",
    "source_locator",
    "checked_at",
    "captured_at",
}


class AssetSelectionError(ValueError):
    """Fixed safe validation reason; contains no untrusted text."""


@dataclass(frozen=True)
class SelectorIdentity:
    name: str
    version: str

    def to_json_object(self) -> dict[str, str]:
        return {"name": self.name, "version": self.version}


class AttemptFailure(Exception):
    def __init__(self, outcome: str, reason: str, result: "AssetSelectionResult | None" = None):
        super().__init__(reason)
        self.outcome, self.reason, self.result = outcome, reason, result


@dataclass(frozen=True)
class _FrozenJSONData:
    data: FrozenJSONObject

    def to_json_object(self) -> dict[str, Any]:
        return copy.deepcopy(self.data)


class AssetSelectionResult(_FrozenJSONData):
    """One parsed SelectionResultV2."""


class RecommendationSelectionResult(_FrozenJSONData):
    """One parsed RecommendationResultV1."""


@dataclass(frozen=True)
class AssetSelectionRequest(_FrozenJSONData):
    catalog_bytes: bytes = b""
    offer_identity: str = ""
    # The offer's asset_id map, built once at publication when one exists.
    offered_by_id: Mapping[str, Mapping] | None = field(default=None, repr=False, compare=False)

    @cached_property
    def offered(self) -> Mapping[str, Mapping]:
        if self.offered_by_id is not None:
            return self.offered_by_id
        return {record["asset_id"]: record for record in self.data["catalog"]}

    def with_deadline(self, deadline_ms: float) -> "AssetSelectionRequest":
        if (
            type(deadline_ms) not in (int, float)
            or not math.isfinite(deadline_ms)
            or deadline_ms <= 0
        ):
            raise AssetSelectionError("invalid_request")
        return AssetSelectionRequest(
            FrozenJSONObject({**self.data, "deadline_ms": deadline_ms}),
            self.catalog_bytes,
            self.offer_identity,
            self.offered_by_id,
        )

    def to_bytes(self, context: "SelectorContext") -> bytes:
        context.check()
        envelope = {key: value for key, value in self.data.items() if key != "catalog"}
        encoded = canonical_json(envelope).encode("utf-8")
        payload = b'{"catalog":' + self.catalog_bytes + b"," + encoded[1:]
        context.check()
        return payload


def _bytes(value: str, limit: int, *, nonempty: bool = True) -> None:
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError:
        raise AssetSelectionError("invalid_result") from None
    if size > limit or (nonempty and not value.strip()):
        raise AssetSelectionError("invalid_result")


def _bounded_labels(value: object) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in ("input_schema", "catalog"):
                continue
            if isinstance(item, str) and key not in _TEXT_EXCEPTIONS | _HASH_FIELDS:
                _bytes(item, 256)
            _bounded_labels(item)
    elif isinstance(value, list | tuple):
        for item in value:
            _bounded_labels(item)


# A caller's request or envelope is invalid_request; invalid_result is for selector output.
_REQUEST_KINDS = frozenset({"request", "envelope"})


def _check(kind: str, value: object) -> dict[str, Any]:
    code = "invalid_request" if kind in _REQUEST_KINDS else "invalid_result"
    try:
        _precheck_pointers(kind, value)
        raw = validate_document(value, _VALIDATORS[kind])
        _bounded_labels(raw)
        return raw
    except (ValueError, TypeError, RecursionError, OverflowError, UnicodeError):
        raise AssetSelectionError(code) from None


def _check_pointer(pointer: str) -> None:
    _bytes(pointer, 256, nonempty=False)
    if re.search(r"~(?![01])", pointer) or (pointer and not pointer.startswith("/")):
        raise AssetSelectionError("invalid_result")


def _precheck_pointers(kind: str, value: object) -> None:
    """Reject bad escapes/bytes before schema work at known result locations."""
    if not isinstance(value, Mapping):
        return
    pools = [value.get("candidates")] if kind == "result" else []
    if kind == "recommendation":
        pools.extend([value.get("base_candidates"), value.get("final_candidates")])
        attempts = value.get("attempts")
        if isinstance(attempts, list | tuple):
            for attempt in attempts:
                if isinstance(attempt, Mapping) and isinstance(attempt.get("result"), Mapping):
                    pools.append(attempt["result"].get("candidates"))
    for candidates in pools:
        if not isinstance(candidates, list | tuple):
            continue
        for candidate in candidates:
            if not isinstance(candidate, Mapping):
                continue
            pointers = candidate.get("evidence_refs")
            if isinstance(pointers, list | tuple):
                for pointer in pointers:
                    if isinstance(pointer, str):
                        _check_pointer(pointer)


def _check_candidates(candidates: list[dict]) -> None:
    # Evidence pointers were already checked by _precheck_pointers in _check.
    if len({c["asset_id"] for c in candidates}) != len(candidates):
        raise AssetSelectionError("invalid_result")


def parse_asset_request(value: object) -> AssetSelectionRequest:
    raw = _check("request", value)
    try:
        _check_query(raw)
        if len({r["asset_id"] for r in raw["catalog"]}) != len(raw["catalog"]):
            raise AssetSelectionError("invalid_request")
        for record in raw["catalog"]:
            validate_asset_record(record)
            if record["kind"] not in raw["requested_kinds"]:
                raise AssetSelectionError("invalid_request")
        catalog_bytes = canonical_json(raw["catalog"]).encode("utf-8")
        if len(catalog_bytes) > MAX_OFFER_BYTES:
            raise AssetSelectionError("invalid_request")
    except (AssetSelectionError, AssetValidationError, UnicodeError):
        raise AssetSelectionError("invalid_request") from None
    return AssetSelectionRequest(
        freeze_json(raw), catalog_bytes, hashlib.sha256(catalog_bytes).hexdigest()
    )


def parse_asset_result(value: object) -> AssetSelectionResult:
    raw = _check("result", value)
    _check_candidates(raw["candidates"])
    return AssetSelectionResult(freeze_json(raw))


def parse_recommendation_result(value: object) -> RecommendationSelectionResult:
    raw = _check("recommendation", value)
    if raw["decision"] != raw["final_decision"]:
        raise AssetSelectionError("invalid_result")
    for name in ("base_candidates", "final_candidates"):
        _check_candidates(raw[name])
        decision = raw[name.replace("candidates", "decision")]
        if bool(raw[name]) != (decision in ("confident", "choice_required")):
            raise AssetSelectionError("invalid_result")
    attempts = raw["attempts"]
    if [a["attempt"] for a in attempts] != list(range(1, len(attempts) + 1)):
        raise AssetSelectionError("invalid_result")
    if raw["decision"] == "empty_catalog":
        if (
            attempts
            or raw["primary_attempt"] is not None
            or raw["base_decision"] != "empty_catalog"
        ):
            raise AssetSelectionError("invalid_result")
    elif raw["primary_attempt"] != 0 or not attempts:
        raise AssetSelectionError("invalid_result")
    if raw["adaptation"] is None and (
        raw["base_candidates"] != raw["final_candidates"]
        or raw["base_decision"] != raw["final_decision"]
    ):
        raise AssetSelectionError("invalid_result")
    # The recommendation schema already validated each embedded SelectionResultV2.
    for attempt in attempts:
        if attempt["result"] is not None:
            _check_candidates(attempt["result"]["candidates"])
    try:
        timestamp(raw["admission"]["checked_at"])
    except AssetValidationError:
        raise AssetSelectionError("invalid_result") from None
    return RecommendationSelectionResult(freeze_json(raw))


@dataclass(frozen=True)
class SelectorContext:
    clock: Callable[[], float]
    check: Callable[[], None]


class AssetSelector(Protocol):
    """A selector proposes; the gateway admits (RM28, RM35).

    A selector class may also declare its capabilities, which the chain reads
    instead of testing for concrete types: ``builtin`` (a gateway-owned
    selector whose dependency failure is a declared unavailable attempt),
    ``transport`` (``"http"`` for a remote service, whose results may not
    claim reserved mechanisms), ``mechanism`` (the attempt label before a
    result names one) and ``observed_cost`` (a cost the gateway itself
    measured, such as zero inference for lexical).
    """

    name: str
    version: str

    async def select(self, request: AssetSelectionRequest, context: SelectorContext) -> object: ...


@dataclass(frozen=True)
class PublishedOffer:
    """One trusted, immutable publication; no private search text is retained."""

    catalog_revision: int
    catalog: object
    catalog_bytes: bytes
    identity: str
    scope: FrozenJSONObject
    permissions: tuple[str, ...]
    requested_kinds: tuple[str, ...]
    index: object
    offered_by_id: Mapping[str, Mapping]

    def request(self, envelope: Mapping, *, context: SelectorContext) -> AssetSelectionRequest:
        context.check()
        raw = _check("envelope", envelope)
        try:
            _check_query(raw)
        except (AssetSelectionError, UnicodeError):
            # Caller input, classified the same way as parse_asset_request.
            raise AssetSelectionError("invalid_request") from None
        if (
            raw["scope"] != self.scope
            or set(raw["permissions"]) != set(self.permissions)
            or sorted(raw["requested_kinds"]) != sorted(self.requested_kinds)
            or raw["catalog_revision"] != self.catalog_revision
            or raw["selection_scope"] != "discovery"
        ):
            raise AssetSelectionError("invalid_request")
        data = freeze_json(raw)
        # Only the small envelope is frozen. The catalog was frozen at publication.
        request = AssetSelectionRequest(
            FrozenJSONObject({**data, "catalog": self.catalog}),
            self.catalog_bytes,
            self.identity,
            self.offered_by_id,
        )
        context.check()
        return request


def _check_query(raw: Mapping) -> None:
    _bytes(raw["task_text"], 8192)
    if sum(len(m["text"].encode("utf-8")) for m in raw["conversation"]) > 32768:
        raise AssetSelectionError("invalid_request")
    for permission in raw["permissions"]:
        _bytes(permission, 256)
