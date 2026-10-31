"""RM16/RM35/RM40/RM41 AC26/AC31/AC32: verified native retrieval evidence."""

import asyncio
import copy
import time
from dataclasses import replace

import pytest

from relaymind.canonical import canonical_digest
from relaymind.evaluation import asset_benchmark, asset_comparison, asset_evidence
from test_asset_diagnostic import development_input as development_input
from test_asset_native_preparation import local_config
from test_asset_native_runtime import native_fixture
from test_asset_selection_benchmark import fixture_config


def native_configuration(
    tmp_path, *, fallback=False, failure=None, manifest_overrides=None, empty_offer=False
):
    """Only the backend/qualification fixture is injected; real factory/worker/run/export."""
    tmp_path.mkdir(exist_ok=True)
    production, release, _ = native_fixture(tmp_path)
    if manifest_overrides:
        import json

        from relaymind.assets.embedding_bundle import load_semantic_bundle
        from relaymind.assets.embedding_config import load_hybrid_config
        from relaymind.assets.embedding_manifest import load_embedding_manifest
        from test_embedding_launcher import loader_for

        root = production.asset_semantics.hybrid.manifest.root
        raw = json.loads((root / "manifest.json").read_text())
        for field, value in manifest_overrides.items():
            raw[field] = value
        (root / "manifest.json").write_text(json.dumps(raw))
        raw_config = json.loads((root / "hybrid.json").read_text())
        raw_config["manifest_digest"] = load_embedding_manifest(
            root / "manifest.json"
        ).manifest_digest
        (root / "hybrid.json").write_text(json.dumps(raw_config))
        parsed = load_hybrid_config(
            root / "hybrid.json", load_embedding_manifest(root / "manifest.json")
        )
        bundle = load_semantic_bundle(
            {"manifest": "manifest.json", "config": "hybrid.json"},
            {},
            config_dir=root,
            loader=loader_for(parsed),
        )
        production = replace(production, asset_semantics=bundle)
    if failure:
        from relaymind.assets.embedding_activation import BackendRegistration
        from test_embedding_backend import NativeDouble

        native = NativeDouble()
        infer = native.infer
        count = 0

        def fault(tokens, invoked):
            nonlocal count
            count += 1
            if count > 1:
                invoked()
                if failure == "timeout":
                    time.sleep(0.20)
                raise RuntimeError("fixture_native_failure")
            return infer(tokens, invoked)

        native.infer = fault
        production.asset_semantics.loader._backends["fixture"] = BackendRegistration(
            lambda: dict(production.asset_semantics.hybrid.manifest.data["runtime"]),
            lambda: "cpu",
            lambda artifact: native,
        )
    production = replace(
        production,
        recommendations=replace(production.recommendations, fallback=fallback),
    )
    if empty_offer:
        production = replace(
            production,
            assets=replace(
                production.assets,
                sources=tuple(
                    replace(source, source_id="excluded") for source in production.assets.sources
                ),
            ),
        )
    base_root = tmp_path / "formal"
    base_root.mkdir()
    base = fixture_config(base_root, repetitions=1, warmups=0, fallback=fallback)
    effective = asset_evidence.effective_asset_config(
        production,
        release,
        runner_digest=canonical_digest(base.inventories["runner_inventory"]),
        protocol_digest=canonical_digest(base.inventories["protocol_inventory"]),
    )
    environment = asset_evidence.collect_benchmark_environment(production=production)
    config = replace(
        base,
        production=production,
        release=release,
        effective=effective,
        environment=environment,
        environment_lock=environment,
        data=dict(
            base.data,
            preparation=local_config(total_ms=2000),
            production_config_digest=canonical_digest(effective),
            environment_lock_digest=canonical_digest(environment),
        ),
    )
    return config


def native_report(tmp_path, **options):
    config = native_configuration(tmp_path, **options)
    report = asyncio.run(asset_benchmark.run_asset_benchmark(config))
    assert report.data["status"] == "completed"
    assert report.data["preparation"]["status"] == (
        "not_applicable" if options.get("empty_offer") else "ready"
    )
    assert report.inputs is not None, report.input_error
    return report


