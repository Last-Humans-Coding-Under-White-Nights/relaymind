"""RM03/RM16/RM38/RM40 AC29/AC31: setup ownership and decided cancellation."""

import asyncio
import threading
import time
from pathlib import Path

import pytest

from relaymind.assets.embedding_activation import BackendRegistration
from relaymind.evaluation import asset_diagnostic_runner
from relaymind.evaluation.asset_native_runtime import NativeBenchmarkRuntime
from relaymind.store.run_store import RunStoreError
from test_asset_diagnostic import development_input as development_input
from test_asset_diagnostic import load
from test_asset_native_preparation import local_config
from test_asset_native_runtime import native_fixture
from test_embedding_backend import NativeDouble


def observe_runtime(monkeypatch, on_created):
    """Observe real owner disposal, without replacing its cleanup behavior."""
    acquired = []
    create = NativeBenchmarkRuntime.create

    async def capture(cls, *args, **kwargs):
        runtime = await create(*args, **kwargs)
        observation = dict(runtime=runtime, path=Path(runtime.directory.name), closes=0)
        acquired.append(observation)
        close = runtime.close

        async def close_and_observe():
            observation["closes"] += 1
            observation["events"] = runtime.catalog.job_event_stream()
            await close()

        runtime.close = close_and_observe
        on_created(runtime)
        return runtime

    monkeypatch.setattr(NativeBenchmarkRuntime, "create", classmethod(capture))
    return acquired


def assert_disposed(observation):
    runtime = observation["runtime"]
    assert observation["closes"] == 1
    assert runtime._closed and runtime.provider._stopping
    assert runtime.worker._task is None or runtime.worker._task.done()
    assert not observation["path"].exists()
    with pytest.raises(RunStoreError, match="closed"):
        runtime.catalog.revision()


class SetupAbort(BaseException):
    pass


@pytest.mark.parametrize("mode", ["local", "none"])
@pytest.mark.parametrize("failure", [RuntimeError, SetupAbort, asyncio.CancelledError])
def test_source_setup_failure_disposes_real_owner_before_query_loop(
    development_input, tmp_path, monkeypatch, mode, failure
):
    """Removing the outer source ownership guard leaks the worker/store/temp root."""
    fixture_root = tmp_path / "native"
    fixture_root.mkdir()
    production, _, _ = native_fixture(fixture_root)
    monkeypatch.setattr(asset_diagnostic_runner, "_load_production_config", lambda p: production)

    def fail_setup(runtime):
        rebuild = runtime.worker.rebuild

        async def actual_lexical_then_failure(*args, **kwargs):
            await rebuild()
            assert runtime.service.primary.engine is None
            raise failure("controlled_setup_abort")

        if mode == "local":
            runtime.prepare_local = actual_lexical_then_failure
        else:
            runtime.worker.rebuild = actual_lexical_then_failure

    acquired = observe_runtime(monkeypatch, fail_setup)

    async def scenario():
        try:
            with pytest.raises(failure):
                await asset_diagnostic_runner.run_source_benchmark(
                    load(development_input),
                    production_config=development_input / "fixture.toml",
                    preparation=local_config() if mode == "local" else None,
                )
            assert len(acquired) == 1
            assert_disposed(acquired[0])
            events = acquired[0]["events"]
            assert events[0]["payload"]["job_kind"] == "lexical"
            assert events[-1]["event_type"] == "published"
            assert all(not event["payload"].get("embeddings") for event in events)
        finally:
            # RED also leaves no fixture resources behind after the assertion.
            for observation in acquired:
                if not observation["runtime"]._closed:
                    await observation["runtime"].close()

    asyncio.run(scenario())


