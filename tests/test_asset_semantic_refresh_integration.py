"""RM03/RM38/RM40/RM41/RM43: reviewed semantic/source owners coexist exactly."""

import asyncio
import copy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from helpers import FakeClock


@pytest.mark.parametrize("protocol", [False, True])
def test_integrated_compiled_inventory_keeps_exact_membership_and_reviewed_anchors(protocol):
    from relaymind.evaluation.asset_corpus import AssetCorpusError
    from relaymind.evaluation.asset_evidence import file_inventory, verify_compiled_asset_inventory

    root = Path(__file__).resolve().parents[1]
    kind = "protocol_inventory" if protocol else "runner_inventory"
    inventory = file_inventory(root, protocol=protocol)
    verify_compiled_asset_inventory(kind, inventory)
    missing = copy.deepcopy(inventory)
    missing["files"].pop()
    with pytest.raises(AssetCorpusError, match="inventory_membership"):
        verify_compiled_asset_inventory(kind, missing)
    forged = copy.deepcopy(inventory)
    member = next(item for item in forged["files"] if item["path"].startswith("src/"))
    member["sha256"] = "0" * 64
    with pytest.raises(AssetCorpusError, match="unsupported_generator"):
        verify_compiled_asset_inventory(kind, forged)


def test_remote_and_semantic_policy_both_bind_the_actual_source_job(tmp_path, monkeypatch):
    from test_asset_refresh import worker
    from test_asset_remote_github import generation

    async def scenario():
        clock = FakeClock()
        docs, files = generation()
        runs, catalog, provider, service = worker(
            tmp_path, monkeypatch, clock, docs=docs, files=files
        )
        service.effective_config_digest = "a" * 64
        source = service.settings.sources[0]
        expected = service._job_config(service._source_job_config(source))
        try:
            await service.tick()
            assert catalog.job_outcomes()[0]["config_digest"] == expected
            service.effective_config_digest = "b" * 64
            assert service._job_config(service._source_job_config(source)) != expected
            semantic_changed = service._job_config(service._source_job_config(source))
            service.settings = replace(
                service.settings, refresh=replace(service.settings.refresh, remote_seconds=3600)
            )
            assert service._job_config(service._source_job_config(source)) != semantic_changed
        finally:
            await service.stop()
            runs.close()

    asyncio.run(scenario())


def test_source_cooldown_keeps_same_worker_semantic_rebuilds_and_deletion_current(
    tmp_path, monkeypatch
):
    from relaymind.assets.embedding_backends import EmbeddingEngine
    from relaymind.assets.embedding_lifecycle import EmbeddingIndexLifecycle
    from relaymind.selection.asset_hybrid import HybridAssetSelector
    from relaymind.store.asset_vectors import AssetVectorStore
    from test_asset_hybrid import setup
    from test_asset_refresh import worker
    from test_asset_remote_github import generation
    from test_embedding_backend import NativeDouble

    async def scenario():
        clock = FakeClock()
        docs, files = generation()
        runs, catalog, provider, service = worker(
            tmp_path, monkeypatch, clock, docs=docs, files=files
        )
        config, index = setup(tmp_path)
        selector = HybridAssetSelector(config, index, None)
        vectors = AssetVectorStore(runs)
        loader = SimpleNamespace(
            load=lambda config, **kwargs: EmbeddingEngine(config.manifest, NativeDouble())
        )
        owner = EmbeddingIndexLifecycle(provider, selector, vectors, loader)
        try:
            await service.tick()
            first = tuple(provider.pin(("skill", "mcp_tool")).catalog)
            service.embedding_lifecycle = owner
            assert service._hard_delay("public") == 300
            await asyncio.wait_for(service.rebuild_embeddings(), 2)
            task = service._task
            assert all(index.compatible(record) is not None for record in first)
            docs2, files2 = generation("2", ("a", "c"))
            docs.clear()
            docs.update(docs2)
            files.clear()
            files.update(files2)
            clock.advance(300)
            await service.tick()
            await asyncio.wait_for(service.rebuild_embeddings(), 2)
            current = tuple(provider.pin(("skill", "mcp_tool")).catalog)
            assert service._task is task
            assert {record["identity"]["package_id"] for record in current} == {"a", "c"}
            assert all(index.compatible(record) is not None for record in current)
            assert all(index.compatible(record) is None for record in first)
            assert catalog.inventory("public").tombstones
            kinds = [
                catalog.job_events(job["job_id"])[0]["payload"]["job_kind"]
                for job in catalog.job_outcomes()
            ]
            assert "refresh" in kinds and "embedding" in kinds
        finally:
            await service.stop()
            runs.close()

    asyncio.run(scenario())
