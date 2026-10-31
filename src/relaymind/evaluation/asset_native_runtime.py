"""Production semantic ownership over an explicitly locked evaluation offer.

This private catalog-input seam is not source acquisition, host authority or
selector/qualification injection. The production factory and sole asset worker
own model loading, vectors, query transport and all recorded native work.
"""

import asyncio
import tempfile
import time
from dataclasses import replace
from pathlib import Path

from relaymind.assets.catalog import CatalogUnavailable, project_asset, validate_asset_record
from relaymind.assets.embedding_bundle import effective_launch_identity
from relaymind.assets.service import AssetCatalogService
from relaymind.assets.sources import SourceFailure, source_config_digest
from relaymind.canonical import canonical_digest
from relaymind.evaluation.asset_corpus_io import AssetCorpusError
from relaymind.recommendations.service import CatalogProvider, RecommendationService
from relaymind.redaction import Redactor
from relaymind.selection.asset_contract import SelectorContext
from relaymind.selection.asset_lexical import IMPLEMENTATION
from relaymind.selection.asset_preparation import (
    normalize_preparation,
    preparation_binding,
    validate_preparation_record,
)
from relaymind.store import RunStore
from relaymind.store.asset_catalog import AssetCatalogStore


class _FrozenCatalogProvider(CatalogProvider):
    """Real store/cache/revision with no invented source-capture success."""

    def __init__(self, store, production, effective):
        self._locked_ids = frozenset(
            item["asset_id"] for item in effective["eligibility"] if item["eligible"]
        )
        super().__init__(
            store,
            production.assets,
            scope={key: effective["recommendations"][key] for key in ("user_id", "workspace_id")},
            permissions=effective["permissions"],
        )
        self._frozen_snapshot = store.snapshot()
        self._frozen_records = tuple(self._frozen_snapshot.record_bytes)
        self._frozen_revision = self._frozen_snapshot.catalog_revision
        self.known_revision = self._frozen_revision
        self.size = len(self._frozen_snapshot.assets)

    def _eligible(self, record):
        return record.asset_id in self._locked_ids

    def _pinned_snapshot(self):
        snapshot = self.store.snapshot()
        if (
            snapshot.catalog_revision != self._frozen_revision
            or tuple(snapshot.record_bytes) != self._frozen_records
        ):
            raise CatalogUnavailable("evaluation_catalog_changed")
        return snapshot

    async def _refresh_owned(self, *, force=False, check=lambda: None, yield_control=None):
        check()
        if self._stopping:
            return False
        snapshot = self._pinned_snapshot()
        config_digest = self.generation_config()
        prepared = await self.cache.prepare(
            snapshot,
            scope=self.scope,
            permissions=self.permissions,
            policy=self.policy,
            context=SelectorContext(self.store.store.clock.monotonic, check, yield_control),
        )
        with self.store.store._lock:
            check()
            self._pinned_snapshot()
            if self._stopping or self.generation_config() != config_digest:
                raise SourceFailure("index_error", rejected_count=0)
            self.store.commit_lexical_generation(
                expected_revision=snapshot.catalog_revision,
                eligible_ids=tuple(
                    record.asset_id for record in snapshot.assets if self.policy(record)
                ),
                asset_ids=tuple(record.asset_id for record in snapshot.assets),
                representation=IMPLEMENTATION,
                config_digest=config_digest,
                advance=False,
                check=check,
            )
            self.cache.install(prepared)
            self.snapshot = snapshot
            self.known_revision = snapshot.catalog_revision
            self.size = len(snapshot.assets)
            self.reason = None
        return True


