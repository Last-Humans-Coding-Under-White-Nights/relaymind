"""RM38/RM41/RM42 AC29/AC32/AC33: qualification before deferred activation."""

import asyncio
from dataclasses import replace

import pytest

from relaymind import launcher
from test_asset_readiness import configured_catalog
from test_embedding_activation import configured
from test_embedding_backend import NativeDouble


def loader_for(config):
    from relaymind.assets.embedding_activation import BackendRegistration, EmbeddingLoader
    from relaymind.assets.embedding_qualification import qualification_key

    entry = BackendRegistration(
        lambda: dict(config.manifest.data["runtime"]),
        lambda: "cpu",
        lambda artifact: NativeDouble(),
    )
    return EmbeddingLoader(
        qualifications={qualification_key(config.manifest, config.digest)},
        backends={"fixture": entry},
    )


def test_explicit_qualified_semantics_parse_but_heavy_load_waits_for_worker(tmp_path):
    from helpers import ScriptedAdapter
    from relaymind.assets.embedding_bundle import load_semantic_bundle

    config = configured(tmp_path)
    bundle = load_semantic_bundle(
        dict(manifest="manifest.json", config="hybrid.json"),
        {},
        config_dir=tmp_path,
        loader=loader_for(config),
    )
    base = configured_catalog(tmp_path, warmup=False, detect=False)
    base = replace(
        base,
        recommendations=replace(base.recommendations, primary="hybrid"),
        asset_semantics=bundle,
    )
    app = launcher.build_app(base, local_adapter=ScriptedAdapter())
    service = app.app.state.recommendations
    worker = app.app.state.asset_catalog
    assert service.primary.engine is None

    async def run():
        try:
            await service.start()
            await worker.rebuild_embeddings()
            assert service.primary.engine is not None
            assert service.provider.readiness()["semantic_coverage"] == dict(ready=1, total=1)
            events = service.provider.store.job_outcomes()
            assert (
                service.provider.store.job_events(events[0]["job_id"])[0]["payload"]["job_kind"]
                == "embedding"
            )
        finally:
            await worker.stop()
            await service.stop()
            app.orchestrator.store.close()

    asyncio.run(run())


def test_supplied_disabled_semantics_still_refuse_unqualified_files(tmp_path):
    from relaymind.assets.embedding_bundle import load_semantic_bundle
    from relaymind.assets.embeddings import EmbeddingError

    configured(tmp_path)
    # Registry authority is required even for an unused supplied hybrid config.
    with pytest.raises(EmbeddingError, match="unqualified_artifact"):
        load_semantic_bundle(
            dict(manifest="manifest.json", config="hybrid.json"), {}, config_dir=tmp_path
        )


def test_effective_semantic_identity_covers_launch_settings_and_all_job_kinds(tmp_path):
    """RM38/RM41/RM42: one redacted effective identity, no second wire field."""
    from helpers import ScriptedAdapter
    from relaymind.assets.embedding_bundle import load_semantic_bundle

    config = configured(tmp_path)
    bundle = load_semantic_bundle(
        dict(manifest="manifest.json", config="hybrid.json"),
        {},
        config_dir=tmp_path,
        loader=loader_for(config),
    )
    base = configured_catalog(tmp_path, warmup=False, detect=False)
    base = replace(
        base,
        recommendations=replace(base.recommendations, primary="hybrid"),
        asset_semantics=bundle,
    )
    one = launcher.build_app(base, local_adapter=ScriptedAdapter())
    # Active launch settings outside selector-specific configuration still bind evidence.
    two = launcher.build_app(replace(base, port=base.port + 1), local_adapter=ScriptedAdapter())
    try:
        a, b = one.app.state.recommendations, two.app.state.recommendations
        assert a._config_digest != b._config_digest
        assert one.app.state.asset_catalog.effective_config_digest == a._config_digest
        assert a.embedding_lifecycle.generation_config() == a._config_digest
    finally:
        one.orchestrator.store.close()
        two.orchestrator.store.close()


def _semantic_worker(tmp_path, load):
    from helpers import ScriptedAdapter
    from relaymind.assets.embedding_activation import BackendRegistration
    from relaymind.assets.embedding_bundle import load_semantic_bundle

    config = configured(tmp_path)
    loader = loader_for(config)
    loader._backends["fixture"] = BackendRegistration(
        lambda: dict(config.manifest.data["runtime"]), lambda: "cpu", load
    )
    bundle = load_semantic_bundle(
        dict(manifest="manifest.json", config="hybrid.json"), {}, config_dir=tmp_path, loader=loader
    )
    base = configured_catalog(tmp_path, warmup=False, detect=False)
    base = replace(
        base,
        recommendations=replace(base.recommendations, primary="hybrid"),
        asset_semantics=bundle,
    )
    launched = launcher.build_app(base, local_adapter=ScriptedAdapter())
    service, worker = launched.app.state.recommendations, launched.app.state.asset_catalog
    worker._next = dict.fromkeys(worker._sources, float("inf"))
    return launched, service, worker


def _job_diagnostic(service):
    store = service.provider.store
    jobs = store.job_outcomes()
    if not jobs:
        return "job=absent"
    job = jobs[0]
    events = [event["event_type"] for event in store.job_events(job["job_id"])]
    return f"job={job['state']}/{job['reason']}; events={events}"


