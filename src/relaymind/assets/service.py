"""One source/derived lexical worker, separate from run admission (RM34, RM38).

Acquisition and derived rebuilds share cancellation, deadline and batch gates;
published metadata supplies discovery data and never host execution authority.
"""

import asyncio
import logging
import math
import threading
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any

from relaymind.assets.catalog import acquisition_budgets
from relaymind.assets.index_lifecycle import IndexLifecycle
from relaymind.assets.remote_contracts import resolve_remote_profile
from relaymind.assets.scheduler import AssetWorkScheduler
from relaymind.assets.settings import AssetSettings
from relaymind.assets.sources import (
    CaptureContext,
    SourceFailure,
    capture_source,
    source_config_digest,
)
from relaymind.canonical import canonical_digest
from relaymind.store.asset_catalog import AssetCatalogStore, Publication
from relaymind.store.asset_remote_state import RemoteSourceStateStore
from relaymind.store.run_store import TraceClock, utc_iso

_LOGGER = logging.getLogger(__name__)

# While a source is due but the gateway is busy, look again this often.
_BUSY_RETRY_SECONDS = 0.1
_REBUILD = object()
_EMBEDDING = object()
_DEFAULT_INDEX = object()


async def _no_change_observer() -> None:
    """The default change observer: nothing derives from the catalog."""