def test_source_failed_native_setup_keeps_planned_rows_and_actual_job_evidence(
    development_input, tmp_path, monkeypatch
):
    """A closed failed setup must be reported, rather than escaping with lost slots."""
    fixture_root = tmp_path / "native"
    fixture_root.mkdir()
    production, _, _ = native_fixture(fixture_root)
    monkeypatch.setattr(asset_diagnostic_runner, "_load_production_config", lambda p: production)

    def fail_load(artifact):
        raise RuntimeError("controlled_native_constructor_failure")

    manifest = production.asset_semantics.hybrid.manifest
    production.asset_semantics.loader._backends["fixture"] = BackendRegistration(
        lambda: dict(manifest.data["runtime"]),
        lambda: "cpu",
        fail_load,
    )
    acquired = observe_runtime(monkeypatch, lambda runtime: None)
    report = asyncio.run(
        asset_diagnostic_runner.run_source_benchmark(
            load(development_input),
            production_config=development_input / "fixture.toml",
            preparation=local_config(),
            repetitions=3,
        )
    )
    assert_disposed(acquired[0])
    assert report["status"] == "blocked" and len(report["rows"]) == 3
    setup = report["preparation"]
    assert setup["status"] == "failed" and setup["reason"] == "build_failed"
    assert setup["observed_ms"] is not None and setup["cleanup_ms"] is not None
    assert setup["local"]["job_events"][-1]["event_type"] == "failed"
    assert setup["local"]["readiness"]["semantic_coverage"] == {"ready": 0, "total": 1}
    assert all(row["status"] == "not_run" and row["request"] is None for row in report["rows"])
    assert all(not event["payload"].get("embeddings") for event in setup["local"]["job_events"])


@pytest.mark.parametrize("cancellation", ["callback", "task"])
def test_source_cancelled_setup_stays_interrupted_after_catalog_change(
    development_input, tmp_path, monkeypatch, cancellation
):
    """A cleanup stale read must not rewrite the already-decided cancellation."""
    fixture_root = tmp_path / "native"
    fixture_root.mkdir()
    production, _, _ = native_fixture(fixture_root)
    monkeypatch.setattr(asset_diagnostic_runner, "_load_production_config", lambda p: production)
    started, release_native = threading.Event(), threading.Event()
    native = NativeDouble()
    stage = {}

    def configure(runtime):
        def infer(tokens, invoked):
            invoked()
            with runtime.runs.sql_transaction() as db:
                db.execute("UPDATE asset_catalog_meta SET revision=revision+1")
            started.set()
            if not release_native.wait(5):
                raise RuntimeError("test_native_not_released")
            return [[3, 4] for _ in tokens]

        native.infer = infer
        manifest = production.asset_semantics.hybrid.manifest
        production.asset_semantics.loader._backends["fixture"] = BackendRegistration(
            lambda: dict(manifest.data["runtime"]),
            lambda: "cpu",
            lambda artifact: native,
        )
        prepare = runtime.prepare_local

        async def observe_cancellation(config, **kwargs):
            before = time.monotonic()
            record = await prepare(
                config, **kwargs, cancelled=lambda: cancellation == "callback" and started.is_set()
            )
            stage["elapsed_ms"] = (time.monotonic() - before) * 1000
            return record

        runtime.prepare_local = observe_cancellation

    acquired = observe_runtime(monkeypatch, configure)

    async def scenario():
        task = asyncio.create_task(
            asset_diagnostic_runner.run_source_benchmark(
                load(development_input),
                production_config=development_input / "fixture.toml",
                preparation=local_config(total_ms=2000),
                repetitions=3,
            )
        )
        try:
            while not started.is_set() and not task.done():
                await asyncio.sleep(0.001)
            assert started.is_set()
            if cancellation == "task":
                task.cancel()
                await asyncio.sleep(0.005)
                task.cancel()
            await asyncio.sleep(0.05)
            release_native.set()
            report = await task
            assert_disposed(acquired[0])
            assert report["status"] == "interrupted" and len(report["rows"]) == 3
            setup = report["preparation"]
            assert setup["status"] == "cancelled" and setup["reason"] == "cancelled"
            assert setup["binding"]["catalog_revision"] == 1 and setup["index_revision"] is None
            assert setup["local"]["readiness"] is None and setup["cleanup_ms"] is not None
            assert setup["observed_ms"] < 2000
            assert setup["observed_ms"] + setup["cleanup_ms"] <= stage["elapsed_ms"] + 5
            assert all(
                row["status"] == "not_run" and row["request"] is None for row in report["rows"]
            )
            events = setup["local"]["job_events"]
            assert events[-1]["event_type"] == "cancelled"
            assert events[-1]["payload"]["retained_catalog_revision"] == 2
            observations = [
                item for event in events for item in event["payload"].get("embeddings", [])
            ]
            assert len(observations) == 1
            assert observations[0]["cost_observations"][0]["value"]["calls"] == 1
            assert not acquired[0]["runtime"].service.primary.index.pin()[0]
        finally:
            release_native.set()
            await asyncio.gather(task, return_exceptions=True)
            for observation in acquired:
                if not observation["runtime"]._closed:
                    await observation["runtime"].close()

    asyncio.run(scenario())
