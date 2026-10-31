"""Fail-closed private similarity stage over one coherent feedback snapshot (RM42)."""

from dataclasses import dataclass

from relaymind.assets.embedding_backends import EmbeddingFailure
from relaymind.assets.embeddings import EmbeddingError, cosine, represent_query
from relaymind.recommendations.adaptation_buffers import PrivateAdaptationBuffers
from relaymind.schema_util import freeze_json
from relaymind.selection.asset_adaptation import adapt_ranking
from relaymind.selection.asset_chain import RecommendationFailure


class AdaptationFailure(RecommendationFailure):
    def __init__(self, code, reason, embeddings=()):
        super().__init__(code, reason)
        self.embeddings = tuple(freeze_json(e) for e in embeddings)


@dataclass(frozen=True)
class AdaptationOutcome:
    candidates: tuple
    decision: str
    adaptation: object
    embeddings: tuple
    guard: object
    close: object


class AdaptationStage:
    def __init__(self, feedback, config, engine, *, buffers=None):
        self.feedback, self.config, self.engine = feedback, config, engine
        self.buffers = PrivateAdaptationBuffers(feedback) if buffers is None else buffers

    def close(self):
        self.buffers.close()

    async def apply(self, primary, request, *, budget, current_catalog, observe_embedding=None):
        req = request.data
        data = primary.data if hasattr(primary, "data") else primary
        budget.check()
        self.feedback.maintain_scope(req["scope"], now=budget.clock.utc_now())
        lease = self.buffers.acquire(req["scope"])
        observations = ()
        snapshot = None
        metadata = None
        feedback_revision = None
        config = self.config.data

        def signature(value):
            return tuple(
                (
                    r["feedback_id"],
                    r["retain_until"],
                    r["feedback"]["asset_id"],
                    r["feedback"]["content_hash"],
                )
                for r in value.records
            )

        def guard():
            budget.check()
            if lease.fenced:
                raise AdaptationFailure(
                    "recommendation_unavailable", "selector_unavailable", observations
                )
            current = current_catalog()
            if current.catalog_revision != req["catalog_revision"]:
                raise AdaptationFailure("recommendation_unavailable", "stale_result", observations)
            current_snapshot = self.feedback.correction_snapshot(
                req["scope"], current, now=budget.clock.utc_now(), check=budget.check
            )
            if (
                lease.fenced
                or current_snapshot.feedback_revision != feedback_revision
                or signature(current_snapshot) != metadata
            ):
                raise AdaptationFailure(
                    "recommendation_unavailable", "selector_unavailable", observations
                )
            budget.check()

        try:
            current = current_catalog()
            if current.catalog_revision != req["catalog_revision"]:
                raise AdaptationFailure("recommendation_unavailable", "stale_result")
            snapshot = self.feedback.correction_snapshot(
                req["scope"], current, now=budget.clock.utc_now(), check=budget.check
            )
            lease.check()
            metadata = signature(snapshot)
            feedback_revision = snapshot.feedback_revision
            history = lease.register(list(snapshot.records))
            snapshot = None
            matches = set()
            if data["candidates"] and history:
                settings = config["representation"]
                inputs = lease.register(
                    [
                        represent_query(
                            req["task_text"], settings["query_prefix"], revision="task_text.v1"
                        )
                    ]
                )
                inputs.extend(
                    represent_query(
                        r["redacted_task_text"], settings["query_prefix"], revision="task_text.v1"
                    )
                    for r in history
                )
                batch = await self.engine.embed(
                    inputs,
                    max_tokens=settings["max_tokens"],
                    deadline=budget.deadline,
                    clock=budget.clock.monotonic,
                    cancelled=lambda: lease.fenced or budget._stopped(),
                    private=lease,
                    observe=observe_embedding,
                )
                observations = (batch.observation,)
                lease.check()
                threshold = config["similarity"]["threshold"]
                for record, vector in zip(history, batch.vectors[1:], strict=True):
                    if cosine(batch.vectors[0], vector) >= threshold:
                        matches.add(record["feedback_id"])
            guard()
            ranked = adapt_ranking(
                data["candidates"],
                data["decision"],
                history,
                matches,
                identity=config["identity"],
                feedback_revision=feedback_revision,
            )
            return AdaptationOutcome(
                ranked.candidates,
                ranked.decision,
                ranked.adaptation,
                observations,
                guard,
                lease.close,
            )
        except BaseException as failure:
            if isinstance(failure, EmbeddingFailure):
                observations = failure.observations
            lease.close()
            try:
                budget.check()
            except RecommendationFailure as stopped:
                raise AdaptationFailure(stopped.code, stopped.reason, observations) from None
            if isinstance(failure, RecommendationFailure):
                raise AdaptationFailure(failure.code, failure.reason, observations) from None
            if isinstance(failure, EmbeddingError):
                raise AdaptationFailure(
                    "recommendation_unavailable", "selector_unavailable", observations
                ) from None
            raise
