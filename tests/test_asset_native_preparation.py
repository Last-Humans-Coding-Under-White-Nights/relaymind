"""RM03/RM16/RM38/RM40/RM41 AC29/AC31/AC32: truthful bounded local setup."""

import asyncio
import copy
import json
import threading
import time

import pytest

from test_asset_diagnostic import development_input as development_input
from test_asset_native_runtime import native_fixture


def local_config(**changes):
    return {"mode": "local", "profile": "cold", "total_ms": 500, **changes}


def test_local_normalization_has_inapplicable_http_defaults_and_strict_primary():
    from relaymind.selection.asset_preparation import normalize_preparation

    value = normalize_preparation(local_config(total_ms=1), http_primary=False, local_primary=True)
    assert value == {
        "mode": "local",
        "url": None,
        "profile": "cold",
        "total_ms": 1,
        "request_ms": 2000,
        "poll_ms": 250,
        "max_polls": 240,
    }
    for bad in (
        dict(profile="reuse"),
        dict(url="http://127.0.0.1:1"),
        dict(total_ms=60001),
        dict(request_ms=1),
        dict(poll_ms=50),
        dict(max_polls=1),
        dict(extra=True),
    ):
        with pytest.raises(ValueError):
            normalize_preparation(local_config(**bad), http_primary=False, local_primary=True)
    with pytest.raises(ValueError):
        normalize_preparation(local_config(), http_primary=True)


def test_local_real_worker_evidence_is_closed_and_keeps_setup_out_of_queries(tmp_path):
    from relaymind.evaluation.asset_native_runtime import NativeBenchmarkRuntime
    from relaymind.selection.asset_preparation import validate_preparation_record

    async def scenario():
        production, release, effective = native_fixture(tmp_path)
        runtime = await NativeBenchmarkRuntime.create(production, release, effective)
        try:
            record = await runtime.prepare_local(
                local_config(), requested_kinds=("mcp_tool", "skill")
            )
            assert record["status"] == "ready" and record["reason"] is None
            assert record["preparation_id"] is None and record["exchanges"] == []
            assert record["observed_ms"] < 500 and record["cleanup_ms"] is None
            assert record["local"]["readiness"]["semantic_coverage"] == {"ready": 1, "total": 1}
            assert record["local"]["job_events"]
            embeddings = [
                embedding
                for event in record["local"]["job_events"]
                if event["event_type"] == "batch.finished"
                for embedding in event["payload"]["embeddings"]
            ]
            assert (
                len(embeddings) == 1
                and embeddings[0]["cost_observations"][0]["value"]["calls"] == 1
            )
            assert runtime.service.primary.observations == ()
            validate_preparation_record(record, binding=record["binding"])
            for key, changed in (
                ("index_revision", "different"),
                ("preparation_id", "00000000-0000-0000-0000-000000000000"),
                ("observed_ms", 501),
            ):
                bad = copy.deepcopy(record)
                bad[key] = changed
                with pytest.raises(ValueError):
                    validate_preparation_record(bad, binding=record["binding"])
        finally:
            await runtime.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("interrupt", ["deadline", "cancel", "task_cancel"])
