"""One bounded local/import staging worker, separate from run admission (RM34).

The catalog supplies discovery data only. Recommendation endpoints, lexical
readiness and the pre-warmup serving order belong to later milestones.
"""

import asyncio
import threading
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

from relaymind.assets.settings import AssetSettings
from relaymind.assets.sources import (
    CaptureContext,
    SourceFailure,
    capture_source,
    source_config_digest,
)
from relaymind.store.asset_catalog import AssetCatalogStore, CatalogConflict, Publication
from relaymind.store.run_store import TraceClock, utc_iso


class AssetCatalogService:
    def __init__(
        self,
        settings: AssetSettings,
        store: AssetCatalogStore,
        *,
        clock: TraceClock | None = None,
        busy: Callable[[], bool] = lambda: False,
        wait: Callable[[float], Awaitable[object]] = asyncio.sleep,
    ) -> None:
        self.settings, self.store = settings, store
        self.clock = clock or TraceClock()
        self.busy, self.wait = busy, wait
        self.warmup_busy = False
        self._sources = {s.source_id: s for s in settings.sources}
        self._next = dict.fromkeys(self._sources, 0.0)
        self._failures = dict.fromkeys(self._sources, 0)
        self._retry = dict.fromkeys(self._sources, 0.0)
        self._pending: dict[str, asyncio.Future[Publication]] = {}
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._stopping = False
        self._cancel = threading.Event()
        self._active_source: str | None = None
        self._wakeup = asyncio.Event()
        self.store.recover_interrupted_jobs()
        for source_id, failures, next_attempt in self.store.schedules():
            if source_id not in self._sources:
                continue
            self._failures[source_id] = failures
            if next_attempt is None and failures:
                delay = min(
                    settings.refresh.backoff_max_seconds,
                    settings.refresh.backoff_initial_seconds * 2 ** min(failures - 1, 32),
                )
                next_attempt = utc_iso(self.clock.utc_now() + timedelta(seconds=delay))
                self.store.schedule_retry(source_id, next_attempt)
            if next_attempt is not None:
                remaining = max(
                    0.0,
                    (datetime.fromisoformat(next_attempt) - self.clock.utc_now()).total_seconds(),
                )
                self._retry[source_id] = self.clock.monotonic() + remaining
                self._next[source_id] = self._retry[source_id]

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if not self.running:
            self._stopping = False
            self._task = asyncio.create_task(self._loop(), name="asset-catalog")

    async def stop(self) -> None:
        self._stopping = True
        self._cancel.set()
        self._wakeup.set()
        # Do not cancel/abandon to_thread: its checked staging must drain before
        # closing the database. Publication remains on this event loop.
        if self._task is not None:
            await self._task
        async with self._lock:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(SourceFailure("cancelled", rejected_count=0))
            self._pending.clear()

    async def rescan(self, source_id: str) -> Publication:
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
        self.start()
        self._wakeup.set()
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            if self._active_source == source_id:
                self._cancel.set()
            elif self._pending.get(source_id) is future:
                self._pending.pop(source_id, None)
                future.cancel()
            raise

    async def _loop(self) -> None:
        while not self._stopping:
            await self.tick()
            if self._stopping:
                break
            # The injected wait is used for scheduling tests; production waking
            # races a bounded wait so explicit rescan/shutdown is immediate.
            sleeper = asyncio.create_task(self.wait(0.1))
            wake = asyncio.create_task(self._wakeup.wait())
            try:
                await asyncio.wait((sleeper, wake), return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in (sleeper, wake):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(sleeper, wake, return_exceptions=True)
                self._wakeup.clear()

    async def tick(self) -> None:
        async with self._lock:
            if self._stopping or self.warmup_busy or self.busy():
                return
            now = self.clock.monotonic()
            source_id = next((key for key in self._pending if self._retry[key] <= now), None)
            if source_id is None:
                source_id = next(
                    (
                        key
                        for key in self._sources
                        if self._next[key] <= now and self._retry[key] <= now
                    ),
                    None,
                )
            if source_id is None:
                return
            self._active_source = source_id
            self._cancel = threading.Event()
            started = self.clock.monotonic()
            source = self._sources[source_id]
            job_id = self.store.begin_job(source_id, source_config_digest(source))
            context = CaptureContext(
                self.clock.utc_now(),
                monotonic=self.clock.monotonic,
                cancelled=lambda: self._cancel.is_set() or self._stopping,
                observe_busy=lambda: self.warmup_busy or self.busy(),
            )
            future = self._pending.get(source_id)
            try:
                inventory = self.store.inventory(source_id)
                task = asyncio.create_task(
                    asyncio.to_thread(capture_source, source, previous=inventory, context=context)
                )
                try:
                    capture = await asyncio.shield(task)
                except asyncio.CancelledError:
                    self._cancel.set()
                    try:
                        await asyncio.shield(task)
                    except SourceFailure:
                        pass
                    raise SourceFailure("cancelled", rejected_count=0) from None
                context.check()
                if self._stopping or self._cancel.is_set():
                    raise SourceFailure("cancelled", rejected_count=0)
                result = self.store.publish(
                    capture, expected_revision=inventory.revision, check=context.check
                )
                self._failures[source_id] = 0
                self._retry[source_id] = 0
                self._next[source_id] = (
                    self.clock.monotonic() + self.settings.refresh.local_scan_seconds
                    if source.kind == "local_directory"
                    else float("inf")
                )
                self.store.finish_job(
                    job_id,
                    state="succeeded",
                    reason=None,
                    duration_ms=max(0, (self.clock.monotonic() - started) * 1000),
                    received_bytes=context.received_bytes,
                    batch_ms=tuple(context.batch_durations_ms),
                    overlap_observed=context.busy_observed_at is not None,
                    drain_ms=None
                    if context.busy_observed_at is None
                    else max(0, (self.clock.monotonic() - context.busy_observed_at) * 1000),
                )
                if future is not None and not future.done():
                    future.set_result(result)
            except Exception as exc:
                failure = (
                    exc
                    if isinstance(exc, SourceFailure)
                    else SourceFailure(
                        "catalog_conflict" if isinstance(exc, CatalogConflict) else "index_error"
                    )
                )
                self.store.record_failure(source_id, failure)
                self._failures[source_id] += 1
                refresh = self.settings.refresh
                delay = min(
                    refresh.backoff_max_seconds,
                    refresh.backoff_initial_seconds * 2 ** min(self._failures[source_id] - 1, 32),
                )
                self._retry[source_id] = self.clock.monotonic() + delay
                self._next[source_id] = self._retry[source_id]
                self.store.schedule_retry(
                    source_id, utc_iso(self.clock.utc_now() + timedelta(seconds=delay))
                )
                self.store.finish_job(
                    job_id,
                    state="cancelled" if failure.reason == "cancelled" else "failed",
                    reason=failure.reason,
                    duration_ms=max(0, (self.clock.monotonic() - started) * 1000),
                    received_bytes=context.received_bytes,
                    batch_ms=tuple(context.batch_durations_ms),
                    overlap_observed=context.busy_observed_at is not None,
                    drain_ms=None
                    if context.busy_observed_at is None
                    else max(0, (self.clock.monotonic() - context.busy_observed_at) * 1000),
                )
                if future is not None and not future.done():
                    future.set_exception(failure)
            finally:
                self._active_source = None
                if future is not None and self._pending.get(source_id) is future:
                    self._pending.pop(source_id)