def test_native_saved_report_regrades_with_observed_inference_and_no_lexical_zero(tmp_path):
    report = native_report(tmp_path)
    for row in report.data["results"]:
        assert row["attempts"][0]["mechanism"] == "hybrid_weighted_rrf"
        assert len(row["embeddings"]) == 1
        assert row["attempts"][0]["cost_observations"] == row["embeddings"][0]["cost_observations"]
    summaries, _ = asset_comparison.validate_report_evidence(report)
    overall = next(
        s
        for s in summaries
        if s["dimensions"]["lane"] == "all"
        and s["dimensions"]["kind"] == "all"
        and s["dimensions"]["availability"] == "all"
        and s["dimensions"]["stale"] is None
        and s["dimensions"]["retrieval_mode"] == "service"
    )
    calls = next(
        c for c in overall["costs"] if c["metric"] == "calls" and c["provenance"] == "observed"
    )
    count = len(report.data["results"])
    assert calls["known"] == count and calls["unknown"] == 0 and calls["total"] == count
    cpu = next(
        c for c in overall["costs"] if c["metric"] == "cpu_ms" and c["provenance"] == "observed"
    )
    assert cpu["total"] is None and cpu["unknown"] == count
    setup_calls = [
        embedding
        for event in report.data["preparation"]["local"]["job_events"]
        for embedding in event["payload"].get("embeddings", [])
    ]
    assert len(setup_calls) == 1  # setup is not copied into either query row


@pytest.mark.parametrize("failure", ["failed", "timeout"])
def test_failed_native_work_and_gateway_floor_keep_distinct_owned_costs(tmp_path, failure):
    report = native_report(tmp_path, fallback=True, failure=failure)
    assert any(len(r["attempts"]) == 2 for r in report.data["results"])
    assert any(r["embeddings"] for r in report.data["results"])
    asset_comparison.validate_report_evidence(report)
    for row in report.data["results"]:
        if row["embeddings"]:
            assert row["attempts"][0]["outcome"] in ("unavailable", "timeout")
            assert row["attempts"][1]["mechanism"] == "gateway_lexical_fallback"
            assert row["attempts"][0]["cost_observations"][0]["value"]["calls"] == 1
            assert row["attempts"][1]["cost_observations"][0]["value"]["calls"] == 0


def test_runtime_display_native_versus_verified_nonuse_is_self_contained(tmp_path):
    native = native_report(tmp_path / "native")
    lexical_root = tmp_path / "lexical"
    lexical_root.mkdir()
    lexical = asyncio.run(
        asset_benchmark.run_asset_benchmark(fixture_config(lexical_root, repetitions=1, warmups=0))
    )
    comparison = asset_comparison.compare_asset_reports(lexical, native, quality_only=True)
    data = comparison.to_json_object()
    assert data["status"] == "compatible", data["reasons"]
    assert data["performance_comparable"] is False
    assert data["arms"]["a"]["runtime"] == dict(
        use="unused", backend=None, version=None, build=None, device=None, config_digest=None
    )
    used = data["arms"]["b"]["runtime"]
    assert used == dict(
        use="used",
        backend="fixture",
        version="1",
        build="1",
        device="cpu",
        config_digest=native.data["environment"]["runtime_config_digest"],
    )
    # Renderer gets no source reports, resolver or filesystem access.
    rendered = asset_comparison.render_asset_comparison(copy.deepcopy(data))
    assert "Runtime" in rendered and "fixture" in rendered and "unused" in rendered
    assert "not comparable" in rendered
    unknown = copy.copy(native)
    object.__setattr__(unknown, "inputs", None)
    bad = asset_comparison.compare_asset_reports(lexical, unknown, quality_only=True).data
    assert bad["status"] == "incompatible" and bad["arms"]["b"]["runtime"]["use"] == "unknown"
    assert bad["arms"]["a"]["runtime"]["use"] == "unused"


def test_native_bundle_offline_load_and_compare_never_opens_manifest_locators(
    tmp_path, monkeypatch
):
    from relaymind.assets import embedding_config, embedding_manifest

    report = native_report(tmp_path / "native")
    output = tmp_path / "native.json"
    asset_benchmark.write_asset_benchmark_report(report, output)

    def forbidden(*args, **kwargs):
        raise AssertionError("offline compare must not activate a runtime or follow embedded paths")

    monkeypatch.setattr(embedding_manifest, "read_semantic_json", forbidden)
    monkeypatch.setattr(embedding_config, "read_semantic_json", forbidden)
    loaded = asset_benchmark.load_asset_benchmark_report(
        output, resolver=output.with_name(output.name + ".resolver.json")
    )
    assert loaded.inputs is not None, loaded.input_error
    comparison = asset_comparison.compare_asset_reports(loaded, loaded, quality_only=True)
    assert comparison.data["status"] == "compatible", comparison.data["reasons"]
    assert comparison.data["arms"]["a"]["runtime"]["use"] == "used"


