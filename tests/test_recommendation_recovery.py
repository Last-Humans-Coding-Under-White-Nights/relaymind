"""RM36/AC27: recovery, private retention and committed acquisition readiness."""

import asyncio
from dataclasses import replace
from datetime import timedelta

import pytest

from recommendation_helpers import (
    BODY,
    NOW,
    SCOPE,
    accepted,
    claim,
    client_for,
    empty_result,
    setup,
)
from relaymind.store import RunStore


def test_restart_records_one_interrupted_receipt_without_compute(tmp_path):
    from relaymind.store.recommendations import RecommendationStore

    path = tmp_path / "db"
    with RunStore(path) as runs:
        store = RecommendationStore(runs)
        rid = store.begin(accepted(), claim()).view["recommendation_id"]
        store.append_attempt_event(
            SCOPE,
            rid,
            0,
            dict(
                event_type="selector.started",
                payload=dict(
                    attempt=1,
                    selector={"name": "lexical", "version": "1"},
                    mechanism="lexical_overlap",
                    remaining_ms=100,
                ),
            ),
            now=NOW,
            active_ms=7,
        )
    with RunStore(path) as runs:
        store = RecommendationStore(runs)
        assert store.recover(now=NOW + timedelta(days=1))["recovered"] == 1
        assert store.recover(now=NOW + timedelta(days=1))["recovered"] == 0
        result = store.lookup_claim(claim(), now=NOW + timedelta(days=1)).response
        assert result.status == 503 and result.body["error"]["code"] == "recommendation_unavailable"
        events = store.events(SCOPE, rid, after_seq=0, limit=100, now=NOW + timedelta(days=1))[
            "events"
        ]
        assert events[-1]["payload"]["reason"] == "interrupted" and events[-1]["elapsed_ms"] == 7
        assert (
            store.begin(accepted(created_at=NOW + timedelta(days=1)), claim("next")).view["state"]
            == "computing"
        )


def test_retention_hook_failure_rolls_back_all_owned_private_rows(tmp_path):
    from relaymind.store.recommendations import RecommendationStore

    with RunStore(tmp_path / "db") as runs:
        store = RecommendationStore(runs)
        rid = store.begin(accepted(), claim()).view["recommendation_id"]
        store.finish(SCOPE, rid, 0, "ready", result=empty_result(), now=NOW)

        def fail(db, parent):
            db.execute("DELETE FROM recommendation_events WHERE recommendation_id=?", (parent,))
            raise RuntimeError("private cache failed")

        store.register_purge_hook(fail)
        with pytest.raises(RuntimeError):
            store.sweep(now=NOW + timedelta(days=30))
        assert store.lookup_claim(claim(), now=NOW).response.status == 200
        assert len(store.events(SCOPE, rid, after_seq=0, limit=100, now=NOW)["events"]) == 2


def test_first_failure_and_successful_empty_or_partial_generation(tmp_path):
    from relaymind.assets.sources import CaptureContext, capture_source

    async def go():
        app, service, runs, catalog = setup(tmp_path, populate=False)
        catalog.record_failure("example", rejected_count=1, exclusions=())
        assert catalog.snapshot().catalog_revision > 0
        await service.refresh_catalog()
        assert service.readiness()["reason"] == "no_lexical_generation"
        async with await client_for(app) as c:
            assert (await c.post("/relay/v1/recommendations", json=BODY)).status_code == 503
            source = service.provider.settings.sources[0]
            captured = capture_source(
                source,
                previous=catalog.inventory("example"),
                context=CaptureContext(service.clock.utc_now()),
            )
            catalog.publish(captured, expected_revision=catalog.revision())
            await service.refresh_catalog()
            assert service.readiness()["state"] == "empty"
            response = await c.post("/relay/v1/recommendations", json=BODY)
            assert (
                response.status_code == 200 and response.json()["result"]["primary_attempt"] is None
            )
            assert response.json()["result"]["attempts"] == []
            # Partial empty is also an actual successful committed acquisition.
            catalog.publish(replace(captured, complete=False), expected_revision=catalog.revision())
            await service.refresh_catalog()
            assert service.readiness()["state"] == "empty"
        runs.close()

    asyncio.run(go())


def test_configured_success_proof_and_stale_refresh(tmp_path):
    async def go():
        app, service, runs, catalog = setup(tmp_path)
        await service.refresh_catalog()
        assert service.readiness()["state"] == "ready"
        catalog.record_failure("example", rejected_count=1, exclusions=())
        await service.refresh_catalog()
        assert service.readiness()["state"] == "ready"
        offer = service.provider.pin(["skill"])
        assert offer.index.records[0].stale
        # Same source name with changed acquisition identity does not inherit success.
        service.provider.settings = replace(
            service.provider.settings,
            sources=(replace(service.provider.settings.sources[0], path=tmp_path / "other"),),
        )
        await service.refresh_catalog()
        assert service.readiness()["state"] == "unavailable"
        with runs.sql_transaction() as db:
            db.execute("UPDATE asset_catalog_sources SET last_success_at='invalid'")
        service.provider.settings = replace(
            service.provider.settings,
            sources=(replace(service.provider.settings.sources[0], path=tmp_path / "source"),),
        )
        await service.refresh_catalog()
        assert service.readiness()["reason"] == "corrupt_catalog"
        runs.close()

    asyncio.run(go())


