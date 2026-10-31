"""Full-offer local semantic/lexical fusion with exact vector identity (RM41)."""

import asyncio
import math
from types import MappingProxyType, SimpleNamespace

from relaymind.assets.embedding_config import SemanticConfig
from relaymind.assets.embeddings import (
    EmbeddingError,
    cosine,
    identity_keys,
    normalize_vectors,
    represent_record,
)
from relaymind.canonical import canonical_digest
from relaymind.schema_util import freeze_json
from relaymind.selection.asset_lexical import _token_batches, record_fields


class HybridIndex:
    """A compatible immutable-vector publication; job/store ownership surrounds it."""

    def __init__(self, config: SemanticConfig, scope):
        self.config, self.scope = config, freeze_json(scope)
        self._publication = (MappingProxyType({}), None, MappingProxyType({}))

    @property
    def _entries(self):
        return self._publication[0]

    @property
    def revision(self):
        return self._publication[1]

    def pin(self):
        return self._publication

    def key(self, record) -> str:
        representation = represent_record(
            record, self.config.data["representation"]["document_prefix"]
        )
        manifest = self.config.manifest
        return canonical_digest(
            dict(
                scope=self.scope,
                asset_id=record["asset_id"],
                content_hash=record["content_hash"],
                source_version=record["source_version"],
                schema_hash=record["identity"].get("schema_hash"),
                representation_revision=representation.revision,
                bytes_digest=representation.byte_digest,
                segmentation_digest=representation.segmentation_digest,
                manifest_digest=manifest.manifest_digest,
                artifact_hash=manifest.artifact_hash,
                runtime=manifest.data["runtime"],
                device=manifest.data["device"],
                config_digest=self.config.digest,
            )
        )

    def compatible(self, record, *, publication=None):
        pinned = self._publication if publication is None else publication
        cached = self.prepared(record, publication=pinned)
        key = self.key(record) if cached is None else cached[1]
        return pinned[0].get(key)

    def prepared(self, record, *, publication):
        cache = publication[2] if len(publication) > 2 else {}
        value = cache.get(record["asset_id"])
        return value if value is not None and value[0] == _signature(record) else None

    def publish_batch(self, records, vectors, *, check=lambda: None) -> None:
        # Engine returns normalized binary64 vectors. Validate but do not normalize
        # a second time: that would change the fixed operation-order result.
        normalize_vectors(
            vectors, count=len(records), dimensions=self.config.manifest.data["dimensions"]
        )
        staged = dict(self._entries)
        prepared = dict(self._publication[2])
        for record, vector in zip(records, vectors, strict=True):
            check()
            total = 0.0
            for value in vector:
                total = total + float(value) * float(value)
            if not math.isfinite(total) or abs(total - 1.0) > 1e-12:
                raise EmbeddingError("unnormalized_vector")
            key = self.key(record)
            staged[key] = tuple(float(x) for x in vector)
            # Public record tokens/keys are prepared by the background owner,
            # pinned atomically with vectors, and never include query text.
            prepared[record["asset_id"]] = (_signature(record), key, _fields(record))
        check()
        revision = _revision(staged)
        self._publication = (MappingProxyType(staged), revision, MappingProxyType(prepared))

    def reconcile(self, records) -> None:
        keys = {self.key(record) for record in records}
        entries = {key: value for key, value in self._entries.items() if key in keys}
        revision = _revision(entries) if entries else None
        prepared = {
            asset: value for asset, value in self._publication[2].items() if value[1] in entries
        }
        self._publication = (MappingProxyType(entries), revision, MappingProxyType(prepared))


def _revision(entries):
    return "hybrid:" + canonical_digest(
        [dict(key=key, vector=entries[key]) for key in sorted(entries)]
    )


def _fields(record):
    return record_fields(
        SimpleNamespace(
            kind=record["kind"], description=record["description"], identity=record["identity"]
        )
    )


def _field_names(record):
    return ("/description", *(f"/identity/{key}" for key in identity_keys(record["kind"])))


