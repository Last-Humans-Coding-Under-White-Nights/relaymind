"""RM34/AC25: explicit configuration and one bounded cancellable catalog worker."""

import asyncio
import threading
from datetime import UTC, datetime, timedelta

import pytest

from launcher_helpers import write_config
from relaymind.assets.sources import SourceFailure
from relaymind.launcher import LaunchError, build_app, load_config
from relaymind.store import RunStore
from test_asset_sources import imported, skill


def service_api():
    from relaymind.assets import service
    from relaymind.assets.settings import parse_asset_settings, parse_recommendation_settings
    from relaymind.store.asset_catalog import AssetCatalogStore

    return service, parse_asset_settings, parse_recommendation_settings, AssetCatalogStore


def settings(path, *, kind="local_directory", refresh=None):
    _, parse, _, _ = service_api()
    return parse(
        {
            "sources": [
                {
                    "source_id": "example",
                    "kind": kind,
                    "path": str(path),
                    "adapter_contract": None,
                    "secret_ref": None,
                }
            ]
        },
        refresh or {},
        config_dir=path.parent,
    )


class Clock:
    def __init__(self):
        self.seconds = 0.0

    def monotonic(self):
        return self.seconds

    def utc_now(self):
        return datetime(2026, 10, 29, tzinfo=UTC) + timedelta(seconds=self.seconds)


def test_config_paths_are_config_relative_and_settings_closed(tmp_path, monkeypatch):
    _, parse, rec, _ = service_api()
    monkeypatch.chdir(tmp_path.parent)
    value = {
        "sources": [
            {
                "source_id": "example",
                "kind": "local_directory",
                "path": "skills",
                "adapter_contract": None,
                "secret_ref": None,
            }
        ]
    }
    parsed = parse(value, {}, config_dir=tmp_path)
    assert parsed.sources[0].path == tmp_path / "skills"
    assert parsed.refresh.local_scan_seconds == 30
    assert parsed.refresh.remote_seconds == 3600
    assert parsed.refresh.backoff_initial_seconds == 60
    assert parsed.refresh.backoff_max_seconds == 3600
    with pytest.raises(ValueError):
        parse({"sources": value["sources"] * 2}, {}, config_dir=tmp_path)
    for key, val in (("local_scan_seconds", True), ("remote_seconds", 0), ("bogus", 1)):
        with pytest.raises(ValueError):
            parse(value, {key: val}, config_dir=tmp_path)
    with pytest.raises(ValueError):
        parse(
            value, {"backoff_initial_seconds": 100, "backoff_max_seconds": 60}, config_dir=tmp_path
        )
    for key, val in (("secret_ref", "env:SECRET"), ("extra", "x"), ("kind", "qualified_remote")):
        source = value["sources"][0] | {key: val}
        with pytest.raises(ValueError):
            parse({"sources": [source]}, {}, config_dir=tmp_path)
    assert not rec({}, source_count=0).enabled
    with pytest.raises(ValueError):
        rec({"enabled": True}, source_count=1)
    with pytest.raises(ValueError):
        rec({"enabled": True, "user_id": "u", "workspace_id": "w"}, source_count=0)
    for override in (
        {"total_ms": True},
        {"primary_ms": 501},
        {"primary": "hybrid"},
        {"primary": "http", "name": "x", "version": "1", "url": "https://127.0.0.1"},
        {"primary": "http", "name": "x", "version": "1", "url": "http://u:p@localhost"},
        {"unexpected": 1},
    ):
        with pytest.raises(ValueError):
            rec(override, source_count=1)
    good = rec(
        {
            "primary": "http",
            "name": "model_router",
            "version": "1",
            "url": "http://localhost:9000/select",
        },
        source_count=1,
    )
    assert good.primary == "http"


def test_launcher_disabled_no_jobs_and_enabled_background_lifespan(tmp_path):
    # This is actual launcher wiring, with the existing deterministic model double.
    from helpers import ScriptedAdapter

    path = write_config(tmp_path)
    launched = build_app(load_config(path), local_adapter=ScriptedAdapter(turns=[]))
    assert getattr(launched.app.state, "asset_catalog", None) is None
    launched.orchestrator.store.close()
    skill(tmp_path / "ordinary")
    path.write_text(
        path.read_text() + '\n[recommendations]\nenabled=true\nuser_id="u"\nworkspace_id="w"\n'
        '\n[[assets.sources]]\nsource_id="example"\nkind="local_directory"\npath="ordinary"\n'
    )
    launched = build_app(load_config(path), local_adapter=ScriptedAdapter(turns=[]))
    service = launched.app.state.asset_catalog
    assert service.store.snapshot().catalog_revision == 0

    async def run():
        async with launched.app.router.lifespan_context(launched.app):
            result = await asyncio.wait_for(service.rescan("example"), 2)
            assert result.revision == 1
            assert service.store.snapshot().assets[0].availability == "unknown"
            assert launched.orchestrator.store.active_run_id() is None
        assert not service.running

    asyncio.run(run())
    launched.orchestrator.store.close()


