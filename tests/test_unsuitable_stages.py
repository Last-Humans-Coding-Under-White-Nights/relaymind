"""Unsuitable detection from local stages (#170; RM26, RM27; spec 0.48 section 14.4.7).

The phase 2 run measured a constrained configuration `ready` in every case,
although its local stages kept spending the whole active_seconds budget. Spec
0.48 records an outcome for each local stage, always, and with
placement.detect_stages true classifies a ready model unsuitable when its
three latest local stages all ran out of local time and the latest is younger
than placement.probe_after_seconds. A stale streak expires, so the next local
stage measures the model again, and a stage that fits ends the streak. The
key defaults to false: on the recorded evidence the unsuitable path was not
better (section 14.4.7), so the default keeps the spec 0.47 behavior.

Deterministic: scripted model doubles and a fake clock (section 13).
"""

import json
import re
from datetime import timedelta

import pytest

from api_helpers import call, make_app
from gateway_helpers import (
    EXTERNAL_MODEL_ID,
    LOCAL_MODEL_ID,
    STAGING_OK,
    build_gateway,
    health_tail,
    route_turn,
    run,
    text_turn,
    tool_turn,
)
from helpers import T0, FakeClock, of_type
from relaymind.adapters import GenerationFailed
from relaymind.api.app import ChatSettings
from relaymind.orchestrator.budgets import LocalBudgets
from relaymind.orchestrator.local_status import (
    STAGE_STREAK,
    LocalModelClass,
    LocalStatus,
    stage_status,
)
from relaymind.orchestrator.placement import PlacementSettings
from relaymind.orchestrator.route_decision import ExecutionMode
from relaymind.orchestrator.run_state import RunState
from relaymind.store.outcomes import StageOutcome, project_stage_outcome
from relaymind.store.run_store import utc_iso
from test_ac02_handoff import ANSWER as COMPLEX_ANSWER
from test_ac02_handoff import PROMPT as COMPLEX_PROMPT

ACTIVE = 4.0
PROBE_AFTER = 600.0


def slow_routing(clock: FakeClock, seconds: float = 4.5):
    """Each routing generation takes seconds on the fake clock: over active_seconds alone."""

    def tick(request):
        if request.generation_id.endswith("routing"):
            clock.advance(seconds)

    return tick


def gateway_with(
    tmp_path, turns, *, clock, external_turns=(), detect=True, detect_stages=True, **options
):
    return build_gateway(
        tmp_path,
        turns,
        clock=clock,
        external_turns=list(external_turns),
        detect=detect,
        detect_stages=detect_stages,
        **options,
    )


def time_out_locally(gateway, count: int) -> None:
    """count local_only runs whose routing generation spends the whole stage budget."""
    for _ in range(count):
        outcome, events = run(gateway, execution_mode="local_only")
        assert outcome.state is RunState.FAILED
        assert of_type(events, "run.failed")[0].payload["budget"] == "active_seconds"


def outcome(minutes: float, ran_out: bool, run_id: str = "run_x") -> StageOutcome:
    return StageOutcome(
        run_id=run_id, ran_out_of_time=ran_out, ended_at=T0 + timedelta(minutes=minutes)
    )


# -- the rule (pure) -------------------------------------------------------------------


def rule(outcomes, *, now_minutes: float):
    return stage_status(
        list(reversed(outcomes)),  # the store returns the newest first
        now=T0 + timedelta(minutes=now_minutes),
        probe_after_seconds=PROBE_AFTER,
        active_seconds=ACTIVE,
    )


def test_the_streak_is_three_stages():
    """Spec 0.48: the three latest local stages decide."""
    assert STAGE_STREAK == 3


def test_three_recent_stages_that_ran_out_of_time_are_unsuitable():
    status = rule([outcome(0, True), outcome(1, True), outcome(2, True)], now_minutes=3)
    assert status is not None and status.status == LocalModelClass.UNSUITABLE
    assert status.reason == (
        "the last 3 local stages ran out of local time (active_seconds 4 s);"
        " the latest ended at 2026-10-20T03:02:00Z"
    )


