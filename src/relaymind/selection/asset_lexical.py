"""Deterministic projection-only lexical floor; no inference/calibration (RM35)."""

import asyncio
import hashlib
import json
import re
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from types import MappingProxyType

from relaymind.assets.catalog import AssetRecord, CatalogSnapshot, validate_asset_record
from relaymind.canonical import canonical_json
from relaymind.schema_util import freeze_json
from relaymind.selection.asset_contract import (
    AssetSelectionRequest,
    AttemptFailure,
    PublishedOffer,
    SelectorContext,
)

# Han extensions are included; punctuation and field boundaries never join runs.
_HAN = "\\u3400-\\u4dbf\\u4e00-\\u9fff\\uf900-\\ufaff\\U00020000-\\U000323af"
_HAN_CHAR = re.compile(f"[{_HAN}]")
IMPLEMENTATION = "lexical/1:nfc-casefold-han-bigrams/1"


def tokenize(value: str) -> frozenset[str]:
    result = set()
    # Split mixed Han/Latin segments explicitly, while keeping other letters/numbers.
    value = unicodedata.normalize("NFC", value).casefold()
    run, han = "", None
    for char in value + " ":
        category = "han" if _HAN_CHAR.fullmatch(char) else "word" if char.isalnum() else None
        if category != han or category is None:
            if run:
                result.update(
                    (run[i : i + 2] for i in range(len(run) - 1))
                    if han == "han" and len(run) > 1
                    else [run]
                )
            run = ""
        if category is not None:
            run += char
        han = category
    return frozenset(result)


@dataclass(frozen=True)
class LexicalIndex:
    catalog_revision: int
    offer_identity: str
    index_revision: str
    records: tuple[AssetRecord, ...]
    fields: object


async def build_lexical_index(
    catalog_revision: int,
    records: Sequence[AssetRecord],
    *,
    context: SelectorContext,
    catalog_bytes: bytes | None = None,
) -> LexicalIndex:
    context.check()
    fields = {}
    for i, record in enumerate(records):
        context.check()
        values = {"/description": record.description}
        if record.kind == "skill":
            values["/identity/package_id"] = record.identity["package_id"]
        else:
            for key in ("server_id", "tool_name", "exposed_name"):
                values[f"/identity/{key}"] = record.identity[key]
        fields[record.asset_id] = tuple((key, tokenize(value)) for key, value in values.items())
        if (i + 1) % 25 == 0:
            await asyncio.sleep(0)
    if catalog_bytes is None:
        catalog_bytes = canonical_json([r.to_json_object() for r in records]).encode("utf-8")
    digest = hashlib.sha256(catalog_bytes).hexdigest()
    revision = hashlib.sha256(f"{IMPLEMENTATION}:{catalog_revision}:{digest}".encode()).hexdigest()
    context.check()
    return LexicalIndex(
        catalog_revision, digest, revision, tuple(records), MappingProxyType(fields)
    )


class LexicalAssetSelector:
    name = "lexical"
    version = "1"
    mechanism = "lexical_overlap"

    def __init__(self, index: LexicalIndex | None):
        self.index = index

    async def select(self, request: AssetSelectionRequest, context: SelectorContext) -> object:
        start = context.clock()
        context.check()
        req, index = request.data, self.index
        base = dict(
            schema_version=2,
            recommendation_id=req["recommendation_id"],
            catalog_revision=req["catalog_revision"],
            host_snapshot_revision=req["host_snapshot_revision"],
            selector={"name": self.name, "version": self.version},
            mechanism=self.mechanism,
            candidates=[],
            index_revision=None if index is None else index.index_revision,
            elapsed_ms=0,
            usage_claims=None,
        )
        if (
            index is None
            or index.catalog_revision != req["catalog_revision"]
            or index.offer_identity != request.offer_identity
        ):
            return base | dict(outcome="not_ready", decision=None)
        query = tokenize(req["task_text"])
        ranked = []
        for i, record in enumerate(index.records):
            context.check()
            fields = [
                (pointer, query & tokens) for pointer, tokens in index.fields[record.asset_id]
            ]
            overlap = set().union(*(tokens for _, tokens in fields))
            if overlap:
                ranked.append(
                    dict(
                        asset_id=record.asset_id,
                        kind=record.kind,
                        content_hash=record.content_hash,
                        source_version=record.source_version,
                        score=len(overlap),
                        score_kind="token_overlap_count",
                        evidence_refs=[pointer for pointer, tokens in fields if tokens],
                    )
                )
            if (i + 1) % 25 == 0:
                await asyncio.sleep(0)
        ranked.sort(key=lambda c: (-c["score"], c["asset_id"].encode("utf-8")))
        context.check()
        return base | dict(
            outcome="ranked" if ranked else "no_match",
            decision="choice_required" if ranked else "no_match",
            candidates=ranked[: req["candidate_limit"]],
            elapsed_ms=max(0, (context.clock() - start) * 1000),
        )


