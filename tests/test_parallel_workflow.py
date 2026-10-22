"""AC14: the bounded parallel workflow (RM14, RM15, RM16, RM22, RM23, #30).

Spec 0.25 section 4.4: in parallel_demo (and in auto where the operator
policy admits it) a run that routes parallel runs two independent
read-only branches, local_health and external_checklist, joins them, and
asks a new external call to combine the observation and the provisional
checklist. All model output comes from scripted doubles (spec section
13); the sandbox is a disposable copy.
"""

import asyncio
import json
import shutil
import threading

import pytest

from gateway_helpers import (
    build_gateway,
    executed_tools,
    replace_handler,
    route_turn,
    text_turn,
    tool_turn,
)
from helpers import ACCEPTED_PAYLOAD, DEMO_SKILLS_ROOT, FakeClock, ScriptedAdapter, of_type
from native_helpers import GatedAdapter
from relaymind.adapters import GenerationFailed, Message, Role
from relaymind.orchestrator.budgets import LocalBudgets
from relaymind.orchestrator.local_loop import ConfiguredModel
from relaymind.orchestrator.run_state import RunState
from relaymind.store.trace_event import EventType, validate_trace_event
from relaymind.tools import CallerContext
from relaymind.tools.executor import ToolExecutor
from relaymind.tools.sandbox import MUTATE_PERMISSION, READ_PERMISSION
from relaymind.ui.render import render_trace
from relaymind.ui.settings import DataMode, UiSettings
from relaymind.ui.trace_view import trace_view_from

WORKFLOW = "health-and-diagnostic-checklist"
PROMPT = "Run the health checklist for staging-api."
DRAFT = "1. Check the upstream dependencies.\n2. Review recent error logs."
CHECKLIST = "Diagnostic checklist (draft, not log-backed):\n- Check the upstream dependencies."
ANSWER = "Observation: staging-api is healthy (checked at 2026-10-20T02:58:00Z).\n\n" + CHECKLIST
PAYMENTS_ANSWER = (
    "Observation: payments-api is unavailable (checked at 2026-10-20T02:59:00Z).\n\n" + CHECKLIST
)


def local_turns(service="staging-api"):
    return [route_turn(WORKFLOW), tool_turn("get_service_health", service=service)]


def parallel_gateway(tmp_path, *, local=None, external=None, **options):
    options.setdefault("enabled_workflows", {WORKFLOW})
    return build_gateway(
        tmp_path,
        local_turns() if local is None else local,
        external_turns=[text_turn(DRAFT), text_turn(ANSWER)] if external is None else external,
        **options,
    )


def run_parallel(gateway, prompt=PROMPT, **options):
    options.setdefault("execution_mode", "parallel_demo")
    outcome = asyncio.run(gateway.orchestrator.run_text(prompt, **options))
    return outcome, gateway.store.events(outcome.run_id)


def user(text):
    return (Message(role=Role.USER, content=text),)


def index_of(events, event_type, **match):
    for i, event in enumerate(events):
        if event.event_type.value == event_type and all(
            event.payload.get(k) == v for k, v in match.items()
        ):
            return i
    raise AssertionError(f"no {event_type} event with {match}")


# --- AC14: both branches and the synthesis call --------------------------------------


def test_parallel_demo_runs_both_branches_and_the_synthesis_call(tmp_path):
    """AC14, RM15: local health, external draft, then a new external call.

    The run completes with the external model as the final-answer model;
    the local model made the routing and the branch generation, the
    external model made the draft and the synthesis.
    """
    gateway = parallel_gateway(tmp_path)

    outcome, events = run_parallel(gateway)

    assert outcome.state is RunState.COMPLETED
    assert outcome.answer == ANSWER
    record = gateway.store.get_run(outcome.run_id)
    assert (record.state, record.error_code) == ("completed", None)
    assert executed_tools(events) == ["get_service_health"]
    calls = [(e.payload["actor"], e.payload["purpose"]) for e in of_type(events, "model.started")]
    assert sorted(calls) == sorted(
        [("local", "routing"), ("local", "skill"), ("external", "draft"), ("external", "external")]
    )
    assert len(gateway.external.requests) == 2
    assert len(gateway.adapter.requests) == 2
    (completed,) = of_type(events, "run.completed")
    assert completed.payload["final_answer_model"] == "double-external"
    assert completed.payload["external_calls"] == 2
    assert len(completed.payload["model_calls"]) == 4
    # The external model published the answer after the join (RM20).
    deltas = of_type(events, "answer.delta")
    assert {e.actor for e in deltas} == {"external"}
    assert "".join(e.payload["text"] for e in deltas) == ANSWER
    assert gateway.store.active_run_id() is None


def test_the_run_moves_through_parallel_running_and_then_external_running(tmp_path):
    """RM15, RM17, section 5: parallel_running joins and hands ownership on."""
    states = []
    gateway = parallel_gateway(tmp_path)

    def watch(request):
        record = gateway.store.get_run(request.generation_id.split("_gen")[0])
        states.append((request.generation_id.rsplit("_", 1)[1], record.state))

    gateway.external.on_generate = watch
    outcome, _ = run_parallel(gateway)

    assert outcome.state is RunState.COMPLETED
    assert states == [("draft", "parallel_running"), ("external", "external_running")]