def _signature(record):
    # Exact relevant public values; immutable scalars only, no mutable record alias.
    return (
        record["asset_id"],
        record["kind"],
        record["description"],
        record["content_hash"],
        record["source_version"],
        record["identity"].get("schema_hash"),
        *(record["identity"][key] for key in identity_keys(record["kind"])),
    )


def _tokens(value):
    tokens = set()
    for part in _token_batches(value):
        yield
        tokens.update(part)
    return frozenset(tokens)


def _field_steps(record):
    fields = []
    for pointer in _field_names(record):
        value = (
            record["description"]
            if pointer == "/description"
            else record["identity"][pointer.split("/")[-1]]
        )
        fields.append((pointer, (yield from _tokens(value))))
    return tuple(fields)


def _compatibility_steps(records, index, publication):
    vectors = {}
    for record in records:
        yield
        vectors[record["asset_id"]] = index.compatible(record, publication=publication)
    return vectors


async def _cooperative(steps, context):
    # No detached CPU task: each bounded unit remains under the request's
    # original clock/cancellation owner, including an all-missing scan.
    while True:
        context.check()
        try:
            next(steps)
        except StopIteration as done:
            context.check()
            return done.value
        if context.yield_control is None:
            await asyncio.sleep(0)
        else:
            await context.yield_control()
        context.check()


def _not_ready(req, config: SemanticConfig, publication) -> dict:
    return dict(
        schema_version=2,
        recommendation_id=req["recommendation_id"],
        catalog_revision=req["catalog_revision"],
        host_snapshot_revision=req["host_snapshot_revision"],
        selector=dict(config.data["identity"]),
        mechanism="hybrid_weighted_rrf",
        outcome="not_ready",
        decision=None,
        candidates=[],
        index_revision=publication[1],
        elapsed_ms=0,
        usage_claims=None,
    )


def _rank_steps(
    request,
    config: SemanticConfig,
    index: HybridIndex,
    query_vector,
    *,
    publication=None,
    vectors=None,
):
    req = request.data if hasattr(request, "data") else request
    publication = index.pin() if publication is None else publication
    result = _not_ready(req, config, publication)
    if req["scope"] != index.scope:
        result["outcome"] = "unavailable"
        return result
    offered = {record["asset_id"]: record for record in req["catalog"]}
    if vectors is None:
        vectors = yield from _compatibility_steps(req["catalog"], index, publication)
    if not any(vector is not None for vector in vectors.values()):
        return result
    query = yield from _tokens(req["task_text"])
    lexical, semantic, lexical_pointers = [], [], {}
    fusion = config.data["fusion"]
    for key, record in offered.items():
        yield
        cached = index.prepared(record, publication=publication)
        fields = cached[2] if cached is not None else (yield from _field_steps(record))
        overlapping = [(pointer, query & tokens) for pointer, tokens in fields]
        overlap = set().union(*(tokens for _, tokens in overlapping))
        if overlap:
            lexical.append((key, len(overlap)))
            lexical_pointers[key] = {pointer for pointer, tokens in overlapping if tokens}
        vector = vectors[key]
        if vector is not None:
            score = cosine(query_vector, vector)
            if score >= fusion["min_cosine"]:
                semantic.append((key, score))
    yield
    lexical.sort(key=lambda pair: (-pair[1], pair[0].encode("utf-8")))
    semantic.sort(key=lambda pair: (-pair[1], pair[0].encode("utf-8")))
    lex_rank = {key: rank for rank, (key, _) in enumerate(lexical, 1)}
    sem_rank = {key: rank for rank, (key, _) in enumerate(semantic, 1)}
    candidates = []
    for key in lex_rank.keys() | sem_rank.keys():
        yield
        lex_term = (
            fusion["lexical_weight"] / (fusion["k"] + lex_rank[key]) if key in lex_rank else 0.0
        )
        sem_term = (
            fusion["semantic_weight"] / (fusion["k"] + sem_rank[key]) if key in sem_rank else 0.0
        )
        record = offered[key]
        fields = _field_names(record)
        pointers = set(fields) if key in sem_rank else set()
        pointers.update(lexical_pointers.get(key, ()))
        candidates.append(
            dict(
                asset_id=key,
                kind=record["kind"],
                content_hash=record["content_hash"],
                source_version=record["source_version"],
                score=lex_term + sem_term,
                score_kind="weighted_rrf.v1",
                evidence_refs=[p for p in fields if p in pointers],
            )
        )
    yield
    candidates.sort(key=lambda c: (-c["score"], c["asset_id"].encode("utf-8")))
    candidates = candidates[: req["candidate_limit"]]
    if not candidates:
        result.update(outcome="no_match", decision="no_match")
        return result
    top, second = candidates[0]["score"], candidates[1]["score"] if len(candidates) > 1 else 0.0
    confidence = config.data["confidence"]
    confident = (
        all(vector is not None for vector in vectors.values())
        and top >= confidence["min_score"]
        and top - second >= confidence["min_margin"]
    )
    result.update(
        outcome="ranked",
        decision="confident" if confident else "choice_required",
        candidates=candidates,
    )
    return result


