"""One bounded local/import staging worker, separate from run admission (RM34).

The catalog supplies discovery data only. Recommendation endpoints, lexical
readiness and the pre-warmup serving order belong to later milestones.
"""

import asyncio
import logging
import math
import threading
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from relaymind.assets.settings import AssetSettings
from relaymind.assets.sources import (
    CaptureContext,
    SourceFailure,
    capture_source,
    source_config_digest,
)
from relaymind.store.asset_catalog import AssetCatalogStore, Publication
from relaymind.store.run_store import TraceClock, utc_iso

_LOGGER = logging.getLogger(__name__)

# While a source is due but the gateway is busy, look again this often.
_BUSY_RETRY_SECONDS = 0.1


class AssetCatalogService:
    def __init__(
        self,
        settings: AssetSettings,
        store: AssetCatalogStore,
        *,
        clock: TraceClock | None = None,
        busy: Callable[[], bool] = lambda: False,
    ) -> None:
        self.settings, self.store = settings, store
        self.clock = clock or TraceClock()
        self.busy = busy
        self._sources = {s.source_id: s for s in settings.sources}
        self._next = dict.fromkeys(self._sources, 0.0)
        self._failures = dict.fromkeys(self._sources, 0)
        self._retry = dict.fromkeys(self._sources, 0.0)
        self._pending: dict[str, asyncio.Future[Publication]] = {}
        self._waiters = dict.fromkeys(self._sources, 0)
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._stopping = False
        self._cancel = threading.Event()
        self._active_future: asyncio.Future[Publication] | None = None
        self._wakeup = asyncio.Event()
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

    async def stop(self) -> None:
        self._stopping = True
        self._cancel.set()
        self._wakeup.set()
        # Do not cancel/abandon to_thread: its checked staging must drain before
        # closing the database. The SQL commit remains on this event loop.
        try:
            if self._task is not None:
                try:
                    # Shielded: cancelling stop() itself never cancels the draining worker.
                    await asyncio.shield(self._task)
                except asyncio.CancelledError:
                    current = asyncio.current_task()
                    if not self._task.cancelled() or (current and current.cancelling()):
                        raise
                except Exception:
                    pass  # The done callback already logged how the worker ended.
        finally:
            # Even when stop() is cancelled, no rescan caller is left waiting.
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(SourceFailure("cancelled", rejected_count=0))
            self._pending.clear()

    async def rescan(self, source_id: str) -> Publication:
        """Run this source now, ignoring its failure backoff (an explicit operator request).

        Callers asking for the same source share one job. A cancelled caller cancels
        that job only when no other caller still waits for it.
        """
        if source_id not in self._sources:
            raise ValueError("unknown_source")
        if self._stopping:
            raise SourceFailure("cancelled", rejected_count=0)
        future = self._pending.get(source_id)
        if future is None:
            future = asyncio.get_running_loop().create_future()
            # Consume abandoned waiter exceptions without hiding them from any
            # callers still awaiting the shared source job.
            future.add_done_callback(lambda done: None if done.cancelled() else done.exception())
            self._pending[source_id] = future
        self._waiters[source_id] += 1
        self.start()
        self._wakeup.set()
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            if self._waiters[source_id] == 1 and not future.done():
                if self._active_future is future:
                    self._cancel.set()
                elif self._pending.get(source_id) is future:
                    self._pending.pop(source_id, None)
                    future.cancel()
            raise
        finally:
            self._waiters[source_id] -= 1

    def _due(self, now: float) -> str | None:
        # An explicit rescan is due at once; a periodic one after its backoff.
        pending = next(iter(self._pending), None)
        if pending is not None:
            return pending
        # The source due longest goes first, so slow scans cannot starve a later
        # source; declaration order only breaks ties (min keeps the first).
        due = [k for k in self._sources if self._next[k] <= now and self._retry[k] <= now]
        return min(due, key=lambda k: max(self._next[k], self._retry[k]), default=None)

    def _delay(self) -> float:
        if self._pending:
            return 0.0
        now = self.clock.monotonic()
        due = min((max(self._next[k], self._retry[k]) for k in self._sources), default=math.inf)
        return max(0.0, due - now)

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
            # Drain even if this task is cancelled again: staging must never be
            # left running while the caller closes the database.
            while not work.done():
                try:
                    await asyncio.shield(work)
                except BaseException:
                    pass
            raise

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
            self._cancel = threading.Event()
            started = self.clock.monotonic()
            source = self._sources[source_id]
            job_id = self.store.begin_job(source_id, source_config_digest(source))
            context = CaptureContext(
                self.clock.utc_now(),
                monotonic=self.clock.monotonic,
                cancelled=lambda: self._cancel.is_set() or self._stopping,
                observe_busy=self.busy,
            )
            future = self._pending.get(source_id)
            self._active_future = future
            try:
                inventory = await self._drained(self.store.inventory, source_id)
                capture = await self._drained(
                    capture_source, source, previous=inventory, context=context
                )
                context.check()
                if self._stopping or self._cancel.is_set():
                    raise SourceFailure("cancelled", rejected_count=0)
                prepared = await self._drained(self.store.prepare, capture, check=context.check)
                result = self.store.commit(
                    prepared, expected_revision=inventory.revision, check=context.check
                )
                self._failures[source_id] = 0
                self._retry[source_id] = 0
                self._next[source_id] = (
                    self.clock.monotonic() + self.settings.refresh.local_scan_seconds
                    if source.kind == "local_directory"
                    else float("inf")
                )
                self._finish(job_id, context, started, "succeeded", None)
                if future is not None and not future.done():
                    future.set_result(result)
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
                raise
            except Exception as exc:
                failure = (
                    exc
                    if isinstance(exc, SourceFailure)
                    # A publication conflict is an index error (spec 8.1 closed reasons).
                    else SourceFailure("index_error", rejected_count=0)
                )
                self._fail(source_id, job_id, context, started, future, failure)
            finally:
                self._active_future = None
                if future is not None and self._pending.get(source_id) is future:
                    self._pending.pop(source_id)
            return False

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
        self, job_id: str, context: CaptureContext, started: float, state: str, reason: str | None
    ) -> None:
        now = self.clock.monotonic()
        self.store.finish_job(
            job_id,
            state=state,
            reason=reason,
            duration_ms=max(0, (now - started) * 1000),
            received_bytes=context.received_bytes,
            batch_ms=tuple(context.batch_durations_ms),
            overlap_observed=context.busy_observed_at is not None,
            drain_ms=None
            if context.busy_observed_at is None
            else max(0, (now - context.busy_observed_at) * 1000),
        )


def _log_worker_end(task: asyncio.Task) -> None:
    """A worker that ended with an error is reported, never lost silently."""
    if not task.cancelled() and task.exception() is not None:
        _LOGGER.error("asset catalog worker stopped", exc_info=task.exception())