@pytest.mark.parametrize(
    "outcomes",
    [
        [],
        [outcome(0, True), outcome(1, True)],
        # A stage that fits ends the streak; an older one does not count.
        [outcome(0, True), outcome(1, True), outcome(2, True), outcome(3, False)],
        [outcome(0, True), outcome(1, False), outcome(2, True), outcome(3, True)],
    ],
)
def test_without_a_streak_of_three_the_rule_says_nothing(outcomes):
    assert rule(outcomes, now_minutes=4) is None


def test_a_streak_expires_after_probe_after_seconds():
    """RM26: the class is a measurement, never a permanent label."""
    streak = [outcome(0, True), outcome(1, True), outcome(2, True)]
    assert rule(streak, now_minutes=11.99) is not None
    assert rule(streak, now_minutes=12) is None


def test_one_more_stage_that_runs_out_of_time_renews_an_expired_streak():
    """The trial stage after expiry: one more time-out is enough, so it costs one stage."""
    streak = [outcome(0, True), outcome(1, True), outcome(2, True), outcome(30, True)]
    assert rule(streak, now_minutes=31) is not None


# -- what the store records for a local stage ----------------------------------------


def stage_outcomes(gateway):
    return gateway.store.latest_local_stage_outcomes(LOCAL_MODEL_ID, limit=10)


def test_a_local_only_stage_that_spends_active_seconds_ran_out_of_time(tmp_path):
    clock = FakeClock()
    gateway = gateway_with(
        tmp_path, [route_turn("check-service-health")], clock=clock, on_generate=slow_routing(clock)
    )
    outcome_, events = run(gateway, execution_mode="local_only")

    (recorded,) = stage_outcomes(gateway)
    assert recorded.run_id == outcome_.run_id and recorded.ran_out_of_time is True
    (failed,) = of_type(events, "run.failed")
    assert utc_iso(recorded.ended_at) == failed.timestamp


def test_an_auto_handoff_for_active_seconds_records_its_budget(tmp_path):
    """Spec 0.48 section 8: handoff.started names the limit that stopped the local stage."""
    clock = FakeClock()
    gateway = gateway_with(
        tmp_path,
        [route_turn("check-service-health")],
        clock=clock,
        on_generate=slow_routing(clock),
        external_turns=[
            tool_turn("get_service_health", service="staging-api"),
            text_turn(STAGING_OK),
        ],
    )
    outcome_, events = run(gateway)

    assert outcome_.state is RunState.COMPLETED
    (handoff,) = of_type(events, "handoff.started")
    assert handoff.payload["reason_code"] == "local_budget"
    assert handoff.payload["budget"] == "active_seconds"
    (recorded,) = stage_outcomes(gateway)
    assert recorded.ran_out_of_time is True and utc_iso(recorded.ended_at) == handoff.timestamp


def test_a_handoff_for_another_reason_has_no_budget_and_fits(tmp_path):
    """A complex skill hands off without spending the stage: the device was not the limit."""
    gateway = gateway_with(
        tmp_path,
        [route_turn("investigate-performance")],
        clock=FakeClock(),
        external_turns=[
            tool_turn("get_latest_deployment", call_id="e1", service="staging-api"),
            tool_turn("get_metrics", call_id="e2", service="staging-api", window_minutes=60),
            tool_turn("get_recent_logs", call_id="e3", service="staging-api", limit=3),
            text_turn(COMPLEX_ANSWER),
        ],
    )
    _, events = run(gateway, COMPLEX_PROMPT)

    (handoff,) = of_type(events, "handoff.started")
    assert "budget" not in handoff.payload
    (recorded,) = stage_outcomes(gateway)
    assert recorded.ran_out_of_time is False


def test_a_stage_that_completes_locally_fits(tmp_path):
    gateway = gateway_with(
        tmp_path, [route_turn("check-service-health"), *health_tail()], clock=FakeClock()
    )
    outcome_, _ = run(gateway)

    assert outcome_.state is RunState.COMPLETED
    (recorded,) = stage_outcomes(gateway)
    assert recorded.ran_out_of_time is False