def test_branches_and_the_synthesis_are_attributable_in_the_trace(tmp_path):
    """RM22, AC14: each model call and tool event names its branch."""
    gateway = parallel_gateway(tmp_path)

    _, events = run_parallel(gateway)

    started = {
        e.payload["purpose"]: e.payload.get("branch") for e in of_type(events, "model.started")
    }
    assert started == {
        "routing": None,
        "skill": "local_health",
        "draft": "external_checklist",
        "external": "synthesis",
    }
    finished = {
        e.payload["purpose"]: e.payload.get("branch") for e in of_type(events, "model.finished")
    }
    assert finished == started
    for kind in ("tool.proposed", "tool.started", "tool.finished"):
        (event,) = of_type(events, kind)
        assert event.payload["branch"] == "local_health"
    for event in events:
        validate_trace_event(event.to_json_object())


def test_the_join_records_the_ownership_transfer_before_the_synthesis(tmp_path):
    """RM14, RM20, AC14: handoff.started at the join; no answer text before it."""
    gateway = parallel_gateway(tmp_path)

    _, events = run_parallel(gateway)

    (handoff,) = of_type(events, "handoff.started")
    assert handoff.actor == "gateway"
    assert handoff.payload["reason_code"] == "parallel_workflow"
    assert handoff.payload["model"] == "double-external"
    assert handoff.payload["workflow"] == WORKFLOW
    assert handoff.payload["allowed_tools"] == []
    (finished_tool,) = of_type(events, "tool.finished")
    assert handoff.payload["operation_ids"] == [finished_tool.payload["operation_id"]]
    join = events.index(handoff)
    assert join > index_of(events, "tool.finished")
    assert join > index_of(events, "model.finished", purpose="draft")
    assert join < index_of(events, "model.started", purpose="external")
    assert join < index_of(events, "answer.delta")


def test_the_local_branch_runs_while_the_external_draft_is_in_flight(tmp_path):
    """RM15, AC14: the branches overlap; neither waits for the other to finish.

    The draft generation waits until the health tool has started, and
    the tool waits until the draft generation has started. Sequential
    branches could not pass both waits.
    """
    entered = threading.Event()
    draft_started = threading.Event()
    seen = {}

    class DraftFirst(ScriptedAdapter):
        async def generate(self, request):
            if request.generation_id.endswith("_draft"):
                draft_started.set()
                seen["tool_entered"] = await asyncio.to_thread(entered.wait, 5)
            async for event in super().generate(request):
                yield event

    external = DraftFirst(turns=[text_turn(DRAFT), text_turn(ANSWER)], locality="external")
    gateway = build_gateway(
        tmp_path,
        local_turns(),
        enabled_workflows={WORKFLOW},
        external_model=ConfiguredModel(adapter=external, model_id="double-external"),
    )
    original = gateway.tools.get("get_service_health").declaration.handler

    def handler(arguments):
        entered.set()
        seen["draft_started"] = draft_started.wait(5)
        return original(arguments)

    replace_handler(gateway, "get_service_health", handler)

    outcome, _ = run_parallel(gateway)

    assert outcome.state is RunState.COMPLETED
    assert seen == {"tool_entered": True, "draft_started": True}


def test_the_draft_request_is_made_from_the_request_alone(tmp_path):
    """RM15: nothing from the other branch reaches a running generation."""
    gateway = parallel_gateway(tmp_path)

    _, _ = run_parallel(gateway)

    draft = gateway.external.requests[0]
    assert draft.tool_schemas == ()
    assert [m.role for m in draft.messages if m.role is Role.TOOL] == []
    text = " ".join(m.content or "" for m in draft.messages)
    assert PROMPT in text
    assert "2026-10-20T02:58:00Z" not in text
    assert "checklist" in draft.messages[0].content.lower()


def test_the_synthesis_gets_the_observation_as_data_and_the_draft_as_provisional(tmp_path):
    """RM12, RM15, RM16: a new request combines the tool result and the labeled draft."""
    gateway = parallel_gateway(tmp_path)

    _, events = run_parallel(gateway)

    synthesis = gateway.external.requests[1]
    assert synthesis.tool_schemas == ()
    tool_messages = [m for m in synthesis.messages if m.role is Role.TOOL]
    assert len(tool_messages) == 1
    assert "2026-10-20T02:58:00Z" in tool_messages[0].content
    (finished,) = of_type(events, "tool.finished")
    assert tool_messages[0].tool_call_id == finished.payload["operation_id"]
    # The draft is the second part of the request: after the observation. It
    # is untrusted model text, so it is a data message, never a system one
    # (RM12), and it is JSON-encoded, so it cannot close its own fence.
    last = synthesis.messages[-1]
    assert last.role is Role.USER
    header, _, body = last.content.partition("\n")
    assert "provisional" in header.lower()
    assert "not log-backed" in header.lower()
    assert json.loads(body) == {"provisional_draft": DRAFT}
    system = "\n".join(m.content or "" for m in synthesis.messages if m.role is Role.SYSTEM)
    assert DRAFT not in system


