"""RM03/RM17/RM34/RM38/RM43 AC25/AC29/AC34: the same worker owns periodic refresh."""

import asyncio
from datetime import timedelta

import pytest

from helpers import FakeClock
from relaymind.assets.service import AssetCatalogService
from relaymind.assets.settings import parse_asset_settings
from relaymind.recommendations.service import CatalogProvider
from relaymind.store import RunStore
from relaymind.store.asset_catalog import AssetCatalogStore
from relaymind.store.run_store import utc_iso
from test_asset_remote_contracts import PROFILE, source
from test_asset_remote_github import generation, http


def worker(
    tmp_path,
    monkeypatch,
    clock,
    *,
    docs=None,
    files=None,
    source_id="public",
    local=None,
    remote_seconds=300,
):
    from relaymind.assets import remote_transport

    original = remote_transport.RemoteTransport
    # Constructor seam supplies deterministic HTTP; installed URLs/profile remain real and closed.
    if docs is not None:
        monkeypatch.setattr(
            remote_transport,
            "RemoteTransport",
            lambda profile, secret_ref=None: original(
                profile, secret_ref=secret_ref, http_transport=http(docs, files)
            ),
        )
    config = parse_asset_settings(
        {
            "sources": [source(source_id=source_id)]
            + (
                []
                if local is None
                else [dict(source_id="local", kind="local_directory", path=str(local))]
            )
        },
        {"remote_seconds": remote_seconds},
        config_dir=tmp_path,
    )
    runs = RunStore(tmp_path / "db", clock=clock)
    catalog = AssetCatalogStore(runs)
    provider = CatalogProvider(
        catalog, config, scope={"user_id": "u", "workspace_id": "w"}, permissions=()
    )
    service = AssetCatalogService(
        config,
        catalog,
        clock=clock,
        after_change=provider.refresh,
        index_lifecycle=provider.lifecycle,
    )
    return runs, catalog, provider, service


def test_real_capture_worker_schedules_periodic_change_and_retains_atomic_generation(
    tmp_path, monkeypatch
):
    async def run():
        clock = FakeClock()
        docs, files = generation()
        runs, catalog, provider, service = worker(
            tmp_path, monkeypatch, clock, docs=docs, files=files
        )
        try:
            await service.tick()
            first = provider.pin(("skill", "mcp_tool"))
            assert len(first.catalog) == 2
            assert service._delay() == 300
            docs2, files2 = generation("2", ("a", "c"))
            docs.clear()
            docs.update(docs2)
            files.clear()
            files.update(files2)
            clock.advance(299)
            assert service._due(clock.monotonic()) is None
            clock.advance(1)
            await service.tick()
            second = provider.pin(("skill", "mcp_tool"))
            assert second.catalog_revision > first.catalog_revision
            assert {r["identity"]["package_id"] for r in second.catalog} == {"a", "c"}
            assert catalog.inventory("public").tombstones
            revision = catalog.revision()
            clock.advance(300)
            await service.tick()
            assert catalog.revision() == revision
            assert service._delay() == 300
        finally:
            await service.stop()
            runs.close()

    asyncio.run(run())


def test_profile_cooldown_survives_restart_rename_and_explicit_rescan(tmp_path, monkeypatch):
    async def run():
        clock = FakeClock()
        docs, files = generation()
        runs, catalog, provider, service = worker(
            tmp_path, monkeypatch, clock, docs=docs, files=files
        )
        await service.tick()
        await service.stop()
        runs.close()
        # This is still the same installed source profile, despite its different local ID.

        monkeypatch.undo()
        runs2, catalog2, provider2, service2 = worker(
            tmp_path, monkeypatch, clock, source_id="renamed"
        )
        assert service2._due(clock.monotonic()) is None
        pending = asyncio.create_task(service2.rescan("renamed"))
        await asyncio.sleep(0.02)
        assert not pending.done() and len(catalog2.job_outcomes()) == 1
        assert service2._delay() == 300
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        await service2.stop()
        runs2.close()

    asyncio.run(run())