class NativeBenchmarkRuntime:
    """One disposable store, shared production service and derived-only worker."""

    def __init__(self):
        self.directory = self.runs = self.service = self.worker = None
        self.catalog = self.provider = None
        self._closed = False
        self._prepared = False
        self._preparation_deadline = None
        self._preparation_cancelled = lambda: False
        self._preparation_clock = time.monotonic

    def _check_preparation(self):
        if self._preparation_deadline is None:
            return
        if self._preparation_cancelled():
            raise SourceFailure("cancelled", rejected_count=0)
        now = self._preparation_clock()
        if now >= self._preparation_deadline:
            raise SourceFailure("deadline", rejected_count=0)
        return now

    @classmethod
    async def create(cls, production, release, effective):
        runtime = cls()
        runtime.directory = tempfile.TemporaryDirectory(prefix="asset-native-runtime-")
        temporary = Path(runtime.directory.name)
        root = temporary.parent.resolve(strict=True) / temporary.name
        try:
            if root.is_symlink():
                raise AssetCorpusError("unsafe_file")
            runtime.runs = RunStore(root / "evaluation.sqlite3")
            runtime.catalog = AssetCatalogStore(runtime.runs)
            eligible = {item["asset_id"] for item in effective["eligibility"] if item["eligible"]}
            documents = tuple(
                project_asset(
                    validate_asset_record(asset["record"]),
                    body=None,
                    redactor=Redactor(),
                    secrets=(),
                )
                for asset in release.catalog["assets"]
                if asset["record"]["asset_id"] in eligible
            )
            runtime.catalog.initialize_evaluation_offer(
                documents,
                config_digests={
                    source.source_id: source_config_digest(source)
                    for source in production.assets.sources
                },
            )
            runtime.provider = _FrozenCatalogProvider(runtime.catalog, production, effective)
            runtime.service = RecommendationService.for_provider(
                runtime.runs,
                runtime.provider,
                production.recommendations,
                semantics=production.asset_semantics,
            )
            if production.asset_semantics is not None:
                runtime.service._config_digest = canonical_digest(
                    effective_launch_identity(production, runtime.service.redactor)
                )
                if runtime.service.embedding_lifecycle is not None:
                    runtime.service.embedding_lifecycle.config_digest = (
                        runtime.service._config_digest
                    )
            runtime.worker = AssetCatalogService(
                # Acquisition remains the separately verified locked-input seam.
                # Only real lexical/semantic work is queued on this same owner.
                replace(production.assets, sources=()),
                runtime.catalog,
                busy=lambda: (
                    runtime.runs.active_run_id() is not None
                    or runtime.runs.probe_reservation() is not None
                ),
                busy_since=runtime.runs.foreground_busy_since,
                after_change=runtime.service.refresh_catalog,
                index_lifecycle=runtime.provider.lifecycle,
                embedding_lifecycle=runtime.service.embedding_lifecycle,
                effective_config_digest=runtime.service._config_digest,
                work_check=runtime._check_preparation,
            )
            return runtime
        except BaseException:
            await runtime.close()
            raise

    async def prepare_local(
        self, config, *, requested_kinds, cancelled=lambda: False, clock=time.monotonic
    ):
        """Cold setup on the actual worker, with no query or HTTP stand-ins.

        Static configuration/qualification validation precedes this interval and
        must be disclosed separately by the report. Native load, real derived
        jobs and usable-generation checks consume this one preparation budget.
        """
        config = normalize_preparation(config, http_primary=False, local_primary=True)
        primary = self.service.primary
        if (
            config["mode"] != "local"
            or self._prepared
            or self._closed
            or self.service.embedding_lifecycle is None
            or primary.engine is not None
            or self.catalog.job_outcomes()
        ):
            raise ValueError("invalid_local_preparation_owner")
        snapshot = self.provider._pinned_snapshot()
        catalog = [record.to_json_object() for record in snapshot.assets]
        if any(record["kind"] not in requested_kinds for record in catalog):
            raise ValueError("preparation_kind_binding")
        binding = preparation_binding(
            selector=dict(name=primary.name, version=primary.version),
            catalog_revision=snapshot.catalog_revision,
            catalog=catalog,
            scope=self.provider.scope,
            permissions=self.provider.permissions,
            selection_scope="discovery",
            requested_kinds=requested_kinds,
        )
        self._prepared = True
        started = clock()
        self._preparation_deadline = started + config["total_ms"] / 1000
        self._preparation_clock, self._preparation_cancelled = clock, cancelled
        record = dict(
            config=config,
            binding=binding,
            preparation_id=None,
            status="failed",
            reason="build_failed",
            index_revision=None,
            observed_ms=None,
            cleanup_ms=None,
            exchanges=[],
            observed_costs=[],
            local=dict(job_events=[], readiness=None),
        )

        async def build():
            self._check_preparation()
            await self.worker.rebuild()
            self._check_preparation()
            if catalog:
                try:
                    await self.worker.rebuild_embeddings()
                except SourceFailure as failure:
                    # A compatible published prefix can remain usable after a
                    # later batch fails. Deadline/cancellation never earns ready.
                    if failure.reason != "index_error":
                        raise
            self._check_preparation()
            self.provider._pinned_snapshot()
            readiness = self.provider.readiness()
            if readiness["pending_jobs"] or not readiness["lexical_ready"]:
                raise SourceFailure("index_error", rejected_count=0)
            if catalog and (primary.engine is None or not readiness["semantic_coverage"]["ready"]):
                raise SourceFailure("index_error", rejected_count=0)
            return readiness

        operation = asyncio.create_task(build())
        stopped = False
        try:
            while not operation.done():
                self._check_preparation()
                await asyncio.wait(
                    {operation}, timeout=min(0.025, max(0, self._preparation_deadline - clock()))
                )
            readiness = operation.result()
            self._check_preparation()
            record.update(
                status="ready" if catalog else "not_applicable",
                reason=None,
                index_revision=primary.index.revision if catalog else None,
            )
            record["local"]["readiness"] = readiness
            record["local"]["job_events"] = list(self.catalog.job_event_stream())
            self._check_preparation()
            record["observed_ms"] = max(0, (clock() - started) * 1000)
            validate_preparation_record(record, binding=binding)
            # Record the decision after collecting/validating its evidence.
            # The final synchronous fence includes that work in the budget.
            decision_at = self._check_preparation()
            record["observed_ms"] = max(0, (decision_at - started) * 1000)
        except (SourceFailure, CatalogUnavailable, asyncio.CancelledError) as failure:
            stopped = True
            record["index_revision"] = None
            if isinstance(failure, asyncio.CancelledError) or (
                isinstance(failure, SourceFailure) and failure.reason == "cancelled"
            ):
                record.update(status="cancelled", reason="cancelled")
            elif isinstance(failure, SourceFailure) and failure.reason == "deadline":
                record.update(status="timeout", reason="deadline_exceeded")
            elif isinstance(failure, CatalogUnavailable):
                record.update(status="stale_catalog", reason="catalog_stale")
        finally:
            # Fix decision time before draining. A cancelled native invocation
            # remains owned, and its actual cleanup can exceed the setup budget.
            if stopped:
                record["observed_ms"] = max(0, (clock() - started) * 1000)
            if stopped or not operation.done():
                cleanup_started = clock()
                operation.cancel()
                cleanup = asyncio.create_task(self.worker.stop())
                while not cleanup.done():
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        pass
                cleanup.result()
                settled = asyncio.gather(operation, return_exceptions=True)
                while not settled.done():
                    try:
                        await asyncio.shield(settled)
                    except asyncio.CancelledError:
                        pass
                settled.result()
                record["cleanup_ms"] = max(0, (clock() - cleanup_started) * 1000)
            if stopped:
                record["local"]["job_events"] = list(self.catalog.job_event_stream())
                try:
                    self.provider._pinned_snapshot()
                    record["local"]["readiness"] = self.provider.readiness()
                except CatalogUnavailable:
                    # Cleanup may discover stale inputs, but cannot rewrite a
                    # cancellation decision made before the owned native drain.
                    if record["status"] != "cancelled":
                        record.update(
                            status="stale_catalog", reason="catalog_stale", index_revision=None
                        )
                    record["local"]["readiness"] = None
            self._preparation_deadline = None
        # Raw job embeddings are the cost source. No inclusive duplicate is
        # added here; unknown native resource counters stay unknown in that wire.
        if stopped:
            validate_preparation_record(record, binding=binding)
        return record

    async def close(self):
        if self._closed:
            return
        try:
            if self.worker is not None:
                await self.worker.stop()
            if self.service is not None:
                await self.service.stop()
        finally:
            if self.runs is not None:
                self.runs.close()
            if self.directory is not None:
                self.directory.cleanup()
            self._closed = True
