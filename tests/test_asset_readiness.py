"""RM38/AC29: lexical serving precedes warm-up; inference keeps its own fence."""

import asyncio
from dataclasses import replace

import httpx
import pytest

from helpers import ScriptedAdapter
from launcher_helpers import write_config
from recommendation_helpers import BODY, build_app
from relaymind import launcher
from relaymind.adapters import Completed, GenerationFailed, TextDelta
from relaymind.adapters.chat_completions import ChatCompletionsConfig
from relaymind.api.server import build_server
from relaymind.assets.settings import AssetSettings, RecommendationSettings, SourceConfig
from relaymind.orchestrator.local_status import LocalModelClass
from relaymind.orchestrator.route_decision import ExecutionMode
from relaymind.store import RunStore
from relaymind.store.asset_catalog import AssetCatalogStore
from test_asset_sources import capture, skill
from test_chat_disconnect_cancel import until


class WarmupBarrier(ScriptedAdapter):
    def __init__(self, *, fail=False):
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = False
        self.fail = fail

    async def generate(self, request):
        self.requests.append(request)
        self.entered.set()
        await self.release.wait()
        if self.fail:
            yield GenerationFailed(code="unavailable", message="fixture unavailable")
        else:
            yield TextDelta(text="fixture warm-up")
            yield Completed(finish_reason="stop")

    async def aclose(self):
        self.closed = True


class SlowClose(WarmupBarrier):
    def __init__(self):
        super().__init__()
        self.closing = asyncio.Event()
        self.close_release = asyncio.Event()

    async def aclose(self):
        self.closing.set()
        await self.close_release.wait()
        self.closed = True


def configured_catalog(tmp_path, *, warmup=True, detect=True):
    source = tmp_path / "ordinary"
    skill(source)
    config = replace(
        launcher.load_config(write_config(tmp_path)),
        recommendations=RecommendationSettings(enabled=True, user_id="u", workspace_id="w"),
        assets=AssetSettings((SourceConfig("example", "local_directory", source),)),
        warmup=warmup,
        detect=detect,
    )
    with RunStore(config.store_path) as runs:
        AssetCatalogStore(runs).publish(capture(source), expected_revision=0)
    return config


def test_loopback_lexical_answer_while_warmup_is_blocked(tmp_path):
    """RM38/AC29: a real listener answers with the full committed lexical offer."""

    async def go():
        adapter = WarmupBarrier()
        config = configured_catalog(tmp_path)
        launched = build_app(config, local_adapter=adapter)
        owner = launched.app.state.startup_warmup
        assert not adapter.requests
        server = build_server(launched.app, port=0, on_started=owner.start)
        serving = asyncio.create_task(server.serve())
        try:
            await asyncio.wait_for(adapter.entered.wait(), 3)
            port = server.servers[0].sockets[0].getsockname()[1]
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}",
                headers={
                    "Authorization": "Bearer " + launched.app.state.application_token,
                    "Idempotency-Key": "startup-lexical",
                },
            ) as client:
                readiness = (await client.get("/relay/v1/recommendations/readiness")).json()
                assert readiness["state"] == "ready" and readiness["catalog_size"] == 1
                result = await client.post("/relay/v1/recommendations", json=BODY)
                assert result.status_code == 200, result.text
                assert result.json()["displayed_candidates"]
                assert len(adapter.requests) == 1
                native = await client.get("/readyz")
                assert native.status_code == 503
                assert not launched.orchestrator.local_status.usable
                assert launched.orchestrator.mode_refusal(ExecutionMode.LOCAL_ONLY)
                assert launched.orchestrator.store.active_run_id() is None
                assert launched.orchestrator.store.probe_reservation() is None
                assert not launched.app.state.asset_catalog.store.job_outcomes()
            adapter.release.set()
            await until(lambda: owner.task.done())
            assert launched.orchestrator.local_status.usable
            assert len(adapter.requests) == 2
        finally:
            adapter.release.set()
            server.should_exit = True
            await serving
            launched.orchestrator.store.close()

    asyncio.run(go())