def test_production_source_observer_publishes_cache_and_actual_job_events(tmp_path):
    from relaymind.assets.service import AssetCatalogService
    from relaymind.store.index_job_event import validate_job_event

    async def go():
        app, service, runs, catalog = setup(tmp_path, populate=False)
        worker = AssetCatalogService(
            service.provider.settings, catalog, after_change=service.refresh_catalog
        )
        await worker.tick()
        assert service.readiness()["state"] == "empty"
        jobs = catalog.job_outcomes()
        events = catalog.job_events(jobs[0]["job_id"])
        assert [e["event_type"] for e in events] == ["queued", "started", "published"]
        for e in events:
            validate_job_event(e)
        from test_asset_sources import skill

        skill(tmp_path / "source")
        await worker.rescan("example")
        assert service.readiness()["state"] == "ready"
        jobs = catalog.job_outcomes()
        events = catalog.job_events(jobs[0]["job_id"])
        assert [e["event_type"] for e in events] == [
            "queued",
            "started",
            "batch.finished",
            "published",
        ]
        batch = events[2]["payload"]
        assert (
            batch["accepted_count"] == 1
            and batch["received_bytes"] > 0
            and batch["embeddings"] == []
        )
        await worker.stop()
        await service.stop()
        runs.close()

    asyncio.run(go())


@pytest.mark.parametrize("observer_error", [RuntimeError, asyncio.CancelledError])
def test_completed_source_job_survives_derived_observer_failure(tmp_path, observer_error):
    """RM36/AC27: derived preparation cannot rewrite committed source evidence."""
    from relaymind.assets.service import AssetCatalogService

    async def go():
        _, service, runs, catalog = setup(tmp_path, populate=False)

        async def fail_after_change():
            raise observer_error("derived preparation failed")

        worker = AssetCatalogService(
            service.provider.settings, catalog, after_change=fail_after_change
        )
        try:
            try:
                await worker.tick()
            except (RuntimeError, asyncio.CancelledError):
                pass
            job = catalog.job_outcomes()[0]
            assert job["state"] == "succeeded"
            assert [event["event_type"] for event in catalog.job_events(job["job_id"])] == [
                "queued",
                "started",
                "published",
            ]
            with runs.sql_transaction() as db:
                source = db.execute(
                    "SELECT last_success_at, consecutive_failures FROM asset_catalog_sources "
                    "WHERE source_id='example'"
                ).fetchone()
            assert source["last_success_at"] is not None
            assert source["consecutive_failures"] == 0
            assert all(not asset.stale for asset in catalog.snapshot().assets)
        finally:
            await worker.stop()
            await service.stop()
            runs.close()

    asyncio.run(go())


def test_recovered_old_job_has_only_actual_interrupted_history(tmp_path):
    from relaymind.store.asset_catalog import AssetCatalogStore

    path = tmp_path / "db"
    with RunStore(path) as runs:
        cat = AssetCatalogStore(runs)
        job = cat.begin_job("example", "a" * 64)
        # Represent a genuine pre-event migration job without synthesizing its history.
        with runs.sql_transaction() as db:
            db.execute("DELETE FROM asset_index_job_events WHERE job_id=?", (job,))
    with RunStore(path) as runs:
        cat = AssetCatalogStore(runs)
        assert cat.recover_interrupted_jobs() == 1
        events = cat.job_events(job)
        assert [e["event_type"] for e in events] == ["failed"]
        assert events[0]["payload"]["reason"] == "interrupted"


def test_launcher_wires_recommendations_recovery_and_catalog_worker(tmp_path):
    """RM36/AC27: production app consumes the separate service and source observer."""
    from gateway_helpers import ScriptedAdapter
    from launcher_helpers import write_config
    from relaymind.assets.settings import AssetSettings, RecommendationSettings, SourceConfig
    from relaymind.launcher import build_app, load_config
    from test_asset_sources import skill

    source = tmp_path / "skills"
    skill(source)
    configured = replace(
        load_config(write_config(tmp_path)),
        recommendations=RecommendationSettings(enabled=True, user_id="u", workspace_id="w"),
        assets=AssetSettings((SourceConfig("example", "local_directory", source),)),
    )
    launched = build_app(configured, local_adapter=ScriptedAdapter([]))
    assert launched.app.state.recommendations is not None

    async def go():
        async with launched.app.router.lifespan_context(launched.app):
            await launched.app.state.asset_catalog.rescan("example")
            assert launched.app.state.recommendations.readiness()["state"] == "ready"
            async with await client_for(launched.app) as c:
                assert (await c.post("/relay/v1/recommendations", json=BODY)).status_code == 200

    asyncio.run(go())