def test_provider_cooldown_larger_than_backoff_cap_and_reservation_are_durable(tmp_path):
    from relaymind.assets.sources import SourceFailure
    from relaymind.store.asset_remote_state import RemoteSourceStateStore

    clock = FakeClock()
    with RunStore(tmp_path / "db", clock=clock) as runs:
        state = RemoteSourceStateStore(runs)
        later = clock.utc_now() + timedelta(seconds=7200)
        state.defer_until(PROFILE, utc_iso(later))
        state.defer_until(PROFILE, utc_iso(clock.utc_now() + timedelta(seconds=300)))
        assert state.not_before(PROFILE) == utc_iso(later)
        with pytest.raises(SourceFailure):
            state.reserve_poll(PROFILE, clock.utc_now(), 300)
    with RunStore(tmp_path / "db", clock=clock) as runs:
        state = RemoteSourceStateStore(runs)
        assert state.not_before(PROFILE) == utc_iso(later)
        clock.advance(7200)
        state.reserve_poll(PROFILE, clock.utc_now(), 300)
        with pytest.raises(SourceFailure):
            state.reserve_poll(PROFILE, clock.utc_now(), 300)


def test_cooldown_pending_source_does_not_starve_local_work(tmp_path, monkeypatch):
    async def run():
        clock = FakeClock()
        from test_asset_sources import skill

        local = tmp_path / "local"
        skill(local)
        runs, catalog, provider, service = worker(tmp_path, monkeypatch, clock, local=local)
        service.remote_state.defer_until(
            PROFILE, utc_iso(clock.utc_now() + timedelta(seconds=7200))
        )
        future = service._queue("public")
        try:
            assert service._due(clock.monotonic()) == "local"
            await service.tick()
            assert not future.done()
            assert len(catalog.snapshot().assets) == 1
            assert service._delay() > 0
        finally:
            await service.stop()
            runs.close()

    asyncio.run(run())


def test_actual_http_fixture_worker_failure_stale_repair_and_recovery(tmp_path, monkeypatch):
    """RM16/RM17/RM38/RM43: real local socket I/O, atomic generations, honest outage."""
    from relaymind.assets import remote_transport
    from remote_source_fixture import ForwardToFixture, reply_for, server

    docs, files = generation()
    broken = False

    def reply(path):
        if broken and path.endswith("/skills/c/SKILL.md"):
            return 500, {}, [b"fixture outage"]
        return reply_for(docs, files, path)

    with server(reply) as port:
        original = remote_transport.RemoteTransport
        monkeypatch.setattr(
            remote_transport,
            "RemoteTransport",
            lambda profile, secret_ref=None: original(
                profile, secret_ref=secret_ref, http_transport=ForwardToFixture(port)
            ),
        )

        async def run():
            nonlocal broken
            clock = FakeClock()
            runs, catalog, provider, service = worker(tmp_path, monkeypatch, clock)
            try:
                await service.tick()
                assert len(provider.pin(("skill", "mcp_tool")).catalog) == 2
                old = {r.asset_id: r.content_hash for r in catalog.snapshot().assets}
                docs2, files2 = generation("2", ("a", "c"))
                docs.clear()
                docs.update(docs2)
                files.clear()
                files.update(files2)
                broken = True
                clock.advance(300)
                await service.tick()
                await service.rebuild()
                assert {r.asset_id: r.content_hash for r in catalog.snapshot().assets} == old
                assert all(r.stale for r in catalog.snapshot().assets)
                assert catalog.snapshot().sources[0]["last_success_at"] is not None
                failed = [
                    j
                    for j in catalog.job_outcomes()
                    if j["source_id"] == "public" and j["state"] == "failed"
                ]
                assert len(failed) == 1 and failed[0]["reason"] == "source_unavailable"
                assert provider.readiness()["lexical_ready"]
                broken = False
                clock.advance(300)
                await service.tick()
                assert {r.identity["package_id"] for r in catalog.snapshot().assets} == {"a", "c"}
                assert not any(r.stale for r in catalog.snapshot().assets)
                assert service._failures["public"] == 0 and service._delay() == 300
            finally:
                await service.stop()
                runs.close()

        asyncio.run(run())