# --- provenance (RM16) ------------------------------------------------------------------


def test_an_unavailable_service_is_reported_unavailable(tmp_path):
    """RM16, AC14: the observation is the returned status, not a healthy one."""
    gateway = parallel_gateway(
        tmp_path,
        local=local_turns("payments-api"),
        external=[text_turn(DRAFT), text_turn(PAYMENTS_ANSWER)],
    )

    outcome, events = run_parallel(gateway, "Run the health checklist for payments-api.")

    assert outcome.state is RunState.COMPLETED
    assert "unavailable" in outcome.answer
    (finished,) = of_type(events, "tool.finished")
    assert finished.payload["result"]["data"]["status"] == "unavailable"


def test_a_synthesis_that_calls_an_unavailable_service_healthy_is_corrected(tmp_path):
    """RM16: the completion predicate rejects it; a correction call follows."""
    bad = "Observation: payments-api is healthy (checked at 2026-10-20T02:59:00Z).\n\n" + CHECKLIST
    gateway = parallel_gateway(
        tmp_path,
        local=local_turns("payments-api"),
        external=[text_turn(DRAFT), text_turn(bad), text_turn(PAYMENTS_ANSWER)],
    )

    outcome, events = run_parallel(gateway, "Run the health checklist for payments-api.")

    assert outcome.state is RunState.COMPLETED
    assert outcome.answer == PAYMENTS_ANSWER
    assert "healthy" not in "".join(e.payload["text"] for e in of_type(events, "answer.delta"))
    assert len(gateway.external.requests) == 3


def test_a_checklist_without_its_draft_label_is_not_published(tmp_path):
    """RM16: a drafted checklist is never reported as a log-backed diagnosis."""
    unlabeled = (
        "Observation: staging-api is healthy (checked at 2026-10-20T02:58:00Z).\n- Check logs."
    )
    gateway = parallel_gateway(
        tmp_path,
        external=[text_turn(DRAFT), text_turn(unlabeled), text_turn(unlabeled)],
        budgets=LocalBudgets(external_generations=3),
    )

    outcome, events = run_parallel(gateway)

    assert outcome.state is RunState.FAILED
    assert outcome.error_code == "external_failed"
    assert outcome.reason_code == "parallel_workflow"
    assert of_type(events, "answer.delta") == []


# --- failures settle both branches -------------------------------------------------------


def test_a_local_branch_stop_cancels_the_draft_and_counts_its_call(tmp_path):
    """RM04, RM14, section 12.1: the abandoned draft stays in the inventory.

    The local model answers in text twice (no tool call; one repair), so
    the branch stops with invalid_output while the draft is in flight.
    """
    external = GatedAdapter(turns=[text_turn(DRAFT)], locality="external")
    gateway = build_gateway(
        tmp_path,
        [route_turn(WORKFLOW), text_turn("I think it is fine."), text_turn("Still fine.")],
        enabled_workflows={WORKFLOW},
        external_model=ConfiguredModel(adapter=external, model_id="gated-external"),
    )

    outcome, events = run_parallel(gateway)

    assert outcome.state is RunState.FAILED
    assert outcome.error_code == "needs_external"
    assert outcome.reason_code == "invalid_output"
    (failed,) = of_type(events, "run.failed")
    drafts = [c for c in failed.payload["model_calls"] if c["purpose"] == "draft"]
    assert len(drafts) == 1
    assert failed.payload["external_calls"] == 1
    (draft_done,) = [
        e for e in of_type(events, "model.finished") if e.payload["purpose"] == "draft"
    ]
    assert draft_done.payload["error"]["code"] == "cancelled"
    assert external.cancelled == [drafts[0]["call_id"]]
    assert failed.payload["workflow"]["abandoned_calls"] == [drafts[0]["call_id"]]
    assert not of_type(events, "handoff.started")
    assert of_type(events, "answer.delta") == []
    assert gateway.store.active_run_id() is None


def test_a_failed_draft_fails_the_run_as_external_failed(tmp_path):
    """Section 11: an external branch that cannot finish is external_failed."""
    gateway = parallel_gateway(
        tmp_path,
        external=[[GenerationFailed(code="unreachable", message="no route to host")]],
    )

    outcome, events = run_parallel(gateway)

    assert outcome.state is RunState.FAILED
    assert outcome.error_code == "external_failed"
    assert outcome.reason_code == "parallel_workflow"
    (failed,) = of_type(events, "run.failed")
    assert failed.payload["external_calls"] == 1
    assert of_type(events, "answer.delta") == []
    assert not of_type(events, "handoff.started")
    assert gateway.store.active_run_id() is None


