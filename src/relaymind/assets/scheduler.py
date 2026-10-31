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
        # A closed window may retain short CPU fragments across native I/O.
        # Flush only through its last CPU boundary, even on failure/cancel.
        now = (
            context.cpu_quiescent_at
            if context.foreground_window is not None and context.cpu_quiescent_at is not None
            else context.monotonic()
        )
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
        if context.foreground_window is not None:
            context.end_cpu_window()
            self.finish_batch(context, force=True)
            return
        # Admission can occur after check_limits while the busy query waits
        # for its transaction. Read the actual onset again at the drain point.
        context.busy_since = context.observe_busy_since()
        now = context.monotonic()
        if context.busy_since is not None:
            if context.busy_observed_at is None:
                context.busy_observed_at = now
            elapsed = max(0, now - context.busy_since) * 1000
            context.drain_ms = max(context.drain_ms or 0, elapsed)
        self.finish_batch(context, force=True)

    def end_batch(self, context: CaptureContext, *, force: bool = False) -> None:
        active, observed = context.end_cpu_window()
        if active or force:
            self.finish_batch(context, force=force or observed)
        context.cpu_resuming = True

    def _checkpoint(self, context: CaptureContext) -> bool:
        context.check_limits()
        busy = self.busy()
        if context.foreground_window is not None:
            if busy:
                self._drain(context)
                context.cpu_resuming = True
            else:
                active, observed = context.end_cpu_window()
                if active:
                    self.finish_batch(context, force=observed)
        else:
            self.finish_batch(context)
            if busy:
                self._drain(context)
        return busy

    @staticmethod
    def _begin(context: CaptureContext) -> None:
        if context.foreground_window is not None:
            context.foreground_window.begin()

    def _admit(self, context: CaptureContext) -> bool:
        if context.foreground_window is None:
            return True
        started = context.monotonic()
        # Observe before the final authoritative query: a local acquisition
        # after that query must reach an already active window. Never query
        # SQLite while holding the window lock (commit takes the other order).
        try:
            self._begin(context)
            busy = self.busy()
        except BaseException:
            context.foreground_window.end()
            raise
        if busy:
            # This provisional attempt admitted no CPU. Keep the previous
            # quiescent boundary and exclude its waiting from CPU/drain.
            context.foreground_window.end()
            return False
        if context.cpu_resuming:
            if context.cpu_quiescent_at is not None:
                context.work_batch_started += max(0, started - context.cpu_quiescent_at)
            context.cpu_quiescent_at = None
            context.cpu_resuming = False
        return True

    def checkpoint(self, context: CaptureContext) -> None:
        """A capture/preparation thread waits on a signal, still under its original deadline."""
        busy = self._checkpoint(context)
        while busy or not self._admit(context):
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
                if context.foreground_window is None:
                    context.work_batch_started += waited
            busy = False

    async def async_checkpoint(self, context: CaptureContext) -> None:
        """Yield serving-loop work; wake signals both thread and coroutine waiters."""
        event = asyncio.Event()
        waiter = (asyncio.get_running_loop(), event)
        # Register before checking the predicate, so an idle/cancel signal
        # between the check and the wait cannot be lost.
        with self._condition:
            self._async_waiters.add(waiter)
        try:
            busy = self._checkpoint(context)
            while True:
                if busy:
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
                        if context.foreground_window is None:
                            context.work_batch_started += waited
                if context.foreground_window is None:
                    break
                # The serving-loop yield is quiescent too. Recheck foreground
                # ownership before admitting the continuation's CPU window.
                await asyncio.sleep(0)
                context.check_limits()
                if self._admit(context):
                    break
                busy = True
        finally:
            with self._condition:
                self._async_waiters.discard(waiter)
        # Admission of a new foreground run must be able to update busy.
        if context.foreground_window is None:
            await asyncio.sleep(0)
