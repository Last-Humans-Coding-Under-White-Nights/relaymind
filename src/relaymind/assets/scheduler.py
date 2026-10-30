"""Cooperative batch gates for the existing single asset worker (RM38/AC29)."""

import asyncio
import threading
from collections.abc import Callable

from relaymind.assets.sources import CaptureContext
from relaymind.store.run_store import utc_iso

CPU_BATCH_SECONDS = 0.05
_WAIT_SECONDS = 0.05


class AssetWorkScheduler:
    """No worker or execution slot of its own; synchronous and async work share a gate."""

    def __init__(self, *, busy: Callable[[], bool]):
        self.busy = busy
        self._condition = threading.Condition()
        self._async_waiters: set[tuple[asyncio.AbstractEventLoop, asyncio.Event]] = set()

    def wake(self) -> None:
        with self._condition:
            self._condition.notify_all()
            for loop, event in self._async_waiters:
                loop.call_soon_threadsafe(event.set)

    def finish_batch(self, context: CaptureContext, *, force: bool = False) -> None:
        now = context.monotonic()
        duration = max(0, now - context.work_batch_started)
        if duration > 0 and (force or duration >= CPU_BATCH_SECONDS):
            context.work_batches.append(
                dict(
                    timestamp=utc_iso(context.utc_now()),
                    accepted_count=0,
                    rejected_count=0,
                    received_bytes=0,
                    duration_ms=duration * 1000,
                )
            )
            context.work_batch_started = now

    def _drain(self, context: CaptureContext) -> None:
        now = context.monotonic()
        if context.busy_since is not None:
            elapsed = max(0, now - context.busy_since) * 1000
            context.drain_ms = max(context.drain_ms or 0, elapsed)
        self.finish_batch(context, force=True)

    def checkpoint(self, context: CaptureContext) -> None:
        """A capture/preparation thread waits on a signal, still under its original deadline."""
        context.check_limits()
        self.finish_batch(context)
        if not self.busy():
            return
        self._drain(context)
        started = context.monotonic()
        try:
            with self._condition:
                while self.busy():
                    context.check_limits()
                    remaining = context.deadline - context.monotonic()
                    self._condition.wait(timeout=max(0, min(_WAIT_SECONDS, remaining)))
        finally:
            waited = max(0, context.monotonic() - started)
            context.batch_started += waited
            context.work_batch_started += waited

    async def async_checkpoint(self, context: CaptureContext) -> None:
        """Yield serving-loop work; wake signals both thread and coroutine waiters."""
        event = asyncio.Event()
        waiter = (asyncio.get_running_loop(), event)
        # Register before checking the predicate, so an idle/cancel signal
        # between the check and the wait cannot be lost.
        with self._condition:
            self._async_waiters.add(waiter)
        try:
            context.check_limits()
            self.finish_batch(context)
            if self.busy():
                self._drain(context)
                started = context.monotonic()
                try:
                    while True:
                        event.clear()
                        context.check_limits()
                        if not self.busy():
                            break
                        remaining = context.deadline - context.monotonic()
                        try:
                            await asyncio.wait_for(
                                event.wait(), max(0, min(_WAIT_SECONDS, remaining))
                            )
                        except TimeoutError:
                            pass
                finally:
                    waited = max(0, context.monotonic() - started)
                    context.batch_started += waited
                    context.work_batch_started += waited
        finally:
            with self._condition:
                self._async_waiters.discard(waiter)
        # Admission of a new foreground run must be able to update busy.
        await asyncio.sleep(0)
