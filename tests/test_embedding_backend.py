"""RM38/RM41, AC29/AC32: actual inference ownership and fenced results."""

import asyncio
import threading
import time

import pytest

from test_embedding_manifest import fixture


def api():
    from relaymind.assets import embedding_backends

    return embedding_backends


class NativeDouble:
    def encode_segment(self, value):
        return [ord(c) for c in value]

    def special_tokens(self, tokens):
        return [1, *tokens, 2]

    def infer(self, token_batches, invoked):
        invoked()
        return [[3, 4] for _ in token_batches]

    def cancel(self):
        pass


def make_engine(tmp_path, native=None):
    from relaymind.assets.embedding_manifest import load_embedding_manifest

    path, _ = fixture(tmp_path)
    return api().EmbeddingEngine(load_embedding_manifest(path), native or NativeDouble())


def test_batch_records_one_real_invocation_and_normalized_outputs(tmp_path):
    from relaymind.assets.embeddings import represent_query

    engine = make_engine(tmp_path)
    batch = asyncio.run(
        engine.embed([represent_query("你好", "")], max_tokens=10, deadline=time.monotonic() + 1)
    )
    assert batch.vectors == ((0.6, 0.8),)
    assert batch.observation["input_bytes"] == 6
    assert batch.observation["input_tokens"] == 4
    assert batch.observation["outcome"] == "succeeded"
    cost = batch.observation["cost_observations"][0]["value"]
    assert cost["calls"] == 1 and cost["input_tokens"] == 4
    assert cost["output_tokens"] is None and cost["cpu_ms"] is None


def test_overflow_and_predispatch_deadline_never_fabricate_inference(tmp_path):
    from relaymind.assets.embeddings import represent_query

    engine = make_engine(tmp_path)
    for max_tokens, deadline in [(1, time.monotonic() + 1), (10, time.monotonic() - 1)]:
        with pytest.raises(api().EmbeddingFailure) as caught:
            asyncio.run(
                engine.embed(
                    [represent_query("Hello", "")], max_tokens=max_tokens, deadline=deadline
                )
            )
        assert caught.value.observations == ()


def test_invalid_native_output_retains_actual_failed_inference_cost(tmp_path):
    class Invalid(NativeDouble):
        def infer(self, token_batches, invoked):
            invoked()
            return [[float("nan"), 1]]

    from relaymind.assets.embeddings import represent_query

    engine = make_engine(tmp_path, Invalid())
    with pytest.raises(api().EmbeddingFailure) as caught:
        asyncio.run(
            engine.embed(
                [represent_query("Hello", "")], max_tokens=20, deadline=time.monotonic() + 1
            )
        )
    observation = caught.value.observations[0]
    assert observation["outcome"] == "failed"
    assert observation["cost_observations"][0]["value"]["calls"] == 1


def test_cancellation_fences_late_vector_and_keeps_engine_busy_until_drain(tmp_path):
    class Slow(NativeDouble):
        def __init__(self):
            self.started, self.release = threading.Event(), threading.Event()

        def infer(self, token_batches, invoked):
            invoked()
            self.started.set()
            self.release.wait(1)
            return [[3, 4]]

    from relaymind.assets.embeddings import represent_query

    native = Slow()
    engine = make_engine(tmp_path, native)

    async def run():
        cancelled = False
        pending = asyncio.create_task(
            engine.embed(
                [represent_query("你好", "")],
                max_tokens=20,
                deadline=time.monotonic() + 1,
                cancelled=lambda: cancelled,
            )
        )
        while not native.started.is_set():
            await asyncio.sleep(0.001)
        cancelled = True
        with pytest.raises(api().EmbeddingFailure) as caught:
            await pending
        assert caught.value.observations[0]["outcome"] == "cancelled"
        with pytest.raises(api().EmbeddingFailure, match="embedding_busy"):
            await engine.embed(
                [represent_query("Hi", "")], max_tokens=20, deadline=time.monotonic() + 1
            )
        native.release.set()
        await engine.drain()
        result = await engine.embed(
            [represent_query("Hi", "")], max_tokens=20, deadline=time.monotonic() + 1
        )
        assert result.vectors == ((0.6, 0.8),)

    asyncio.run(run())


def test_inference_deadline_and_observation_use_the_owners_clock(tmp_path):
    from relaymind.assets.embeddings import represent_query

    engine = make_engine(tmp_path)
    result = asyncio.run(
        engine.embed([represent_query("Hello", "")], max_tokens=20, deadline=11, clock=lambda: 10)
    )
    assert result.vectors == ((0.6, 0.8),)
    assert result.observation["duration_ms"] == 0


def test_live_observation_handle_can_fence_inference_without_private_input_refs(tmp_path):
    from relaymind.assets.embeddings import represent_query

    class Slow(NativeDouble):
        def __init__(self):
            self.started, self.release = threading.Event(), threading.Event()

        def infer(self, tokens, invoked):
            invoked()
            self.started.set()
            self.release.wait(1)
            return [[3, 4]]

        def cancel(self):
            self.release.set()

    native = Slow()
    engine = make_engine(tmp_path, native)

    async def run():
        runs = []
        pending = asyncio.create_task(
            engine.embed(
                [represent_query("private", "")],
                max_tokens=20,
                deadline=time.monotonic() + 1,
                observe=runs.append,
            )
        )
        while not native.started.is_set():
            await asyncio.sleep(0.001)
        runs[0].fence("cancelled")
        obs = runs[0].snapshot()
        assert obs["input_bytes"] == 7 and obs["input_tokens"] == 9
        assert obs["cost_observations"][0]["value"]["calls"] == 1
        assert obs["outcome"] == "cancelled"
        assert "private" not in str(obs)
        with pytest.raises(api().EmbeddingFailure):
            await pending
        await engine.drain()
        assert runs[0].snapshot() == obs

    asyncio.run(run())


def test_repeated_cancellation_of_drain_retains_native_owner_until_cleanup(tmp_path):
    """RM38/RM41: shutdown cannot abandon a still-owned native inference thread."""
    import threading

    from relaymind.assets.embedding_backends import EmbeddingEngine, EmbeddingFailure
    from relaymind.assets.embeddings import represent_query
    from test_embedding_activation import configured

    class Held(NativeDouble):
        def __init__(self):
            self.started, self.release = threading.Event(), threading.Event()

        def infer(self, tokens, invoked):
            invoked()
            self.started.set()
            self.release.wait(2)
            return [[3, 4] for _ in tokens]

        def cancel(self):
            pass

    async def run():
        config = configured(tmp_path)
        native = Held()
        engine = EmbeddingEngine(config.manifest, native)
        pending = asyncio.create_task(
            engine.embed(
                [represent_query("hello", "")], max_tokens=20, deadline=time.monotonic() + 2
            )
        )
        for _ in range(100):
            if native.started.is_set():
                break
            await asyncio.sleep(0.001)
        assert native.started.is_set()
        pending.cancel()
        with pytest.raises(EmbeddingFailure):
            await pending
        drain = asyncio.create_task(engine.drain())
        await asyncio.sleep(0)
        drain.cancel()
        await asyncio.sleep(0)
        drain.cancel()
        await asyncio.sleep(0)
        try:
            assert not drain.done()
        finally:
            native.release.set()
        with pytest.raises(asyncio.CancelledError):
            await drain
        assert engine._pending is None

    asyncio.run(run())