async def _wait_for_native_or_job(started, pending, service, *, watchdog=5):
    waiter = asyncio.create_task(started.wait())
    try:
        finished, _ = await asyncio.wait(
            {waiter, pending}, timeout=watchdog, return_when=asyncio.FIRST_COMPLETED
        )
        if pending in finished:
            try:
                outcome = pending.result()
            except BaseException as failure:
                detail = f"{type(failure).__name__}:{failure}"
            else:
                detail = f"returned:{type(outcome).__name__}"
            raise AssertionError(
                f"embedding_job_finished_before_native_start: {detail}; {_job_diagnostic(service)}"
            )
        if waiter in finished:
            return
        raise AssertionError(f"embedding_native_start_watchdog: {_job_diagnostic(service)}")
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)


async def _wait_for_embedding_job_terminal(service, *, watchdog=5):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + watchdog
    while True:
        jobs = service.provider.store.job_outcomes()
        if jobs and jobs[0]["state"] != "running":
            return jobs[0]
        if loop.time() >= deadline:
            raise AssertionError(f"embedding_job_drain_watchdog: {_job_diagnostic(service)}")
        await asyncio.sleep(0.01)


async def _close_semantic_worker(launched, service, worker, pending=None, release=None):
    if release is not None:
        release.set()
    if pending is not None:
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
    try:
        await worker.stop()
    finally:
        try:
            await service.stop()
        finally:
            launched.orchestrator.store.close()


def _exercise_cancelled_shared_embedding_job(tmp_path, *, load_delay=0, drain_delay=0):
    import threading
    import time

    native = NativeDouble()
    native.started, native.release = threading.Event(), threading.Event()
    native.infer_watchdog_expired = False
    native.cancel = native.release.set

    def load(_artifact):
        time.sleep(load_delay)
        return native

    launched, service, worker = _semantic_worker(tmp_path, load)

    async def run():
        pending = None
        loop = asyncio.get_running_loop()
        ready = asyncio.Event()

        def infer(tokens, invoked):
            invoked()
            native.started.set()
            loop.call_soon_threadsafe(ready.set)
            if not native.release.wait(5):
                native.infer_watchdog_expired = True
                raise AssertionError("native_infer_watchdog")
            time.sleep(drain_delay)
            return [[3, 4] for _ in tokens]

        native.infer = infer
        try:
            await service.start()
            pending = asyncio.create_task(worker.rebuild_embeddings())
            await _wait_for_native_or_job(ready, pending, service)
            assert native.started.is_set()
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            job = await _wait_for_embedding_job_terminal(service)
            assert job["state"] == "cancelled"
            assert native.release.is_set() and not native.infer_watchdog_expired
            assert service.primary.index.revision is None
            assert service.primary.engine._pending is None
            events = service.provider.store.job_events(job["job_id"])
            embedding = [
                e
                for e in events
                if e["event_type"] == "batch.finished" and e["payload"]["embeddings"]
            ][0]["payload"]["embeddings"][0]
            assert embedding["outcome"] == "cancelled"
            assert embedding["cost_observations"][0]["value"]["calls"] == 1
            assert not any(e["event_type"] == "published" for e in events)
        finally:
            await _close_semantic_worker(launched, service, worker, pending, native.release)

    asyncio.run(run())


def test_cancelled_shared_embedding_job_drains_and_keeps_actual_cost(tmp_path):
    """RM38/RM41: operator cancellation fences an in-flight native batch."""
    _exercise_cancelled_shared_embedding_job(tmp_path)


@pytest.mark.parametrize(("load_delay", "drain_delay"), [(0.35, 0), (0, 0.35)])
def test_delayed_embedding_start_and_drain_keep_cancellation_evidence(
    tmp_path, load_delay, drain_delay
):
    """RM38/RM41: scheduling and drain duration do not change ownership."""
    _exercise_cancelled_shared_embedding_job(
        tmp_path, load_delay=load_delay, drain_delay=drain_delay
    )


@pytest.mark.parametrize("mode", ["failed", "blocked"])
def test_embedding_startup_diagnostic_and_cleanup(tmp_path, mode):
    """RM38/RM41: an early failed job or stuck loader is reported and drained."""
    import threading

    release = threading.Event()
    entered = threading.Event()
    loop = None
    entered_async = None

    def load(_artifact):
        entered.set()
        loop.call_soon_threadsafe(entered_async.set)
        if mode == "failed":
            raise RuntimeError("fixture_loader_failed")
        assert release.wait(5), "fixture_loader_watchdog"
        return NativeDouble()

    launched, service, worker = _semantic_worker(tmp_path, load)

    async def run():
        nonlocal loop, entered_async
        pending = None
        loop = asyncio.get_running_loop()
        entered_async = asyncio.Event()
        try:
            await service.start()
            pending = asyncio.create_task(worker.rebuild_embeddings())
            if mode == "blocked":
                await asyncio.wait_for(entered_async.wait(), 5)
            with pytest.raises(
                AssertionError,
                match="embedding_job_finished_before_native_start.*SourceFailure"
                if mode == "failed"
                else "embedding_native_start_watchdog.*running",
            ):
                await _wait_for_native_or_job(
                    asyncio.Event(), pending, service, watchdog=5 if mode == "failed" else 0.1
                )
            assert entered.is_set()
        finally:
            await _close_semantic_worker(launched, service, worker, pending, release)

    asyncio.run(run())
