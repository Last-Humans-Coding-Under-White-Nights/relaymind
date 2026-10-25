"""RM17/RM25/RM27: declared test-fixture priors never fabricate runs."""

import hashlib
import json
from datetime import timedelta

import pytest

from gateway_helpers import (  # noqa: F401 - close_gateway_stores is an autouse fixture
    STAGING_OK,
    build_gateway,
    close_gateway_stores,
    route_turn,
    run,
    text_turn,
    tool_turn,
)
from relaymind.store import RunStore


def fixture_source(tmp_path):
    gateway = build_gateway(
        tmp_path / "gateway",
        [
            route_turn("check-service-health"),
            tool_turn("get_service_health", service="staging-api"),
            text_turn(STAGING_OK),
            text_turn(STAGING_OK),
        ],
    )
    outcome, _ = run(gateway)
    events = [e.to_json_object() for e in gateway.store.events(outcome.run_id)]
    from dataclasses import asdict

    from relaymind.orchestrator.local_loop import LOCAL_OPTIONS

    configuration = {
        "local": {
            "model_id": gateway.orchestrator.model.model_id,
            "artifact_sha256": "a" * 64,
            "manifest_sha256": "b" * 64,
            "runtime_banner": "declared test fixture",
            "runtime_argv": ["fixture"],
        },
        "external": None,
        "local_generation_options": asdict(LOCAL_OPTIONS),
        "budgets": gateway.orchestrator.budgets.to_json_object(),
    }
    source = {
        "kind": "relaymind.correctness_corpus",
        "prior_configuration": configuration,
        "application": {"head": "fixture"},
        "records": {"case/auto": {"run_id": outcome.run_id, "state": outcome.state.value}},
        "traces": {outcome.run_id: events},
    }
    path = tmp_path / "source.json"
    path.write_text(json.dumps(source))
    bundle = {
        "schema_version": 1,
        "kind": "relaymind.placement_prior",
        "partitions": [
            {
                "configuration": configuration,
                "include_outcomes": True,
                "sources": [
                    {
                        "file": path.name,
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                        "run_ids": [outcome.run_id],
                    }
                ],
            }
        ],
    }
    prior = tmp_path / "prior.json"
    prior.write_text(json.dumps(bundle))
    return gateway, prior, configuration, source, bundle


def test_prior_import_is_atomic_idempotent_and_creates_no_runs(tmp_path):
    from relaymind.evaluation.prior import load_prior

    gateway, path, configuration, source, bundle = fixture_source(tmp_path)
    prior = load_prior(path, configuration)
    with RunStore(tmp_path / "new.sqlite", clock=gateway.store.clock) as store:
        result = prior.seed(store)
        assert result["outcomes"] == 1
        assert result["timings"] == 4
        assert store.unfinished_runs(()) == []
        assert prior.seed(store) == result
        original = gateway.store.outcome_estimate(
            gateway.orchestrator.model.model_id, "check-service-health"
        )
        assert (
            store.outcome_estimate(gateway.orchestrator.model.model_id, "check-service-health")
            == original
        )
        assert all(s.run_id is None for s in store.model_timings())
        gateway.store.clock.advance(timedelta(days=8).total_seconds())
        with pytest.raises(ValueError, match="retention"):
            prior.seed(store)


@pytest.mark.parametrize(
    "change", ["counterfactual", "sequence", "terminal", "digest", "configuration", "duplicate"]
)
def test_bad_source_rejected_before_import(tmp_path, change):
    from relaymind.evaluation.prior import load_prior

    _, path, configuration, source, bundle = fixture_source(tmp_path)
    run_id = next(iter(source["traces"]))
    if change == "counterfactual":
        source["kind"] = "relaymind.counterfactual"
    if change == "sequence":
        source["traces"][run_id][1]["seq"] = 100
    if change == "terminal":
        source["traces"][run_id].pop()
    if change == "configuration":
        configuration = {"wrong": True}
    if change == "duplicate":
        bundle["partitions"][0]["sources"] *= 2
    source_path = tmp_path / "source.json"
    source_path.write_text(json.dumps(source))
    if change != "digest":
        bundle["partitions"][0]["sources"][0]["sha256"] = hashlib.sha256(
            source_path.read_bytes()
        ).hexdigest()
    else:
        bundle["partitions"][0]["sources"][0]["sha256"] = "0" * 64
    path.write_text(json.dumps(bundle))
    with pytest.raises(ValueError):
        load_prior(path, configuration)