def test_a_generation_limit_measures_the_model_not_the_device(tmp_path):
    """Spec 0.48: a stop on local_generations fits; only time limits ran out of time."""
    gateway = gateway_with(
        tmp_path,
        [
            route_turn("check-service-health"),
            tool_turn("get_service_health", service="staging-api"),
        ],
        clock=FakeClock(),
        budgets=LocalBudgets(local_generations=1),
    )
    outcome_, events = run(gateway, execution_mode="local_only")

    assert of_type(events, "run.failed")[0].payload["budget"] == "local_generations"
    (recorded,) = stage_outcomes(gateway)
    assert recorded.run_id == outcome_.run_id and recorded.ran_out_of_time is False


def test_external_only_records_no_local_stage(tmp_path):
    gateway = gateway_with(tmp_path, [], clock=FakeClock(), external_turns=[text_turn("Hi.")])
    run(gateway, "Say hi.", execution_mode="external_only")
    assert stage_outcomes(gateway) == []


LOCAL_STAGE = {"local_generation": True, "execution_mode": "local_only"}


@pytest.mark.parametrize(
    ("kind", "payload"),
    [
        pytest.param(
            "run.failed",
            {"error_code": "internal_error", "local_active_ms": 4100},
            id="internal_error",
        ),
        pytest.param(
            "run.failed",
            {"error_code": "interrupted", "local_active_ms": 4100},
            id="interrupted",
        ),
        # capability_missing is a reason code (RM04): with needs_external when
        # degraded mode is off, and on handoff.started when it is on.
        pytest.param(
            "run.failed",
            {
                "error_code": "needs_external",
                "reason_code": "capability_missing",
                "local_active_ms": 4100,
            },
            id="needs_external-capability_missing",
        ),
        pytest.param(
            "handoff.started",
            {
                "reason_code": "capability_missing",
                "degraded_mode": True,
                "local_failure": "unreachable",
                "local_active_ms": 4100,
            },
            id="degraded-handoff",
        ),
    ],
)
def test_a_failure_that_says_nothing_about_the_device_records_no_outcome(kind, payload):
    """Spec 0.48 section 14.4.7: no evidence, so neither ran out of time nor fit."""
    assert project_stage_outcome(LOCAL_STAGE, kind, payload) is None


@pytest.mark.parametrize("degraded", [False, True], ids=["needs_external", "degraded"])
def test_a_local_failure_between_time_outs_does_not_end_the_streak(tmp_path, degraded):
    """A crashed local model is not a stage that fit, so the streak carries on."""
    clock = FakeClock()
    routings = 0

    def tick(request):
        nonlocal routings
        if request.generation_id.endswith("routing"):
            routings += 1
            if routings != 3:  # the third run's routing fails at once
                clock.advance(4.5)

    gateway = gateway_with(
        tmp_path,
        [
            route_turn("check-service-health"),
            route_turn("check-service-health"),
            [GenerationFailed(code="unreachable", message="no route to host")],
            route_turn("check-service-health"),
        ],
        clock=clock,
        on_generate=tick,
        external_turns=[text_turn(STAGING_OK)],
        degraded_mode=degraded,
    )
    time_out_locally(gateway, STAGE_STREAK - 1)
    _, events = run(gateway, execution_mode="auto" if degraded else "local_only")
    ended = of_type(events, "handoff.started" if degraded else "run.failed")[0]
    assert ended.payload["reason_code"] == "capability_missing"

    assert [o.ran_out_of_time for o in stage_outcomes(gateway)] == [True] * (STAGE_STREAK - 1)
    time_out_locally(gateway, 1)
    assert gateway.orchestrator.local_status.status == LocalModelClass.UNSUITABLE


# -- the class the gateway reports and uses ------------------------------------------