def test_local_interruption_drains_native_work_and_preserves_known_cost(tmp_path, interrupt):
    """RM03/RM16/RM38/RM41: cleanup is real and may exceed HTTP's 1000 ms."""
    from relaymind.assets.embedding_activation import BackendRegistration
    from relaymind.evaluation.asset_native_runtime import NativeBenchmarkRuntime
    from relaymind.selection.asset_preparation import validate_preparation_record
    from test_embedding_backend import NativeDouble

    async def scenario():
        production, release, effective = native_fixture(tmp_path)
        native = NativeDouble()
        started, release_native = threading.Event(), threading.Event()

        def infer(tokens, invoked):
            invoked()
            started.set()
            if not release_native.wait(5):
                raise RuntimeError("test_native_not_released")
            return [[3, 4] for _ in tokens]

        native.infer = infer
        production.asset_semantics.loader._backends["fixture"] = BackendRegistration(
            lambda: dict(production.asset_semantics.hybrid.manifest.data["runtime"]),
            lambda: "cpu",
            lambda artifact: native,
        )
        runtime = await NativeBenchmarkRuntime.create(production, release, effective)

        preparation_task = None

        async def release_later():
            while not started.is_set():
                await asyncio.sleep(0.001)
            if interrupt == "task_cancel":
                preparation_task.cancel()
                await asyncio.sleep(0.005)
                preparation_task.cancel()
            await asyncio.sleep(1.2 if interrupt == "deadline" else 0.05)
            release_native.set()

        release_task = asyncio.create_task(release_later())
        before = time.monotonic()
        try:
            preparation_task = asyncio.create_task(
                runtime.prepare_local(
                    local_config(total_ms=100 if interrupt == "deadline" else 2000),
                    requested_kinds=("skill", "mcp_tool"),
                    cancelled=lambda: interrupt == "cancel" and started.is_set(),
                )
            )
            record = await preparation_task
            assert record["status"] == ("timeout" if interrupt == "deadline" else "cancelled")
            assert record["index_revision"] is None and started.is_set()
            assert record["cleanup_ms"] is not None and release_native.is_set()
            if interrupt == "deadline":
                assert record["observed_ms"] < 500
                assert record["cleanup_ms"] > 1000
                assert time.monotonic() - before > 1.1
            embeddings = [
                embedding
                for event in record["local"]["job_events"]
                for embedding in event["payload"].get("embeddings", [])
            ]
            assert len(embeddings) == 1
            assert embeddings[0]["cost_observations"][0]["value"]["calls"] == 1
            assert embeddings[0]["cost_observations"][0]["value"]["cpu_ms"] is None
            assert record["local"]["readiness"]["semantic_coverage"]["ready"] == 0
            events = copy.deepcopy(record["local"]["job_events"])
            await asyncio.sleep(0.01)
            assert list(runtime.catalog.job_event_stream()) == events
            validate_preparation_record(record)
        finally:
            release_native.set()
            await release_task
            await runtime.close()

    asyncio.run(scenario())


def test_local_evidence_collection_consumes_original_deadline(tmp_path, monkeypatch):
    """RM03/RM40: late evidence cannot turn a completed build into ready setup."""
    from relaymind.evaluation.asset_native_runtime import NativeBenchmarkRuntime

    async def scenario():
        production, release, effective = native_fixture(tmp_path)
        runtime = await NativeBenchmarkRuntime.create(production, release, effective)
        stream = runtime.catalog.job_event_stream
        calls = []

        def slow_evidence():
            if not calls:
                calls.append(True)
                time.sleep(0.15)
            return stream()

        monkeypatch.setattr(runtime.catalog, "job_event_stream", slow_evidence)
        try:
            record = await runtime.prepare_local(
                local_config(total_ms=100), requested_kinds=("skill", "mcp_tool")
            )
            assert record["status"] == "timeout" and record["observed_ms"] >= 150
            assert record["index_revision"] is None and record["cleanup_ms"] is not None
            assert record["local"]["job_events"]
        finally:
            await runtime.close()

    asyncio.run(scenario())


def test_local_ready_record_is_validated_in_its_returned_state(tmp_path, monkeypatch):
    """RM03/RM40/AC31: the decision time written last is part of the checked record."""
    import itertools

    from relaymind.evaluation import asset_native_runtime
    from relaymind.evaluation.asset_native_runtime import NativeBenchmarkRuntime

    checked = []
    real = asset_native_runtime.validate_preparation_record

    def recorded(record, **options):
        checked.append(copy.deepcopy(record))
        return real(record, **options)

    monkeypatch.setattr(asset_native_runtime, "validate_preparation_record", recorded)
    # Every clock read advances 0.1 ms, so the decision time always differs
    # from the evidence time, even where the real monotonic clock is coarse.
    ticks = itertools.count()

    async def scenario():
        production, release, effective = native_fixture(tmp_path)
        runtime = await NativeBenchmarkRuntime.create(production, release, effective)
        try:
            return await runtime.prepare_local(
                local_config(),
                requested_kinds=("mcp_tool", "skill"),
                clock=lambda: next(ticks) / 10000,
            )
        finally:
            await runtime.close()

    record = asyncio.run(scenario())
    assert record["status"] == "ready"
    assert checked and checked[-1] == record


