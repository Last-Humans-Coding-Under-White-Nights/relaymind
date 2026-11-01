"""Owned local embedding inference, truthful observations and late-result fences (RM41)."""

import asyncio
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from relaymind.assets.embedding_manifest import EmbeddingManifest
from relaymind.assets.embeddings import EmbeddingError, Representation, normalize_vectors
from relaymind.schema_util import freeze_json


class NativeEmbeddingBackend(Protocol):
    def encode_segment(self, value: str) -> Sequence[int]: ...
    def special_tokens(self, tokens: Sequence[int]) -> Sequence[int]: ...
    def infer(self, token_batches: Sequence[Sequence[int]], invoked: Callable[[], None]): ...
    def cancel(self) -> None: ...


class EmbeddingFailure(EmbeddingError):
    def __init__(self, reason: str, observations=()):
        super().__init__(reason)
        self.observations = tuple(freeze_json(o) for o in observations)


@dataclass(frozen=True)
class EmbeddingBatch:
    vectors: tuple[tuple[float, ...], ...]
    observation: object


class EmbeddingRun:
    """Live counters only; an owner may synchronously fence and flush before terminal."""

    def __init__(
        self, manifest, revision, byte_count, token_count, clock, deadline, cancelled, cancel
    ):
        self.manifest, self.revision = manifest, revision
        self.byte_count, self.token_count = byte_count, token_count
        self.clock, self.deadline, self.cancelled, self.cancel = clock, deadline, cancelled, cancel
        self._lock = threading.RLock()
        self.started = self.ended = self.outcome = None
        self.fenced = False

    def invoke(self):
        with self._lock:
            if self.fenced or self.cancelled():
                raise EmbeddingFailure("embedding_cancelled")
            if self.clock() >= self.deadline:
                raise EmbeddingFailure("embedding_deadline")
            if self.started is not None:
                raise EmbeddingFailure("invalid_backend_invocation")
            self.started = self.clock()

    def finish(self, outcome):
        with self._lock:
            if self.ended is None:
                self.ended, self.outcome = self.clock(), outcome

    def fence(self, outcome="cancelled"):
        with self._lock:
            self.fenced = True
            self.finish(outcome)
            self.cancel()

    def snapshot(self):
        with self._lock:
            if self.started is None:
                return None
            return freeze_json(
                dict(
                    model=dict(self.manifest.data["model"]),
                    artifact_hash=self.manifest.artifact_hash,
                    representation_revision=self.revision,
                    device=self.manifest.data["device"],
                    input_bytes=self.byte_count,
                    input_tokens=self.token_count,
                    duration_ms=max(
                        0.0,
                        ((self.clock() if self.ended is None else self.ended) - self.started)
                        * 1000,
                    ),
                    cost_observations=[
                        dict(
                            provenance="observed",
                            value=dict(
                                input_tokens=self.token_count,
                                output_tokens=None,
                                cached_tokens=None,
                                reasoning_tokens=None,
                                calls=1,
                                cpu_ms=None,
                                peak_bytes=None,
                            ),
                        )
                    ],
                    outcome=self.outcome or "failed",
                )
            )


class EmbeddingEngine:
    """One native invocation at a time, retained until actual native cleanup.

    Activation/factory owns reviewed backend and artifact authority. Explicit
    constructor injection is the deterministic-test/development seam, never an
    operator config or environment bypass of the production qualification registry.
    """

    def __init__(self, manifest: EmbeddingManifest, native: NativeEmbeddingBackend):
        self.manifest, self.native = manifest, native
        self._pending = None

    async def drain(self) -> None:
        pending = self._pending
        cancelled = False
        if pending is not None:
            while not pending.done():
                try:
                    await asyncio.shield(pending)
                except asyncio.CancelledError:
                    current = asyncio.current_task()
                    cancelled = cancelled or (current is not None and current.cancelling() > 0)
                except Exception:
                    break
        if cancelled:
            raise asyncio.CancelledError

    async def embed(
        self,
        inputs: Sequence[Representation],
        *,
        max_tokens: int,
        deadline: float,
        cancelled: Callable[[], bool] = lambda: False,
        clock: Callable[[], float] = time.monotonic,
        private=None,
        observe=None,
        work_control=None,
    ) -> EmbeddingBatch:
        if self._pending is not None and not self._pending.done():
            raise EmbeddingFailure("embedding_busy")
        run = None
        token_batches = []
        if private is not None:
            private.register(token_batches)
            private.register_cancel(self.native.cancel)

        def check():
            if cancelled() or run is not None and run.fenced:
                raise EmbeddingFailure("embedding_cancelled")
            if clock() >= deadline:
                raise EmbeddingFailure("embedding_deadline")

        try:
            check()
            if not 1 <= len(inputs) <= 25 or len({i.revision for i in inputs}) != 1:
                raise EmbeddingFailure("invalid_embedding_batch")
            for value in inputs:
                check()
                tokens = value.token_ids(self.native, max_tokens)
                if private is not None:
                    tokens = private.register(list(tokens))
                token_batches.append(tokens)
            check()
            run = EmbeddingRun(
                self.manifest,
                inputs[0].revision,
                sum(len(i.data) for i in inputs),
                sum(map(len, token_batches)),
                clock,
                deadline,
                cancelled,
                self.native.cancel,
            )

            def invoked():
                run.invoke()

            if private is not None:
                invoked.register_buffer = private.register
                private.register_cancel(run.fence)
            if observe is not None:
                observe(run)

            def infer():
                if work_control is not None:
                    work_control.check()
                try:
                    return self.native.infer(token_batches, invoked)
                finally:
                    if work_control is not None:
                        work_control.end_cpu_window()

            if work_control is not None:
                work_control.end_cpu_window()
            task = asyncio.create_task(asyncio.to_thread(infer))
            self._pending = task

            def consume(finished):
                # No late result writes to a cache/index or an erased request.
                try:
                    finished.exception()
                except asyncio.CancelledError:
                    pass
                if self._pending is finished:
                    self._pending = None

            task.add_done_callback(consume)
            while not task.done():
                check()
                await asyncio.wait([task], timeout=min(0.005, max(0, deadline - clock())))
            check()
            vectors = normalize_vectors(
                task.result(), count=len(inputs), dimensions=self.manifest.data["dimensions"]
            )
            run.finish("succeeded")
            obs = run.snapshot()
            if obs is None:
                raise EmbeddingFailure("missing_backend_invocation")
            return EmbeddingBatch(vectors, freeze_json(obs))
        except (Exception, asyncio.CancelledError) as failure:
            reason = (
                "embedding_cancelled"
                if isinstance(failure, asyncio.CancelledError) or cancelled()
                else str(failure)
                if isinstance(failure, EmbeddingError)
                else "embedding_failed"
            )
            if run is not None:
                run.fence("cancelled" if reason == "embedding_cancelled" else "failed")
            else:
                self.native.cancel()
            obs = None if run is None else run.snapshot()
            raise EmbeddingFailure(reason, () if obs is None else [obs]) from None