def test_three_stages_that_ran_out_of_time_make_auto_managed_external(tmp_path):
    """RM26: the fourth request runs as managed external execution, with no local call."""
    clock = FakeClock()
    gateway = gateway_with(
        tmp_path,
        [route_turn("check-service-health")] * 3,
        clock=clock,
        on_generate=slow_routing(clock),
        external_turns=[
            tool_turn("get_service_health", service="staging-api"),
            text_turn(STAGING_OK),
        ],
    )
    time_out_locally(gateway, STAGE_STREAK)
    local_calls = len(gateway.adapter.requests)

    status = gateway.orchestrator.local_status
    assert status.status == LocalModelClass.UNSUITABLE
    assert status.reason.startswith("the last 3 local stages ran out of local time")

    outcome_, events = run(gateway)
    assert outcome_.state is RunState.COMPLETED and outcome_.answer == STAGING_OK
    accepted = events[0].payload
    assert accepted["local_model"] == LOCAL_MODEL_ID
    assert (accepted["local_model_status"], accepted["local_model_reason"]) == (
        "unsuitable",
        status.reason,
    )
    (route,) = of_type(events, "route.selected")
    assert route.payload["decision"]["reason_code"] == "no_local_model"
    assert route.payload["local_model_status"] == "unsuitable"
    assert len(gateway.adapter.requests) == local_calls
    assert {e.payload["model"] for e in of_type(events, "model.started")} == {EXTERNAL_MODEL_ID}


def test_two_stages_that_ran_out_of_time_leave_the_model_ready(tmp_path):
    clock = FakeClock()
    gateway = gateway_with(
        tmp_path,
        [route_turn("check-service-health")] * 2,
        clock=clock,
        on_generate=slow_routing(clock),
    )
    time_out_locally(gateway, STAGE_STREAK - 1)
    assert gateway.orchestrator.local_status == LocalStatus.ready()


def test_local_only_stays_permitted_and_parallel_demo_does_not(tmp_path):
    """Section 7.4: the unsuitable class keeps local_only, as the startup class does."""
    clock = FakeClock()
    gateway = gateway_with(
        tmp_path,
        [route_turn("check-service-health")] * 3,
        clock=clock,
        on_generate=slow_routing(clock),
    )
    time_out_locally(gateway, STAGE_STREAK)

    orchestrator = gateway.orchestrator
    assert orchestrator.mode_refusal(ExecutionMode.LOCAL_ONLY) is None
    assert "unsuitable" in orchestrator.mode_refusal(ExecutionMode.PARALLEL_DEMO)


def test_the_streak_expires_and_the_next_stage_measures_the_model_again(tmp_path):
    """RM26: a stale streak expires; a trial stage that fits keeps the model ready."""
    clock = FakeClock()
    gateway = gateway_with(
        tmp_path,
        [route_turn("check-service-health")] * 3
        + [route_turn("check-service-health"), *health_tail()],
        clock=clock,
        on_generate=slow_routing(clock),
        placement=PlacementSettings(probe_after_seconds=PROBE_AFTER),
    )
    time_out_locally(gateway, STAGE_STREAK)
    assert gateway.orchestrator.local_status.status == LocalModelClass.UNSUITABLE

    clock.advance(PROBE_AFTER)
    assert gateway.orchestrator.local_status == LocalStatus.ready()

    # The trial stage: this routing is fast, and the stage completes locally.
    gateway.adapter.on_generate = None
    outcome_, events = run(gateway)
    assert outcome_.state is RunState.COMPLETED
    assert events[0].payload["local_model_status"] == "ready"
    assert gateway.orchestrator.local_status == LocalStatus.ready()


def test_with_detect_false_the_rule_does_not_apply(tmp_path):
    """Section 14.1: detect false keeps the behavior before spec 0.31."""
    clock = FakeClock()
    gateway = gateway_with(
        tmp_path,
        [route_turn("check-service-health")] * 3,
        clock=clock,
        on_generate=slow_routing(clock),
        detect=False,
    )
    time_out_locally(gateway, STAGE_STREAK)
    assert gateway.orchestrator.local_status == LocalStatus.ready()