def test_local_catalog_change_retains_old_binding_and_failed_job_cost(tmp_path):
    """RM16/RM38/RM40: stale source input cannot be rebound after inference."""
    from relaymind.assets.embedding_activation import BackendRegistration
    from relaymind.evaluation.asset_native_runtime import NativeBenchmarkRuntime
    from relaymind.selection.asset_preparation import validate_preparation_record
    from test_embedding_backend import NativeDouble

    async def scenario():
        production, release, effective = native_fixture(tmp_path)
        runtime = await NativeBenchmarkRuntime.create(production, release, effective)
        native = NativeDouble()

        def infer(tokens, invoked):
            invoked()
            with runtime.runs.sql_transaction() as db:
                db.execute("UPDATE asset_catalog_meta SET revision=revision+1")
            return [[3, 4] for _ in tokens]

        native.infer = infer
        production.asset_semantics.loader._backends["fixture"] = BackendRegistration(
            lambda: dict(production.asset_semantics.hybrid.manifest.data["runtime"]),
            lambda: "cpu",
            lambda artifact: native,
        )
        try:
            record = await runtime.prepare_local(
                local_config(), requested_kinds=("skill", "mcp_tool")
            )
            assert (
                record["status"] == "stale_catalog" and record["binding"]["catalog_revision"] == 1
            )
            assert runtime.catalog.revision() == 2 and record["local"]["readiness"] is None
            assert record["index_revision"] is None
            events = record["local"]["job_events"]
            assert events[-1]["payload"]["retained_catalog_revision"] == 2
            observations = [
                item for event in events for item in event["payload"].get("embeddings", [])
            ]
            assert (
                len(observations) == 1
                and observations[0]["cost_observations"][0]["value"]["calls"] == 1
            )
            assert not runtime.service.primary.index.pin()[0]
            validate_preparation_record(record)
        finally:
            await runtime.close()

    asyncio.run(scenario())


def test_local_partial_generation_keeps_failed_job_and_usable_prefix(tmp_path):
    """RM16/RM38/RM41: a later batch failure cannot erase real cost or prefix."""
    from dataclasses import replace

    from relaymind.assets.embedding_activation import BackendRegistration
    from relaymind.assets.embedding_config import validate_hybrid_config
    from relaymind.canonical import canonical_json
    from relaymind.evaluation.asset_native_runtime import NativeBenchmarkRuntime
    from relaymind.selection.asset_preparation import validate_preparation_record
    from test_embedding_backend import NativeDouble
    from test_embedding_launcher import loader_for

    async def scenario():
        production, release, effective = native_fixture(tmp_path)
        bundle = production.asset_semantics
        data = json.loads(canonical_json(bundle.hybrid.data))
        data["index"]["batch_size"] = 1
        config = validate_hybrid_config(data, bundle.hybrid.manifest)
        loader = loader_for(config)
        native = NativeDouble()
        calls = []

        def infer(tokens, invoked):
            invoked()
            calls.append(True)
            if len(calls) == 2:
                raise RuntimeError("test_second_batch_failure")
            return [[3, 4] for _ in tokens]

        native.infer = infer
        loader._backends["fixture"] = BackendRegistration(
            lambda: dict(config.manifest.data["runtime"]),
            lambda: "cpu",
            lambda artifact: native,
        )
        production = replace(
            production, asset_semantics=replace(bundle, hybrid=config, loader=loader)
        )
        extra = copy.deepcopy(release.catalog["assets"][0])
        extra["record"]["asset_id"] += "-second"
        release = replace(
            release, catalog=dict(release.catalog, assets=[*release.catalog["assets"], extra])
        )
        effective = dict(
            effective,
            eligibility=[
                *effective["eligibility"],
                dict(effective["eligibility"][0], asset_id=extra["record"]["asset_id"]),
            ],
        )
        runtime = await NativeBenchmarkRuntime.create(production, release, effective)
        try:
            record = await runtime.prepare_local(
                local_config(), requested_kinds=("skill", "mcp_tool")
            )
            assert record["status"] == "ready" and len(calls) == 2
            assert record["local"]["readiness"]["semantic_coverage"] == {"ready": 1, "total": 2}
            assert record["local"]["readiness"]["failed_jobs"] == 1
            assert any(event["event_type"] == "failed" for event in record["local"]["job_events"])
            embeddings = [
                embedding
                for event in record["local"]["job_events"]
                for embedding in event["payload"].get("embeddings", [])
            ]
            assert len(embeddings) == 2 and {item["outcome"] for item in embeddings} == {
                "succeeded",
                "failed",
            }
            validate_preparation_record(record)
        finally:
            await runtime.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("name", ["asset_benchmark_report", "asset_benchmark_comparison"])