def test_static_launcher_rejects_unqualified_artifacts_and_keeps_model_requirement(tmp_path):
    path = write_config(tmp_path)
    for extra in (
        '\n[asset_adaptation]\nenabled=true\nconfig="x"\n',
        '\n[asset_hybrid]\nmanifest="x"\nconfig="x"\n',
        "\n[assets]\nunknown=[]\n",
    ):
        original = path.read_text()
        path.write_text(original + extra)
        with pytest.raises(LaunchError):
            load_config(path)
        path.write_text(original)
    path.write_text("[recommendations]\nenabled=false\n")
    with pytest.raises(LaunchError, match="configure at least one model"):
        load_config(path)


def test_timed_scan_busy_pause_explicit_rescan_and_no_execution_slot(tmp_path):
    s, _, _, facade = service_api()
    skill(tmp_path / "source")
    clock, busy = Clock(), [True]
    with RunStore(clock=clock) as run_store:
        catalog = facade(run_store)
        service = s.AssetCatalogService(
            settings(tmp_path / "source"), catalog, clock=clock, busy=lambda: busy[0]
        )

        async def run():
            await service.tick()
            assert catalog.snapshot().catalog_revision == 0
            busy[0] = False
            await service.tick()
            assert catalog.snapshot().catalog_revision == 1
            skill(tmp_path / "source", description="Updated")
            clock.seconds = 29
            await service.tick()
            assert catalog.snapshot().catalog_revision == 1
            clock.seconds = 30
            await service.tick()
            assert catalog.snapshot().catalog_revision == 2
            assert run_store.active_run_id() is None
            result = await service.rescan("example")
            assert not result.changed
            await service.stop()
            assert all(j["duration_ms"] >= 0 for j in catalog.job_outcomes())

        asyncio.run(run())


def test_shutdown_drains_capture_and_never_commits_after_cancel(tmp_path, monkeypatch):
    s, _, _, facade = service_api()
    skill(tmp_path / "source")
    entered, release = threading.Event(), threading.Event()
    real = s.capture_source

    def blocked(source, **kwargs):
        entered.set()
        release.wait(timeout=3)
        return real(source, **kwargs)

    monkeypatch.setattr(s, "capture_source", blocked)
    with RunStore() as run_store:
        catalog = facade(run_store)
        service = s.AssetCatalogService(settings(tmp_path / "source"), catalog)

        async def run():
            pending = asyncio.create_task(service.rescan("example"))
            assert await asyncio.to_thread(entered.wait, 1)
            stopping = asyncio.create_task(service.stop())
            await asyncio.sleep(0)
            release.set()
            await stopping
            with pytest.raises(SourceFailure) as caught:
                await pending
            assert str(caught.value) == "cancelled"
            assert catalog.snapshot().catalog_revision == 0
            assert catalog.job_outcomes()[0]["state"] == "cancelled"

        asyncio.run(run())


def test_failure_backoff_and_import_not_periodically_polled(tmp_path):
    s, _, _, facade = service_api()
    manifest, _ = imported(tmp_path / "import")
    clock = Clock()
    with RunStore(clock=clock) as run_store:
        catalog = facade(run_store)
        service = s.AssetCatalogService(
            settings(manifest, kind="metadata_import"), catalog, clock=clock
        )

        async def run():
            await service.tick()
            assert catalog.snapshot().catalog_revision == 1
            manifest.write_text("bad")
            clock.seconds = 3600
            await service.tick()
            assert len(catalog.job_outcomes()) == 1
            with pytest.raises(SourceFailure):
                await service.rescan("example")
            assert len(catalog.job_outcomes()) == 2
            clock.seconds += 59
            await service.tick()
            assert len(catalog.job_outcomes()) == 2
            clock.seconds += 1
            await service.tick()
            assert len(catalog.job_outcomes()) == 3
            clock.seconds += 119
            await service.tick()
            assert len(catalog.job_outcomes()) == 3
            await service.stop()

        asyncio.run(run())


def test_cancelling_direct_tick_drains_thread_before_return(tmp_path, monkeypatch):
    s, _, _, facade = service_api()
    skill(tmp_path / "source")
    entered, release = threading.Event(), threading.Event()
    real = s.capture_source

    def blocked(source, **kwargs):
        entered.set()
        release.wait(timeout=3)
        return real(source, **kwargs)

    monkeypatch.setattr(s, "capture_source", blocked)
    with RunStore() as run_store:
        catalog = facade(run_store)
        service = s.AssetCatalogService(settings(tmp_path / "source"), catalog)

        async def run():
            task = asyncio.create_task(service.tick())
            assert await asyncio.to_thread(entered.wait, 1)
            task.cancel()
            await asyncio.sleep(0.01)
            assert not task.done()
            release.set()
            await task
            await service.stop()
            assert catalog.snapshot().catalog_revision == 0
            assert catalog.job_outcomes()[0]["state"] == "cancelled"

        asyncio.run(run())