def rank_hybrid(
    request, config: SemanticConfig, index: HybridIndex, query_vector, *, publication=None
) -> dict:
    """Pure deterministic kernel; the production selector drives the same steps cooperatively."""
    steps = _rank_steps(request, config, index, query_vector, publication=publication)
    while True:
        try:
            next(steps)
        except StopIteration as done:
            return done.value


class HybridAssetSelector:
    """Production v2 selector; only its coordinator may dispatch a lexical floor."""

    builtin = True
    mechanism = "hybrid_weighted_rrf"
    # Never announce a previous request's dynamic usage before this dispatch.
    observed_cost = None

    def __init__(self, config, index, engine):
        self.config, self.index, self.engine = config, index, engine
        self.name, self.version = (
            config.data["identity"]["name"],
            config.data["identity"]["version"],
        )
        self.observations = ()

    async def select(self, request, context):
        from relaymind.assets.embedding_backends import EmbeddingFailure
        from relaymind.assets.embeddings import represent_query
        from relaymind.selection.asset_contract import AttemptFailure

        start = context.clock()
        self.observations = ()
        context.check()
        req = request.data
        publication = self.index.pin()
        vectors = await _cooperative(
            _compatibility_steps(req["catalog"], self.index, publication), context
        )
        if req["scope"] != self.index.scope or not any(v is not None for v in vectors.values()):
            return await _cooperative(
                _rank_steps(
                    request, self.config, self.index, None, publication=publication, vectors=vectors
                ),
                context,
            )
        if self.engine is None:
            return _not_ready(req, self.config, publication)
        try:
            representation = self.config.data["representation"]
            batch = await self.engine.embed(
                [represent_query(req["task_text"], representation["query_prefix"])],
                max_tokens=representation["max_tokens"],
                deadline=start + req["deadline_ms"] / 1000,
                clock=context.clock,
                observe=context.observe_embedding,
            )
            self.observations = (batch.observation,)
        except EmbeddingFailure as failure:
            self.observations = failure.observations
            if str(failure) == "embedding_busy":
                raise AttemptFailure("not_ready", "selector_not_ready") from None
            if str(failure) == "embedding_cancelled":
                raise AttemptFailure("cancelled", "cancelled") from None
            if str(failure) == "embedding_deadline":
                raise AttemptFailure("timeout", "deadline_exceeded") from None
            raise AttemptFailure("unavailable", "selector_unavailable") from None
        context.check()
        result = await _cooperative(
            _rank_steps(
                request,
                self.config,
                self.index,
                batch.vectors[0],
                publication=publication,
                vectors=vectors,
            ),
            context,
        )
        result["elapsed_ms"] = max(0, (context.clock() - start) * 1000)
        context.check()
        return result