def test_crash_after_atomic_commit_recovers_content_and_cooldown_without_refetch(
    tmp_path, monkeypatch
):
    """RM17/RM38/RM43: interrupted job repair preserves an already committed generation."""

    class Crash(BaseException):
        pass

    async def run():
        clock = FakeClock()
        docs, files = generation()
        runs, catalog, provider, service = worker(
            tmp_path, monkeypatch, clock, docs=docs, files=files
        )
        old_finish = service._finish

        def crash(*args, **kwargs):
            raise Crash()

        service._finish = crash
        with pytest.raises(Crash):
            await service.tick()
        assert (
            len(catalog.snapshot().assets) == 2 and catalog.job_outcomes()[0]["state"] == "running"
        )
        service._finish = old_finish
        await service.stop()
        runs.close()
        monkeypatch.undo()
        runs2, catalog2, provider2, service2 = worker(tmp_path, monkeypatch, clock)
        try:
            assert catalog2.job_outcomes()[0]["state"] == "interrupted"
            assert {r.source_version for r in catalog2.snapshot().assets} == {"1" * 40}
            await provider2.refresh()
            assert len(provider2.pin(("skill", "mcp_tool")).catalog) == 2
            assert service2._due(clock.monotonic()) is None
            assert service2._hard_delay("public") == 300
        finally:
            await service2.stop()
            runs2.close()

    asyncio.run(run())


def test_foreground_during_real_network_wait_pauses_cpu_and_retains_no_false_drain(
    tmp_path, monkeypatch
):
    """RM38 AC29: response waiting is quiescent, then CPU waits for the real foreground slot."""
    import threading

    from relaymind.assets import remote_transport
    from relaymind.assets import service as service_module
    from remote_source_fixture import ForwardToFixture, reply_for, server

    docs, files = generation()
    entered = threading.Event()
    release = threading.Event()
    requests = []
    contexts = []

    def reply(path):
        requests.append(path)
        if path.endswith("/git/ref/heads/master"):
            entered.set()
            assert release.wait(2)
        status, headers, chunks = reply_for(docs, files, path)
        if path.endswith("/git/ref/heads/master"):
            headers["X-Poll-Interval"] = "300"
        return status, headers, chunks

    with server(reply) as port:
        original_transport = remote_transport.RemoteTransport
        monkeypatch.setattr(
            remote_transport,
            "RemoteTransport",
            lambda profile, secret_ref=None: original_transport(
                profile, secret_ref=secret_ref, http_transport=ForwardToFixture(port)
            ),
        )
        original_capture = service_module.capture_source

        def capture(*args, context, **kwargs):
            contexts.append(context)
            return original_capture(*args, context=context, **kwargs)

        monkeypatch.setattr(service_module, "capture_source", capture)

        async def run():
            clock = FakeClock()
            runs, catalog, provider, service = worker(tmp_path, monkeypatch, clock)
            service.busy = lambda: runs.active_run_id() is not None
            service.busy_since = runs.foreground_busy_since
            deferred_busy = []
            defer = service.remote_state.defer_until

            def observe_defer(*args):
                deferred_busy.append(service.busy())
                return defer(*args)

            service.remote_state.defer_until = observe_defer
            task = asyncio.create_task(service.tick())
            active = None
            try:
                assert await asyncio.to_thread(entered.wait, 2)
                active = runs.create_run("accepted")
                runs.set_state(
                    active.run_id,
                    expected_version=1,
                    from_state="accepted",
                    to_state="routing",
                    holds_slot=True,
                )
                clock.advance(0.2)
                release.set()
                await asyncio.sleep(0.05)
                assert len(requests) == 1 and not task.done()
                runs.set_state(
                    active.run_id, expected_version=2, from_state="routing", to_state="completed"
                )
                service.scheduler.wake()
                await task
                ctx = contexts[0]
                assert sum(ctx.native_io_ms) >= 199
                assert ctx.drain_ms is None
                assert not any(deferred_busy)
                assert all(batch["duration_ms"] < 50 for batch in ctx.work_batches)
                assert len(catalog.snapshot().assets) == 2
            finally:
                release.set()
                await service.stop()
                runs.close()

        asyncio.run(run())