def test_published_benchmark_schemas_accept_only_closed_local_record(tmp_path, name):
    """RM40/AC31: the published JSON schema carries the same local protocol."""
    from relaymind.evaluation.asset_native_runtime import NativeBenchmarkRuntime
    from relaymind.schema_util import StrictValidator, load_packaged_schema

    async def scenario():
        production, release, effective = native_fixture(tmp_path)
        runtime = await NativeBenchmarkRuntime.create(production, release, effective)
        try:
            record = await runtime.prepare_local(
                local_config(), requested_kinds=("skill", "mcp_tool")
            )
        finally:
            await runtime.close()
        schema = load_packaged_schema("relaymind.evaluation", name + ".v1.json")
        validator = StrictValidator({"$ref": "#/$defs/PreparationRecord", "$defs": schema["$defs"]})
        assert not list(validator.iter_errors(record))
        for mutate in (
            lambda value: value["local"].update(extra=True),
            lambda value: value.update(preparation_id="00000000-0000-0000-0000-000000000000"),
            lambda value: value["config"].update(total_ms=60001),
            lambda value: value["config"].update(profile="reuse"),
            lambda value: value["local"]["job_events"][0]["payload"].update(extra=True),
            lambda value: value["local"]["readiness"].update(extra=True),
        ):
            bad = copy.deepcopy(record)
            mutate(bad)
            assert list(validator.iter_errors(bad))

    asyncio.run(scenario())


def test_published_benchmark_input_schema_admits_bounded_local_config():
    from relaymind.schema_util import StrictValidator, load_packaged_schema

    schema = load_packaged_schema("relaymind.evaluation", "asset_benchmark_config.v1.json")
    validator = StrictValidator(schema["properties"]["preparation"])
    assert not list(validator.iter_errors(local_config()))
    for bad in (
        local_config(total_ms=60001),
        local_config(request_ms=1),
        local_config(profile="reuse"),
    ):
        assert list(validator.iter_errors(bad))


def test_native_normalized_selector_and_prepared_wrapper_keep_actual_mechanism(tmp_path):
    from dataclasses import replace

    from relaymind.evaluation.asset_benchmark import _normal
    from relaymind.evaluation.asset_native_runtime import NativeBenchmarkRuntime
    from relaymind.selection.asset_preparation import PreparedAssetSelector
    from test_asset_selection_benchmark import fixture_config

    async def scenario():
        production, release, effective = native_fixture(tmp_path)
        base_root = tmp_path / "benchmark"
        base_root.mkdir()
        base = fixture_config(base_root, repetitions=1, warmups=0)
        config = replace(
            base,
            production=production,
            release=release,
            effective=effective,
            data=dict(base.data, preparation=local_config()),
        )
        normal = _normal(config)
        assert normal["selector"] == {
            **dict(production.asset_semantics.hybrid.data["identity"]),
            "transport": "in_process",
        }
        runtime = await NativeBenchmarkRuntime.create(production, release, effective)
        try:
            record = await runtime.prepare_local(
                local_config(), requested_kinds=("mcp_tool", "skill")
            )
            wrapped = PreparedAssetSelector(runtime.service.primary, record)
            assert wrapped.mechanism == "hybrid_weighted_rrf" and wrapped.builtin
            assert wrapped.transport == "in_process"
            assert wrapped.observations == () and wrapped.observed_cost is None
        finally:
            await runtime.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("interrupted", [False, True])