def test_invalid_direct_import_rolls_back_and_conflicting_reimport_fails(tmp_path):
    """RM17: bulk boundary itself validates provenance/outcome counts atomically."""
    from relaymind.evaluation.prior import load_prior

    gateway, path, config, _, _ = fixture_source(tmp_path)
    prior = load_prior(path, config)
    bad = [{**prior.outcomes[0], "local_completed": True}]
    with RunStore(tmp_path / "invalid.sqlite", clock=gateway.clock) as store:
        with pytest.raises(ValueError):
            store.import_placement_prior(
                bundle_digest=prior.bundle_digest,
                configuration_digest=prior.configuration_digest,
                timings=list(prior.timings),
                outcomes=bad,
                report=prior.provenance,
            )
        assert store.model_timings() == []
        prior.seed(store)
        with pytest.raises(ValueError, match="conflict"):
            store.import_placement_prior(
                bundle_digest="0" * 64,
                configuration_digest=prior.configuration_digest,
                timings=list(prior.timings),
                outcomes=list(prior.outcomes),
                report=prior.provenance,
            )


def test_each_fresh_store_has_same_prior_and_nullable_usage(tmp_path):
    """RM25/RM27: absent provider usage stays unknown across independent stores."""
    from relaymind.evaluation.prior import load_prior

    gateway, path, config, _, _ = fixture_source(tmp_path)
    prior = load_prior(path, config)
    with (
        RunStore(tmp_path / "a.sqlite", clock=gateway.clock) as a,
        RunStore(tmp_path / "b.sqlite", clock=gateway.clock) as b,
    ):
        assert prior.seed(a) == prior.seed(b)
        assert a.model_timings() == b.model_timings()
        assert [s.input_tokens for s in a.model_timings()] == [
            s.input_tokens for s in prior.timings
        ]


def test_imported_outcomes_enable_expected_wait_without_fake_runs(tmp_path):
    """RM27: timing plus actual terminal imported history evaluates the inequality."""
    from relaymind.evaluation.prior import load_prior
    from relaymind.orchestrator.placement import Placement, PlacementSettings, Prediction

    local_dir = tmp_path / "local"
    local_dir.mkdir()
    local, path, config, source, bundle = fixture_source(local_dir)
    from relaymind.adapters import GenerationFailed

    gateway = build_gateway(
        tmp_path / "external",
        [route_turn("check-service-health"), [GenerationFailed(code="offline", message="offline")]],
        external_turns=[
            tool_turn("get_service_health", service="staging-api"),
            text_turn(STAGING_OK),
        ],
        degraded_mode=True,
    )
    outcome, events = run(gateway)
    from dataclasses import asdict

    from relaymind.orchestrator.local_loop import EXTERNAL_OPTIONS

    config["external"] = {
        "base_url": "https://example.invalid/v1",
        "model": gateway.orchestrator.external.model_id,
        "context_limit": 128000,
        "request_options": {},
        "generation_options": asdict(EXTERNAL_OPTIONS),
    }
    source["records"]["external/auto"] = {"run_id": outcome.run_id, "state": outcome.state.value}
    source["traces"][outcome.run_id] = [e.to_json_object() for e in events]
    source_path = local_dir / "source.json"
    source_path.write_text(json.dumps(source))
    ref = bundle["partitions"][0]["sources"][0]
    ref["run_ids"].append(outcome.run_id)
    ref["sha256"] = hashlib.sha256(source_path.read_bytes()).hexdigest()
    path.write_text(json.dumps(bundle))
    prior = load_prior(path, config)
    with RunStore(tmp_path / "prior.sqlite", clock=local.clock) as store:
        prior.seed(store)
        external_model = gateway.orchestrator.external.model_id
        estimate = store.outcome_estimate(
            local.orchestrator.model.model_id, "check-service-health", external_model=external_model
        )
        assert estimate.local_completed == 1
        assert estimate.external_completed == 1
        placement = Placement(store, PlacementSettings(policy="expected_wait"))
        prediction = Prediction(10, {"model": local.orchestrator.model.model_id})
        decision = placement.admit(
            prediction,
            mode="auto",
            external_model=external_model,
            stage_seconds=4,
            run_seconds=60,
            skill_id="check-service-health",
        )
        assert decision.inputs["local_success_rate"] == 0.5
        assert decision.inputs["expected_external_ms"] == 0
        assert decision.reason == "expected_wait"


