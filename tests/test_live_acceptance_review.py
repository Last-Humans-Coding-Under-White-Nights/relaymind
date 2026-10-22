"""Regression checks for live evidence integrity (RM09–RM24, spec section 13)."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from helpers import load_script_module
from test_live_acceptance_script import (
    _swap_result,
    _swap_trace,
)


@pytest.fixture(scope="module")
def harness():
    return load_script_module("live_acceptance")


def event(kind, seq, **payload):
    return {"event_type": kind, "seq": seq, "actor": "local", "elapsed_ms": seq, "payload": payload}


def view(state="completed", **extra):
    return {
        "state": state,
        "error_code": None,
        "reason_code": None,
        "output": None,
        "run_id": "r1",
        "state_version": 3,
        **extra,
    }


def trace_api(events, state=None):
    async def run_to_end(result, *args, **kwargs):
        result.run_ids.append("r1")
        return state or view(), events

    return SimpleNamespace(run_to_end=run_to_end)


def gateway_failures(result):
    assert result.error is None, result.to_json_object()
    return [c for c in result.checks if c.owner == "gateway" and not c.ok]


@pytest.mark.parametrize("partial_checks", [[], [{"owner": "gateway", "ok": True}]])
def test_live_gate_rejects_scenario_errors(tmp_path, monkeypatch, partial_checks):
    """RM16, spec 13: an exception with no checks or only partial checks cannot pass."""
    import test_live_acceptance as live

    document = {
        "scenarios": [
            {
                "name": "broken",
                "status": "error",
                "checks": partial_checks,
                "error": "scenario did not complete",
            }
        ],
        "launches": [],
    }
    monkeypatch.setattr(live.shutil, "which", lambda _: "llama-server")
    monkeypatch.setattr(
        live, "load_script_module", lambda _: SimpleNamespace(run_live=lambda _: document)
    )
    with pytest.raises(AssertionError):
        live.test_gateway_checks_hold_on_the_live_model(tmp_path)


@pytest.mark.parametrize("name", ["ac12_cancel_generation", "ac12_restart"])
def test_a_missed_cancel_or_restart_fails_the_live_gate(harness, tmp_path, monkeypatch, name):
    """AC12, RM14/RM17: finishing before the stop is not a successful interruption test."""
    import test_live_acceptance as live

    ended = [
        event("model.started", 1, call_id="c"),
        event("model.finished", 2, call_id="c"),
        event("run.completed", 3),
    ]
    api = SimpleNamespace(
        cancel=AsyncMock(return_value=httpx.Response(409)),
        view=AsyncMock(return_value=view()),
        ended=AsyncMock(return_value=(view(), ended)),
    )
    monkeypatch.setattr(harness, "_start_generating", AsyncMock(return_value=view()))
    result = asyncio.run(harness.SCENARIOS[name].run(api, restart=AsyncMock(return_value=(api, 0))))
    assert result.status == "error"
    document = {"scenarios": [result.to_json_object()], "launches": []}
    monkeypatch.setattr(live.shutil, "which", lambda _: "llama-server")
    monkeypatch.setattr(
        live, "load_script_module", lambda _: SimpleNamespace(run_live=lambda _: document)
    )
    with pytest.raises(AssertionError):
        live.test_gateway_checks_hold_on_the_live_model(tmp_path)


@pytest.mark.parametrize("skill", ["investigate-performance", "check-service-health"])
def test_ac07_checks_gateway_without_blaming_model_routing(harness, skill):
    """AC07/RM04: bypassed escalation fails; a wrong model skill stays a model finding."""
    events = [
        event("route.selected", 1, decision={"route": "local", "skill_id": skill}),
        event("answer.delta", 2, text="answer"),
        event("run.completed", 3, external_calls=0),
    ]
    result = asyncio.run(harness.SCENARIOS["ac07_needs_external"].run(trace_api(events)))
    failures = gateway_failures(result)
    assert bool(failures) == (skill == "investigate-performance"), result.to_json_object()
    assert any(c.owner == "gateway" for c in result.checks)


def test_ac07_missing_failed_event_is_a_gateway_finding(harness):
    """AC07/RM22: a failed view without its terminal event must not crash the checker."""
    state = view("failed", error_code="needs_external")
    result = asyncio.run(harness.SCENARIOS["ac07_needs_external"].run(trace_api([], state)))
    assert gateway_failures(result)


@pytest.mark.parametrize(
    "damage", ["no_finishes", "one_finish", "duplicate_finish", "missing_branch", "late_finish"]
)
def test_synthesis_requires_every_branch_call_to_finish(harness, damage):
    """AC14/RM15: empty, incomplete, or duplicate completion evidence cannot pass."""
    starts = [
        event("model.started", 1, call_id="a", branch="local_health"),
        event("model.started", 2, call_id="b", branch="external_checklist"),
    ]
    finishes = [event("model.finished", 3, call_id="a"), event("model.finished", 4, call_id="b")]
    if damage == "no_finishes":
        finishes = []
    elif damage == "one_finish":
        finishes.pop()
    elif damage == "duplicate_finish":
        finishes[1] = event("model.finished", 4, call_id="a")
    elif damage == "missing_branch":
        starts.pop()
        finishes.pop()
    elif damage == "late_finish":
        finishes[1]["seq"] = 6
    events = (
        starts
        + finishes
        + [
            event("model.started", 5, call_id="s", branch="synthesis"),
            event("run.completed", 7, workflow={}),
        ]
    )
    result = asyncio.run(harness.SCENARIOS["parallel_demo"].run(trace_api(events)))
    assert gateway_failures(result)


@pytest.mark.parametrize("missing", ["trace", "accepted"])
def test_swap_records_missing_admission_evidence(harness, missing):
    """AC08/RM18/RM22: missing trace data is a gateway finding, not a whole-harness crash."""
    traces = {"r1": _swap_trace("a"), "r2": _swap_trace("b")}
    if missing == "trace":
        del traces["r2"]
    else:
        traces["r2"].pop(0)
    result = harness.ac08_external_swap(
        _swap_result(harness, "external", "r1"),
        _swap_result(harness, "external-second", "r2"),
        traces=traces,
        # The admission check ends the comparison before the files are read.
        configs={"external": "", "external-second": ""},
        endpoints={
            "external": harness.ExternalEndpoint("https://one.invalid/v1", "a", 8192),
            "external-second": harness.ExternalEndpoint(
                "https://two.invalid/v1", "b", 8192, harness.SECOND_EXTERNAL_KEY_ENV
            ),
        },
    )
    assert gateway_failures(result)


@pytest.mark.parametrize("missing_id", [False, True])
def test_handoff_allows_result_replay_but_requires_operation_ids(harness, missing_id):
    """AC02/RM10/RM13: replaying recorded data is allowed; managed events need operation IDs."""
    first = event("tool.finished", 1, operation_id="op", status="succeeded")
    if missing_id:
        first["payload"].pop("operation_id")
    events = [
        first,
        event("handoff.started", 2, operation_ids=["op"]),
        event("tool.finished", 3, operation_id="op", status="succeeded", replayed=True),
    ]
    result = asyncio.run(harness.SCENARIOS["ac02_handoff"].run(trace_api(events)))
    assert bool(gateway_failures(result)) == missing_id, result.to_json_object()


@pytest.mark.parametrize("target", [None, "absent-db"])
def test_approval_handles_missing_and_absent_targets(harness, monkeypatch, target):
    """AC05/RM09/RM11: invalid approvals fail; a refused absent target changes nothing."""
    before = {"databases": ["demo-orders-db"], "revision": 3}
    events = [event("tool.started", 1, tool_name="delete_demo_database")]

    async def decide(api, result, **kwargs):
        result.run_ids.append("r1")
        result.observations.update(
            sandbox_before=before,
            proposed_arguments={
                "expected_revision": 3,
                **({"database_id": target} if target else {}),
            },
        )
        return view(), events, {"approve": True}

    monkeypatch.setattr(harness, "_decide", decide)
    api = SimpleNamespace(
        sandbox=lambda: before,
        events=AsyncMock(return_value=events),
        resume=AsyncMock(
            return_value=httpx.Response(409, json={"error": {"code": "run_terminal"}})
        ),
    )
    result = asyncio.run(harness.SCENARIOS["ac05_approve"].run(api))
    assert bool(gateway_failures(result)) == (target is None), result.to_json_object()


def test_empty_replay_trace_is_a_gateway_finding(harness):
    """AC11/RM17/RM22: no events cannot count as a valid replay stream."""
    api = SimpleNamespace(
        start_run=AsyncMock(return_value="r1"), ended=AsyncMock(return_value=(view(), []))
    )
    result = asyncio.run(harness.SCENARIOS["ac11_replay_idempotency"].run(api))
    assert gateway_failures(result)


def test_non_json_conflict_response_is_a_gateway_finding(harness):
    """AC11/RM17: an HTML error page is a failed error-contract check, not a parser crash."""
    events = [event("run.completed", 1)]
    api = SimpleNamespace(
        start_run=AsyncMock(return_value="r1"),
        ended=AsyncMock(return_value=(view(), events)),
        events=AsyncMock(side_effect=[[], events]),
        start=AsyncMock(
            side_effect=[
                httpx.Response(202, json={"run_id": "r1"}, headers={"Idempotent-Replayed": "true"}),
                httpx.Response(503, text="<html>unavailable</html>"),
            ]
        ),
    )
    result = asyncio.run(harness.SCENARIOS["ac11_replay_idempotency"].run(api))
    assert gateway_failures(result)


@pytest.mark.parametrize("damage", ["missing_accepted", "wrapped_hit"])
def test_cache_requires_admission_and_a_preceding_hit(harness, damage):
    """AC13/RM24: route at index zero cannot use the trace's last event as its predecessor."""
    route = event(
        "route.selected", 1, decision={"skill_id": "check-service-health"}, routing_cache="hit"
    )
    route["actor"] = "gateway"
    events = [route, event("run.accepted", 2, routing_cache=True), event("cache.hit", 3)]
    if damage == "missing_accepted":
        events.pop(1)
    result = asyncio.run(
        harness.SCENARIOS["routing_cache"].run(
            trace_api(events), prompts=[("health?", "en", "staging-api")], rounds=1, warmup=None
        )
    )
    assert gateway_failures(result)