def test_a_blank_draft_fails_the_run(tmp_path):
    """RM15: an empty checklist draft is a failed external branch."""
    gateway = parallel_gateway(tmp_path, external=[text_turn("   ")])

    outcome, _ = run_parallel(gateway)

    assert (outcome.error_code, outcome.reason_code) == ("external_failed", "parallel_workflow")


def test_a_synthesis_that_fails_ends_as_external_failed(tmp_path):
    """Section 11: the synthesis call is an external call like any other."""
    gateway = parallel_gateway(
        tmp_path,
        external=[text_turn(DRAFT), [GenerationFailed(code="timeout", message="slow")]],
    )

    outcome, events = run_parallel(gateway)

    assert (outcome.error_code, outcome.reason_code) == ("external_failed", "parallel_workflow")
    (failed,) = of_type(events, "run.failed")
    assert failed.payload["external_calls"] == 2
    assert of_type(events, "answer.delta") == []


def test_no_room_for_the_external_calls_ends_before_any_branch(tmp_path):
    """RM03, RM04: the entire-run limits leave no room; no external call is made."""
    gateway = parallel_gateway(tmp_path, budgets=LocalBudgets(run_generations=1))

    outcome, events = run_parallel(gateway)

    assert (outcome.error_code, outcome.reason_code) == ("needs_external", "local_budget")
    (failed,) = of_type(events, "run.failed")
    assert failed.payload["dispatch_blocked"] == "run_generations"
    assert gateway.external.requests == []
    assert executed_tools(events) == []


def test_an_external_request_over_the_context_limit_fails_before_any_call(tmp_path):
    """RM13: the draft request is measured like every external request."""
    from helpers import make_capabilities

    gateway = parallel_gateway(tmp_path, external_capabilities=make_capabilities(context_limit=10))

    outcome, events = run_parallel(gateway)

    assert outcome.error_code == "context_limit_exceeded"
    assert gateway.external.requests == []
    # The draft request is measured before parallel_running: the local
    # branch never starts for a run that cannot dispatch it.
    assert len(gateway.adapter.requests) == 1
    assert executed_tools(events) == []
    assert "parallel_running" not in [e.payload.get("to_state") for e in events]


# --- cancel and restart (AC12, #29, #8) ----------------------------------------------------


def test_cancel_during_the_draft_settles_both_branches(tmp_path):
    """RM14, RM17, AC12: the cancel stops the draft; the local branch is settled."""
    external = GatedAdapter(turns=[text_turn(DRAFT), text_turn(ANSWER)], locality="external")
    gateway = build_gateway(
        tmp_path,
        local_turns(),
        enabled_workflows={WORKFLOW},
        external_model=ConfiguredModel(adapter=external, model_id="gated-external"),
    )

    async def go():
        started = gateway.orchestrator.start(user(PROMPT), execution_mode="parallel_demo")
        await external.started.wait()
        # Let the local branch finish its tool call while the draft is held.
        for _ in range(200):
            if of_type(gateway.store.events(started.run_id), "tool.finished"):
                break
            await asyncio.sleep(0.01)
        version = gateway.store.get_run(started.run_id).version
        record = await gateway.orchestrator.cancel(started.run_id, version)
        return started.run_id, record

    run_id, record = asyncio.run(go())

    assert record.state == "cancelled"
    events = gateway.store.events(run_id)
    (cancelled,) = of_type(events, "run.cancelled")
    assert cancelled.payload["reason"] == "cancel_requested"
    assert cancelled.payload["external_calls"] == 1
    assert cancelled.payload["workflow"]["abandoned_calls"] == [external.cancelled[0]]
    validate_trace_event(cancelled.to_json_object())
    (draft_done,) = [
        e for e in of_type(events, "model.finished") if e.payload["purpose"] == "draft"
    ]
    assert draft_done.payload["error"]["code"] == "cancelled"
    assert external.interrupted == external.cancelled
    assert executed_tools(events) == ["get_service_health"]
    assert of_type(events, "answer.delta") == []
    assert not of_type(events, "handoff.started")
    assert gateway.store.active_run_id() is None


def test_cancel_with_the_health_tool_in_flight_settles_the_operation(tmp_path):
    """RM10, RM14, AC12: a cancel while the local branch's tool runs settles the call."""
    external = GatedAdapter(turns=[text_turn(DRAFT), text_turn(ANSWER)], locality="external")
    gateway = build_gateway(
        tmp_path,
        local_turns(),
        enabled_workflows={WORKFLOW},
        external_model=ConfiguredModel(adapter=external, model_id="gated-external"),
    )
    entered = threading.Event()
    release = threading.Event()
    original = gateway.tools.get("get_service_health").declaration.handler

    def handler(arguments):
        entered.set()
        release.wait(5)
        return original(arguments)

    replace_handler(gateway, "get_service_health", handler, timeout_seconds=30.0)

    async def go():
        started = gateway.orchestrator.start(user(PROMPT), execution_mode="parallel_demo")
        assert await asyncio.to_thread(entered.wait, 5)
        version = gateway.store.get_run(started.run_id).version
        record = await gateway.orchestrator.cancel(started.run_id, version)
        release.set()
        await asyncio.sleep(0.05)
        return started.run_id, record

    run_id, record = asyncio.run(go())

    assert record.state == "cancelled"
    operations = gateway.store.tool_operations(run_id)
    assert [op["status"] for op in operations] != ["pending"]
    events = gateway.store.events(run_id)
    (cancelled,) = of_type(events, "run.cancelled")
    assert cancelled.payload["reason"] == "cancel_requested"
    assert gateway.store.active_run_id() is None