@pytest.mark.parametrize(
    "mutation",
    [
        "lexical_mechanism",
        "artifact",
        "device",
        "representation",
        "input_bytes",
        "input_tokens",
        "zero_calls",
        "missing_call",
        "prepared_revision",
    ],
)
def test_native_tampering_never_regrades_as_a_valid_query(tmp_path, mutation):
    report = native_report(tmp_path)
    data = copy.deepcopy(report.data)
    row = data["results"][0]
    if mutation == "lexical_mechanism":
        row["attempts"][0]["mechanism"] = "lexical_overlap"
        row["attempts"][0]["result"]["mechanism"] = "lexical_overlap"
    elif mutation == "prepared_revision":
        row["attempts"][0]["result"]["index_revision"] = "wrong-index"
        row["attempts"][0]["index_revision"] = "wrong-index"
    elif mutation == "missing_call":
        row["embeddings"] = []
        row["attempts"][0]["cost_observations"] = []
    else:
        observation = row["embeddings"][0]
        if mutation == "artifact":
            observation["artifact_hash"] = "0" * 64
        elif mutation == "device":
            observation["device"] = "wrong-device"
        elif mutation == "representation":
            observation["representation_revision"] = "task_text.v1"
        elif mutation == "input_bytes":
            observation["input_bytes"] += 1
        elif mutation == "input_tokens":
            observation["input_tokens"] = 8192
        else:
            observation["cost_observations"][0]["value"]["calls"] = 0
        row["attempts"][0]["cost_observations"] = copy.deepcopy(observation["cost_observations"])
    with pytest.raises(ValueError):
        asset_comparison.validate_report_evidence(replace(report, data=data))


def test_lexical_run_cannot_acquire_an_unconfigured_embedding(tmp_path):
    native = native_report(tmp_path / "native")
    root = tmp_path / "lexical"
    root.mkdir()
    lexical = asyncio.run(
        asset_benchmark.run_asset_benchmark(fixture_config(root, repetitions=1, warmups=0))
    )
    data = copy.deepcopy(lexical.data)
    # Use rows that actually completed: on a slow runner the first row of
    # either report can time out and carry no embeddings at all.
    observed = next(r["embeddings"] for r in native.data["results"] if r["embeddings"])
    target = next(r for r in data["results"] if r["status"] == "ready")
    target["embeddings"] = copy.deepcopy(observed)
    assert target["embeddings"]
    with pytest.raises(ValueError, match="unconfigured_embedding"):
        asset_comparison.validate_report_evidence(replace(lexical, data=data))


def test_closed_runtime_projection_unknown_and_historical_shapes(tmp_path):
    from relaymind.evaluation.asset_benchmark_io import validate_benchmark_document

    native = native_report(tmp_path)
    current = asset_comparison.compare_asset_reports(
        native, native, quality_only=True
    ).to_json_object()
    assert current["arms"]["a"]["runtime"]["use"] == "used"
    historical = copy.deepcopy(current)
    for side in ("a", "b"):
        historical["arms"][side].pop("runtime")
    before = copy.deepcopy(historical)
    validate_benchmark_document("comparison", historical)
    asset_comparison.render_asset_comparison(historical)
    assert historical == before
    for mutation in (
        "mixed_protocol",
        "extra",
        "partial_used",
        "false_unused",
        "unknown_compatible",
    ):
        bad = copy.deepcopy(current)
        runtime = bad["arms"]["a"]["runtime"]
        if mutation == "mixed_protocol":
            bad["arms"]["a"].pop("runtime")
        elif mutation == "extra":
            runtime["path"] = "/not-an-output-field"
        elif mutation == "partial_used":
            runtime["build"] = None
        elif mutation == "false_unused":
            runtime["use"] = "unused"
        else:
            runtime["use"] = "unknown"
        with pytest.raises(ValueError):
            validate_benchmark_document("comparison", bad)
    broken = copy.deepcopy(native.data)
    broken["environment_digest"] = "0" * 64
    projection = asset_comparison.runtime_display(replace(native, data=broken))
    assert projection == dict(
        use="unknown", backend=None, version=None, build=None, device=None, config_digest=None
    )


def source_pair(development_input, tmp_path, monkeypatch):
    from relaymind.evaluation import asset_diagnostic_runner as source
    from test_asset_diagnostic import load

    root = tmp_path / "source-native"
    root.mkdir()
    native, _, _ = native_fixture(root)
    lexical = replace(
        native,
        asset_semantics=None,
        recommendations=replace(native.recommendations, primary="lexical"),
    )
    monkeypatch.setattr(
        source,
        "_load_production_config",
        lambda path: native if path.name == "native.toml" else lexical,
    )
    verified = load(development_input)
    left = asyncio.run(
        source.run_source_benchmark(verified, production_config=development_input / "lexical.toml")
    )
    right = asyncio.run(
        source.run_source_benchmark(
            verified,
            production_config=development_input / "native.toml",
            preparation=local_config(total_ms=2000),
        )
    )
    return left, right