@pytest.mark.parametrize("fail", [False, True], ids=["cancelled", "runtime-failure"])
def test_serving_warmup_owns_adapter_and_shutdown_fence(tmp_path, fail):
    """RM38/AC29: owned warm-up closes before store shutdown and never assumes ready."""

    async def go():
        adapter = WarmupBarrier(fail=fail)
        config = configured_catalog(tmp_path)
        launched = build_app(config, local_adapter=ScriptedAdapter())
        owner = launched.app.state.startup_warmup
        owner.launched = replace(launched, new_local_adapter=lambda: adapter)
        async with launched.app.router.lifespan_context(launched.app):
            owner.start()
            await asyncio.wait_for(adapter.entered.wait(), 3)
            launched.orchestrator.observe_local_health(True, True, "fixture healthy")
            assert not launched.orchestrator.local_status.usable
            if fail:
                adapter.release.set()
                await until(lambda: owner.task.done())
        assert owner.task.done() and adapter.closed
        assert launched.orchestrator.local_status.status == LocalModelClass.UNAVAILABLE
        assert launched.app.state.recommendations._sweeper is None
        launched.orchestrator.store.close()

    asyncio.run(go())


def test_enabled_main_defers_warmup_to_started_hook(tmp_path, monkeypatch):
    """RM38/AC29: enabled main never performs synchronous pre-listener inference."""
    config = configured_catalog(tmp_path)
    launched = build_app(config, local_adapter=ScriptedAdapter())
    monkeypatch.setattr(launcher, "load_config", lambda path: config)
    monkeypatch.setattr(launcher, "build_app", lambda *args, **kwargs: launched)
    calls = []
    monkeypatch.setattr(launcher, "warm_up_gateway", lambda *args: calls.append("sync") or launched)
    seen = []

    def serve(app, *, host, port, on_started):
        assert not calls
        seen.append(on_started)

    try:
        assert (
            launcher.main(
                ["serve", "--config", "fixture"], serve_fn=serve, stdout=lambda line: None
            )
            == 0
        )
        assert len(seen) == 1
    finally:
        launched.orchestrator.store.close()


def test_enabled_startup_without_warmup_or_detection_adds_no_fence(tmp_path):
    """RM38/AC29: explicitly disabled startup work cannot invent a model check."""
    config = configured_catalog(tmp_path, warmup=False, detect=False)
    adapter = ScriptedAdapter()
    launched = build_app(config, local_adapter=adapter)
    try:
        assert launched.app.state.startup_warmup is None
        assert launched.orchestrator.local_status.usable
        assert adapter.requests == []
    finally:
        launched.orchestrator.store.close()


def test_pending_startup_pauses_idle_probe_but_external_requests_still_run(tmp_path):
    """RM38/AC29: pending local work fences probes, while external inference stays independent."""
    from native_helpers import session, settled, start_run
    from relaymind.orchestrator.placement import PlacementSettings

    async def go():
        adapter = WarmupBarrier()
        external = ScriptedAdapter(
            events=[TextDelta(text="external fixture answer"), Completed(finish_reason="stop")],
            locality="external",
        )
        config = replace(
            configured_catalog(tmp_path),
            external=ChatCompletionsConfig(
                base_url="https://example.invalid/v1", model="external-fixture", context_limit=16000
            ),
            placement=PlacementSettings(policy="device_aware", probe_after_seconds=0.01),
        )
        # RM38/AC29: this test deliberately waits for real idle-probe time.
        launched = launcher.build_app(config, local_adapter=adapter, external_adapter=external)
        owner = launched.app.state.startup_warmup
        async with launched.app.router.lifespan_context(launched.app):
            probe = launched.app.state.idle_probe
            assert probe._scheduler is not None
            await probe.tick()
            assert not adapter.requests
            assert launched.orchestrator.store.probe_reservation() is None
            owner.start()
            await asyncio.wait_for(adapter.entered.wait(), 3)
            async with session(launched.app) as client:
                response = await start_run(
                    client, "Explain the supplied text.", execution_mode="external_only"
                )
                assert response.status_code == 202, response.text
                view = await settled(client, response.json()["run_id"])
                assert view["state"] == "completed", view
                await client.get("/readyz")
                assert not launched.orchestrator.local_status.usable
            assert len(adapter.requests) == 1
            assert external.requests
            adapter.release.set()
            await until(lambda: owner.task.done())
            await asyncio.sleep(0.02)
            await probe.tick()
            await until(lambda: len(adapter.requests) >= 3)
        launched.orchestrator.store.close()

    asyncio.run(go())