def test_cancel_during_the_synthesis_stops_the_external_call(tmp_path):
    """RM14, RM20, AC12: a cancel after the join stops the synthesis; no answer is published."""
    external = HeldSecond(turns=[text_turn(DRAFT), text_turn(ANSWER)], locality="external")
    gateway = build_gateway(
        tmp_path,
        local_turns(),
        enabled_workflows={WORKFLOW},
        external_model=ConfiguredModel(adapter=external, model_id="gated-external"),
    )

    async def go():
        started = gateway.orchestrator.start(user(PROMPT), execution_mode="parallel_demo")
        await external.synthesis_started.wait()
        version = gateway.store.get_run(started.run_id).version
        record = await gateway.orchestrator.cancel(started.run_id, version)
        return started.run_id, record

    run_id, record = asyncio.run(go())

    assert record.state == "cancelled"
    events = gateway.store.events(run_id)
    (cancelled,) = of_type(events, "run.cancelled")
    assert cancelled.payload["external_calls"] == 2
    assert of_type(events, "answer.delta") == []
    synthesis = [e for e in of_type(events, "model.finished") if e.payload["purpose"] == "external"]
    assert synthesis[0].payload["error"]["code"] == "cancelled"
    assert external.cancelled == [synthesis[0].payload["call_id"]]
    # The synthesis is not a parallel branch call: it is not abandoned (section 8).
    assert cancelled.payload["workflow"]["abandoned_calls"] == []
    assert gateway.store.active_run_id() is None


