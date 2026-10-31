"""RM38/RM41 AC29/AC32: durable exact vectors, scoped recovery and atomic batches."""

import asyncio

import pytest

from relaymind.store import RunStore
from test_asset_catalog import record
from test_asset_hybrid import setup


def api():
    from relaymind.store import asset_vectors

    return asset_vectors


def test_verified_vectors_recover_only_for_exact_current_keys_and_deletions(tmp_path):
    config, index = setup(tmp_path)
    runs = RunStore(tmp_path / "db")
    store = api().AssetVectorStore(runs)
    original = record(description="original")
    store.save_batch(index, [original], [(1.0, 0.0)])
    recovered = store.restore(config, index.scope, [original])
    assert recovered.compatible(original) == (1.0, 0.0)
    changed = record(description="changed metadata same original hash")
    assert store.restore(config, index.scope, [changed]).compatible(changed) is None
    assert (
        store.restore(config, {"user_id": "different", "workspace_id": "w"}, [original]).compatible(
            original
        )
        is None
    )
    store.prune(config, index.scope, [])
    assert store.restore(config, index.scope, [original]).compatible(original) is None
    runs.close()


def test_cancelled_storage_batch_rolls_back_all_vector_rows(tmp_path):
    config, index = setup(tmp_path)
    runs = RunStore(tmp_path / "db")
    store = api().AssetVectorStore(runs)
    records = [record(asset_id="a"), record(asset_id="b")]

    def check():
        with runs.sql_read() as db:
            if db.execute("SELECT count(*) FROM asset_vectors").fetchone()[0]:
                raise RuntimeError("cancelled after first staged SQL write")

    with pytest.raises(RuntimeError):
        store.save_batch(index, records, [(1.0, 0.0), (0.0, 1.0)], check=check)
    recovered = store.restore(config, index.scope, records)
    assert recovered.compatible(records[0]) is None and recovered.compatible(records[1]) is None
    runs.close()


def test_corrupt_numeric_vector_cannot_become_a_recovered_index(tmp_path):
    from relaymind.assets.embeddings import EmbeddingError

    config, index = setup(tmp_path)
    runs = RunStore(tmp_path / "db")
    store = api().AssetVectorStore(runs)
    original = record()
    store.save_batch(index, [original], [(1.0, 0.0)])
    with runs.sql_transaction() as db:
        db.execute("UPDATE asset_vectors SET vector_json='[0,0]'")
    with pytest.raises(EmbeddingError):
        store.restore(config, index.scope, [original])
    runs.close()


def test_background_owner_batches_public_vectors_and_keeps_inference_out_of_queries(tmp_path):
    async def scenario():
        """RM38/RM41 AC29/AC32: rebuild batches <=25 on the shared owner."""
        from types import SimpleNamespace

        from relaymind.assets.embedding_backends import EmbeddingEngine
        from relaymind.assets.embedding_lifecycle import EmbeddingIndexLifecycle
        from relaymind.assets.sources import CaptureContext
        from relaymind.selection.asset_hybrid import HybridAssetSelector
        from test_embedding_backend import NativeDouble

        config, index = setup(tmp_path)
        records = [record(asset_id=f"a{i}") for i in range(28)]
        offer = SimpleNamespace(catalog=records, catalog_revision=1)
        provider = SimpleNamespace(pin=lambda kinds: offer)
        runs = RunStore(tmp_path / "db")
        vectors = api().AssetVectorStore(runs)
        selector = HybridAssetSelector(config, index, None)
        loader = SimpleNamespace(
            load=lambda config, **kwargs: EmbeddingEngine(config.manifest, NativeDouble())
        )
        owner = EmbeddingIndexLifecycle(provider, selector, vectors, loader)
        context = CaptureContext(runs.clock.utc_now())

        async def cpu(function, *args, **kwargs):
            return function(*args, **kwargs)

        result = await owner.rebuild(context=context, run_cpu=cpu)
        assert result.revision == 1 and len(index.pin()[0]) == 28
        batches = context.batch_observations
        assert [b["accepted_count"] for b in batches] == [25, 3]
        assert all(len(b["embeddings"]) == 1 for b in batches)
        assert selector.observations == ()
        assert selector.engine is not None
        # A restarted owner restores exact vectors without inference calls.
        selector2 = HybridAssetSelector(config, setup(tmp_path)[1], None)
        owner2 = EmbeddingIndexLifecycle(provider, selector2, vectors, loader)
        context2 = CaptureContext(runs.clock.utc_now())
        await owner2.rebuild(context=context2, run_cpu=cpu)
        assert len(selector2.index.pin()[0]) == 28 and context2.batch_observations == []
        records.clear()
        await owner2.rebuild(context=context2, run_cpu=cpu)
        assert selector2.index.revision is None
        assert vectors.restore(config, index.scope, []).revision is None
        await owner.stop()
        await owner2.stop()
        runs.close()

    asyncio.run(scenario())


