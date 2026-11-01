"""Owned recommendation computation, coherent offers and cleanup (RM36/AC27)."""

import asyncio
import logging
import re
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace

from relaymind.assets.catalog import CatalogUnavailable, timestamp
from relaymind.assets.index_lifecycle import IndexLifecycle
from relaymind.assets.settings import AssetSettings, RecommendationSettings
from relaymind.assets.sources import SourceFailure, source_config_digest
from relaymind.canonical import canonical_digest
from relaymind.recommendations.errors import RecommendationError
from relaymind.recommendations.limits import CANDIDATE_LIMIT, recommendation_budgets
from relaymind.redaction import Redactor
from relaymind.selection.asset_chain import (
    AttemptFinished,
    AttemptStarted,
    RecommendationFailure,
    SelectionBudget,
    cancelled_attempt,
    select_assets,
)
from relaymind.selection.asset_contract import SelectorContext
from relaymind.selection.asset_factory import build_asset_selector
from relaymind.selection.asset_lexical import IMPLEMENTATION, LexicalAssetSelector, OfferCache
from relaymind.store.asset_catalog import AssetCatalogStore
from relaymind.store.recommendations import (
    RecommendationClaim,
    RecommendationStore,
    StoredResponse,
)
from relaymind.store.run_store import RunStore, TraceClock

_LOGGER = logging.getLogger(__name__)
# How long stop() waits for cancelled create tasks to record their outcome.
_STOP_WAIT_SECONDS = 5.0
_ALL_KINDS = ("skill", "mcp_tool")


def readiness_body(**fields) -> dict:
    """One ReadinessV1 shape (section 7.6); fields override the unavailable defaults."""
    return (
        dict(
            schema_version=1,
            state="unavailable",
            reason="disabled",
            catalog_revision=None,
            catalog_size=0,
            lexical_ready=False,
            semantic_coverage=dict(ready=0, total=fields.get("catalog_size", 0)),
            pending_jobs=0,
            failed_jobs=0,
        )
        | fields
    )