def test_conflicting_derived_rows_cannot_reuse_same_bundle_identity(tmp_path):
    """RM17: same digest cannot bless a changed projection on re-import."""
    from relaymind.evaluation.prior import load_prior

    gateway, path, config, _, _ = fixture_source(tmp_path)
    prior = load_prior(path, config)
    with RunStore(tmp_path / "identity.sqlite", clock=gateway.clock) as store:
        prior.seed(store)
        changed = [{**prior.outcomes[0], "local_completed": 0, "local_escalated": 1}]
        with pytest.raises(ValueError, match="conflict"):
            store.import_placement_prior(
                bundle_digest=prior.bundle_digest,
                configuration_digest=prior.configuration_digest,
                timings=list(prior.timings),
                outcomes=changed,
                report=prior.provenance,
            )


def test_report_names_each_timing_source_and_retained_counts(tmp_path):
    """RM25: each imported sample retains byte digest, source run and call identity."""
    from relaymind.evaluation.prior import load_prior

    gateway, path, config, _, _ = fixture_source(tmp_path)
    prior = load_prior(path, config)
    assert len(prior.provenance["timing_sources"]) == 4
    assert len({r["call_id"] for r in prior.provenance["timing_sources"]}) == 4
    assert prior.provenance["retained_timings"] == 4
    assert prior.provenance["retained_outcomes"] == 1


def test_seed_report_names_missing_expected_wait_prerequisites(tmp_path):
    """RM27: a timing-only prior cannot imply observed expected-wait applicability."""
    from helpers import load_script_module
    from relaymind.evaluation.prior import load_prior

    session = load_script_module("evaluation_session")
    gateway, path, config, _, bundle = fixture_source(tmp_path)
    bundle["partitions"][0]["include_outcomes"] = False
    path.write_text(json.dumps(bundle))
    prior = load_prior(path, config)
    with RunStore(tmp_path / "timing-only.sqlite", clock=gateway.clock) as store:
        prior.seed(store)
        report = session.prerequisites(prior, store)
    assert all("local_outcomes" in row["missing"] for row in report)
    assert all("external_model" in row["missing"] for row in report)
    assert all(row["external_completed"] == 0 for row in report)


def rewrite_source(path, source, bundle):
    source_path = path.parent / "source.json"
    source_path.write_text(json.dumps(source))
    bundle["partitions"][0]["sources"][0]["sha256"] = hashlib.sha256(
        source_path.read_bytes()
    ).hexdigest()
    path.write_text(json.dumps(bundle))


@pytest.mark.parametrize(
    "change", ["accepted_model", "call_model", "local_option", "request_option", "structure"]
)
@pytest.mark.parametrize("selected", [True, False])
def test_prior_binds_observed_models_options_in_every_partition(tmp_path, change, selected):
    """RM25/RM27: exact configuration labels must agree with their source traces."""
    from copy import deepcopy

    from relaymind.evaluation.prior import load_prior

    _, path, config, source, bundle = fixture_source(tmp_path)
    events = next(iter(source["traces"].values()))
    if not selected:
        # Keep a valid selected partition; the invalid second partition must still fail.
        other = deepcopy(bundle["partitions"][0])
        other["configuration"]["local"]["runtime_banner"] = "other test runtime"
        other["sources"] = []
        requested = other["configuration"]
        bundle["partitions"].append(other)
    else:
        requested = config
    if change == "accepted_model":
        config["local"]["model_id"] = "wrong-local"
    elif change == "call_model":
        for e in events:
            if e["event_type"] in ("model.started", "model.finished"):
                e["payload"]["model"] = "wrong-local"
        for call in events[-1]["payload"]["model_calls"]:
            call["model"] = "wrong-local"
    elif change == "local_option":
        events[0]["payload"]["answer_options"] = {"temperature": 0.75}
    elif change == "request_option":
        next(e for e in events if e["event_type"] == "model.started")["payload"][
            "request_options"
        ] = {"reasoning_effort": "high"}
    else:
        config["local"]["artifact_sha256"] = "not a digest"
    rewrite_source(path, source, bundle)
    with pytest.raises(ValueError):
        load_prior(path, requested)