def test_cached_vectors_without_loaded_engine_are_honestly_not_ready(tmp_path):
    async def scenario():
        import time

        from relaymind.selection.asset_contract import SelectorContext, parse_asset_request
        from relaymind.selection.asset_hybrid import HybridAssetSelector
        from test_asset_selection import request

        config, index = setup(tmp_path)
        original = record()
        index.publish_batch([original], [(1.0, 0.0)])
        result = await HybridAssetSelector(config, index, None).select(
            parse_asset_request(request(catalog=[original])),
            SelectorContext(time.monotonic, lambda: None),
        )
        assert result["outcome"] == "not_ready" and result["candidates"] == []

    asyncio.run(scenario())


def test_embedding_jobs_share_worker_and_persist_batch_observations(tmp_path):
    """RM38/RM41 AC29/AC32: derived semantic work owns one real job chain."""
    from relaymind.assets.embedding_backends import EmbeddingEngine
    from relaymind.assets.embeddings import represent_query
    from relaymind.assets.service import AssetCatalogService
    from relaymind.assets.settings import AssetSettings
    from relaymind.store.asset_catalog import AssetCatalogStore, Publication
    from test_embedding_backend import NativeDouble

    async def scenario():
        config, _ = setup(tmp_path)
        runs = RunStore(tmp_path / "db")
        catalog = AssetCatalogStore(runs)
        engine = EmbeddingEngine(config.manifest, NativeDouble())
        calls = []

        class Owner:
            revision = "hybrid:fixture"

            def generation_config(self):
                return config.digest

            async def rebuild(self, *, context, run_cpu):
                calls.append("embedding")
                batch = await engine.embed(
                    [represent_query("asset", "")],
                    max_tokens=20,
                    deadline=context.deadline,
                    clock=context.monotonic,
                    work_control=context,
                )
                from relaymind.store.run_store import utc_iso

                context.batch_observations.append(
                    dict(
                        timestamp=utc_iso(context.utc_now()),
                        accepted_count=1,
                        rejected_count=0,
                        received_bytes=0,
                        duration_ms=1,
                        embeddings=[batch.observation],
                    )
                )
                return Publication(catalog.revision(), False, (), ())

            async def stop(self):
                await engine.drain()

        worker = AssetCatalogService(AssetSettings(), catalog, embedding_lifecycle=Owner())
        try:
            await worker.rebuild_embeddings()
            assert calls == ["embedding"]
            job = catalog.job_outcomes()[0]
            assert job["state"] == "succeeded"
            events = catalog.job_events(job["job_id"])
            assert events[0]["payload"]["job_kind"] == "embedding"
            batch = next(e for e in events if e["event_type"] == "batch.finished")
            assert batch["payload"]["embeddings"][0]["cost_observations"][0]["value"]["calls"] == 1
            assert events[-1]["payload"]["index_revision"] == "hybrid:fixture"
        finally:
            await worker.stop()
            runs.close()

    asyncio.run(scenario())


def test_changed_catalog_during_native_batch_cannot_publish_stale_vectors(tmp_path):
    """RM38/RM41: commit guards fence an obsolete generation without hiding its cost."""
    from types import SimpleNamespace

    from relaymind.assets.embedding_backends import EmbeddingEngine
    from relaymind.assets.embedding_lifecycle import EmbeddingIndexLifecycle
    from relaymind.assets.sources import CaptureContext, SourceFailure
    from relaymind.selection.asset_hybrid import HybridAssetSelector
    from test_embedding_backend import NativeDouble

    config, index = setup(tmp_path)
    records = [record()]
    offer = SimpleNamespace(catalog=records, catalog_revision=1)

    class Changed(NativeDouble):
        def infer(self, tokens, invoked):
            values = super().infer(tokens, invoked)
            offer.catalog_revision = 2
            records[0] = record(description="new metadata")
            return values

    async def scenario():
        runs = RunStore(tmp_path / "db")
        vectors = api().AssetVectorStore(runs)
        selector = HybridAssetSelector(config, index, EmbeddingEngine(config.manifest, Changed()))
        owner = EmbeddingIndexLifecycle(
            SimpleNamespace(pin=lambda _: offer), selector, vectors, None
        )
        context = CaptureContext(runs.clock.utc_now())

        async def cpu(function, *args, **kwargs):
            return function(*args, **kwargs)

        with pytest.raises(SourceFailure, match="index_error"):
            await owner.rebuild(context=context, run_cpu=cpu)
        assert index.revision is None
        assert vectors.restore(config, index.scope, [record()]).revision is None
        assert context.batch_observations[0]["accepted_count"] == 0
        assert context.batch_observations[0]["embeddings"][0]["outcome"] == "succeeded"
        await owner.stop()
        runs.close()

    asyncio.run(scenario())