class AssetCatalogService:
    def __init__(
        self,
        settings: AssetSettings,
        store: AssetCatalogStore,
        *,
        clock: TraceClock | None = None,
        busy: Callable[[], bool] = lambda: False,
        busy_since: Callable[[], float | None] = lambda: None,
        after_change: Callable[[], Awaitable[None]] | None = None,
        index_lifecycle: IndexLifecycle | None = None,
        embedding_lifecycle=None,
        effective_config_digest=None,
    ) -> None:
        self.settings, self.store = settings, store
        self.clock = clock or TraceClock()
        self.busy = busy
        self.busy_since = busy_since
        self.after_change = after_change or _no_change_observer
        self.index_lifecycle = index_lifecycle
        self.embedding_lifecycle = embedding_lifecycle
        self.effective_config_digest = effective_config_digest
        self.scheduler = AssetWorkScheduler(busy=lambda: self.busy())
        self._sources = {s.source_id: s for s in settings.sources}
        self.remote_state = RemoteSourceStateStore(store.store)
        self._profiles = {
            s.source_id: resolve_remote_profile(s.adapter_contract)
            for s in settings.sources
            if s.kind == "qualified_remote"
        }
        self._next = dict.fromkeys(self._sources, 0.0)
        self._failures = dict.fromkeys(self._sources, 0)
        self._retry = dict.fromkeys(self._sources, 0.0)
        self._pending: dict[str | object, asyncio.Future[Publication]] = {}
        self._waiters = dict.fromkeys(self._sources, 0)
        self._waiters[_REBUILD] = 0
        self._waiters[_EMBEDDING] = 0
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._stopping = False
        self._cancel = threading.Event()
        self._active_future: asyncio.Future[Publication] | None = None
        self._wakeup = asyncio.Event()
        self._observer_future: asyncio.Future[Publication] | None = None
        self.store.recover_interrupted_jobs()
        for source_id, failures, next_attempt in self.store.schedules():
            if source_id not in self._sources:
                continue
            self._failures[source_id] = failures
            if next_attempt is None and failures:
                delay = self._backoff(failures)
                next_attempt = utc_iso(self.clock.utc_now() + timedelta(seconds=delay))
                self.store.schedule_retry(source_id, next_attempt)
            if next_attempt is not None:
                remaining = max(
                    0.0,
                    (datetime.fromisoformat(next_attempt) - self.clock.utc_now()).total_seconds(),
                )
                self._retry[source_id] = self.clock.monotonic() + remaining
                self._next[source_id] = self._retry[source_id]
        provenance = self.store.source_provenance()
        for source_id, profile in self._profiles.items():
            previous = provenance.get(source_id, {})
            if not self._failures[source_id] and previous.get("last_success_at") is not None:
                # A persisted successful schedule is ordinary cadence, not failure
                # backoff or provider authority. Recompute it for current settings.
                self._retry[source_id] = 0.0
                next_time = datetime.fromisoformat(previous["last_success_at"]) + timedelta(
                    seconds=max(self.settings.refresh.remote_seconds, profile.minimum_poll_seconds)
                )
                self._next[source_id] = self.clock.monotonic() + max(
                    0.0, (next_time - self.clock.utc_now()).total_seconds()
                )

    def _source_job_config(self, source):
        profile = self._profiles.get(source.source_id)
        return canonical_digest(
            dict(
                source=source_config_digest(source),
                refresh=asdict(self.settings.refresh),
                profile=None if profile is None else asdict(profile),
                budgets=acquisition_budgets(),
                generation_policy=None
                if self.index_lifecycle is None
                else self.index_lifecycle.provider.generation_config(),
            )
        )

    def _job_config(self, specific):
        if self.effective_config_digest is None:
            return specific
        return canonical_digest(dict(effective=self.effective_config_digest, specific=specific))

    def _backoff(self, failures: int) -> float:
        refresh = self.settings.refresh
        return min(
            refresh.backoff_max_seconds,
            refresh.backoff_initial_seconds * 2 ** min(failures - 1, 32),
        )

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if not self.running:
            self._stopping = False
            self._task = asyncio.create_task(self._loop(), name="asset-catalog")
            self._task.add_done_callback(_log_worker_end)

    async def _notify_changed(self, *, allow_defer: bool = True) -> None:
        """Tell the derived-cache observer the catalog may have changed.

        Failures change visible stale state too (and so the revision), so every
        finished job notifies. The observer has separate ownership: its failure
        is logged and never turns committed source evidence into a failure.
        """
        if self.index_lifecycle is not None and allow_defer:
            if self._stopping:
                return
            try:
                needs_repair = not self.index_lifecycle.provider._cache_valid(self.store.revision())
            except Exception:
                needs_repair = True
            if self.busy() or needs_repair:
                # A failure's new stale bytes may need a derived generation.
                # Queue that repair on this same owner; notification must not
                # dispatch unmeasured index work while inference is busy.
                self._observer_future = self._queue(_REBUILD)
                self.start()
                self._wakeup.set()
                return
        try:
            await self.after_change()
            if self.embedding_lifecycle is not None and not self._stopping:
                self._queue(_EMBEDDING)
                self.start()
                self._wakeup.set()
        except Exception:
            _LOGGER.exception("asset catalog derived preparation failed")

    async def stop(self) -> None:
        self._stopping = True
        self._cancel.set()
        self.scheduler.wake()
        self._wakeup.set()
        # Capture this lifetime's owner and pending futures. An old stop waiter
        # must never clear a later lifetime's queue after its worker has drained.
        task, pending = self._task, tuple(self._pending.items())
        cancelled = False
        if task is not None:
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    current = asyncio.current_task()
                    if current is not None and current.cancelling():
                        cancelled = True
                except Exception:
                    break  # The done callback logs worker failures.
        # No capture/finalization writes remain when stop returns or raises,
        # even when the caller cancels repeatedly during native work.
        for source_id, future in pending:
            if not future.done():
                future.set_exception(SourceFailure("cancelled", rejected_count=0))
            if self._pending.get(source_id) is future:
                self._pending.pop(source_id)
        if self.embedding_lifecycle is not None:
            await self.embedding_lifecycle.stop()
        if cancelled:
            raise asyncio.CancelledError

    async def rescan(self, source_id: str) -> Publication:
        """Run this source now, ignoring its failure backoff (an explicit operator request).

        Callers asking for the same source share one job. A cancelled caller cancels
        that job only when no other caller still waits for it.
        """
        if source_id not in self._sources:
            raise ValueError("unknown_source")
        return await self._request(source_id)

    async def rebuild(self) -> Publication:
        """Queue one derived lexical rebuild on the existing background owner (RM38)."""
        if self.index_lifecycle is None:
            raise SourceFailure("index_error", rejected_count=0)
        return await self._request(_REBUILD)

    async def rebuild_embeddings(self) -> Publication:
        """Queue bounded semantic work on the same source/lexical owner."""
        if self.embedding_lifecycle is None:
            raise SourceFailure("index_error", rejected_count=0)
        return await self._request(_EMBEDDING)

    async def _request(self, source_id: str | object) -> Publication:
        if self._stopping:
            raise SourceFailure("cancelled", rejected_count=0)
        future = self._queue(source_id)
        self._waiters[source_id] += 1
        self.start()
        self._wakeup.set()
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            if (
                self._waiters[source_id] == 1
                and self._observer_future is not future
                and not future.done()
            ):
                if self._active_future is future:
                    self._cancel.set()
                    self.scheduler.wake()
                elif self._pending.get(source_id) is future:
                    self._pending.pop(source_id, None)
                    future.cancel()
            raise
        finally:
            self._waiters[source_id] -= 1

    def _queue(self, source_id: str | object) -> asyncio.Future[Publication]:
        future = self._pending.get(source_id)
        if future is None:
            future = asyncio.get_running_loop().create_future()
            # Consume abandoned waiter exceptions without hiding them from any
            # callers still awaiting the shared source job.
            future.add_done_callback(lambda done: None if done.cancelled() else done.exception())
            self._pending[source_id] = future
        return future

    def _hard_delay(self, source_id):
        profile = self._profiles.get(source_id)
        if profile is None:
            return 0.0
        value = self.remote_state.not_before(profile.profile_id)
        return (
            0.0
            if value is None
            else max(0.0, (datetime.fromisoformat(value) - self.clock.utc_now()).total_seconds())
        )

    def _due(self, now: float) -> str | object | None:
        for source_id in self._pending:
            if self._hard_delay(source_id) <= 0:
                return source_id
        due = [
            key
            for key in self._sources
            if self._next[key] <= now and self._retry[key] <= now and self._hard_delay(key) <= 0
        ]
        return min(due, key=lambda key: max(self._next[key], self._retry[key]), default=None)

    def _delay(self) -> float:
        now = self.clock.monotonic()
        delays = [self._hard_delay(key) for key in self._pending]
        delays.extend(
            max(0.0, self._next[key] - now, self._retry[key] - now, self._hard_delay(key))
            for key in self._sources
            if key not in self._pending
        )
        return min(delays, default=math.inf)

    async def _loop(self) -> None:
        while not self._stopping:
            # Cleared before the tick, so a rescan during it wakes the next wait.
            self._wakeup.clear()
            busy = await self.tick()
            if self._stopping:
                break
            delay = _BUSY_RETRY_SECONDS if busy else self._delay()
            # Sleep until the next source is due, or an explicit rescan/shutdown.
            try:
                await asyncio.wait_for(self._wakeup.wait(), None if math.isinf(delay) else delay)
            except TimeoutError:
                pass

    async def _drained(self, function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Run blocking work in a thread; on cancellation, fence and drain it first."""
        work = asyncio.ensure_future(asyncio.to_thread(function, *args, **kwargs))
        try:
            return await asyncio.shield(work)
        except asyncio.CancelledError:
            self._cancel.set()
            self.scheduler.wake()
            # Drain even if this task is cancelled again: staging must never be
            # left running while the caller closes the database.
            while not work.done():
                try:
                    await asyncio.shield(work)
                except BaseException:
                    pass
            raise

    async def _drained_cpu(
        self,
        capture_context: CaptureContext,
        function: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        def execute():
            capture_context.check()
            try:
                return function(*args, **kwargs)
            finally:
                self.scheduler.end_batch(capture_context)

        self.scheduler.end_batch(capture_context)
        result = await self._drained(execute)
        await capture_context.async_checkpoint()
        return result

    async def tick(self) -> bool:
        """Run at most one due source; return True when a due source waits on busy."""
        async with self._lock:
            if self._stopping:
                return False
            source_id = self._due(self.clock.monotonic())
            if source_id is None:
                return False
            if self.busy():
                return True
            if source_id is _REBUILD or source_id is _EMBEDDING:
                await self._tick_rebuild(embedding=source_id is _EMBEDDING)
                return False
            self._cancel = threading.Event()
            started = self.clock.monotonic()
            source = self._sources[source_id]
            job_id = self.store.begin_job(
                source_id, self._job_config(self._source_job_config(source))
            )
            context = CaptureContext(
                self.clock.utc_now(),
                monotonic=self.clock.monotonic,
                cancelled=lambda: self._cancel.is_set() or self._stopping,
                observe_busy=self.busy,
                observe_busy_since=lambda: self.busy_since(),
                utc_now=self.clock.utc_now,
                work_gate=self.scheduler.checkpoint,
                async_gate=self.scheduler.async_checkpoint,
                foreground_window=self.store.store.foreground_window(
                    monotonic=self.clock.monotonic
                ),
                finish_cpu=self.scheduler.end_batch,
            )
            profile = self._profiles.get(source_id)
            reserved = False

            def defer_remote(when):
                nonlocal reserved
                if not reserved:
                    self.remote_state.reserve_poll(
                        profile.profile_id, self.clock.utc_now(), profile.minimum_poll_seconds
                    )
                    reserved = True
                self.remote_state.defer_until(profile.profile_id, utc_iso(when))

            if profile is not None:
                context.defer_remote = defer_remote
            future = self._pending.get(source_id)
            self._active_future = future
            try:
                inventory = await self._drained(self.store.inventory, source_id)
                capture = await self._drained_cpu(
                    context, capture_source, source, previous=inventory, context=context
                )
                context.check_limits()
                if self._stopping or self._cancel.is_set():
                    raise SourceFailure("cancelled", rejected_count=0)
                prepared = await self._drained_cpu(
                    context, self.store.prepare, capture, check=context.check
                )
                if self.index_lifecycle is None:
                    result = self.store.commit(
                        prepared, expected_revision=inventory.revision, check=context.check_limits
                    )
                else:
                    result = await self.index_lifecycle.publish(
                        prepared,
                        expected_revision=inventory.revision,
                        context=context,
                        run_cpu=lambda function, *args, **kwargs: self._drained_cpu(
                            context, function, *args, **kwargs
                        ),
                    )
                self._failures[source_id] = 0
                self._retry[source_id] = 0
                if source.kind == "qualified_remote":
                    interval = max(
                        self.settings.refresh.remote_seconds, profile.minimum_poll_seconds
                    )
                    self._next[source_id] = self.clock.monotonic() + interval
                    self.store.schedule_retry(
                        source_id, utc_iso(self.clock.utc_now() + timedelta(seconds=interval))
                    )
                else:
                    self._next[source_id] = (
                        self.clock.monotonic() + self.settings.refresh.local_scan_seconds
                        if source.kind == "local_directory"
                        else float("inf")
                    )
                self._finish(job_id, context, started, "succeeded", None)
            except asyncio.CancelledError:
                # The thread was drained; record the job, then let the cancel propagate.
                self._fail(
                    source_id,
                    job_id,
                    context,
                    started,
                    future,
                    SourceFailure("cancelled", rejected_count=0),
                )
                await self._notify_changed()
                raise
            except Exception as exc:
                failure = (
                    exc
                    if isinstance(exc, SourceFailure)
                    # A publication conflict is an index error (spec 8.1 closed reasons).
                    else SourceFailure("index_error", rejected_count=0)
                )
                self._fail(source_id, job_id, context, started, future, failure)
                await self._notify_changed()
            else:
                try:
                    await self._notify_changed()
                finally:
                    # Even a cancelled observer leaves the committed result delivered.
                    if future is not None and not future.done():
                        future.set_result(result)
            finally:
                if context.foreground_window is not None:
                    self.store.store.release_foreground_window(context.foreground_window)
                self._active_future = None
                if future is not None and self._pending.get(source_id) is future:
                    self._pending.pop(source_id)
            return False

    async def _tick_rebuild(self, *, embedding=False) -> None:
        owner = self.embedding_lifecycle if embedding else self.index_lifecycle
        assert owner is not None
        token = _EMBEDDING if embedding else _REBUILD
        self._cancel = threading.Event()
        started = self.clock.monotonic()
        context = CaptureContext(
            self.clock.utc_now(),
            monotonic=self.clock.monotonic,
            cancelled=lambda: self._cancel.is_set() or self._stopping,
            observe_busy=self.busy,
            observe_busy_since=lambda: self.busy_since(),
            utc_now=self.clock.utc_now,
            work_gate=self.scheduler.checkpoint,
            async_gate=self.scheduler.async_checkpoint,
            finish_cpu=self.scheduler.end_batch,
        )
        future = self._pending[token]
        self._active_future = future
        job_id = self.store.begin_job(
            None,
            self._job_config(
                owner.generation_config() if embedding else owner.provider.generation_config()
            ),
            job_kind="embedding" if embedding else "lexical",
        )
        context.foreground_window = self.store.store.foreground_window(
            monotonic=self.clock.monotonic
        )
        try:
            await context.async_checkpoint()
            if embedding:
                result = await owner.rebuild(
                    context=context,
                    run_cpu=lambda function, *args, **kwargs: self._drained_cpu(
                        context, function, *args, **kwargs
                    ),
                )
            else:
                result = await owner.rebuild(
                    check=context.check_limits, yield_control=context.async_checkpoint
                )
            context.check_limits()
            self._finish(
                job_id,
                context,
                started,
                "succeeded",
                None,
                index_revision=owner.revision if embedding else _DEFAULT_INDEX,
            )
        except asyncio.CancelledError:
            context.rejected_count = 0
            self._finish(
                job_id,
                context,
                started,
                "cancelled",
                "shutdown",
                index_revision=owner.revision if embedding else _DEFAULT_INDEX,
            )
            raise
        except Exception as exc:
            failure = (
                exc
                if isinstance(exc, SourceFailure)
                else SourceFailure("index_error", rejected_count=0)
            )
            state, reason = "failed", failure.reason
            if failure.reason == "cancelled":
                state, reason = "cancelled", "shutdown" if self._stopping else "operator"
            context.rejected_count = failure.rejected_count
            self._finish(
                job_id,
                context,
                started,
                state,
                reason,
                index_revision=owner.revision if embedding else _DEFAULT_INDEX,
            )
            if not future.done():
                future.set_exception(failure)
        else:
            try:
                if self._observer_future is future:
                    await self._notify_changed(allow_defer=False)
            finally:
                # Observer cancellation cannot undo a committed publication.
                if not future.done():
                    future.set_result(result)
        finally:
            self.store.store.release_foreground_window(context.foreground_window)
            if not future.done():
                future.set_exception(SourceFailure("cancelled", rejected_count=0))
            self._active_future = None
            if self._observer_future is future:
                self._observer_future = None
            if self._pending.get(token) is future:
                self._pending.pop(token)

    def _fail(
        self,
        source_id: str,
        job_id: str,
        context: CaptureContext,
        started: float,
        future: "asyncio.Future[Publication] | None",
        failure: SourceFailure,
    ) -> None:
        self.store.record_failure(
            source_id, rejected_count=failure.rejected_count, exclusions=failure.exclusions
        )
        self._failures[source_id] += 1
        delay = self._backoff(self._failures[source_id])
        self._retry[source_id] = self.clock.monotonic() + delay
        self._next[source_id] = self._retry[source_id]
        self.store.schedule_retry(
            source_id, utc_iso(self.clock.utc_now() + timedelta(seconds=delay))
        )
        context.rejected_count = failure.rejected_count
        state, reason = "failed", failure.reason
        if failure.reason == "cancelled":
            # Spec 8.1: a cancelled job's reason is shutdown or operator.
            task = asyncio.current_task()
            shutdown = self._stopping or (task is not None and task.cancelling() > 0)
            state, reason = "cancelled", "shutdown" if shutdown else "operator"
        self._finish(job_id, context, started, state, reason)
        if future is not None and not future.done():
            future.set_exception(failure)

    def _finish(
        self,
        job_id: str,
        context: CaptureContext,
        started: float,
        state: str,
        reason: str | None,
        *,
        index_revision=_DEFAULT_INDEX,
    ) -> None:
        now = self.clock.monotonic()
        self.scheduler.end_batch(context, force=True)
        if context.native_io_ms:
            _LOGGER.info("asset native I/O job=%s duration_ms=%s", job_id, context.native_io_ms)
        batches = sorted(
            [*context.batch_observations, *context.work_batches],
            key=lambda batch: batch["timestamp"],
        )
        automatic_index = index_revision is _DEFAULT_INDEX
        if automatic_index:
            index_revision = None
        if automatic_index and self.index_lifecycle is not None:
            try:
                index_revision = self.index_lifecycle.provider.pin(
                    ("skill", "mcp_tool")
                ).index.index_revision
            except Exception:
                pass  # No compatible index measurement is available.
        self.store.finish_job(
            job_id,
            state=state,
            reason=reason,
            duration_ms=max(0, (now - started) * 1000),
            received_bytes=context.received_bytes,
            batch_ms=tuple(b["duration_ms"] for b in batches),
            overlap_observed=context.busy_observed_at is not None,
            batches=tuple(batches),
            index_revision=index_revision,
            accepted_count=sum(b["accepted_count"] for b in context.batch_observations)
            + context.batch_records,
            rejected_count=context.rejected_count,
            drain_ms=context.drain_ms,
        )


def _log_worker_end(task: asyncio.Task) -> None:
    """A worker that ended with an error is reported, never lost silently."""
    if not task.cancelled() and task.exception() is not None:
        _LOGGER.error("asset catalog worker stopped", exc_info=task.exception())