class HeldSecond(ScriptedAdapter):
    """An external double whose second generation (the synthesis) waits forever."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.synthesis_started = asyncio.Event()
        self.live: set[str] = set()

    async def cancel(self, generation_id):
        if generation_id in self.live:
            self.cancelled.append(generation_id)

    async def generate(self, request):
        self.live.add(request.generation_id)
        try:
            if request.generation_id.endswith("_external"):
                self.synthesis_started.set()
                await asyncio.Event().wait()
            async for event in super().generate(request):
                yield event
        finally:
            self.live.discard(request.generation_id)


def test_restart_during_the_branches_fails_the_run_as_interrupted(tmp_path):
    """RM10, RM17, AC12: parallel_running becomes interrupted; the pending read is failed.

    The model_calls inventory is rebuilt from model.started, so the
    branch calls of the process that died stay in it.
    """
    old = parallel_gateway(tmp_path, external=[])
    machine = old.orchestrator.machine
    record = machine.create(accepted=(EventType.RUN_ACCEPTED, "gateway", ACCEPTED_PAYLOAD))
    for state in (RunState.ROUTING, RunState.PARALLEL_RUNNING):
        record = machine.transition(record.run_id, record.version, state)
    run_id = record.run_id
    for call_id, actor, purpose, branch in (
        (f"{run_id}_gen2_skill", "local", "skill", "local_health"),
        (f"{run_id}_gen3_draft", "external", "draft", "external_checklist"),
    ):
        old.store.append_event(
            run_id,
            EventType.MODEL_STARTED,
            actor,
            {
                "call_id": call_id,
                "model": "m",
                "actor": actor,
                "purpose": purpose,
                "branch": branch,
            },
        )
    arguments = {"service": "staging-api"}
    operation_id = ToolExecutor.operation_id(run_id, "get_service_health", arguments)
    old.store.record_tool_operation(
        run_id,
        operation_id=operation_id,
        proposal_id="prop_health",
        tool_name="get_service_health",
        model="double-qwen3-0.6b",
        arguments=arguments,
    )
    old.store.close()

    new = parallel_gateway(tmp_path, local=[], external=[], clock=old.clock)
    report = new.orchestrator.recover_after_restart()

    assert report.runs[0].interrupted_state == "parallel_running"
    after = new.store.get_run(run_id)
    assert (after.state, after.error_code) == ("failed", "interrupted")
    assert new.store.get_tool_operation(operation_id)["status"] == "failed"
    events = new.store.events(run_id)
    (failed,) = of_type(events, "run.failed")
    assert failed.payload["interrupted_state"] == "parallel_running"
    assert [c["purpose"] for c in failed.payload["model_calls"]] == ["skill", "draft"]
    assert failed.payload["external_calls"] == 1
    validate_trace_event(failed.to_json_object())
    assert new.adapter.requests == [] and new.external.requests == []


# --- measurement (RM15, RM25) ---------------------------------------------------------------


def test_the_extra_call_latency_is_measured_and_reported(tmp_path):
    """RM15, RM25, section 12.1: the synthesis duration is in the terminal event."""
    clock = FakeClock()
    external = ScriptedAdapter(
        turns=[text_turn(DRAFT), text_turn(ANSWER)],
        locality="external",
        on_generate=lambda request: clock.advance(2),
    )
    gateway = build_gateway(
        tmp_path,
        local_turns(),
        clock=clock,
        enabled_workflows={WORKFLOW},
        external_model=ConfiguredModel(adapter=external, model_id="double-external"),
    )

    outcome, events = run_parallel(gateway)

    assert outcome.state is RunState.COMPLETED
    (completed,) = of_type(events, "run.completed")
    workflow = completed.payload["workflow"]
    (synthesis,) = [
        e for e in of_type(events, "model.finished") if e.payload["purpose"] == "external"
    ]
    assert synthesis.payload["duration_ms"] == 2000
    # The draft call takes 2 s on the shared fake clock; the tool runs meanwhile.
    assert workflow["workflow_id"] == WORKFLOW
    assert workflow["branch_wall_ms"] == 2000
    assert workflow["external_checklist_ms"] == 2000
    assert 0 <= workflow["local_health_ms"] <= 2000
    assert workflow["extra_call_ms"] == 2000
    assert workflow["abandoned_calls"] == []
    validate_trace_event(completed.to_json_object())


def test_a_run_without_the_workflow_has_no_workflow_summary(tmp_path):
    """Section 8: the summary exists only for a run that started the workflow."""
    gateway = build_gateway(tmp_path, [route_turn("check-service-health"), *health_turns()])
    outcome = asyncio.run(gateway.orchestrator.run_text("Is staging-api healthy right now?"))
    events = gateway.store.events(outcome.run_id)
    (completed,) = of_type(events, "run.completed")
    assert "workflow" not in completed.payload
    assert all("branch" not in e.payload for e in of_type(events, "model.started"))


def health_turns():
    from gateway_helpers import health_tail

    return health_tail()


def test_abandoned_calls_count_in_the_native_external_metrics(tmp_path):
    """Section 12.1: external usage includes the abandoned draft call."""
    from relaymind.orchestrator.recovery import model_calls

    external = GatedAdapter(turns=[text_turn(DRAFT)], locality="external")
    gateway = build_gateway(
        tmp_path,
        [route_turn(WORKFLOW), text_turn("fine"), text_turn("fine")],
        enabled_workflows={WORKFLOW},
        external_model=ConfiguredModel(adapter=external, model_id="gated-external"),
    )

    outcome, _ = run_parallel(gateway)

    calls = model_calls(gateway.store, outcome.run_id)
    assert sum(1 for c in calls if c["actor"] == "external") == 1


# --- the demo UI (RM23) -----------------------------------------------------------------------


def view_of(gateway, run_id):
    return trace_view_from(gateway.store.get_run(run_id), gateway.store.events(run_id))


def test_the_trace_view_lists_each_stage_as_its_own_actor(tmp_path):
    """RM23, AC14: local_health, external_checklist, and synthesis are separate actors."""
    gateway = parallel_gateway(tmp_path)
    outcome, _ = run_parallel(gateway)

    view = view_of(gateway, outcome.run_id)

    stages = {s["branch"]: s for s in view["workflow"]["stages"]}
    assert set(stages) == {"local_health", "external_checklist", "synthesis"}
    assert stages["local_health"]["actor"] == "local"
    assert stages["external_checklist"]["actor"] == "external"
    assert stages["synthesis"]["actor"] == "external"
    assert all(s["finished"] for s in stages.values())
    assert view["workflow"]["extra_call_ms"] is not None
    assert [op["branch"] for op in view["operations"]] == ["local_health"]
    assert [p["branch"] for p in view["proposals"]] == ["local_health"]
    assert {c["branch"] for c in view["model_calls"]} == {
        None,
        "local_health",
        "external_checklist",
        "synthesis",
    }


def test_a_run_without_the_workflow_has_no_workflow_view(tmp_path):
    """RM23: the UI invents nothing for a run that did not run the workflow."""
    gateway = build_gateway(tmp_path, [route_turn("check-service-health"), *health_turns()])
    outcome = asyncio.run(gateway.orchestrator.run_text("Is staging-api healthy right now?"))
    assert view_of(gateway, outcome.run_id)["workflow"] is None


def test_the_rendered_trace_shows_the_workflow_section_escaped(tmp_path):
    """RM12, RM23: the section names the actors and escapes model text."""
    gateway = parallel_gateway(tmp_path)
    outcome, _ = run_parallel(gateway)

    html = render_trace(view_of(gateway, outcome.run_id), UiSettings(mode=DataMode.DOUBLE))

    assert "Parallel workflow" in html
    for branch in ("local_health", "external_checklist", "synthesis"):
        assert branch in html
    assert "Extra call latency" in html


# --- the modes that run the workflow ---------------------------------------------------------


def test_an_admitted_parallel_workflow_runs_in_auto(tmp_path):
    """RM15, section 4.4: auto runs the workflow when operator policy admits it."""
    gateway = parallel_gateway(tmp_path, parallel_in_auto=True)

    outcome, events = run_parallel(gateway, execution_mode="auto")

    assert outcome.state is RunState.COMPLETED
    assert of_type(events, "route.selected")[0].payload["decision"]["route"] == "parallel"
    assert len(of_type(events, "handoff.started")) == 1


def test_a_skill_that_declares_a_workflow_that_is_not_enabled_still_fails(tmp_path):
    """RM21, section 4.4: configuration without the skill's workflow is unavailable."""
    gateway = parallel_gateway(tmp_path, enabled_workflows={"some-other-workflow"}, external=[])

    outcome, _ = run_parallel(gateway)

    assert outcome.error_code == "workflow_unavailable"