def test_interrupted_recovery_observes_initial_backoff(tmp_path):
    s, _, _, facade = service_api()
    skill(tmp_path / "source")
    clock = Clock()
    with RunStore(clock=clock) as run_store:
        catalog = facade(run_store)
        catalog.begin_job("example", "0" * 64)
        service = s.AssetCatalogService(settings(tmp_path / "source"), catalog, clock=clock)

        async def run():
            await service.tick()
            assert catalog.snapshot().catalog_revision == 0
            clock.seconds = 60
            await service.tick()
            assert catalog.snapshot().catalog_revision == 1
            await service.stop()

        asyncio.run(run())


def test_inflight_overlap_and_batch_duration_are_observed(tmp_path, monkeypatch):
    s, _, _, facade = service_api()
    skill(tmp_path / "source")
    clock, busy = Clock(), [False]
    actual = s.capture_source

    def overlap(source, **kwargs):
        busy[0] = True
        clock.seconds = 0.02
        kwargs["context"].check()
        clock.seconds = 0.04
        return actual(source, **kwargs)

    monkeypatch.setattr(s, "capture_source", overlap)
    with RunStore(clock=clock) as run_store:
        catalog = facade(run_store)
        service = s.AssetCatalogService(
            settings(tmp_path / "source"), catalog, clock=clock, busy=lambda: busy[0]
        )

        async def run():
            await service.tick()
            outcome = catalog.job_outcomes()[0]
            assert outcome["overlap_observed"]
            assert outcome["drain_ms"] == pytest.approx(20)
            assert outcome["batch_ms"] == [40]
            assert outcome["duration_ms"] == 40
            await service.stop()

        asyncio.run(run())


@pytest.mark.parametrize("phase", ["preparation", "transaction"])
def test_one_job_deadline_covers_preparation_and_rollback_before_commit(
    tmp_path, monkeypatch, phase
):
    """RM34/AC25: publication never gets a fresh 60-second budget."""
    import relaymind.store.asset_catalog as storage

    s, _, _, facade = service_api()
    skill(tmp_path / "source")
    clock = Clock()
    real_capture, real_project = s.capture_source, storage.project_asset

    def near_deadline(*args, **kwargs):
        got = real_capture(*args, **kwargs)
        clock.seconds = 59.9
        return got

    def expensive_projection(*args, **kwargs):
        clock.seconds += 1
        return real_project(*args, **kwargs)

    monkeypatch.setattr(s, "capture_source", near_deadline)
    if phase == "preparation":
        monkeypatch.setattr(storage, "project_asset", expensive_projection)
    with RunStore(clock=clock) as store:
        catalog = facade(store)
        if phase == "transaction":
            with store.sql_transaction() as connection:
                connection.create_function(
                    "advance_job_clock", 0, lambda: setattr(clock, "seconds", 60.9)
                )
                connection.execute(
                    "CREATE TRIGGER slow_publish AFTER INSERT ON asset_catalog_assets "
                    "BEGIN SELECT advance_job_clock(); END"
                )
        service = s.AssetCatalogService(settings(tmp_path / "source"), catalog, clock=clock)

        async def run():
            await service.tick()
            await service.stop()
            outcome = catalog.job_outcomes()[0]
            assert outcome["duration_ms"] == pytest.approx(60900)
            assert outcome["state"] == "failed"
            assert outcome["reason"] == "deadline"
            assert catalog.snapshot().catalog_revision == 0
            assert catalog.snapshot().assets == ()

        asyncio.run(run())


def test_interrupted_started_retry_uses_new_failure_backoff(tmp_path):
    """RM34/AC25: recovery cannot reuse the elapsed schedule of the retry that crashed."""
    s, _, _, facade = service_api()
    skill(tmp_path / "source")
    clock = Clock()
    with RunStore(clock=clock) as store:
        catalog = facade(store)
        catalog.record_failure("example", SourceFailure("source_unavailable"))
        catalog.schedule_retry("example", "2026-10-29T00:01:00Z")
        clock.seconds = 60
        catalog.begin_job("example", "0" * 64)
        clock.seconds = 61
        service = s.AssetCatalogService(settings(tmp_path / "source"), catalog, clock=clock)

        async def run():
            await service.tick()
            assert catalog.snapshot().catalog_revision == 0
            clock.seconds = 180
            await service.tick()
            assert catalog.snapshot().catalog_revision == 0
            clock.seconds = 181
            await service.tick()
            assert catalog.snapshot().catalog_revision == 1
            await service.stop()

        asyncio.run(run())