class CatalogProvider:
    """Operational compatible generation; acquisition and the index keep their owners."""

    def __init__(
        self,
        store: AssetCatalogStore,
        settings: AssetSettings,
        *,
        scope: Mapping,
        permissions: Sequence[str],
        policy: Callable = lambda record: True,
    ):
        self.store, self.settings = store, settings
        self.scope, self.permissions, self._policy = dict(scope), tuple(permissions), policy
        self._eligible_sources = frozenset(s.source_id for s in settings.sources)
        self.cache = OfferCache(current_revision=store.revision)
        self.snapshot = None
        self.reason = "no_lexical_generation"
        self.known_revision = None
        self.size = 0
        self._stopping = False
        self._refresh_epoch = 0
        self._refresh_lock = asyncio.Lock()
        self.lifecycle = IndexLifecycle(self)
        self.semantic_index = None

    @property
    def policy(self) -> Callable:
        # Bound once by feedback/admission consumers, but resolves current
        # publication eligibility and operator policy on every candidate check.
        return self._eligible

    @policy.setter
    def policy(self, policy: Callable) -> None:
        self._policy = policy
        self.cache.invalidate()
        self.reason = "no_lexical_generation"

    def _eligible(self, record) -> bool:
        return record.source_id in self._eligible_sources and self._policy(record)

    def generation_config(self) -> str:
        return canonical_digest(
            dict(
                sources={s.source_id: source_config_digest(s) for s in self.settings.sources},
                permissions=self.permissions,
                scope=self.scope,
                representation=IMPLEMENTATION,
            )
        )

    async def refresh(self) -> None:
        # The cache can invalidate during a failed older publication. Keep
        # snapshot acquisition and publication under one provider owner.
        async with self._refresh_lock:
            await self._refresh_owned()

    async def _refresh_owned(
        self,
        *,
        force: bool = False,
        check: Callable[[], None] = lambda: None,
        yield_control: Callable[[], Awaitable[None]] | None = None,
    ) -> bool:
        if self._stopping:
            return False
        self._refresh_epoch += 1
        epoch = self._refresh_epoch
        failed_reason = "corrupt_catalog"
        preserve_compatible = False
        try:
            revision = self.store.revision()
            if self.snapshot is not None and revision == self.snapshot.catalog_revision:
                # Nothing committed since the last publication: recheck provenance
                # only, without reading and validating every record again.
                snapshot, rows = self.snapshot, self.store.source_provenance()
            else:
                # Provenance and record bytes are read from the same SQLite snapshot.
                snapshot, rows = self.store.snapshot_with_provenance()
            self.known_revision, self.size = snapshot.catalog_revision, len(snapshot.assets)
            configured = {s.source_id: source_config_digest(s) for s in self.settings.sources}
            self._eligible_sources = frozenset(
                source_id
                for source_id, digest in configured.items()
                if source_id in rows and rows[source_id]["config_digest"] == digest
            )
            success = False
            for source_id, digest in configured.items():
                row = rows.get(source_id)
                if row is None or row["config_digest"] != digest:
                    continue
                fields = [row[k] for k in ("last_success_at", "captured_at", "content_hash")]
                if any(v is not None for v in fields):
                    if (
                        any(v is None for v in fields)
                        or re.fullmatch(r"[0-9a-f]{64}", fields[2]) is None
                    ):
                        raise CatalogUnavailable("catalog_corrupt")
                    timestamp(fields[0])
                    timestamp(fields[1])
                    success = True
            if not success:
                self.cache.invalidate()
                self.snapshot = None
                self.reason = "no_lexical_generation"
                return False
            if any(r.source_id in configured and r.source_id not in rows for r in snapshot.assets):
                # Retained inactive sources can leave the offer, but a configured
                # source's missing provenance cannot establish compatibility.
                failed_reason = "no_lexical_generation"
                raise CatalogUnavailable("catalog_configuration_changed")
            manifest_corrupt = False
            try:
                manifest = self.store.lexical_generation(snapshot=snapshot)
            except CatalogUnavailable:
                # Safe committed records/provenance were validated above. An
                # invalid derived marker is rebuilt without source acquisition;
                # its unknown prior eligibility cannot reuse the old revision.
                manifest, manifest_corrupt = None, True
            asset_ids = tuple(r.asset_id for r in snapshot.assets)
            eligible_ids = tuple(r.asset_id for r in snapshot.assets if self.policy(r))
            previous_ids = () if manifest is None else tuple(manifest["eligible_ids"])
            eligibility_changed = eligible_ids != previous_ids
            # Add/remove publication already owns those identities' revision.
            # A separate eligibility change to a surviving record is a new
            # committed generation even if newer metadata lacked an index.
            advance = manifest_corrupt or bool(
                snapshot.catalog_revision > 0
                if manifest is None
                else (set(eligible_ids) ^ set(previous_ids))
                & set(manifest["asset_ids"])
                & set(asset_ids)
            )
            config_digest = self.generation_config()
            preserve_compatible = bool(
                not advance
                and not eligibility_changed
                and manifest is not None
                and manifest["catalog_revision"] == snapshot.catalog_revision
                and manifest["config_digest"] == config_digest
                and manifest["representation"] == IMPLEMENTATION
                and self.snapshot is not None
                and self._cache_valid(snapshot.catalog_revision)
            )
            # Cancellation/deadline is not evidence of corrupt metadata. First
            # establish whether the already-serving offer is still compatible.
            failed_reason = "no_lexical_generation"
            check()
            if (
                not force
                and snapshot is self.snapshot
                and self._cache_valid(snapshot.catalog_revision)
                and not eligibility_changed
                and manifest is not None
                and manifest["config_digest"] == config_digest
                and manifest["representation"] == IMPLEMENTATION
            ):
                self.reason = None
                return True
            options = dict(
                scope=self.scope,
                permissions=self.permissions,
                policy=self.policy,
                context=SelectorContext(self.store.store.clock.monotonic, check, yield_control),
            )

            def guard():
                nonlocal preserve_compatible
                check()
                if (
                    self.generation_config() != config_digest
                    or tuple(r.asset_id for r in snapshot.assets if self.policy(r)) != eligible_ids
                ):
                    preserve_compatible = False
                    raise CatalogUnavailable("catalog_policy_changed")

            if advance:
                snapshot = replace(snapshot, catalog_revision=snapshot.catalog_revision + 1)
            prepared = await self.cache.prepare(snapshot, **options)
            with self.store.store._lock:
                guard()
                self.store.commit_lexical_generation(
                    expected_revision=snapshot.catalog_revision - int(advance),
                    eligible_ids=eligible_ids,
                    asset_ids=asset_ids,
                    representation=IMPLEMENTATION,
                    config_digest=config_digest,
                    advance=advance,
                    check=check,
                )
                self.cache.install(prepared)
            if self._stopping:
                self.cache.invalidate()
                return False
            if epoch != self._refresh_epoch:
                return False
            self.snapshot = snapshot
            self.known_revision = snapshot.catalog_revision
            self.reason = None
            return True
        except Exception as exc:
            # A losing refresh cannot tear down a newer compatible publication.
            if epoch != self._refresh_epoch:
                return False
            if preserve_compatible and self.snapshot is not None:
                try:
                    revision = self.store.revision()
                except Exception:
                    revision = None
                if revision is not None and self._cache_valid(revision):
                    self.reason = None
                    if isinstance(exc, SourceFailure):
                        raise
                    return False
            self.cache.invalidate()
            self.snapshot = None
            self.reason = failed_reason
            if isinstance(exc, SourceFailure):
                raise
            return False

    def _cache_valid(self, revision: int) -> bool:
        try:
            self.cache.get("discovery", _ALL_KINDS, catalog_revision=revision)
        except Exception:
            return False
        return True

    def pin(self, requested_kinds):
        try:
            revision = self.store.revision()
            self.known_revision = revision
            return self.cache.get("discovery", requested_kinds, catalog_revision=revision)
        except Exception:
            raise RecommendationError("recommendation_unavailable") from None

    def current(self):
        if self.snapshot is None or self.store.revision() != self.snapshot.catalog_revision:
            raise CatalogUnavailable("catalog_projection_unavailable")
        return self.snapshot

    def readiness(self):
        try:
            offer = self.pin(_ALL_KINDS)
            size = len(offer.catalog)
            state, reason, lexical = (
                ("ready", "usable_catalog", True) if size else ("empty", "empty_catalog", True)
            )
        except RecommendationError:
            size, state, reason, lexical = (
                self.size,
                "unavailable",
                self.reason or "no_lexical_generation",
                False,
            )
        semantic_ready = 0
        if lexical and self.semantic_index is not None:
            pinned = self.semantic_index.pin()
            semantic_ready = sum(
                self.semantic_index.compatible(r, publication=pinned) is not None
                for r in offer.catalog
            )
        jobs = self.store.job_counts()
        return readiness_body(
            state=state,
            reason=reason,
            catalog_revision=self.known_revision,
            catalog_size=size,
            lexical_ready=lexical,
            semantic_coverage=dict(ready=semantic_ready, total=size),
            pending_jobs=jobs.get("running", 0),
            failed_jobs=jobs.get("failed", 0) + jobs.get("interrupted", 0),
        )

    def start(self):
        self._stopping = False

    def stop(self):
        self._stopping = True
        self.cache.invalidate()