def test_repeated_shutdown_cancellation_still_drains_owned_adapter_close(tmp_path):
    """RM38/AC29: store ownership outlives even repeatedly cancelled warm-up cleanup."""

    async def go():
        adapter = SlowClose()
        launched = build_app(configured_catalog(tmp_path), local_adapter=ScriptedAdapter())
        owner = launched.app.state.startup_warmup
        owner.launched = replace(launched, new_local_adapter=lambda: adapter)
        owner.start()
        await adapter.entered.wait()
        stop = asyncio.create_task(owner.stop())
        await adapter.closing.wait()
        stop.cancel()
        await asyncio.sleep(0)
        stop.cancel()
        await asyncio.sleep(0)
        try:
            assert not stop.done()
            assert not owner.task.done()
        finally:
            adapter.close_release.set()
            await asyncio.gather(stop, return_exceptions=True)
            await asyncio.gather(owner.task, return_exceptions=True)
            launched.orchestrator.store.close()
        assert adapter.closed

    asyncio.run(go())


def test_concurrent_stop_callers_share_one_owned_adapter_drain(tmp_path):
    """RM38/AC29 I1: a second stop must never recancel the ongoing adapter close."""

    async def go():
        adapter = SlowClose()
        launched = build_app(configured_catalog(tmp_path), local_adapter=ScriptedAdapter())
        owner = launched.app.state.startup_warmup
        owner.launched = replace(launched, new_local_adapter=lambda: adapter)
        owner.start()
        await adapter.entered.wait()
        first = asyncio.create_task(owner.stop())
        await adapter.closing.wait()
        second = asyncio.create_task(owner.stop())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        try:
            assert not owner.task.done()
            assert not first.done() and not second.done()
        finally:
            adapter.close_release.set()
            await asyncio.gather(first, second)
            launched.orchestrator.store.close()
        assert adapter.closed

    asyncio.run(go())


@pytest.mark.parametrize("started", [False, True], ids=["late-callback", "pre-entry-cancel"])
def test_stop_before_warmup_entry_closes_the_lifetime_and_clears_pending(tmp_path, started):
    """RM38/AC29 I2: no finally-entry assumption and no late callback after teardown."""

    async def go():
        adapter = WarmupBarrier()
        launched = build_app(configured_catalog(tmp_path), local_adapter=adapter)
        owner = launched.app.state.startup_warmup
        if started:
            owner.start()
        await owner.stop()
        try:
            assert not launched.orchestrator.local_startup_pending
            assert launched.orchestrator.local_status.status == LocalModelClass.UNAVAILABLE
            previous = owner.task
            owner.start()
            await asyncio.sleep(0)
            assert owner.task is previous
            assert adapter.requests == []
        finally:
            await owner.stop()
            launched.orchestrator.store.close()

    asyncio.run(go())


def test_second_lifespan_rearms_cancelled_startup_before_other_components(tmp_path):
    """RM38/AC29 I2: a new lifespan owns a fresh check, without duplicate same-lifetime work."""

    async def go():
        launched = build_app(configured_catalog(tmp_path), local_adapter=ScriptedAdapter())
        owner = launched.app.state.startup_warmup
        adapters = [WarmupBarrier(), WarmupBarrier()]
        pending = iter(adapters)
        owner.launched = replace(launched, new_local_adapter=lambda: next(pending))
        async with launched.app.router.lifespan_context(launched.app):
            owner.start()
            await adapters[0].entered.wait()
        assert adapters[0].closed
        previous = owner.task
        async with launched.app.router.lifespan_context(launched.app):
            assert launched.orchestrator.local_startup_pending
            assert launched.app.state.asset_catalog.busy()
            owner.start()
            owner.start()
            await asyncio.wait_for(adapters[1].entered.wait(), 1)
            assert owner.task is not previous
            adapters[1].release.set()
            await until(lambda: owner.task.done())
            assert launched.orchestrator.local_status.usable
            finished = owner.task
            owner.start()
            assert owner.task is finished
        assert adapters[1].closed
        launched.orchestrator.store.close()

    asyncio.run(go())


