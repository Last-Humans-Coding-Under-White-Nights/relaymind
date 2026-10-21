"""Degraded mode: an unavailable local model routes to external by policy (#21).

Section 11, RM04, RM16: an unavailable local model, a missing local
capability or a failed local generation, may route directly to external
only when the operator policy permits degraded mode and external
dispatch is permitted. The trace records degraded mode: route.selected
with mechanism degraded_mode when no decision exists yet, and
handoff.started with degraded_mode and local_failure. Without the
policy the run ends as needs_external (RM04). Deterministic, with
scripted model doubles.
"""

import asyncio
import json

import pytest

from gateway_helpers import (
    EXTERNAL_MODEL_ID,
    STAGING_OK,
    assert_needs_external,
    build_gateway,
    clarify_turn,
    local_eligible_skills,
    route_turn,
    run,
    seed_with,
    text_turn,
    tool_turn,
)
from helpers import BASELINE, make_capabilities, of_type
from relaymind.adapters import GenerationFailed
from relaymind.api.errors import error_from_outcome
from relaymind.orchestrator.run_state import RunState
from relaymind.redaction import Redactor

UNREACHABLE = [GenerationFailed(code="unreachable", message="no route to host")]
HEALTH_EXTERNAL = [tool_turn("get_service_health", call_id="e1", service="staging-api")]

# Before any routing decision exists (the router could not run).
BEFORE_ROUTING = [
    pytest.param([], make_capabilities(tool_calling=False), id="no-tool-calling"),
    pytest.param([], make_capabilities(tokenizer_available=False), id="no-tokenizer"),
    pytest.param([UNREACHABLE], None, id="routing-failed"),
]


@pytest.mark.parametrize(("turns", "capabilities"), BEFORE_ROUTING)
def test_an_unavailable_local_model_routes_to_external_when_policy_permits(
    tmp_path, turns, capabilities
):
    """Section 11: direct to external, and the trace records degraded mode."""
    gateway = build_gateway(
        tmp_path,
        turns,
        capabilities=capabilities,
        external_turns=[text_turn(STAGING_OK)],
        degraded_mode=True,
    )
    outcome, events = run(gateway)

    assert outcome.state is RunState.COMPLETED
    assert outcome.answer == STAGING_OK
    # The gateway recorded its own decision: route external, no skill.
    (selected,) = of_type(events, "route.selected")
    assert selected.payload["mechanism"] == "degraded_mode"
    assert selected.payload["decision"]["route"] == "external"
    assert selected.payload["decision"]["skill_id"] is None
    assert selected.payload["decision"]["reason_code"] == "capability_missing"
    (handoff,) = of_type(events, "handoff.started")
    assert handoff.payload["degraded_mode"] is True
    assert handoff.payload["local_failure"].strip()
    assert handoff.payload["reason_code"] == "capability_missing"
    assert handoff.payload["model"] == EXTERNAL_MODEL_ID
    (completed,) = of_type(events, "run.completed")
    assert completed.payload["final_answer_model"] == EXTERNAL_MODEL_ID
    assert completed.payload["external_calls"] == 1
    assert len(gateway.external.requests) == 1


def test_a_local_generation_failure_after_routing_hands_off_with_the_evidence(tmp_path):
    """Section 11: the failure comes after a decision; the run keeps its evidence."""
    gateway = build_gateway(
        tmp_path,
        [
            route_turn("check-service-health"),
            tool_turn("get_service_health", service="staging-api"),
            UNREACHABLE,
        ],
        external_turns=[text_turn(STAGING_OK)],
        degraded_mode=True,
    )
    outcome, events = run(gateway)

    assert outcome.state is RunState.COMPLETED
    (selected,) = of_type(events, "route.selected")
    assert selected.payload["mechanism"] == "native_function_calling"
    (handoff,) = of_type(events, "handoff.started")
    assert handoff.payload["degraded_mode"] is True
    assert "unreachable" in handoff.payload["local_failure"]
    assert len(handoff.payload["operation_ids"]) == 1
    # The collected evidence was not run again (RM10, RM13).
    assert len(gateway.store.tool_operations(outcome.run_id)) == 1
    assert [e.actor for e in of_type(events, "model.started")][-1] == "external"


@pytest.mark.parametrize(("turns", "capabilities"), BEFORE_ROUTING)
def test_without_the_policy_the_run_ends_as_needs_external(tmp_path, turns, capabilities):
    """RM04: degraded mode is off by default, as before."""
    gateway = build_gateway(
        tmp_path, turns, capabilities=capabilities, external_turns=[text_turn(STAGING_OK)]
    )
    outcome, events = run(gateway)

    assert_needs_external(gateway, outcome, events, "capability_missing")
    assert not of_type(events, "handoff.started")
    assert not of_type(events, "route.selected")