def test_source_native_pair_exports_exact_semantic_and_implementation_preimages(
    development_input, tmp_path, monkeypatch
):
    from relaymind.evaluation import asset_diagnostic_runner as source

    lexical, native = source_pair(development_input, tmp_path, monkeypatch)
    assert native["configuration_documents"]
    for id, document in native["configuration_documents"].items():
        assert id == "referenced_config:" + canonical_digest(document)
    for kind, field in [
        ("runner_inventory", "runner_digest"),
        ("protocol_inventory", "protocol_digest"),
    ]:
        assert canonical_digest(native[kind]) == native[field]
    assert native["environment"]["runtime"] == "fixture"
    paired = source.compare_source_benchmarks(lexical, native)
    assert paired["compatible"] and paired["paired_slots"] == len(native["rows"])
    assert paired["runtime"]["left"]["use"] == "unused"
    assert paired["runtime"]["right"]["use"] == "used"
    assert paired["acceptance_eligible"] is False


@pytest.mark.parametrize(
    "mutation",
    ["missing_preimage", "tampered_preimage", "forged_inventory", "wrong_model", "zero_calls"],
)
def test_source_native_compare_refuses_inconsistent_native_evidence(
    development_input, tmp_path, monkeypatch, mutation
):
    from relaymind.evaluation import asset_diagnostic_runner as source

    lexical, native = source_pair(development_input, tmp_path, monkeypatch)
    changed = copy.deepcopy(native)
    if mutation == "missing_preimage":
        changed.pop("configuration_documents", None)
    elif mutation == "tampered_preimage":
        documents = changed.get("configuration_documents", {})
        if documents:
            documents[next(iter(documents))]["schema_version"] = 99
        else:
            changed["configuration_documents"] = {"forged": {"schema_version": 99}}
    elif mutation == "forged_inventory":
        changed["runner_inventory"] = {"schema_version": 1, "files": []}
    else:
        observation = changed["rows"][0]["embeddings"][0]
        if mutation == "wrong_model":
            observation["model"]["name"] = "wrong-native-model"
        else:
            observation["cost_observations"][0]["value"]["calls"] = 0
        # Preserve redundant producer copies so this exercises actual binding,
        # rather than merely the old redundant-array equality check.
        changed["rows"][0]["result"]["embeddings"] = copy.deepcopy(changed["rows"][0]["embeddings"])
    with pytest.raises(ValueError, match="invalid_development_report"):
        source.compare_source_benchmarks(lexical, changed)


@pytest.mark.parametrize("quality_only", [False, True])
def test_two_native_runtime_builds_never_become_an_artifact_variable(tmp_path, quality_only):
    a = native_report(tmp_path / "a")
    b = native_report(
        tmp_path / "b",
        manifest_overrides={
            "runtime": dict(backend="fixture", version="1", build="different-execution-build")
        },
    )
    paired = asset_comparison.compare_asset_reports(a, b, quality_only=quality_only).data
    assert paired["status"] == "incompatible"
    assert paired["arms"]["a"]["runtime"]["use"] == paired["arms"]["b"]["runtime"]["use"] == "used"
    assert any(reason["field"].startswith("/environment/runtime/") for reason in paired["reasons"])


def test_declared_native_model_choice_keeps_shared_runtime_invariants(tmp_path):
    a = native_report(tmp_path / "a")
    b = native_report(
        tmp_path / "b", manifest_overrides={"model": dict(name="fixture-alternative", version="1")}
    )
    paired = asset_comparison.compare_asset_reports(a, b, quality_only=True).data
    assert paired["status"] == "compatible", paired["reasons"]
    assert (
        paired["arms"]["a"]["runtime"]["config_digest"]
        != paired["arms"]["b"]["runtime"]["config_digest"]
    )
    assert paired["arms"]["a"]["runtime"]["build"] == paired["arms"]["b"]["runtime"]["build"]
    assert paired["arms"]["a"]["artifacts"] != paired["arms"]["b"]["artifacts"]