class OfferCache:
    """Explicit compatible-publication primitive; get never rebuilds missing data.

    Public catalog_revision is the semantic identity, including committed stale,
    availability and eligibility changes. The epoch only fences concurrent
    preparation. Operational update wiring/readiness remains #194/#196.
    """

    def __init__(self, *, current_revision: Callable[[], int] | None = None):
        self._revision = None
        self._offers = {}
        self._epoch = 0
        self._last_records = None
        self._last_revision = None
        self._current_revision = current_revision

    def invalidate(self) -> None:
        self._epoch += 1
        self._offers = {}
        self._revision = None

    async def publish(
        self,
        snapshot: CatalogSnapshot,
        *,
        scope: dict,
        permissions: Sequence[str],
        policy: Callable[[AssetRecord], bool],
        context: SelectorContext,
        catalog_revision: int | None = None,
    ) -> None:
        # Prepare beside the last compatible publication. An explicit
        # invalidate still fences it when authoritative state changes.
        self._epoch += 1
        epoch = self._epoch
        context.check()
        if catalog_revision is not None and catalog_revision != snapshot.catalog_revision:
            raise AttemptFailure("unavailable", "selector_unavailable")
        records = []
        encoded_records = []
        if snapshot.record_bytes and len(snapshot.record_bytes) != len(snapshot.assets):
            raise AttemptFailure("unavailable", "selector_unavailable")
        for i, raw in enumerate(snapshot.assets):
            context.check()
            if snapshot.record_bytes:
                # Stored bytes were validated/serialized by the publication owner.
                # Match positions/identities without reserializing their records.
                encoded = snapshot.record_bytes[i]
                if json.loads(encoded) != raw.to_json_object():
                    raise AttemptFailure("unavailable", "selector_unavailable")
                record = raw
            else:
                # Trusted in-process construction without a stored byte carrier.
                record = validate_asset_record(raw.to_json_object())
                encoded = canonical_json(record.to_json_object()).encode("utf-8")
            if policy(record):
                records.append(record)
                encoded_records.append(encoded)
            if (i + 1) % 25 == 0:
                await asyncio.sleep(0)
        records = tuple(records)
        if (
            self._last_revision is not None
            and snapshot.catalog_revision < self._last_revision
            or self._last_revision == snapshot.catalog_revision
            and self._last_records != records
        ):
            # A policy/availability/freshness change cannot create a second offer
            # under the old revision. No live policy mutation is enabled here.
            self.invalidate()
            raise AttemptFailure("unavailable", "selector_unavailable")
        prepared = {}
        for kinds in (("skill",), ("mcp_tool",), ("mcp_tool", "skill")):
            context.check()
            selected = [
                (record, encoded)
                for record, encoded in zip(records, encoded_records, strict=True)
                if record.kind in kinds
            ]
            eligible = tuple(r for r, _ in selected)
            encoded = b"[" + b",".join(value for _, value in selected) + b"]"
            if len(eligible) > 1000 or len(encoded) > 32 * 1024 * 1024:
                raise AttemptFailure("unavailable", "selector_unavailable")
            index = await build_lexical_index(
                snapshot.catalog_revision, eligible, catalog_bytes=encoded, context=context
            )
            prepared[kinds] = PublishedOffer(
                snapshot.catalog_revision,
                eligible,
                freeze_json([r.to_json_object() for r in eligible]),
                encoded,
                hashlib.sha256(encoded).hexdigest(),
                freeze_json(scope),
                tuple(permissions),
                kinds,
                index,
            )
        context.check()
        if (
            self._epoch != epoch
            or self._current_revision is not None
            and self._current_revision() != snapshot.catalog_revision
        ):
            raise AttemptFailure("unavailable", "selector_unavailable")
        self._offers, self._revision = prepared, snapshot.catalog_revision
        self._last_revision, self._last_records = snapshot.catalog_revision, records

    def get(
        self, selection_scope: str, requested_kinds: Sequence[str], *, catalog_revision: int
    ) -> PublishedOffer:
        kinds = tuple(sorted(requested_kinds))
        if (
            selection_scope != "discovery"
            or catalog_revision != self._revision
            or kinds not in self._offers
            or self._current_revision is not None
            and self._current_revision() != catalog_revision
        ):
            raise AttemptFailure("unavailable", "selector_unavailable")
        return self._offers[kinds]