def test_prior_legacy_missing_accepted_model_uses_actual_calls(tmp_path):
    """RM27: old traces can identify the local model from actual calls, never a guess."""
    from relaymind.evaluation.prior import load_prior

    gateway, path, config, source, bundle = fixture_source(tmp_path)
    events = next(iter(source["traces"].values()))
    events[0]["payload"].pop("local_model", None)
    rewrite_source(path, source, bundle)
    prior = load_prior(path, config)
    assert prior.outcomes[0]["local_model"] == gateway.orchestrator.model.model_id
    config["local"]["model_id"] = "wrong-fallback"
    rewrite_source(path, source, bundle)
    with pytest.raises(ValueError):
        load_prior(path, config)


@pytest.mark.parametrize("selected", [True, False])
@pytest.mark.parametrize("change", ["model", "request_options", "generation_options"])
def test_prior_binds_external_model_and_options(tmp_path, change, selected):
    """RM25/RM27: provider observations cannot cross an exact configuration partition."""
    from dataclasses import asdict

    from relaymind.evaluation.prior import load_prior
    from relaymind.orchestrator.local_loop import EXTERNAL_OPTIONS

    _, path, config, source, bundle = fixture_source(tmp_path)
    gateway = build_gateway(
        tmp_path / "remote",
        [],
        external_turns=[
            tool_turn("get_service_health", service="staging-api"),
            text_turn(STAGING_OK),
        ],
        degraded_mode=True,
    )
    outcome, events = run(gateway, execution_mode="external_only")
    source["records"] = {"external": {"run_id": outcome.run_id, "state": outcome.state.value}}
    source["traces"] = {outcome.run_id: [e.to_json_object() for e in events]}
    bundle["partitions"][0]["sources"][0]["run_ids"] = [outcome.run_id]
    config["external"] = {
        "base_url": "https://example.invalid/v1",
        "model": gateway.orchestrator.external.model_id,
        "context_limit": 128000,
        "request_options": {},
        "generation_options": asdict(EXTERNAL_OPTIONS),
    }
    rewrite_source(path, source, bundle)
    assert load_prior(path, config).timings
    requested = config
    if not selected:
        from copy import deepcopy

        other = deepcopy(bundle["partitions"][0])
        other["configuration"]["local"]["runtime_banner"] = "other runtime"
        other["sources"] = []
        requested = other["configuration"]
        bundle["partitions"].append(other)
    if change == "model":
        config["external"]["model"] = "wrong-remote"
    elif change == "request_options":
        config["external"]["request_options"] = {"reasoning_effort": "high"}
    else:
        source["traces"][outcome.run_id][0]["payload"]["answer_options"] = {"temperature": 0.5}
    rewrite_source(path, source, bundle)
    with pytest.raises(ValueError):
        load_prior(path, requested)


