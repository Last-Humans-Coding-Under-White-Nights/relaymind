"""RM17/RM25/RM27: declared test-fixture priors never fabricate runs."""

import hashlib
import json
from datetime import timedelta

import pytest

from gateway_helpers import STAGING_OK, build_gateway, route_turn, run, text_turn, tool_turn
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
    configuration = {"test_fixture": True}
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
