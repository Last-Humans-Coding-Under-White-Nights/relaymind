"""Owned recommendation computation, coherent offers and cleanup (RM36/AC27)."""

import asyncio
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from relaymind.assets.catalog import CatalogUnavailable, timestamp
from relaymind.assets.settings import AssetSettings, RecommendationSettings
from relaymind.assets.sources import source_config_digest
from relaymind.canonical import canonical_digest
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
    RecommendationError,
    RecommendationStore,
    StoredResponse,
    recommendation_budgets,
)
from relaymind.store.run_store import TraceClock


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
            # Provenance and record bytes are read from the same SQLite snapshot.
            with self.store.store.sql_transaction() as db:
                snapshot = self.store.snapshot()
                self.known_revision, self.size = snapshot.catalog_revision, len(snapshot.assets)
                rows = {
                    r["source_id"]: dict(r)
                    for r in db.execute("SELECT * FROM asset_catalog_sources")
                }
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
                    or rows[r.source_id]["config_digest"] != configured[r.source_id]
                    for r in snapshot.assets
                ):
                    failed_reason = "no_lexical_generation"
                    raise CatalogUnavailable("catalog_configuration_changed")
            if (
                self.snapshot is not None
                and self.snapshot.catalog_revision == snapshot.catalog_revision
            ):
                try:
                    self.cache.get(
                        "discovery",
                        ("skill", "mcp_tool"),
                        catalog_revision=snapshot.catalog_revision,
                    )
                except Exception:
                    pass
                else:
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
                    self.cache.get(
                        "discovery", ("skill", "mcp_tool"), catalog_revision=self.store.revision()
                    )
                except Exception:
                    pass
                else:
                    self.reason = None
                    return
            self.cache.invalidate()
            self.snapshot = None
            self.reason = failed_reason

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
            offer = self.pin(("skill", "mcp_tool"))
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
        jobs = self.store.job_outcomes()
        return dict(
            schema_version=1,
            state=state,
            reason=reason,
            catalog_revision=self.known_revision,
            catalog_size=size,
            lexical_ready=lexical,
            semantic_coverage=dict(ready=0, total=size),
            pending_jobs=sum(j["state"] == "running" for j in jobs),
            failed_jobs=sum(j["state"] in ("failed", "interrupted") for j in jobs),
        )

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
        self._active: dict[str, _Work] = {}
        self._stopping = False
        self._sweeper = None
        self._recovered = False

    def new_budget(self):
        return SelectionBudget(self.settings.total_ms, clock=self.clock)

    async def refresh_catalog(self):
        await self.provider.refresh()

    def recover(self):
        if not self._recovered:
            self.store.recover(now=self.clock.utc_now())
            self._recovered = True

    async def start(self):
        self.recover()
        await self.refresh_catalog()
        self._stopping = False
        if self._sweeper is None:
            self._sweeper = asyncio.create_task(self._sweep_loop(), name="recommendation-retention")

    async def _sweep_loop(self):
        while True:
            await asyncio.sleep(30)
            self.store.sweep(now=self.clock.utc_now())

    async def stop(self):
        self._stopping = True
        self.provider.stop()
        for rid in tuple(self._active):
            await self.cancel_computing(rid, reason="shutdown")
        for work in tuple(self._active.values()):
            await work.budget.drain(0.1)
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
        primary = self.primary or (
            LexicalAssetSelector(offer.index)
            if self.settings.primary == "lexical"
            else build_asset_selector(self.settings)
        )
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
            config_digest=canonical_digest(
                self.redactor.redact(
                    dict(settings=vars(self.settings), permissions=self.provider.permissions)
                )
            ),
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
            candidate_limit=10,
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
                result=result.to_json_object(),
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
        current = self.store.get(self.scope, rid, now=self.clock.utc_now())
        if current.view["state"] == "computing":
            self.store.finish(
                self.scope,
                rid,
                0,
                "cancelled",
                now=self.clock.utc_now(),
                active_ms=work.budget.elapsed_ms(),
                attempts=work.attempts,
                reason=reason,
            )
        work.budget.fence_terminal()

    async def cancel_computing(self, recommendation_id: str, *, reason: str):
        work = self._active.get(recommendation_id)
        if work is None:
            return
        work.signal.reason = reason
        work.budget.cancel()
        self._terminal_cancel(recommendation_id, work, reason)
        if work.task is not asyncio.current_task() and not work.task.done():
            work.task.cancel()
