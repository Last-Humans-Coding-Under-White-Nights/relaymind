"""RM34/RM38, AC25/AC29: observer ownership and committed lexical publication."""

import asyncio

import pytest

from recommendation_helpers import setup
from relaymind.assets.service import AssetCatalogService
from relaymind.recommendations.errors import RecommendationError
from relaymind.store.asset_catalog import CatalogConflict


@pytest.mark.parametrize("phase", ["queued", "active"])
def test_external_cancellation_preserves_observer_owned_rebuild(tmp_path, monkeypatch, phase):
    """RM38/AC29: a departing external waiter cannot discard required catalog repair."""

    async def go():
        _, service, runs, catalog = setup(tmp_path)
        await service.refresh_catalog()
        busy = True
        observed = asyncio.Event()
        entered, release = asyncio.Event(), asyncio.Event()
        prepare = service.provider.cache.prepare

        async def held_prepare(*args, **kwargs):
            entered.set()
            await release.wait()
            return await prepare(*args, **kwargs)

        async def after_change():
            await service.refresh_catalog()
            observed.set()

        worker = AssetCatalogService(
            service.provider.settings,
            catalog,
            busy=lambda: busy,
            after_change=after_change,
            index_lifecycle=service.provider.lifecycle,
        )
        worker._next["example"] = float("inf")
        try:
            catalog.record_failure("example", rejected_count=0, exclusions=())
            await worker._notify_changed()
            caller = asyncio.create_task(worker.rebuild())
            await asyncio.sleep(0)
            if phase == "active":
                monkeypatch.setattr(service.provider.cache, "prepare", held_prepare)
                busy = False
                worker._wakeup.set()
                await asyncio.wait_for(entered.wait(), 1)
            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await caller
            busy = False
            release.set()
            # Drain any active tick and run queued work without a timing poll.
            await worker.tick()
            assert service.readiness()["state"] == "ready"
            assert observed.is_set()
            assert catalog.snapshot().sources[0]["stale"]
            jobs = catalog.job_outcomes()
            assert len(jobs) == 1
            assert jobs[0]["state"] == "succeeded"
        finally:
            release.set()
            await worker.stop()
            assert not runs._foreground_windows
            await service.stop()
            runs.close()

    asyncio.run(go())


@pytest.mark.parametrize("observer_error", [RuntimeError, asyncio.CancelledError])
def test_committed_rebuild_result_survives_observer_error(tmp_path, observer_error):
    """RM34/RM38, AC25/AC29: notification cannot misreport a committed publication."""

    async def go():
        _, service, runs, catalog = setup(tmp_path)
        await service.refresh_catalog()
        busy = True

        async def after_change():
            raise observer_error("fixture observer failure")

        worker = AssetCatalogService(
            service.provider.settings,
            catalog,
            busy=lambda: busy,
            after_change=after_change,
            index_lifecycle=service.provider.lifecycle,
        )
        worker._next["example"] = float("inf")
        try:
            await worker._notify_changed()
            caller = asyncio.create_task(worker.rebuild())
            await asyncio.sleep(0)
            busy = False
            worker._wakeup.set()
            result = await asyncio.wait_for(caller, 1)
            assert result.revision == catalog.revision()
            assert service.readiness()["state"] == "ready"
            jobs = catalog.job_outcomes()
            assert len(jobs) == 1
            assert jobs[0]["state"] == "succeeded"
            events = catalog.job_events(jobs[0]["job_id"])
            terminal = [
                event["event_type"]
                for event in events
                if event["event_type"] in {"published", "failed", "cancelled"}
            ]
            assert terminal == ["published"]
        finally:
            await worker.stop()
            assert not runs._foreground_windows
            await service.stop()
            runs.close()

    asyncio.run(go())


@pytest.mark.parametrize("serving", [True, False])
def test_failed_manifest_commit_never_installs_prepared_offer(tmp_path, monkeypatch, serving):
    """RM34/RM38, AC25/AC29: readers only see committed compatible generations."""

    async def go():
        _, service, runs, catalog = setup(tmp_path)
        await service.refresh_catalog()
        provider = service.provider
        manifest = catalog.lexical_generation()
        previous = provider.pin(["skill"]) if serving else None
        if not serving:
            provider.cache.invalidate()
        visible_at_commit = []

        def failed_commit(**kwargs):
            assert kwargs["expected_revision"] == manifest["catalog_revision"]
            assert not kwargs.get("advance", False)
            try:
                visible_at_commit.append(provider.pin(["skill"]))
            except RecommendationError:
                visible_at_commit.append(None)
            raise CatalogConflict("fixture commit conflict")

        monkeypatch.setattr(catalog, "commit_lexical_generation", failed_commit)
        try:
            assert not await provider._refresh_owned(force=True)
            assert len(visible_at_commit) == 1
            assert visible_at_commit[0] is previous
            assert catalog.lexical_generation() == manifest
            if serving:
                assert provider.pin(["skill"]) is previous
            else:
                assert provider.readiness()["state"] == "unavailable"
        finally:
            await service.stop()
            runs.close()

    asyncio.run(go())