@pytest.mark.parametrize("mutation", ["model", "artifact_hash", "device", "cost"])
def test_local_preparation_observation_must_bind_its_actual_native_manifest(tmp_path, mutation):
    report = native_report(tmp_path)
    data = copy.deepcopy(report.data)
    observation = next(
        embedding
        for event in data["preparation"]["local"]["job_events"]
        for embedding in event["payload"].get("embeddings", [])
    )
    if mutation == "model":
        observation["model"]["name"] = "wrong-setup-model"
    elif mutation == "cost":
        observation["cost_observations"][0]["value"]["calls"] = 0
    else:
        observation[mutation] = "0" * 64 if mutation == "artifact_hash" else "wrong-setup-device"
    with pytest.raises(ValueError):
        asset_comparison.validate_report_evidence(replace(report, data=data))


def test_native_empty_offer_keeps_configured_runtime_and_verified_no_inference(tmp_path):
    report = native_report(tmp_path, empty_offer=True)
    assert report.data["preparation"]["binding"]["catalog_revision"] == 0
    assert report.data["preparation"]["index_revision"] is None
    for row in report.data["results"]:
        assert row["result"]["decision"] == "empty_catalog"
        assert row["attempts"] == row["embeddings"] == []
        assert row["observed_costs"][0]["value"]["calls"] == 0
    asset_comparison.validate_report_evidence(report)
    compared = asset_comparison.compare_asset_reports(report, report, quality_only=True).data
    assert compared["status"] == "compatible", compared["reasons"]
    assert compared["arms"]["a"]["runtime"]["use"] == "used"


@pytest.mark.parametrize("cancel", [False, True])
def test_formal_setup_abort_disposes_real_owner_before_query_scope(tmp_path, monkeypatch, cancel):
    from test_asset_native_review import SetupAbort, assert_disposed, observe_runtime

    config = native_configuration(tmp_path)
    failure = asyncio.CancelledError if cancel else SetupAbort

    def fault(runtime):
        rebuild = runtime.worker.rebuild

        async def actual_lexical_then_abort(*args, **kwargs):
            await rebuild()
            assert runtime.service.primary.engine is None
            raise failure("controlled_formal_setup_abort")

        runtime.prepare_local = actual_lexical_then_abort

    acquired = observe_runtime(monkeypatch, fault)

    async def scenario():
        try:
            with pytest.raises(failure):
                await asset_benchmark.run_asset_benchmark(config)
            assert len(acquired) == 1
            assert_disposed(acquired[0])
            assert all(not event["payload"].get("embeddings") for event in acquired[0]["events"])
        finally:
            for observation in acquired:
                if not observation["runtime"]._closed:
                    await observation["runtime"].close()

    asyncio.run(scenario())


def test_empty_offer_cannot_hide_a_real_native_observation_in_zero_slot_costs(tmp_path):
    actual = native_report(tmp_path / "actual")
    empty = native_report(tmp_path / "empty", empty_offer=True)
    data = copy.deepcopy(empty.data)
    data["results"][0]["embeddings"] = copy.deepcopy(actual.data["results"][0]["embeddings"])
    data["results"][0]["result"]["embeddings"] = copy.deepcopy(data["results"][0]["embeddings"])
    assert data["results"][0]["observed_costs"][0]["value"]["calls"] == 0
    with pytest.raises(ValueError, match="empty_catalog_evidence"):
        asset_comparison.validate_report_evidence(replace(empty, data=data))


def test_source_empty_offer_cannot_hide_native_work_without_a_dispatched_request(
    development_input, tmp_path, monkeypatch
):
    import json

    from relaymind.evaluation import asset_diagnostic_runner as source
    from test_asset_diagnostic import save_input

    catalog_path = development_input / "catalog.json"
    catalog = json.loads(catalog_path.read_text())
    catalog["assets"] = []
    catalog_path.write_text(json.dumps(catalog))
    path = development_input / "diagnostic.json"
    document = json.loads(path.read_text())
    document["catalog_digest"] = canonical_digest(catalog)
    for case in document["cases"]:
        case.update(reference_class="negative", acceptable_asset_ids=[], source_asset_ids=[])
    save_input(development_input, document)
    _, empty = source_pair(development_input, tmp_path, monkeypatch)
    actual = native_report(tmp_path / "actual")
    changed = copy.deepcopy(empty)
    row = changed["rows"][0]
    assert row["result"]["decision"] == "empty_catalog" and row["request"] is None
    row["embeddings"] = copy.deepcopy(actual.data["results"][0]["embeddings"])
    row["result"]["embeddings"] = copy.deepcopy(row["embeddings"])
    assert row["observed_costs"] == []
    with pytest.raises(ValueError, match="invalid_development_report"):
        source.compare_source_benchmarks(changed, changed)