def test_old_stop_waiter_cannot_clear_new_lifespan_startup_fence(tmp_path, monkeypatch):
    """RM38/AC29: cleanup from an older task may not mutate a rearmed lifetime."""

    async def go():
        launched = build_app(configured_catalog(tmp_path), local_adapter=ScriptedAdapter())
        owner = launched.app.state.startup_warmup
        first_adapter, next_adapter = SlowClose(), WarmupBarrier()
        adapters = iter((first_adapter, next_adapter))
        owner.launched = replace(launched, new_local_adapter=lambda: next(adapters))
        real_shield = asyncio.shield
        late_attached, late_held, release_late = asyncio.Event(), asyncio.Event(), asyncio.Event()
        late = old_task = None

        async def controlled_shield(task):
            is_late = asyncio.current_task() is late and task is old_task
            if is_late:
                late_attached.set()
            try:
                return await real_shield(task)
            finally:
                if is_late:
                    late_held.set()
                    await release_late.wait()

        monkeypatch.setattr(asyncio, "shield", controlled_shield)
        try:
            async with launched.app.router.lifespan_context(launched.app):
                owner.start()
                old_task = owner.task
                await first_adapter.entered.wait()
                first = asyncio.create_task(owner.stop())
                await first_adapter.closing.wait()
                late = asyncio.create_task(owner.stop())
                await late_attached.wait()
                first_adapter.close_release.set()
                await first
                await late_held.wait()
            async with launched.app.router.lifespan_context(launched.app):
                owner.start()
                await asyncio.wait_for(next_adapter.entered.wait(), 1)
                assert owner.task is not old_task
                release_late.set()
                await late
                assert launched.orchestrator.local_startup_pending
                launched.orchestrator.observe_local_health(True, True, "fixture healthy")
                assert not launched.orchestrator.local_status.usable
                next_adapter.release.set()
                await until(lambda: owner.task.done())
        finally:
            first_adapter.close_release.set()
            release_late.set()
            next_adapter.release.set()
            if late is not None:
                await asyncio.gather(late, return_exceptions=True)
            launched.orchestrator.store.close()

    asyncio.run(go())


@pytest.mark.parametrize("warmup,detect", [(False, True), (True, False)])
def test_enabled_startup_respects_independent_warmup_and_detection_switches(
    tmp_path, warmup, detect
):
    """RM38/AC29: health-only detection sends no generations; warm-up-only stays fenced."""

    async def go():
        adapter = WarmupBarrier()
        adapter.release.set()
        launched = build_app(
            configured_catalog(tmp_path, warmup=warmup, detect=detect), local_adapter=adapter
        )
        owner = launched.app.state.startup_warmup
        async with launched.app.router.lifespan_context(launched.app):
            assert not launched.orchestrator.local_status.usable
            owner.start()
            await until(lambda: owner.task.done())
            assert launched.orchestrator.local_status.usable
            assert len(adapter.requests) == (2 if warmup else 0)
        launched.orchestrator.store.close()

    asyncio.run(go())


def test_enabled_lexical_with_no_local_model_needs_no_warmup_owner(tmp_path):
    """RM38/AC29: a valid external-only configuration restores lexical service independently."""
    from test_launcher import EXTERNAL

    async def go():
        base = configured_catalog(tmp_path)
        config = replace(
            launcher.load_config(write_config(tmp_path, EXTERNAL, local=False)),
            assets=base.assets,
            recommendations=base.recommendations,
        )
        external = ScriptedAdapter(locality="external")
        launched = build_app(config, external_adapter=external)
        assert launched.app.state.startup_warmup is None
        async with launched.app.router.lifespan_context(launched.app):
            assert launched.app.state.recommendations.readiness()["state"] == "ready"
            assert launched.orchestrator.local_status.status == LocalModelClass.ABSENT
            assert not external.requests
        launched.orchestrator.store.close()

    asyncio.run(go())
