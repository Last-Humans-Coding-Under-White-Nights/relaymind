"""Owned recommendation computation, coherent offers and cleanup (RM36/AC27)."""

import asyncio
import logging
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from relaymind.assets.catalog import CatalogUnavailable, timestamp
from relaymind.assets.settings import AssetSettings, RecommendationSettings
from relaymind.assets.sources import source_config_digest
from relaymind.canonical import canonical_digest
from relaymind.recommendations.errors import RecommendationError
from relaymind.recommendations.limits import CANDIDATE_LIMIT, recommendation_budgets
from relaymind.redaction import Redactor
from relaymind.selection.asset_chain import (
    AttemptFinished,
    AttemptStarted,
    RecommendationFailure,
    SelectionBudget,
    select_assets,
)
from relaymind.selection.asset_contract import SelectorContext
from relaymind.selection.asset_factory import build_asset_selector
from relaymind.selection.asset_lexical import LexicalAssetSelector, OfferCache
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
        self.scope, self.permissions, self.policy = dict(scope), tuple(permissions), policy
        self.cache = OfferCache(current_revision=store.revision)
        self.snapshot = None
        self.reason = "no_lexical_generation"
        self.known_revision = None
        self.size = 0
        self._stopping = False
        self._refresh_epoch = 0
        self._refresh_lock = asyncio.Lock()

    async def refresh(self) -> None:
        # The cache can invalidate during a failed older publication. Keep
        # snapshot acquisition and publication under one provider owner.
        async with self._refresh_lock:
            await self._refresh_owned()

    async def _refresh_owned(self) -> None:
        if self._stopping:
            return
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
                return
            if any(
                r.source_id not in configured
                or rows.get(r.source_id, {}).get("config_digest") != configured[r.source_id]
                for r in snapshot.assets
            ):
                failed_reason = "no_lexical_generation"
                raise CatalogUnavailable("catalog_configuration_changed")
            if snapshot is self.snapshot and self._cache_valid(snapshot.catalog_revision):
                self.reason = None
                return
            failed_reason = "no_lexical_generation"
            preserve_compatible = True
            await self.cache.publish(
                snapshot,
                scope=self.scope,
                permissions=self.permissions,
                policy=self.policy,
                context=SelectorContext(self.store.store.clock.monotonic, lambda: None),
            )
            if self._stopping:
                self.cache.invalidate()
                return
            if epoch != self._refresh_epoch:
                return
            self.snapshot = snapshot
            self.reason = None
        except Exception:
            # A losing refresh cannot tear down a newer compatible publication.
            if epoch != self._refresh_epoch:
                return
            if preserve_compatible and self.snapshot is not None:
                try:
                    revision = self.store.revision()
                except Exception:
                    revision = None
                if revision is not None and self._cache_valid(revision):
                    self.reason = None
                    return
            self.cache.invalidate()
            self.snapshot = None
            self.reason = failed_reason

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
        jobs = self.store.job_counts()
        return readiness_body(
            state=state,
            reason=reason,
            catalog_revision=self.known_revision,
            catalog_size=size,
            lexical_ready=lexical,
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


class RecommendationService:
    def __init__(
        self,
        store: RecommendationStore,
        provider: CatalogProvider,
        settings: RecommendationSettings,
        *,
        primary=None,
        redactor: Redactor | None = None,
        clock: TraceClock | None = None,
    ):
        self.store, self.provider, self.settings = store, provider, settings
        self.scope = dict(provider.scope)
        self.primary = primary
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
            None if settings.primary == "lexical" else build_asset_selector(settings)
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
    ) -> "RecommendationService":
        """The service for one configured scope over the shared run store."""
        provider = CatalogProvider(
            AssetCatalogStore(run_store),
            asset_settings,
            scope=dict(user_id=settings.user_id, workspace_id=settings.workspace_id),
            permissions=tuple(sorted(permissions)),
        )
        return cls(RecommendationStore(run_store), provider, settings, redactor=redactor)

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
            budget.check()
            if isinstance(event, AttemptStarted):
                payload = dict(
                    attempt=event.attempt,
                    selector=event.selector.to_json_object(),
                    mechanism=event.mechanism,
                    remaining_ms=event.remaining_ms,
                )
                kind = "selector.started"
            elif isinstance(event, AttemptFinished):
                work.attempts.append(event.attempt_record)
                payload = dict(attempt_record=event.attempt_record, embeddings=[])
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
            )

            def guard():
                budget.check()
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
            attempts=work.attempts,
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
        self._terminal_cancel(recommendation_id, work, reason)
        if work.task is not asyncio.current_task() and not work.task.done():
            work.task.cancel()