def test_formal_native_runner_uses_one_prepared_owner_and_conserves_slots(tmp_path, interrupted):
    """RM40/RM41 AC31/AC32: actual production service, setup and query evidence."""
    from dataclasses import replace

    from relaymind.canonical import canonical_digest
    from relaymind.evaluation.asset_benchmark import run_asset_benchmark
    from relaymind.evaluation.asset_evidence import collect_benchmark_environment
    from test_asset_selection_benchmark import fixture_config

    production, release, effective = native_fixture(tmp_path)
    base_root = tmp_path / "benchmark"
    base_root.mkdir()
    base = fixture_config(base_root, repetitions=2, warmups=1)
    environment = collect_benchmark_environment(production=production)
    config = replace(
        base,
        production=production,
        release=release,
        effective=effective,
        environment=environment,
        environment_lock=environment,
        data=dict(
            base.data,
            preparation=local_config(),
            production_config_digest=canonical_digest(effective),
            environment_lock_digest=canonical_digest(environment),
        ),
    )
    report = asyncio.run(run_asset_benchmark(config, interrupted=lambda: interrupted))
    assert len(report.data["results"]) == len(report.data["planned_slots"])
    assert "native_static_validation_outside_timing" in report.data["limits"]
    setup = report.data["preparation"]
    if interrupted:
        assert report.data["status"] == "interrupted" and setup["status"] == "cancelled"
        assert all(
            row["status"] == "not_run" and row["request_digest"] is None
            for row in report.data["results"]
        )
    else:
        assert report.data["status"] == "completed" and setup["status"] == "ready"
        for row in report.data["results"]:
            assert row["status"] == "ready"
            assert row["catalog_revision"] == setup["binding"]["catalog_revision"]
            assert row["offered_projection_digest"] == setup["binding"]["catalog_digest"]
            assert row["attempts"][0]["mechanism"] == "hybrid_weighted_rrf"
            assert len(row["embeddings"]) == 1
        assert report.inputs is not None and report.input_error is None


def test_source_native_runner_reuses_prepared_primary_for_every_row(
    development_input, tmp_path, monkeypatch
):
    """RM40/RM41: source development uses the same real backend/factory ownership."""
    from relaymind.evaluation import asset_diagnostic_runner
    from test_asset_diagnostic import load

    fixture_root = tmp_path / "native"
    fixture_root.mkdir()
    production, _, _ = native_fixture(fixture_root)
    monkeypatch.setattr(asset_diagnostic_runner, "_load_production_config", lambda path: production)
    report = asyncio.run(
        asset_diagnostic_runner.run_source_benchmark(
            load(development_input),
            production_config=development_input / "fixture.toml",
            preparation=local_config(),
            repetitions=2,
        )
    )
    assert report["status"] == "completed" and report["preparation"]["status"] == "ready"
    assert report["qualification"] == "provisional" and report["acceptance_eligible"] is False
    assert "native_static_validation_outside_timing" in report["limits"]
    assert len(report["rows"]) == 2
    for row in report["rows"]:
        assert row["status"] == "ready"
        assert row["attempts"][0]["mechanism"] == "hybrid_weighted_rrf"
        assert len(row["embeddings"]) == 1
        assert (
            row["request"]["catalog_revision"]
            == report["preparation"]["binding"]["catalog_revision"]
        )