def test_by_default_the_rule_does_not_apply_and_outcomes_are_still_recorded(tmp_path):
    """Spec 0.48: detect_stages defaults to false, the spec 0.47 behavior; the evidence stays."""
    clock = FakeClock()
    gateway = build_gateway(
        tmp_path,
        [route_turn("check-service-health")] * 3,
        clock=clock,
        on_generate=slow_routing(clock),
        external_turns=[],
        detect=True,
    )
    assert gateway.orchestrator.detect_stages is False
    time_out_locally(gateway, STAGE_STREAK)

    assert gateway.orchestrator.local_status == LocalStatus.ready()
    assert [o.ran_out_of_time for o in stage_outcomes(gateway)] == [True] * STAGE_STREAK


def test_a_restart_reads_the_recorded_stages(tmp_path):
    """Spec 0.48: outcomes are in the run store, so a restarted gateway applies the rule."""
    clock = FakeClock()
    first = gateway_with(
        tmp_path,
        [route_turn("check-service-health")] * 3,
        clock=clock,
        on_generate=slow_routing(clock),
    )
    time_out_locally(first, STAGE_STREAK)
    first.store.close()

    second = gateway_with(tmp_path, [], clock=clock)
    assert second.orchestrator.local_status.status == LocalModelClass.UNSUITABLE


def test_readiness_reports_the_class_and_its_cause(tmp_path):
    """Section 7.4: readiness shows the measured class, and auto needs the external model."""
    clock = FakeClock()
    gateway = gateway_with(
        tmp_path,
        [route_turn("check-service-health")] * 3,
        clock=clock,
        on_generate=slow_routing(clock),
    )
    time_out_locally(gateway, STAGE_STREAK)
    app = make_app(
        gateway,
        settings=ChatSettings(
            allowed_modes=frozenset({ExecutionMode.AUTO, ExecutionMode.EXTERNAL_ONLY})
        ),
    )

    body = call(app, "GET", "/readyz").json()
    assert body["local_model"]["status"] == "unsuitable"
    assert body["local_model"]["reason"] == gateway.orchestrator.local_status.reason
    assert body["modes"]["auto"] == {"ready": True, "reason": None}


# -- the measurement the hypothesis relies on (section 14.4.7) -----------------------


def external_view(gateway, events):
    """What a managed external run did, without the class fields that name the class.

    Run and operation IDs are random per run, so they are replaced by placeholders.
    """
    view = dict(
        event_types=[e.event_type for e in events],
        route={
            k: v
            for k, v in of_type(events, "route.selected")[0].payload.items()
            if k != "local_model_status"
        },
        handoff=of_type(events, "handoff.started")[0].payload,
        external_requests=[
            ([(m.role, m.content) for m in r.messages], [s.name for s in r.tool_schemas])
            for r in gateway.external.requests
        ],
        answer=of_type(events, "run.completed")[0].payload["answer"],
    )
    text = json.dumps(view, default=str, sort_keys=True)
    return re.sub(r"\b(run|op)_[0-9a-f]{32}\b", r"\1_ID", text)


def test_auto_while_unsuitable_from_stages_is_the_no_local_path(tmp_path):
    """Section 14.4.7: the same request takes the same managed external path as absent."""
    turns = [tool_turn("get_service_health", service="staging-api"), text_turn(STAGING_OK)]
    absent = build_gateway(
        tmp_path / "absent",
        [],
        local_status=LocalStatus.absent(),
        external_turns=list(turns),
        clock=FakeClock(),
    )
    _, absent_events = run(absent)

    clock = FakeClock()
    slow = gateway_with(
        tmp_path / "slow",
        [route_turn("check-service-health")] * 3,
        clock=clock,
        on_generate=slow_routing(clock),
        external_turns=list(turns),
    )
    time_out_locally(slow, STAGE_STREAK)
    clock.now = T0  # the same timestamps in both traces
    _, slow_events = run(slow)

    assert of_type(absent_events, "route.selected")[0].payload["local_model_status"] == "absent"
    assert of_type(slow_events, "route.selected")[0].payload["local_model_status"] == "unsuitable"
    assert external_view(slow, slow_events) == external_view(absent, absent_events)
