"""Offer and current-catalog checks, without managed routing or execution (RM35)."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from relaymind.assets.catalog import AssetRecord, CatalogSnapshot
from relaymind.selection.asset_contract import (
    AssetSelectionRequest,
    AssetSelectionResult,
    AttemptFailure,
    SelectorIdentity,
)
from relaymind.selection.chain import BUILT_IN_MECHANISMS, GATEWAY_MECHANISMS

# Every v1 gateway/built-in label stays reserved, plus the two v2 gateway labels.
_RESERVED = (
    GATEWAY_MECHANISMS
    | frozenset(BUILT_IN_MECHANISMS)
    | {"lexical_overlap", "gateway_lexical_fallback"}
)
AssetPolicy = Callable[[AssetRecord, AssetSelectionRequest], bool]


def _fail(result, *, stale=False):
    raise AttemptFailure(
        "stale" if stale else "invalid", "stale_result" if stale else "invalid_result", result
    )


def _pointer(record, pointer):
    value = record
    if not pointer:
        return
    for part in pointer[1:].split("/"):
        key = part.replace("~1", "/").replace("~0", "~")
        if isinstance(value, Mapping):
            if key not in value:
                raise KeyError
            value = value[key]
        elif (
            isinstance(value, list | tuple)
            and key.isascii()
            and key.isdigit()
            and (key == "0" or not key.startswith("0"))
        ):
            value = value[int(key)]
        else:
            raise KeyError


def validate_against_offer(
    result: AssetSelectionResult,
    request: AssetSelectionRequest,
    expected_identity: SelectorIdentity,
    *,
    http=False,
) -> None:
    data, req = result.data, request.data
    if data["selector"] != expected_identity.to_json_object():
        _fail(result)
    if any(
        data[key] != req[key]
        for key in ("recommendation_id", "catalog_revision", "host_snapshot_revision")
    ):
        _fail(result, stale=True)
    mechanism = data["mechanism"]
    if mechanism == "gateway_lexical_fallback" or (http and mechanism in _RESERVED):
        _fail(result)
    for candidate in data["candidates"]:
        record = request.offered.get(candidate["asset_id"])
        if record is None or candidate["kind"] not in req["requested_kinds"]:
            _fail(result)
        if any(candidate[key] != record[key] for key in ("kind", "content_hash", "source_version")):
            _fail(result)
        if "availability" in candidate and candidate["availability"] != record["availability"]:
            _fail(result)
        try:
            for pointer in candidate["evidence_refs"]:
                _pointer(record, pointer)
        except (KeyError, IndexError, ValueError):
            _fail(result)


@dataclass(frozen=True)
class AdmissionEvidence:
    rechecked_catalog_revision: int


def admit_current(
    result: AssetSelectionResult,
    request: AssetSelectionRequest,
    current: CatalogSnapshot,
    policy: AssetPolicy,
) -> AdmissionEvidence:
    if current.catalog_revision != request.data["catalog_revision"]:
        _fail(result, stale=True)
    wanted = {candidate["asset_id"] for candidate in result.data["candidates"]}
    current_records = {r.asset_id: r for r in current.assets if r.asset_id in wanted}
    for candidate in result.data["candidates"]:
        old = request.offered[candidate["asset_id"]]
        record = current_records.get(candidate["asset_id"])
        if record is None:
            _fail(result, stale=True)
        fresh = record.to_json_object()
        # Observation freshness/times can vary without changing content identity.
        if any(
            fresh[k] != old[k] for k in old if k not in {"captured_at", "stale", "availability"}
        ):
            _fail(result, stale=True)
        if fresh["availability"] != old["availability"] or not policy(record, request):
            _fail(result)
    return AdmissionEvidence(current.catalog_revision)
