"""RM03/RM38/RM40/RM41 AC29/AC31/AC32: one real prepared native owner."""

import asyncio
from dataclasses import replace

from relaymind.canonical import canonical_digest
from relaymind.store import RunStore
from test_asset_selection_benchmark import fixture_config
from test_embedding_activation import configured
from test_embedding_launcher import loader_for


def native_fixture(tmp_path):
    from relaymind.assets.embedding_bundle import load_semantic_bundle
    from relaymind.evaluation.asset_evidence import effective_asset_config

    config = fixture_config(tmp_path, repetitions=1, warmups=0)
    root = tmp_path / "semantic"
    root.mkdir()
    semantic = configured(root)
    bundle = load_semantic_bundle(
        {"manifest": "manifest.json", "config": "hybrid.json"},
        {},
        config_dir=root,
        loader=loader_for(semantic),
    )
    production = replace(
        config.production,
        recommendations=replace(config.production.recommendations, primary="hybrid"),
        asset_semantics=bundle,
    )
    effective = effective_asset_config(
        production,
        config.release,
        runner_digest=canonical_digest(config.inventories["runner_inventory"]),
        protocol_digest=canonical_digest(config.inventories["protocol_inventory"]),
    )
    return production, config.release, effective


def test_shared_production_factory_uses_supplied_catalog_owner_without_context_free_selector(
    tmp_path,
):
    from relaymind.recommendations.service import CatalogProvider, RecommendationService
    from relaymind.selection.asset_hybrid import HybridAssetSelector
    from relaymind.store.asset_catalog import AssetCatalogStore

    production, release, effective = native_fixture(tmp_path)
    runs = RunStore(tmp_path / "factory.sqlite")
    provider = CatalogProvider(
        AssetCatalogStore(runs),
        production.assets,
        scope={key: effective["recommendations"][key] for key in ("user_id", "workspace_id")},
        permissions=effective["permissions"],
    )
    try:
        service = RecommendationService.for_provider(
            runs, provider, production.recommendations, semantics=production.asset_semantics
        )
        assert service.provider is provider and isinstance(service.primary, HybridAssetSelector)
        assert service.primary.engine is None
        assert service.primary.index.scope == provider.scope
        assert service.embedding_lifecycle.hybrid_owner.provider is provider
    finally:
        runs.close()


def test_frozen_runtime_bootstrap_preserves_store_offer_and_uses_only_one_derived_worker(tmp_path):
    from relaymind.evaluation.asset_native_runtime import NativeBenchmarkRuntime
    from relaymind.selection.asset_hybrid import HybridAssetSelector

    async def scenario():
        production, release, effective = native_fixture(tmp_path)
        runtime = await NativeBenchmarkRuntime.create(production, release, effective)
        try:
            assert isinstance(runtime.service.primary, HybridAssetSelector)
            assert runtime.service.primary.engine is None
            snapshot = runtime.catalog.snapshot()
            assert snapshot.catalog_revision == 1
            assert [record.to_json_object() for record in snapshot.assets] == [
                asset["record"] for asset in release.catalog["assets"]
            ]
            assert runtime.catalog.job_outcomes() == ()
            assert all(
                row["last_success_at"] is None
                for row in runtime.catalog.source_provenance().values()
            )
            assert runtime.worker.settings.sources == ()
            await runtime.worker.rebuild()
            task = runtime.worker._task
            await runtime.worker.rebuild_embeddings()
            assert runtime.worker._task is task
            offer = runtime.provider.pin(("skill", "mcp_tool"))
            assert offer.catalog_revision == runtime.catalog.revision()
            assert all(
                runtime.service.primary.index.compatible(record) is not None
                for record in offer.catalog
            )
            assert runtime.provider.readiness()["semantic_coverage"] == {"ready": 1, "total": 1}
            jobs = runtime.catalog.job_outcomes()
            kinds = [
                runtime.catalog.job_events(job["job_id"])[0]["payload"]["job_kind"] for job in jobs
            ]
            assert sorted(kinds) == ["embedding", "lexical"]
            assert all(job["source_id"] is None and job["state"] == "succeeded" for job in jobs)
        finally:
            await runtime.close()

    asyncio.run(scenario())


def test_locked_bootstrap_requires_fresh_store_and_rolls_back_cancelled_install(tmp_path):
    import pytest

    from relaymind.assets.catalog import project_asset, validate_asset_record
    from relaymind.assets.sources import SourceFailure, source_config_digest
    from relaymind.redaction import Redactor
    from relaymind.store.asset_catalog import AssetCatalogStore, CatalogConflict

    production, release, effective = native_fixture(tmp_path)
    runs = RunStore(tmp_path / "bootstrap.sqlite")
    store = AssetCatalogStore(runs)
    documents = tuple(
        project_asset(
            validate_asset_record(asset["record"]), body=None, redactor=Redactor(), secrets=()
        )
        for asset in release.catalog["assets"]
    )
    digests = {
        source.source_id: source_config_digest(source) for source in production.assets.sources
    }

    def cancel_after_insert():
        with runs.sql_read() as db:
            if db.execute("SELECT count(*) FROM asset_catalog_assets").fetchone()[0]:
                raise SourceFailure("cancelled", rejected_count=0)

    try:
        with pytest.raises(SourceFailure, match="cancelled"):
            store.initialize_evaluation_offer(
                documents, config_digests=digests, check=cancel_after_insert
            )
        assert store.snapshot().assets == () and store.revision() == 0
        assert store.source_provenance() == {} and store.job_outcomes() == ()
        store.initialize_evaluation_offer(documents, config_digests=digests)
        before = store.snapshot().record_bytes
        with pytest.raises(CatalogConflict, match="evaluation_store_not_fresh"):
            store.initialize_evaluation_offer((), config_digests=digests)
        assert store.snapshot().record_bytes == before
    finally:
        runs.close()


def test_locked_bootstrap_refuses_unbound_or_malformed_source_configuration(tmp_path):
    import pytest

    from relaymind.assets.catalog import AssetValidationError, project_asset, validate_asset_record
    from relaymind.redaction import Redactor
    from relaymind.store.asset_catalog import AssetCatalogStore

    production, release, effective = native_fixture(tmp_path)
    runs = RunStore(tmp_path / "invalid-bootstrap.sqlite")
    store = AssetCatalogStore(runs)
    documents = tuple(
        project_asset(
            validate_asset_record(asset["record"]), body=None, redactor=Redactor(), secrets=()
        )
        for asset in release.catalog["assets"]
    )
    try:
        for digest in (None, "unbound", "f" * 65):
            with pytest.raises(AssetValidationError):
                store.initialize_evaluation_offer(documents, config_digests={"fixture": digest})
            assert store.snapshot().assets == () and store.revision() == 0
        with pytest.raises(AssetValidationError):
            store.initialize_evaluation_offer(
                documents + documents, config_digests={"fixture": "f" * 64}
            )
    finally:
        runs.close()
