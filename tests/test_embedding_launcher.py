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


def test_cancelled_shared_embedding_job_drains_and_keeps_actual_cost(tmp_path):
    """RM38/RM41: operator cancellation fences an in-flight native batch."""
    import threading

    from helpers import ScriptedAdapter
    from relaymind.assets.embedding_bundle import load_semantic_bundle

    config = configured(tmp_path)
    native = NativeDouble()
    native.started, native.release = threading.Event(), threading.Event()

    def infer(tokens, invoked):
        invoked()
        native.started.set()
        native.release.wait(1)
        return [[3, 4] for _ in tokens]

    native.infer = infer
    native.cancel = native.release.set
    loader = loader_for(config)
    from relaymind.assets.embedding_activation import BackendRegistration

    loader._backends["fixture"] = BackendRegistration(
        lambda: dict(config.manifest.data["runtime"]), lambda: "cpu", lambda artifact: native
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

    async def run():
        try:
            await service.start()
            pending = asyncio.create_task(worker.rebuild_embeddings())
            for _ in range(200):
                if native.started.is_set():
                    break
                await asyncio.sleep(0.001)
            assert native.started.is_set()
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            for _ in range(200):
                if service.provider.store.job_outcomes()[0]["state"] != "running":
                    break
                await asyncio.sleep(0.001)
            job = service.provider.store.job_outcomes()[0]
            assert job["state"] == "cancelled"
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
            native.release.set()
            await worker.stop()
            await service.stop()
            launched.orchestrator.store.close()

    asyncio.run(run())