def test_local_only_forbids_degraded_mode(tmp_path):
    """Section 7.4: local_only never calls externally, whatever the policy."""
    gateway = build_gateway(
        tmp_path,
        [UNREACHABLE],
        external_turns=[text_turn(STAGING_OK)],
        degraded_mode=True,
    )
    outcome, events = run(gateway, execution_mode="local_only")

    assert_needs_external(gateway, outcome, events, "capability_missing")
    assert not of_type(events, "handoff.started")
    assert error_from_outcome(outcome).status == 422


def test_no_external_model_means_needs_external_with_the_policy_on(tmp_path):
    """Section 11: degraded mode needs external dispatch to be permitted."""
    gateway = build_gateway(tmp_path, [UNREACHABLE], degraded_mode=True)
    outcome, events = run(gateway)

    assert_needs_external(gateway, outcome, events, "capability_missing")


def test_a_failed_external_model_after_degraded_routing_is_an_explicit_failure(tmp_path):
    """AC07: the local model is down and the external one fails: 503, no invented answer."""
    gateway = build_gateway(
        tmp_path,
        [UNREACHABLE],
        external_turns=[[GenerationFailed(code="down", message="x")]],
        degraded_mode=True,
    )
    outcome, events = run(gateway)

    assert outcome.error_code == "external_failed"
    assert outcome.answer is None
    assert not of_type(events, "answer.delta")
    (failed,) = of_type(events, "run.failed")
    assert failed.payload["reason_code"] == "capability_missing"
    assert error_from_outcome(outcome).status == 503


def test_an_ordinary_handoff_does_not_claim_degraded_mode(tmp_path):
    """RM16: the flag appears only when the local model was unavailable."""
    gateway = build_gateway(
        tmp_path,
        [],
        external_turns=[*HEALTH_EXTERNAL, text_turn(STAGING_OK)],
        degraded_mode=True,
    )
    outcome, events = run(gateway, execution_mode="external_only")

    assert outcome.state is RunState.COMPLETED
    (handoff,) = of_type(events, "handoff.started")
    assert "degraded_mode" not in handoff.payload
    assert "local_failure" not in handoff.payload


def test_a_degraded_handoff_after_a_clarification_records_its_own_decision(tmp_path):
    """Section 11: the resumed stage has no routing decision yet; the gateway records one.

    The clarify decision belongs to the stage before the wait (section
    5). When the routing generation of the resumed stage fails, the trace
    shows route.selected with mechanism degraded_mode before
    handoff.started, and the outcome reports the external decision.
    """
    gateway = build_gateway(
        tmp_path,
        [clarify_turn(), UNREACHABLE],
        external_turns=[text_turn(STAGING_OK)],
        degraded_mode=True,
    )
    orchestrator = gateway.orchestrator
    waiting = asyncio.run(orchestrator.run_text("Is the service healthy?"))
    assert waiting.state is RunState.WAITING_INPUT
    version = gateway.store.get_run(waiting.run_id).version
    outcome = asyncio.run(orchestrator.resume(waiting.run_id, version, "staging-api"))
    events = gateway.store.events(outcome.run_id)

    assert outcome.state is RunState.COMPLETED
    assert [e.payload["mechanism"] for e in of_type(events, "route.selected")] == [
        "native_function_calling",
        "degraded_mode",
    ]
    types = [e.event_type for e in events]
    assert types.index("clarification.answered") < max(
        i for i, t in enumerate(types) if t == "route.selected"
    )
    assert max(i for i, t in enumerate(types) if t == "route.selected") < types.index(
        "handoff.started"
    )
    assert outcome.decision.route.value == "external"
    assert outcome.decision.reason_code == "capability_missing"


def test_the_local_failure_in_the_trace_is_scrubbed_of_known_secrets(tmp_path):
    """RM12, section 8: a secret seen in a tool result never reaches local_failure.

    handoff.started records the cause of the local failure, which is
    adapter text like the error message of model.finished, so the run's
    known secret values are scrubbed from it.
    """
    secret = "svc-account:K9x2-live-7731"
    services = json.loads(BASELINE.read_text("utf-8"))["services"]
    services["staging-api"]["logs"][0].update(source=secret)
    gateway = build_gateway(
        tmp_path,
        [
            route_turn("investigate-performance"),
            tool_turn("get_recent_logs", call_id="c1", service="staging-api", limit=3),
            [GenerationFailed(code=f"refused {secret}", message="x")],
        ],
        skill_roots=local_eligible_skills(tmp_path),
        seed=seed_with(tmp_path, services=services),
        redactor=Redactor(secret_fields={"source"}),
        external_turns=[[GenerationFailed(code="down", message="x")]],
        external_capabilities=make_capabilities(tokenizer_available=False),
        degraded_mode=True,
    )
    outcome, events = run(gateway, "Why is staging-api slow right now?")

    assert outcome.error_code == "external_failed"
    (handoff,) = of_type(events, "handoff.started")
    assert handoff.payload["degraded_mode"] is True
    assert handoff.payload["local_failure"].startswith("local generation failed: refused")
    assert secret not in handoff.payload["local_failure"]