def test_stop_drains_owned_real_http_and_never_commits_partial_generation(tmp_path, monkeypatch):
    """RM17/RM38/RM43: source shutdown closes owned HTTP before returning/store close."""
    import threading

    from relaymind.assets import remote_transport
    from relaymind.assets.sources import SourceFailure
    from remote_source_fixture import ForwardToFixture, reply_for, server

    docs, files = generation()
    entered = threading.Event()
    release = threading.Event()
    hold = False

    def reply(path):
        if hold and path.endswith("/skills/c/SKILL.md"):
            raw = files["skills/c/SKILL.md"]

            def chunks():
                yield raw[:5]
                entered.set()
                release.wait(5)
                yield raw[5:]

            return 200, {}, chunks()
        return reply_for(docs, files, path)

    with server(reply) as port:
        original = remote_transport.RemoteTransport
        monkeypatch.setattr(
            remote_transport,
            "RemoteTransport",
            lambda profile, secret_ref=None: original(
                profile, secret_ref=secret_ref, http_transport=ForwardToFixture(port)
            ),
        )

        async def run():
            nonlocal hold
            clock = FakeClock()
            runs, catalog, provider, service = worker(tmp_path, monkeypatch, clock)
            pending = None
            try:
                await service.tick()
                old = {r.asset_id: r.content_hash for r in catalog.snapshot().assets}
                docs2, files2 = generation("2", ("a", "c"))
                docs.clear()
                docs.update(docs2)
                files.clear()
                files.update(files2)
                hold = True
                clock.advance(300)
                pending = asyncio.create_task(service.rescan("public"))
                assert await asyncio.to_thread(entered.wait, 5)
                await service.stop()
                with pytest.raises(SourceFailure, match="cancelled"):
                    await pending
                assert not service.running
                assert {r.asset_id: r.content_hash for r in catalog.snapshot().assets} == old
                job = next(
                    j
                    for j in catalog.job_outcomes()
                    if j["source_id"] == "public" and j["state"] == "cancelled"
                )
                assert not any(
                    e["event_type"] == "published" for e in catalog.job_events(job["job_id"])
                )
                assert service._hard_delay("public") == 300
            finally:
                release.set()
                await service.stop()
                if pending is not None:
                    await asyncio.gather(pending, return_exceptions=True)
                runs.close()

        asyncio.run(run())


def test_queued_job_digest_commits_effective_refresh_cadence_and_profile(tmp_path, monkeypatch):
    """RM16/RM43: a 300-second demo cannot report the same effective config as3600."""

    async def run():
        clock = FakeClock()
        digests = []
        for cadence, name in ((300, "short"), (3600, "default")):
            root = tmp_path / name
            root.mkdir()
            docs, files = generation()
            runs, catalog, provider, service = worker(
                root, monkeypatch, clock, docs=docs, files=files, remote_seconds=cadence
            )
            try:
                await service.tick()
                job = catalog.job_outcomes()[0]
                digests.append(catalog.job_events(job["job_id"])[0]["payload"]["config_digest"])
            finally:
                await service.stop()
                runs.close()
                monkeypatch.undo()
        assert digests[0] != digests[1]

    asyncio.run(run())


def test_restart_uses_current_cadence_instead_of_old_success_schedule(tmp_path, monkeypatch):
    """RM38/RM43: persisted success cadence is not an immutable provider cooldown."""

    async def run():
        clock = FakeClock()
        docs, files = generation()
        runs, catalog, provider, service = worker(
            tmp_path, monkeypatch, clock, docs=docs, files=files, remote_seconds=3600
        )
        await service.tick()
        await service.stop()
        runs.close()
        monkeypatch.undo()
        runs2, catalog2, provider2, service2 = worker(
            tmp_path, monkeypatch, clock, remote_seconds=300
        )
        try:
            assert service2._delay() == 300
            clock.advance(300)
            assert service2._due(clock.monotonic()) == "public"
        finally:
            await service2.stop()
            runs2.close()

    asyncio.run(run())