@pytest.mark.parametrize("mode", ["local_only", "external_only"])
def test_the_other_modes_never_run_the_workflow(tmp_path, mode):
    """RM15: local_only and external_only never produce route=parallel."""
    gateway = parallel_gateway(
        tmp_path, external=[text_turn(ANSWER)] if mode == "external_only" else []
    )

    _, events = run_parallel(gateway, execution_mode=mode)

    assert not [e for e in of_type(events, "model.started") if e.payload["purpose"] == "draft"]


# --- the endpoints ------------------------------------------------------------------------


def test_the_chat_endpoint_serves_a_parallel_demo_run(tmp_path):
    """RM19, RM20, AC14: chat in parallel_demo returns the external synthesis, once."""
    from api_helpers import RUN_ID_HEADER, chat, make_app

    gateway = parallel_gateway(tmp_path)
    app = make_app(gateway)

    response = chat(app, prompt=PROMPT, headers={"X-RelayMind-Execution-Mode": "parallel_demo"})

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == ANSWER
    events = gateway.store.events(response.headers[RUN_ID_HEADER])
    assert [e.payload["branch"] for e in of_type(events, "model.started") if "branch" in e.payload]


def test_a_chat_disconnect_during_the_draft_settles_both_branches(tmp_path):
    """RM14, RM17, AC12, #92: closing the request stops the draft like a native cancel."""
    from api_helpers import make_app
    from test_chat_disconnect_cancel import chat_client, run_ended

    external = GatedAdapter(turns=[text_turn(DRAFT), text_turn(ANSWER)], locality="external")
    gateway = build_gateway(
        tmp_path,
        local_turns(),
        enabled_workflows={WORKFLOW},
        external_model=ConfiguredModel(adapter=external, model_id="gated-external"),
    )
    app = make_app(gateway)

    async def go():
        client = chat_client(app, stream=False)
        client._scope["headers"].append((b"x-relaymind-execution-mode", b"parallel_demo"))
        client.start()
        await asyncio.wait_for(external.started.wait(), 10)
        run_id = gateway.store.active_run_id()
        await client.leave()
        await run_ended(gateway, run_id)
        return run_id

    run_id = asyncio.run(go())

    events = gateway.store.events(run_id)
    assert gateway.store.get_run(run_id).state == "cancelled"
    (cancelled,) = of_type(events, "run.cancelled")
    assert cancelled.payload["external_calls"] == 1
    assert external.cancelled == external.interrupted
    assert len(external.cancelled) == 1
    assert of_type(events, "answer.delta") == []
    assert gateway.store.active_run_id() is None


def test_the_demo_form_can_start_a_parallel_demo_run():
    """RM23, spec 0.25 section 9: the UI offers parallel_demo next to the other modes."""
    from relaymind.ui.render import render_shell

    page = render_shell(UiSettings(mode=DataMode.DOUBLE))

    assert '<option value="parallel_demo">parallel_demo</option>' in page


# --- review findings (#30) --------------------------------------------------------------------


def test_no_room_for_two_external_generations_ends_before_any_branch(tmp_path):
    """RM03, RM04, section 11: the draft and the synthesis each need an external generation.

    With external_generations=1 the workflow cannot finish, so the run
    ends as needs_external with the budget named before any branch starts.
    """
    gateway = parallel_gateway(tmp_path, budgets=LocalBudgets(external_generations=1))

    outcome, events = run_parallel(gateway)

    assert outcome.error_code == "needs_external"
    (failed,) = of_type(events, "run.failed")
    assert failed.payload["dispatch_blocked"] == "external_generations"
    assert gateway.external.requests == []
    assert executed_tools(events) == []
    assert of_type(events, "handoff.started") == []


def test_no_room_at_the_join_ends_without_a_handoff(tmp_path):
    """RM03, RM04, RM14: the room is checked again at the join, before handoff.started.

    The local branch spends a repair generation, so the entire-run
    generation limit leaves nothing for the synthesis. The run ends as
    needs_external with the budget named; authority never moves.
    """
    local = [
        route_turn(WORKFLOW),
        text_turn("staging-api looks fine"),
        tool_turn("get_service_health", service="staging-api"),
    ]
    gateway = parallel_gateway(tmp_path, local=local, budgets=LocalBudgets(run_generations=4))

    outcome, events = run_parallel(gateway)

    assert outcome.error_code == "needs_external"
    (failed,) = of_type(events, "run.failed")
    assert failed.payload["dispatch_blocked"] == "run_generations"
    assert executed_tools(events) == ["get_service_health"]
    assert of_type(events, "handoff.started") == []
    assert len(gateway.external.requests) == 1
    assert "workflow" in failed.payload