def test_commit_index_gap_and_stale_refresh_cannot_invalidate_newer_cache(tmp_path, monkeypatch):
    """RM36/AC27: a mismatched generation is unavailable before replay; stale rebuild loses."""

    async def go():
        app, service, runs, catalog = setup(tmp_path)
        await service.refresh_catalog()
        async with await client_for(app) as c:
            first = await c.post("/relay/v1/recommendations", json=BODY)
            catalog.record_failure("example", rejected_count=1, exclusions=())
            entered, release = asyncio.Event(), asyncio.Event()
            publish = service.provider.cache.publish
            count = 0

            async def controlled(*args, **kwargs):
                nonlocal count
                count += 1
                if count == 1:
                    entered.set()
                    await release.wait()
                    from relaymind.selection.asset_contract import AttemptFailure

                    raise AttemptFailure("unavailable", "selector_unavailable")
                return await publish(*args, **kwargs)

            monkeypatch.setattr(service.provider.cache, "publish", controlled)
            old = asyncio.create_task(service.refresh_catalog())
            await entered.wait()
            assert service.readiness()["state"] == "unavailable"
            assert (await c.post("/relay/v1/recommendations", json=BODY)).status_code == 503
            newer = asyncio.create_task(service.refresh_catalog())
            await asyncio.sleep(0)
            assert not newer.done()
            release.set()
            await old
            await newer
            assert service.readiness()["state"] == "ready"
            assert (await c.post("/relay/v1/recommendations", json=BODY)).content == first.content
        await service.stop()
        runs.close()

    asyncio.run(go())


def test_real_cache_old_refresh_cannot_invalidate_new_compatible_generation(tmp_path, monkeypatch):
    """RM36/AC27: a real 25-record yield cannot let an old refresh destroy a newer cache."""
    from relaymind.selection import asset_lexical
    from test_asset_sources import capture, skill

    async def go():
        app, service, runs, catalog = setup(tmp_path, populate=False)
        source = tmp_path / "source"
        for index in range(25):
            skill(source / f"package-{index:02}", name=f"Package {index:02}")
        catalog.publish(capture(source), expected_revision=0)
        await service.refresh_catalog()
        assert service.readiness()["state"] == "ready"
        async with await client_for(app) as client:
            first = await client.post("/relay/v1/recommendations", json=BODY)
            assert first.status_code == 200

            service.provider.cache.invalidate()
            entered, release = asyncio.Event(), asyncio.Event()
            original_sleep = asyncio.sleep
            old_task = None
            paused = False

            async def pause_actual_yield(delay):
                nonlocal paused
                if delay == 0 and asyncio.current_task() is old_task and not paused:
                    paused = True
                    entered.set()
                    await release.wait()
                return await original_sleep(delay)

            monkeypatch.setattr(asset_lexical.asyncio, "sleep", pause_actual_yield)
            old_task = asyncio.create_task(service.refresh_catalog())
            await asyncio.wait_for(entered.wait(), timeout=2)
            catalog.record_failure("example", rejected_count=1, exclusions=())
            assert service.readiness()["state"] == "unavailable"
            newer = asyncio.create_task(service.refresh_catalog())
            # Before serialization the newer refresh completes while the old
            # one is paused. After serialization it waits for the old owner.
            await asyncio.wait((newer,), timeout=0.25)
            release.set()
            await asyncio.wait_for(asyncio.gather(old_task, newer), timeout=2)
            ready = service.readiness()
            assert ready["state"] == "ready" and ready["catalog_revision"] == 2
            assert len(service.provider.pin(["skill"]).catalog) == 25
            replay = await client.post("/relay/v1/recommendations", json=BODY)
            assert replay.status_code == 200 and replay.content == first.content
        await service.stop()
        runs.close()

    asyncio.run(go())


def test_configuration_mismatch_is_unavailable_without_claiming_corruption(tmp_path):
    async def go():
        _, service, runs, _ = setup(tmp_path)
        await service.refresh_catalog()
        # An additional genuinely successful source does not authorize an old
        # source's assets after its config has changed.
        from relaymind.assets.catalog import SourceCapture
        from relaymind.assets.settings import SourceConfig
        from relaymind.assets.sources import source_config_digest

        second = SourceConfig("second", "local_directory", tmp_path / "second")
        cat = service.provider.store
        capture = SourceCapture(
            "second",
            source_config_digest(second),
            "c" * 64,
            None,
            "2026-10-29T00:00:00Z",
            (),
            (),
            False,
            frozenset(),
            {},
            0,
        )
        cat.publish(capture, expected_revision=cat.revision())
        service.provider.settings = replace(service.provider.settings, sources=(second,))
        await service.refresh_catalog()
        assert service.readiness()["reason"] == "no_lexical_generation"
        assert len(cat.snapshot().assets) == 1
        runs.close()

    asyncio.run(go())