def test_local_preparation_policy_requires_shared_cold_total():
    """RM40/AC31: local setup policy is not exempt from comparison invariants."""
    from relaymind.selection.asset_preparation import (
        normalize_preparation,
        preparation_policy_mismatches,
    )

    local_500 = normalize_preparation(local_config(), http_primary=False, local_primary=True)
    local_900 = normalize_preparation(
        local_config(total_ms=900), http_primary=False, local_primary=True
    )
    assert preparation_policy_mismatches(local_500, local_900) == ("total_ms",)
    assert preparation_policy_mismatches(local_500, local_500) == ()

    http_cold = normalize_preparation(
        {
            "mode": "http",
            "url": "http://127.0.0.1:1/index",
            "profile": "cold",
            "total_ms": 500,
            "request_ms": 100,
        },
        http_primary=True,
        local_primary=True,
    )
    assert preparation_policy_mismatches(local_500, http_cold) == ()
    http_reuse = normalize_preparation(
        {
            "mode": "http",
            "url": "http://127.0.0.1:1/index",
            "profile": "reuse",
            "total_ms": 500,
            "request_ms": 100,
        },
        http_primary=True,
        local_primary=True,
    )
    assert preparation_policy_mismatches(local_500, http_reuse) == ("profile",)
    http_slower = normalize_preparation(
        {
            "mode": "http",
            "url": "http://127.0.0.1:1/index",
            "profile": "cold",
            "total_ms": 900,
            "request_ms": 100,
        },
        http_primary=True,
        local_primary=True,
    )
    assert preparation_policy_mismatches(local_500, http_slower) == ("total_ms",)
    assert preparation_policy_mismatches(local_500, normalize_preparation(None)) == ()


def test_local_offer_is_bounded_to_the_requested_kinds(tmp_path):
    """RM40/AC31: a single-kind cohort does not fail on other catalog kinds."""
    from dataclasses import replace

    from relaymind.canonical import canonical_digest
    from relaymind.evaluation.asset_evidence import effective_asset_config
    from relaymind.evaluation.asset_native_runtime import NativeBenchmarkRuntime

    async def scenario():
        production, release, base = native_fixture(tmp_path)
        tool = copy.deepcopy(release.catalog["assets"][0])
        record = tool["record"]
        record.update(asset_id="calendar-tool", kind="mcp_tool")
        record["source_locator"] = "fixture:calendar-tool"
        record["identity"] = {
            "server_id": "fixture",
            "tool_name": "calendar",
            "exposed_name": "calendar",
            "input_schema": {},
            "schema_hash": canonical_digest({}),
        }
        release = replace(
            release,
            catalog=dict(release.catalog, assets=[*release.catalog["assets"], tool]),
        )
        effective = effective_asset_config(
            production,
            release,
            runner_digest=base["generator"]["runner_digest"],
            protocol_digest=base["generator"]["protocol_digest"],
            requested_kinds=("skill",),
        )
        runtime = await NativeBenchmarkRuntime.create(production, release, effective)
        try:
            assert [record.asset_id for record in runtime.catalog.snapshot().assets] == ["calendar"]
            prepared = await runtime.prepare_local(local_config(), requested_kinds=("skill",))
            assert prepared["status"] == "ready"
            skill = next(
                asset["record"]
                for asset in release.catalog["assets"]
                if asset["record"]["kind"] == "skill"
            )
            assert prepared["binding"]["requested_kinds"] == ["skill"]
            assert prepared["binding"]["catalog_digest"] == canonical_digest([skill])
        finally:
            await runtime.close()

    asyncio.run(scenario())


def test_local_empty_offer_retains_actual_zero_revision_without_inference(tmp_path):
    from dataclasses import replace

    from relaymind.evaluation.asset_native_runtime import NativeBenchmarkRuntime
    from relaymind.selection.asset_preparation import validate_preparation_record

    async def scenario():
        production, release, effective = native_fixture(tmp_path)
        catalog = dict(release.catalog, assets=[])
        release = replace(release, catalog=catalog)
        effective = dict(effective, eligibility=[])
        runtime = await NativeBenchmarkRuntime.create(production, release, effective)
        try:
            record = await runtime.prepare_local(
                local_config(), requested_kinds=("mcp_tool", "skill")
            )
            assert record["status"] == "not_applicable" and record["index_revision"] is None
            assert record["binding"]["catalog_revision"] == 0
            assert record["local"]["readiness"]["reason"] == "empty_catalog"
            assert runtime.service.primary.engine is None and record["observed_costs"] == []
            assert all(
                not event["payload"].get("embeddings") for event in record["local"]["job_events"]
            )
            validate_preparation_record(record)
        finally:
            await runtime.close()

    asyncio.run(scenario())