def test_the_trace_shows_each_branch_time_from_the_terminal_event(tmp_path):
    """RM16, RM23, section 12: a branch's duration includes its tool run, not only its calls."""
    gateway = parallel_gateway(tmp_path)
    original = gateway.tools.get("get_service_health").declaration.handler

    def handler(arguments):
        gateway.clock.advance(1.5)
        return original(arguments)

    replace_handler(gateway, "get_service_health", handler, timeout_seconds=30.0)
    outcome, events = run_parallel(gateway)

    (completed,) = of_type(events, "run.completed")
    figures = completed.payload["workflow"]
    assert figures["local_health_ms"] >= 1500
    view = view_of(gateway, outcome.run_id)
    stages = {s["branch"]: s for s in view["workflow"]["stages"]}
    assert stages["local_health"]["duration_ms"] == figures["local_health_ms"]
    assert stages["external_checklist"]["duration_ms"] == figures["external_checklist_ms"]
    assert stages["synthesis"]["duration_ms"] == figures["extra_call_ms"]


def test_restart_recovery_names_the_branch_of_the_settled_operation(tmp_path):
    """RM10, RM22, section 8: a settled branch operation keeps its branch in tool.finished.

    The interrupted run.failed has no workflow object: its monotonic
    figures ended with the process (spec 0.25 section 4.4). The trace
    still shows the workflow from the branch calls.
    """
    old = parallel_gateway(tmp_path, external=[])
    machine = old.orchestrator.machine
    record = machine.create(accepted=(EventType.RUN_ACCEPTED, "gateway", ACCEPTED_PAYLOAD))
    for state in (RunState.ROUTING, RunState.PARALLEL_RUNNING):
        record = machine.transition(record.run_id, record.version, state)
    run_id = record.run_id
    old.store.append_event(
        run_id,
        EventType.MODEL_STARTED,
        "local",
        {
            "call_id": f"{run_id}_gen2_skill",
            "model": "m",
            "actor": "local",
            "purpose": "skill",
            "branch": "local_health",
        },
    )
    arguments = {"service": "staging-api"}
    operation_id = ToolExecutor.operation_id(run_id, "get_service_health", arguments)
    old.store.record_tool_operation(
        run_id,
        operation_id=operation_id,
        proposal_id="prop_health",
        tool_name="get_service_health",
        model="double-qwen3-0.6b",
        arguments=arguments,
    )
    old.store.append_event(
        run_id,
        EventType.TOOL_STARTED,
        "local",
        {
            "operation_id": operation_id,
            "proposal_id": "prop_health",
            "tool_name": "get_service_health",
            "model": "double-qwen3-0.6b",
            "branch": "local_health",
        },
    )
    old.store.close()

    new = parallel_gateway(tmp_path, local=[], external=[], clock=old.clock)
    new.orchestrator.recover_after_restart()

    events = new.store.events(run_id)
    (finished,) = of_type(events, "tool.finished")
    assert finished.payload["branch"] == "local_health"
    validate_trace_event(finished.to_json_object())
    (failed,) = of_type(events, "run.failed")
    assert "workflow" not in failed.payload
    view = view_of(new, run_id)
    assert view["workflow"]["workflow_id"] is None
    html = render_trace(view, UiSettings(mode=DataMode.DOUBLE))
    assert "not recorded" in html


def test_the_local_branch_prompt_names_only_its_read_only_tools(tmp_path):
    """RM08, RM11, spec 0.25 section 4.4: a mutating tool is never offered to a branch.

    The skill instruction is built from the read-only tools only, so the
    gateway's own prompt never asks for a tool the branch rejects.
    """
    root = tmp_path / "skills"
    shutil.copytree(DEMO_SKILLS_ROOT, root)
    manifest = root / WORKFLOW / "skill.manifest.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["allowed_tools"] = ["get_service_health", "delete_demo_database"]
    manifest.write_text(json.dumps(data), encoding="utf-8")
    gateway = parallel_gateway(
        tmp_path,
        skill_roots=[root],
        caller=CallerContext(permissions=frozenset({READ_PERMISSION, MUTATE_PERMISSION})),
    )

    outcome, _ = run_parallel(gateway)

    assert outcome.state is RunState.COMPLETED
    branch_request = gateway.adapter.requests[1]
    assert [t.name for t in branch_request.tool_schemas] == ["get_service_health"]
    text = "\n".join(m.content or "" for m in branch_request.messages if m.role is Role.SYSTEM)
    assert "get_service_health" in text
    assert "delete_demo_database" not in text