@dataclass
class CancellationSignal:
    recommendation_id: str | None = None
    reason: str | None = None


@dataclass
class _Work:
    budget: SelectionBudget
    task: asyncio.Task
    signal: CancellationSignal
    attempts: list = field(default_factory=list)
    # The dispatched attempt that has not finished yet, and when it started.
    in_flight: tuple[AttemptStarted, float] | None = None
    finish_observer: object = None

    def terminal_attempts(self) -> list:
        """Finished attempts plus a cancelled record for one still in flight."""
        if self.in_flight is None:
            return list(self.attempts)
        started, at = self.in_flight
        if any(record["attempt"] == started.attempt for record in self.attempts):
            return list(self.attempts)
        elapsed = (self.budget.clock.monotonic() - at) * 1000
        return [*self.attempts, cancelled_attempt(started, elapsed)]


class RecommendationService:
    def __init__(
        self,
        store: RecommendationStore,
        provider: CatalogProvider,
        settings: RecommendationSettings,
        *,
        primary=None,
        adaptation=None,
        semantic_identity=None,
        redactor: Redactor | None = None,
        clock: TraceClock | None = None,
    ):
        self.store, self.provider, self.settings = store, provider, settings
        self.scope = dict(provider.scope)
        self.primary = primary
        self.adaptation = adaptation
        self.redactor = redactor or Redactor()
        self.clock = clock or store.store.clock
        self.sweep_seconds = 30.0
        self._active: dict[str, _Work] = {}
        self._stopping = False
        self._sweeper = None
        # Configuration-only values: computed once, not per create.
        self._config_digest = canonical_digest(
            self.redactor.redact(dict(settings=vars(settings), permissions=provider.permissions))
        )
        self._configured_primary = (
            None
            if settings.primary == "lexical" or primary is not None
            else build_asset_selector(settings)
        )
        self.embedding_lifecycle = None
        if semantic_identity is not None:
            self._config_digest = canonical_digest(
                dict(
                    base=self._config_digest,
                    sources=provider.generation_config(),
                    semantics=semantic_identity,
                    budgets=recommendation_budgets(
                        settings.total_ms, settings.primary_ms, settings.fallback
                    ),
                )
            )

    @classmethod
    def from_config(
        cls,
        run_store: RunStore,
        asset_settings: AssetSettings,
        settings: RecommendationSettings,
        *,
        permissions: Sequence[str],
        redactor: Redactor | None = None,
        semantics=None,
    ) -> "RecommendationService":
        """The service for one configured scope over the shared run store."""
        provider = CatalogProvider(
            AssetCatalogStore(run_store),
            asset_settings,
            scope=dict(user_id=settings.user_id, workspace_id=settings.workspace_id),
            permissions=tuple(sorted(permissions)),
        )
        return cls.for_provider(
            run_store, provider, settings, redactor=redactor, semantics=semantics
        )

    @classmethod
    def for_provider(
        cls,
        run_store: RunStore,
        provider: CatalogProvider,
        settings: RecommendationSettings,
        *,
        redactor: Redactor | None = None,
        semantics=None,
    ) -> "RecommendationService":
        """Shared production construction over an already owned catalog provider."""
        primary = None
        owner = None
        if semantics is not None:
            from relaymind.assets.embedding_bundle import SemanticLifecycle

            if settings.primary == "hybrid":
                from relaymind.assets.embedding_lifecycle import EmbeddingIndexLifecycle
                from relaymind.selection.asset_hybrid import HybridAssetSelector, HybridIndex
                from relaymind.store.asset_vectors import AssetVectorStore

                if semantics.hybrid is None:
                    raise ValueError("hybrid requires qualified manifest/config")
                index = HybridIndex(semantics.hybrid, provider.scope)
                primary = HybridAssetSelector(semantics.hybrid, index, None)
                provider.semantic_index = index
                owner = EmbeddingIndexLifecycle(
                    provider, primary, AssetVectorStore(run_store), semantics.loader
                )
            lifecycle = SemanticLifecycle(semantics, provider, owner)
        service = cls(
            RecommendationStore(run_store),
            provider,
            settings,
            primary=primary,
            redactor=redactor,
            semantic_identity=None if semantics is None else semantics.identity(),
        )
        if semantics is not None and (owner is not None or semantics.adaptation_enabled):
            service.embedding_lifecycle = lifecycle
            # Both queued semantic jobs and accepted requests bind this same effective identity.
            lifecycle.config_digest = service._config_digest
        return service

    def new_budget(self):
        return SelectionBudget(self.settings.total_ms, clock=self.clock)

    async def refresh_catalog(self):
        await self.provider.refresh()

    async def start(self):
        """Recover interrupted objects, publish the catalog, start the sweeper.

        This is the only recovery site; it runs before the app accepts work.
        """
        self._stopping = False
        self.provider.start()
        self.store.recover(now=self.clock.utc_now())
        await self.refresh_catalog()
        if self._sweeper is None:
            self._sweeper = asyncio.create_task(self._sweep_loop(), name="recommendation-retention")

    async def _sweep_loop(self):
        while True:
            await asyncio.sleep(self.sweep_seconds)
            try:
                self.store.sweep(now=self.clock.utc_now())
            except Exception as exc:
                # The type only: exception text can hold stored data (RM12).
                _LOGGER.error("recommendation retention sweep failed: %s", type(exc).__name__)

    async def stop(self):
        self._stopping = True
        self.provider.stop()
        works = tuple(self._active.values())
        for rid in tuple(self._active):
            await self.cancel_computing(rid, reason="shutdown")
        for work in works:
            await work.budget.drain(0.1)
        # The create tasks record their outcome before the store can close.
        tasks = {w.task for w in works if w.task is not asyncio.current_task()}
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=_STOP_WAIT_SECONDS)
            if pending:
                _LOGGER.error("recommendation create tasks still running after stop")
        if self._sweeper is not None:
            self._sweeper.cancel()
            await asyncio.gather(self._sweeper, return_exceptions=True)
            self._sweeper = None
        if self.adaptation is not None:
            self.adaptation.close()

    def readiness(self):
        return self.provider.readiness()

    def precheck(self, request):
        if self._stopping or request["selection_scope"] != "discovery":
            raise RecommendationError("recommendation_unavailable")
        return self.provider.pin(request["requested_kinds"])

    async def create(
        self,
        request: Mapping,
        claim: RecommendationClaim,
        budget: SelectionBudget,
        cancel: CancellationSignal,
    ) -> StoredResponse:
        # No sweep/key lookup until runtime/catalog availability has been established.
        offer = self.precheck(request)
        budget.check()
        prior = self.store.lookup_claim(claim, now=self.clock.utc_now())
        if prior is not None:
            if prior.response is None:
                raise RecommendationError("recommendation_in_progress", prior.recommendation_id)
            return prior.response
        primary = self.primary or self._configured_primary or LexicalAssetSelector(offer.index)
        rid = str(uuid.uuid4())
        task_id = canonical_digest(
            dict(task_text=request["task_text"], conversation=request["conversation"])
        )
        accepted = dict(
            request,
            recommendation_id=rid,
            task_id=task_id,
            scope=self.scope,
            budgets=recommendation_budgets(
                self.settings.total_ms, self.settings.primary_ms, self.settings.fallback
            ),
            configured_primary=dict(name=primary.name, version=primary.version),
            config_digest=self._config_digest,
            catalog_revision=offer.catalog_revision,
            catalog=offer.catalog,
            created_at=self.clock.utc_now(),
        )
        accepted.pop("schema_version")
        context = SelectorContext(self.clock.monotonic, budget.check)
        envelope = dict(
            request,
            schema_version=2,
            recommendation_id=rid,
            task_id=task_id,
            scope=self.scope,
            permissions=list(self.provider.permissions),
            catalog_revision=offer.catalog_revision,
            deadline_ms=budget.remaining_ms(),
            candidate_limit=CANDIDATE_LIMIT,
        )
        selection = offer.request(envelope, context=context)
        budget.check()
        begun = self.store.begin(
            accepted, claim, commit_guard=budget.check, active_ms=budget.elapsed_ms()
        )
        if isinstance(begun, StoredResponse):
            return begun
        cancel.recommendation_id = rid
        work = _Work(budget, asyncio.current_task(), cancel)
        self._active[rid] = work

        def observe(event):
            if isinstance(event, AttemptStarted):
                budget.check()
                work.in_flight = (event, budget.clock.monotonic())
                payload = dict(
                    attempt=event.attempt,
                    selector=event.selector.to_json_object(),
                    mechanism=event.mechanism,
                    remaining_ms=event.remaining_ms,
                )
                kind = "selector.started"
            elif isinstance(event, AttemptFinished):
                if budget._terminal or any(
                    r["attempt"] == event.attempt_record["attempt"] for r in work.attempts
                ):
                    return
                # Already observed inference may be flushed after the work clock
                # stops, before the terminal fence; no additional work is admitted.
                if not event.embeddings and not event.flush_after_stop:
                    budget.check()
                work.in_flight = None
                work.attempts.append(event.attempt_record)
                payload = dict(
                    attempt_record=event.attempt_record, embeddings=list(event.embeddings)
                )
                kind = "selector.finished"
            else:
                raise TypeError("unexpected selection event")
            self.store.append_attempt_event(
                self.scope,
                rid,
                0,
                dict(event_type=kind, payload=payload),
                now=self.clock.utc_now(),
                active_ms=budget.elapsed_ms(),
            )

        work.finish_observer = observe
        result = None
        try:
            result = await select_assets(
                selection,
                primary=primary,
                lexical_floor=LexicalAssetSelector(offer.index),
                budget=budget,
                primary_ms=self.settings.primary_ms,
                fallback_enabled=self.settings.fallback,
                current_catalog=self.provider.current,
                policy=lambda record, req: self.provider.policy(record),
                trusted_scope=self.scope,
                trusted_permissions=self.provider.permissions,
                observe=observe,
                adaptation=self.adaptation,
            )

            def guard():
                budget.check()
                if result.commit_guard is not None:
                    result.commit_guard()
                current = self.provider.current()
                if current.catalog_revision != offer.catalog_revision:
                    raise RecommendationFailure("recommendation_unavailable", "stale_result")
                if any(not self.provider.policy(r) for r in offer.index.records):
                    raise RecommendationFailure("recommendation_unavailable", "stale_result")

            self.store.finish(
                self.scope,
                rid,
                0,
                "ready",
                result=result,
                now=self.clock.utc_now(),
                active_ms=budget.elapsed_ms(),
                commit_guard=guard,
            )
            budget.fence_terminal()
        except (RecommendationFailure, asyncio.CancelledError) as exc:
            if isinstance(exc, RecommendationFailure):
                # A total deadline can reject AttemptFinished before its
                # observer appends it. The coordinator still owns that measured
                # record; merge by ordinal so prior observed attempts stay once.
                records = {record["attempt"]: record for record in work.attempts}
                records.update((record["attempt"], record) for record in exc.attempts)
                work.attempts[:] = [records[ordinal] for ordinal in sorted(records)]
            if (
                isinstance(exc, asyncio.CancelledError)
                or cancel.reason is not None
                or getattr(exc, "code", None) == "cancelled"
            ):
                budget.cancel()
                self._terminal_cancel(rid, work, cancel.reason or "shutdown")
                if isinstance(exc, asyncio.CancelledError):
                    raise
            else:
                code = (
                    exc.code
                    if exc.code
                    in ("deadline_exceeded", "recommendation_unavailable", "internal_error")
                    else "internal_error"
                )
                reason = (
                    "deadline"
                    if code == "deadline_exceeded"
                    else "corrupt_catalog"
                    if exc.reason == "corrupt_catalog"
                    else "unavailable"
                    if code == "recommendation_unavailable"
                    else "internal"
                )
                self._terminal_failure(rid, work, code, reason)
        except CatalogUnavailable:
            if cancel.reason:
                self._terminal_cancel(rid, work, cancel.reason)
            else:
                self._terminal_failure(rid, work, "recommendation_unavailable", "corrupt_catalog")
        except Exception:
            if cancel.reason:
                self._terminal_cancel(rid, work, cancel.reason)
            else:
                self._terminal_failure(rid, work, "internal_error", "internal")
        finally:
            if result is not None and result.release is not None:
                result.release()
            self._active.pop(rid, None)
        response = self.store.lookup_claim(claim, now=self.clock.utc_now())
        if response is None or response.response is None:
            raise RecommendationError("internal_error", rid)
        return response.response

    def _terminal_failure(self, rid, work, code, reason):
        self.store.finish(
            self.scope,
            rid,
            0,
            "failed",
            now=self.clock.utc_now(),
            active_ms=work.budget.elapsed_ms(),
            attempts=work.attempts,
            reason=reason,
            code=code,
        )
        work.budget.fence_terminal()

    def _terminal_cancel(self, rid, work, reason):
        self.store.cancel_if_computing(
            self.scope,
            rid,
            now=self.clock.utc_now(),
            active_ms=work.budget.elapsed_ms(),
            attempts=work.terminal_attempts(),
            reason=reason,
        )
        work.budget.fence_terminal()

    def get(self, scope: Mapping, recommendation_id: str):
        """The current view in the caller's configured scope; other scopes are absent."""
        return self.store.get(scope, recommendation_id, now=self.clock.utc_now()).view

    def events(self, scope: Mapping, recommendation_id: str, *, after_seq: int, limit: int):
        return self.store.events(
            scope,
            recommendation_id,
            after_seq=after_seq,
            limit=limit,
            now=self.clock.utc_now(),
        )

    def recorded_response(self, claim: RecommendationClaim) -> StoredResponse | None:
        recorded = self.store.lookup_claim(claim, now=self.clock.utc_now())
        return None if recorded is None else recorded.response

    async def cancel_computing(self, recommendation_id: str, *, reason: str):
        work = self._active.get(recommendation_id)
        if work is None:
            return
        work.signal.reason = reason
        work.budget.cancel()
        # One SQL owner orders available finish evidence before cancellation.
        # The progress closure fences native dispatch and contains counters only.
        with self.store.store.sql_transaction():
            if work.in_flight is not None and work.finish_observer is not None:
                started, _ = work.in_flight
                if started.progress is not None:
                    finished = started.progress("cancelled")
                    if finished.embeddings or finished.flush_after_stop:
                        work.finish_observer(finished)
            self._terminal_cancel(recommendation_id, work, reason)
        if work.task is not asyncio.current_task() and not work.task.done():
            work.task.cancel()