def test_prior_accepts_matching_recorded_provider_options(tmp_path):
    """RM25: an exact provider option match is valid, not a blanket rejection."""
    from dataclasses import asdict

    from relaymind.evaluation.prior import load_prior
    from relaymind.orchestrator.local_loop import EXTERNAL_OPTIONS

    _, path, config, source, bundle = fixture_source(tmp_path)
    gateway = build_gateway(
        tmp_path / "remote", [], external_turns=[text_turn("Fixture")], degraded_mode=True
    )
    outcome, events = run(gateway, execution_mode="external_only")
    traced = [e.to_json_object() for e in events]
    for e in traced:
        if e["event_type"] == "model.started":
            e["payload"]["request_options"] = {"reasoning_effort": "low"}
    config["external"] = {
        "base_url": "https://example.invalid/v1",
        "model": gateway.orchestrator.external.model_id,
        "context_limit": 128000,
        "request_options": {"reasoning_effort": "low"},
        "generation_options": asdict(EXTERNAL_OPTIONS),
    }
    source["records"] = {"remote": {"run_id": outcome.run_id, "state": outcome.state.value}}
    source["traces"] = {outcome.run_id: traced}
    bundle["partitions"][0]["sources"][0]["run_ids"] = [outcome.run_id]
    rewrite_source(path, source, bundle)
    assert load_prior(path, config).timings


@pytest.mark.parametrize("failed", [False, True])
def test_parallel_prior_matches_live_without_local_escalation(tmp_path, failed):
    """RM15/RM27, AC14: parallel branches are not sequential placement attempts."""
    from dataclasses import asdict

    from native_helpers import GatedAdapter
    from relaymind.evaluation.placement_metrics import trace_metrics
    from relaymind.evaluation.prior import load_prior
    from relaymind.orchestrator.local_loop import EXTERNAL_OPTIONS, ConfiguredModel

    _, path, config, source, bundle = fixture_source(tmp_path)
    workflow = "health-and-diagnostic-checklist"
    if failed:
        external = GatedAdapter(turns=[text_turn("Check dependencies.")], locality="external")
        gateway = build_gateway(
            tmp_path / "parallel",
            [route_turn(workflow), text_turn("It is fine."), text_turn("Still fine.")],
            external_model=ConfiguredModel(adapter=external, model_id="gated-external"),
            enabled_workflows={workflow},
            parallel_in_auto=True,
        )
    else:
        gateway = build_gateway(
            tmp_path / "parallel",
            [route_turn(workflow), tool_turn("get_service_health", service="staging-api")],
            external_turns=[
                text_turn("Check dependencies."),
                text_turn(
                    "Observation: "
                    + STAGING_OK
                    + "\nDiagnostic checklist (draft, not log-backed):\n- Check dependencies."
                ),
            ],
            enabled_workflows={workflow},
            parallel_in_auto=True,
        )
    outcome, traced = run(
        gateway, "Run the health checklist for staging-api.", execution_mode="auto"
    )
    assert outcome.state.value == ("failed" if failed else "completed")
    events = [e.to_json_object() for e in traced]
    assert any(e["event_type"] == "model.started" and "branch" in e["payload"] for e in events)
    model = gateway.orchestrator.external.model_id
    config["external"] = {
        "base_url": "https://example.invalid/v1",
        "model": model,
        "context_limit": 128000,
        "request_options": {},
        "generation_options": asdict(EXTERNAL_OPTIONS),
    }
    source["records"] = {"workflow/auto": {"run_id": outcome.run_id, "state": outcome.state.value}}
    source["traces"] = {outcome.run_id: events}
    bundle["partitions"][0]["sources"][0]["run_ids"] = [outcome.run_id]
    rewrite_source(path, source, bundle)
    prior = load_prior(path, config)
    live = gateway.store.outcome_estimate(
        config["local"]["model_id"], workflow, external_model=model
    )
    assert live.local_completed == live.local_escalated == 0
    assert live.external_completed == (0 if failed else 1)
    assert live.median_external_generations == (None if failed else 2)
    with RunStore(tmp_path / "parallel-prior.sqlite", clock=gateway.clock) as store:
        prior.seed(store)
        assert (
            store.outcome_estimate(config["local"]["model_id"], workflow, external_model=model)
            == live
        )
    # Failed/cancelled external work stays in measured timing/call cost, not successful history.
    external_calls = [
        e for e in events if e["event_type"] == "model.started" and e["actor"] == "external"
    ]
    assert len(external_calls) == (1 if failed else 2)
    assert len([t for t in prior.timings if t.actor == "external"]) == len(external_calls)
    assert len(trace_metrics(events)["external_usage"]) == len(external_calls)
    assert events[-1]["payload"]["local_active_ms"] is None
