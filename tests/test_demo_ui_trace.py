"""The demo UI trace view: every listed field, from stored events (RM23, RM25, #28).

Spec 0.20 section 9 (Demo UI) and section 12.1. The view is built only
from a run's stored events and run state, so it shows what a native
reader sees and nothing else. All model output comes from scripted
doubles (spec section 13).
"""

import asyncio
import copy
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from gateway_helpers import (
    EXTERNAL_MODEL_ID,
    LOCAL_MODEL_ID,
    STAGING_OK,
    replace_handler,
    text_turn,
    tool_turn,
)
from helpers import ACCEPTED_PAYLOAD, of_type
from relaymind.adapters import GenerationOptions
from relaymind.orchestrator.local_status import LocalModelClass, LocalStatus
from relaymind.orchestrator.performance import PerformanceSettings
from relaymind.store import RunRecord, RunStore, TraceEvent
from relaymind.store.trace_event import EventType, validate_trace_event
from relaymind.ui.render import render_trace
from relaymind.ui.settings import DataMode, UiSettings
from relaymind.ui.trace_view import build_trace_view, trace_view_from
from ui_helpers import (
    DELETE_ANSWER,
    DELETE_ARGS,
    DELETE_PROMPT,
    GENERATION_SECONDS,
    HEALTH_PROMPT,
    clarifying_gateway,
    delete_gateway,
    external_health_gateway,
    external_health_turns,
    health_gateway,
    run_to_rest,
    timed_gateway,
    without_original_messages,
)

_NOW = datetime(2026, 10, 20, 3, tzinfo=UTC)
# A run that was accepted and nothing more.
FRESH_RECORD = RunRecord("run_1", "routing", 1, None, None, None, _NOW, _NOW)


def view_of(gateway, outcome):
    return build_trace_view(gateway.store, outcome.run_id)


def test_phase_two_evidence_is_read_from_the_run(tmp_path):
    """RM23, RM25, RM28, RM31: recorded options, selector and timing, not defaults."""
    gateway = health_gateway(tmp_path, performance=PerformanceSettings(answer_pass="draft"))
    outcome = run_to_rest(gateway)
    events = gateway.store.events(outcome.run_id)
    view = view_of(gateway, outcome)
    accepted = events[0].payload
    assert view["configuration"]["local_model"] == accepted["local_model"]
    assert view["configuration"]["local_model_status"] == "ready"
    assert view["configuration"]["answer_pass"] == "draft"
    assert view["configuration"]["selection"] == accepted["selection"]
    # Not written by this run: a key that is absent is not recorded, never null.
    assert "placement_policy" not in view["configuration"]
    assert view["answer_pass_applied"] == events[-1].payload["answer_pass_applied"]
    route = of_type(events, "route.selected")[0]
    assert view["route"]["selector"] == route.payload["selector"]
    assert view["routes"] == [view["route"]]
    finished = of_type(events, "model.finished")
    assert [c["first_token_ms"] for c in view["model_calls"]] == [
        e.payload["first_token_ms"] for e in finished
    ]
    shown = render_trace(view, UiSettings(mode="double"))
    assert "Answer pass applied: draft" in shown
    assert "Selector: model_router" in shown
    assert "First token" in shown
    assert "Workflow steps: not recorded" in shown


def test_historical_phase_two_fields_are_not_inferred(tmp_path):
    """RM23: missing phase 2 evidence means not recorded, even after a completion."""
    gateway = health_gateway(tmp_path)
    outcome = run_to_rest(gateway)
    removed = {
        "local_model",
        "local_model_status",
        "local_model_reason",
        "answer_pass",
        "answer_stream",
        "calls_per_generation",
        "prefetch",
        "early_headers",
        "selection",
        "selector",
        "answer_pass_applied",
        "first_token_ms",
    }
    events = [
        replace(e, payload={k: v for k, v in e.payload.items() if k not in removed})
        for e in gateway.store.events(outcome.run_id)
    ]
    view = trace_view_from(gateway.store.get_run(outcome.run_id), events)
    assert view["configuration"] == {}
    assert view["answer_pass_applied"] is None
    assert view["route"]["selector"] is None
    assert all("first_token_ms" not in call for call in view["model_calls"])
    shown = render_trace(view, UiSettings(mode="double"))
    for label in (
        "Local model",
        "Local model status",
        "Placement policy",
        "Answer pass",
        "Answer stream",
        "Selector",
        "Answer pass applied",
    ):
        assert f"{label}: not recorded" in shown
    models = shown.split('<section class="models">')[1].split("</section>")[0]
    assert "<td>not recorded</td>" in models
    assert "no token" not in models


def test_recorded_nulls_render_with_their_meaning(tmp_path):
    """RM23, RM26: a value recorded as null is evidence, not a missing record."""
    gateway = health_gateway(tmp_path)
    outcome = run_to_rest(gateway)
    events = gateway.store.events(outcome.run_id)
    assert events[0].payload["local_model_reason"] is None
    shown = render_trace(view_of(gateway, outcome), UiSettings(mode="double"))
    assert "Local model reason: none" in shown
    assert "Local model reason: not recorded" not in shown


def test_null_timing_and_placement_render_with_their_meaning(tmp_path):
    """RM23, RM27: no first token, no prediction, and no placement are recorded facts."""
    gateway = health_gateway(tmp_path)
    outcome = run_to_rest(gateway)
    changed = []
    for event in gateway.store.events(outcome.run_id):
        payload = dict(event.payload)
        if event.event_type is EventType.ROUTE_SELECTED:
            payload["placement"] = {"role": "local", "predicted_ms": None, "reason": "no sample"}
        if event.event_type is EventType.MODEL_FINISHED:
            payload["first_token_ms"] = None
            payload["predicted_ms"] = None
        changed.append(replace(event, payload=payload))
        validate_trace_event(changed[-1].to_json_object())
    view = trace_view_from(gateway.store.get_run(outcome.run_id), changed)
    shown = render_trace(view, UiSettings(mode="double"))
    models = shown.split('<section class="models">')[1].split("</section>")[0]
    assert "<td>no token</td>" in models
    assert "Predicted: none; actual: 250 ms" in models
    assert "not recorded" not in models
    skill = shown.split('<section class="skill">')[1].split("</section>")[0]
    assert "Predicted: none; actual: 250 ms" in skill


def test_a_running_call_says_its_timing_comes_when_it_ends(tmp_path):
    """RM23, RM25: a call without model.finished yet is running, not missing evidence."""
    gateway = health_gateway(tmp_path)
    outcome = run_to_rest(gateway)
    events = gateway.store.events(outcome.run_id)
    first_finish = next(i for i, e in enumerate(events) if e.event_type is EventType.MODEL_FINISHED)
    record = replace(gateway.store.get_run(outcome.run_id), state="routing")
    view = trace_view_from(record, events[:first_finish])
    shown = render_trace(view, UiSettings(mode="double"))
    models = shown.split('<section class="models">')[1].split("</section>")[0]
    assert "<td>running</td><td>known when the call ends</td>" in models
    assert "actual: known when the call ends" in models
    assert "<td>not recorded</td>" not in models


def test_a_route_the_chain_did_not_select_says_none(tmp_path):
    """RM23, RM28: null selector fields mean the chain chose nothing (spec 0.36)."""
    gateway = health_gateway(tmp_path)
    outcome = run_to_rest(gateway)
    events = gateway.store.events(outcome.run_id)
    first = of_type(events, "route.selected")[0]
    failed = replace(
        first,
        actor="gateway",
        payload={
            "decision": first.payload["decision"],
            "mechanism": "selector_chain",
            "selector": {
                "name": None,
                "version": None,
                "mechanism": None,
                "elapsed_ms": 12,
                "attempts": [{"name": "example_match", "outcome": "none"}],
            },
            "placement": None,
        },
    )
    view = trace_view_from(gateway.store.get_run(outcome.run_id), [*events, failed])
    shown = render_trace(view, UiSettings(mode="double"))
    assert (
        "Selector: none selected; version: none; mechanism: none; elapsed: 12 ms. "
        "Attempts: example_match: none" in shown
    )
    assert "Placement: none" in shown
    assert "Predicted: none; actual: no routing call" in shown


def test_predictions_and_route_call_durations_stay_distinct(tmp_path):
    """RM23, RM27: predicted and measured times are separate, joined only by call ID."""
    gateway = health_gateway(tmp_path)
    outcome = run_to_rest(gateway)
    events = gateway.store.events(outcome.run_id)
    enriched = []
    for event in events:
        payload = dict(event.payload)
        if event.event_type is EventType.ROUTE_SELECTED:
            payload["placement"] = {"role": "local", "predicted_ms": 900, "reason": "sample"}
        if event.event_type is EventType.MODEL_FINISHED:
            payload["first_token_ms"] = 0
            payload["predicted_ms"] = 900
        enriched.append(replace(event, payload=payload))
    view = trace_view_from(gateway.store.get_run(outcome.run_id), enriched)
    assert view["route"]["duration_ms"] == 250
    assert view["route"]["placement"]["predicted_ms"] == 900
    shown = render_trace(view, UiSettings(mode="double"))
    assert "Predicted: 900 ms; actual: 250 ms" in shown
    assert "0 ms" in shown


def test_each_route_keeps_its_own_selector_and_call_timing(tmp_path):
    """RM23, RM28: a resumed gateway decision never borrows the prior call's timing."""
    gateway = health_gateway(tmp_path)
    outcome = run_to_rest(gateway)
    events = gateway.store.events(outcome.run_id)
    first = of_type(events, "route.selected")[0]
    resumed = replace(
        first,
        payload={
            "decision": first.payload["decision"],
            "mechanism": "clarification_resume",
        },
    )
    view = trace_view_from(gateway.store.get_run(outcome.run_id), [*events, resumed])
    assert len(view["routes"]) == 2
    assert view["routes"][0]["duration_ms"] == 250
    assert view["route"]["duration_ms"] is None
    assert view["route"]["selector"] is None
    shown = render_trace(view, UiSettings(mode="double"))
    assert "Selector: model_router" in shown
    assert "Selector: not recorded" in shown


def test_phase_two_labels_escape_recorded_strings(tmp_path):
    """RM12, RM23: stored selector labels and status causes are data, never markup."""
    gateway = health_gateway(tmp_path)
    outcome = run_to_rest(gateway)
    events = gateway.store.events(outcome.run_id)
    hostile = '<img src=x onerror="alert(1)">'
    changed = []
    for event in events:
        payload = copy.deepcopy(dict(event.payload))
        if event.event_type is EventType.RUN_ACCEPTED:
            payload["local_model_reason"] = hostile
            payload["selection"] = [hostile]
        if event.event_type is EventType.ROUTE_SELECTED:
            payload["selector"]["name"] = hostile
            payload["selector"]["attempts"][0]["name"] = hostile
            payload["placement"] = {"predicted_ms": hostile}
        if event.event_type is EventType.MODEL_FINISHED:
            payload["predicted_ms"] = hostile
        changed.append(replace(event, payload=payload))
        validate_trace_event(changed[-1].to_json_object())
    view = trace_view_from(gateway.store.get_run(outcome.run_id), changed)
    shown = render_trace(view, UiSettings(mode="double"))
    assert hostile not in shown
    assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;" in shown


@pytest.mark.parametrize("placement", [[], "unknown"])
def test_unsupported_placement_shapes_do_not_break_a_stored_trace(tmp_path, placement):
    """RM23: optional placement fields not yet constrained by the schema stay readable."""
    gateway = health_gateway(tmp_path)
    outcome = run_to_rest(gateway)
    events = [
        replace(e, payload={**e.payload, "placement": placement})
        if e.event_type in (EventType.MODEL_STARTED, EventType.ROUTE_SELECTED)
        else e
        for e in gateway.store.events(outcome.run_id)
    ]
    for event in events:
        validate_trace_event(event.to_json_object())
    view = trace_view_from(gateway.store.get_run(outcome.run_id), events)
    assert "placement" not in view["route"]
    assert all(c["placement"] is None and "predicted_ms" not in c for c in view["model_calls"])
    assert "Placement: not recorded" in render_trace(view, UiSettings(mode="double"))


def test_a_null_placement_means_no_placement(tmp_path):
    """RM23, RM27: a recorded null placement is shown as none, not as missing."""
    gateway = health_gateway(tmp_path)
    outcome = run_to_rest(gateway)
    events = [
        replace(e, payload={**e.payload, "placement": None})
        if e.event_type in (EventType.MODEL_STARTED, EventType.ROUTE_SELECTED)
        else e
        for e in gateway.store.events(outcome.run_id)
    ]
    view = trace_view_from(gateway.store.get_run(outcome.run_id), events)
    assert view["route"]["placement"] is None
    assert all(c["predicted_ms"] is None for c in view["model_calls"])
    shown = render_trace(view, UiSettings(mode="double"))
    assert "Placement: none" in shown
    assert "Predicted: none; actual: 250 ms" in shown


@pytest.mark.parametrize(
    "selector",
    [
        None,
        "unknown",
        [],
        {},
        {"name": "example_match"},
        {"attempts": None},
        {"attempts": "unknown"},
        {"attempts": [None, 1, {}, {"name": "example_match"}]},
    ],
)
def test_incomplete_selector_evidence_remains_readable(tmp_path, selector):
    """RM23, RM28: malformed historical input never prevents reading the trace."""
    gateway = health_gateway(tmp_path)
    outcome = run_to_rest(gateway)
    events = [
        replace(e, payload={**e.payload, "selector": selector})
        if e.event_type is EventType.ROUTE_SELECTED
        else e
        for e in gateway.store.events(outcome.run_id)
    ]
    view = trace_view_from(gateway.store.get_run(outcome.run_id), events)
    if not isinstance(selector, dict):
        assert view["route"]["selector"] is None
    # Exercise direct rendering too: callers may supply a partial view dictionary.
    view["route"]["selector"] = selector
    shown = render_trace(view, UiSettings(mode="double"))
    skill_section = shown.split('<section class="skill">')[1].split("</section>")[0]
    assert "Selector:" in skill_section
    assert "not recorded" in skill_section
    if isinstance(selector, dict):
        assert (
            "version: not recorded; mechanism: not recorded; elapsed: not recorded" in skill_section
        )
    assert STAGING_OK in shown


@pytest.mark.parametrize(
    "config",
    [
        None,
        "unknown",
        {},
        {"local_model": None},
        {"selection": []},
        {"selection": [1, {"name": "<selector>"}]},
    ],
)
def test_partial_configuration_views_remain_readable(config):
    """RM12, RM23: missing labels remain explicit and unexpected values stay escaped."""
    view = trace_view_from(FRESH_RECORD, [])
    view["configuration"] = config
    shown = render_trace(view, UiSettings(mode="double"))
    if isinstance(config, dict) and "local_model" in config:
        # Recorded null: the run had no local model (RM26).
        assert "Local model: none configured" in shown
    else:
        assert "Local model: not recorded" in shown
    assert "Local model status: not recorded" in shown
    if isinstance(config, dict) and config.get("selection") == []:
        assert "Selector chain: none" in shown
    if isinstance(config, dict) and config.get("selection"):
        assert "Selector chain: 1," in shown
        assert "&lt;selector&gt;" in shown
        assert "<selector>" not in shown


def test_absent_local_model_is_explicit_in_a_trace(tmp_path):
    """RM23, RM26, AC20: explicit absence differs from unrecorded model identity."""
    gateway = timed_gateway(
        tmp_path,
        [],
        local_status=LocalStatus(LocalModelClass.ABSENT, "not configured"),
        external_turns=external_health_turns(),
    )
    outcome = run_to_rest(gateway)
    shown = render_trace(view_of(gateway, outcome), UiSettings(mode="double"))
    assert "Local model: none configured" in shown
    assert "Local model status: absent" in shown
    assert "Local model reason: not configured" in shown


@pytest.mark.parametrize("state", ["routing", "waiting_input", "waiting_confirmation"])
def test_active_run_does_not_claim_answer_pass_evidence_is_missing(state):
    """RM23, RM31: an active run has not yet recorded its applied answer pass."""
    view = trace_view_from(replace(FRESH_RECORD, state=state), [])
    shown = render_trace(view, UiSettings(mode="double"))
    assert "Answer pass applied: known when the run ends" in shown


@pytest.mark.parametrize("state", ["failed", "cancelled"])
def test_a_run_that_did_not_complete_has_no_answer_pass(state):
    """RM23, RM31: only run.completed carries answer_pass_applied (spec 0.32)."""
    view = trace_view_from(replace(FRESH_RECORD, state=state), [])
    shown = render_trace(view, UiSettings(mode="double"))
    assert "Answer pass applied: none, the run did not complete" in shown


def test_model_call_duration_is_shown_once(tmp_path):
    """RM23, RM27: the result column does not repeat measured placement duration."""
    gateway = health_gateway(tmp_path)
    outcome = run_to_rest(gateway)
    shown = render_trace(view_of(gateway, outcome), UiSettings(mode="double"))
    models = shown.split('<section class="models">')[1].split("</section>")[0]
    assert "stop, 250 ms" not in models
    assert models.count("actual: 250 ms") == 4


# -- a completed local run: every listed field ----------------------------------------


def test_a_local_run_shows_every_listed_field(tmp_path):
    """RM23, section 9: prompt, mode, model, skill, tool, result, answer, and timing."""
    gateway = health_gateway(tmp_path)
    outcome = run_to_rest(gateway, execution_mode="local_only")
    view = view_of(gateway, outcome)

    assert view["run_id"] == outcome.run_id
    assert (view["state"], view["error_code"], view["reason_code"]) == ("completed", None, None)
    assert view["prompt"] == [{"role": "user", "content": HEALTH_PROMPT}]
    assert view["execution_mode"] == {"requested": "local_only", "effective": "local_only"}
    assert view["active_model"] == LOCAL_MODEL_ID
    assert [(c["model"], c["actor"], c["purpose"]) for c in view["model_calls"]] == [
        (LOCAL_MODEL_ID, "local", "routing"),
        (LOCAL_MODEL_ID, "local", "skill"),
        (LOCAL_MODEL_ID, "local", "skill"),
        (LOCAL_MODEL_ID, "local", "final"),
    ]
    assert view["skill"]["name"] == "check-service-health"
    assert view["skill"]["version"] and view["skill"]["content_hash"].startswith("sha256:")
    assert view["route"]["route"] == "local"
    assert view["route"]["skill_id"] == "check-service-health"
    assert view["route"]["reason_code"] == "simple_skill"
    (proposal,) = view["proposals"]
    assert (proposal["tool_name"], proposal["arguments"]) == (
        "get_service_health",
        {"service": "staging-api"},
    )
    assert (proposal["model"], proposal["admissible"], proposal["rejection"]) == (
        LOCAL_MODEL_ID,
        True,
        None,
    )
    (operation,) = view["operations"]
    assert operation["tool_name"] == "get_service_health"
    assert operation["status"] == "succeeded"
    assert operation["data"]["status"] == "healthy"
    assert operation["data"]["fixture"]["scenario"] == "baseline"
    assert operation["replayed"] is False
    assert view["failed_operations"] == []
    assert view["escalation"] is None
    assert view["final_answer"] == {"text": STAGING_OK, "model": LOCAL_MODEL_ID}


def test_a_view_is_json_data_made_only_of_the_stored_events(tmp_path):
    """RM23: the view is rebuilt from the events and the run record, nothing else."""
    gateway = health_gateway(tmp_path)
    outcome = run_to_rest(gateway)
    record = gateway.store.get_run(outcome.run_id)
    events = gateway.store.events(outcome.run_id)

    assert trace_view_from(record, events) == build_trace_view(gateway.store, outcome.run_id)


# -- timing (RM25, section 12.1) ------------------------------------------------------


def test_latency_is_the_first_model_action_never_the_completed_answer(tmp_path):
    """RM25, section 12.1: the figure is the first validated model action."""
    gateway = health_gateway(tmp_path)
    outcome = run_to_rest(gateway)
    view = view_of(gateway, outcome)
    events = gateway.store.events(outcome.run_id)
    route = next(e for e in events if e.event_type.value == "route.selected")
    completed = events[-1]

    timing = view["timing"]
    assert timing["first_model_action_ms"] == route.elapsed_ms == int(GENERATION_SECONDS * 1000)
    # Four generations ran: the answer arrived much later than the first action.
    assert completed.elapsed_ms >= 4 * int(GENERATION_SECONDS * 1000)
    assert timing["first_model_action_ms"] < completed.elapsed_ms
    # Run times are separate fields, and no field claims answer latency.
    assert timing["active_ms"] == completed.payload["active_ms"]
    assert timing["waiting_ms"] == completed.payload["waiting_ms"] == 0
    assert set(timing) == {"first_model_action_ms", "active_ms", "waiting_ms"}


def test_a_route_the_gateway_made_itself_is_not_a_model_action(tmp_path):
    """Section 9 (spec 0.20): in external_only the tool call is the first model action."""
    gateway = external_health_gateway(tmp_path)
    outcome = run_to_rest(gateway, execution_mode="external_only")
    view = view_of(gateway, outcome)
    events = gateway.store.events(outcome.run_id)
    proposed = next(e for e in events if e.event_type.value == "tool.proposed")

    assert view["route"]["mechanism"] == "execution_mode"
    assert view["timing"]["first_model_action_ms"] == proposed.elapsed_ms
    assert proposed.elapsed_ms > 0


def test_a_rejected_proposal_is_not_the_first_model_action(tmp_path):
    """RM09, RM25, section 12.1: only a validated proposal counts as a model action."""
    gateway = timed_gateway(
        tmp_path,
        [],
        external_turns=[
            tool_turn("get_service_health", call_id="e0", service=42),
            *external_health_turns(),
        ],
    )
    clock = gateway.clock
    gateway.external.on_generate = lambda request: clock.advance(GENERATION_SECONDS)
    outcome = run_to_rest(gateway, execution_mode="external_only")
    view = view_of(gateway, outcome)
    events = gateway.store.events(outcome.run_id)
    rejected, admitted = [e for e in events if e.event_type.value == "tool.proposed"]

    assert (rejected.payload["admissible"], admitted.payload["admissible"]) == (False, True)
    assert rejected.elapsed_ms < admitted.elapsed_ms
    assert view["timing"]["first_model_action_ms"] == admitted.elapsed_ms


@pytest.mark.parametrize("mechanism", ["clarification_resume", "client_owned"])
def test_any_route_the_gateway_recorded_is_not_a_model_action(tmp_path, mechanism):
    """RM25, section 9: a route.selected by the gateway actor is not a model action."""
    gateway = health_gateway(tmp_path)
    outcome = run_to_rest(gateway)
    record = gateway.store.get_run(outcome.run_id)
    events = [
        replace(e, actor="gateway", payload={**e.payload, "mechanism": mechanism})
        if e.event_type.value == "route.selected"
        else e
        for e in gateway.store.events(outcome.run_id)
    ]
    proposed = next(e for e in events if e.event_type.value == "tool.proposed")

    view = trace_view_from(record, events)
    assert view["timing"]["first_model_action_ms"] == proposed.elapsed_ms


def test_a_prefetch_proposal_is_not_a_model_action(tmp_path):
    """RM29, section 12.1: the gateway's prefetch proposal comes first but is not the model's."""
    gateway = health_gateway(tmp_path)
    outcome = run_to_rest(gateway)
    record = gateway.store.get_run(outcome.run_id)
    events = gateway.store.events(outcome.run_id)
    proposed = next(e for e in events if e.event_type.value == "tool.proposed")
    prefetch = replace(
        proposed,
        actor="gateway",
        elapsed_ms=1,
        payload={**proposed.payload, "mechanism": "prefetch", "model": None},
    )
    # A prefetch runs before any model generation, so its proposal comes first in the trace.
    shown = [events[0], prefetch, *events[1:]]

    view = trace_view_from(record, shown)
    route = next(e for e in events if e.event_type.value == "route.selected")
    assert view["timing"]["first_model_action_ms"] == route.elapsed_ms != prefetch.elapsed_ms
    # The view lists the gateway's proposal like any other, with no model named.
    assert any(p["actor"] == "gateway" and p["model"] is None for p in view["proposals"])


def test_no_model_action_yet_is_reported_as_none_not_zero():
    """Section 9 (spec 0.20): while there is no action, the figure is absent."""
    accepted = validate_trace_event(
        {
            "schema_version": 1,
            "run_id": "run_1",
            "seq": 1,
            "timestamp": "2026-10-20T03:00:00Z",
            "elapsed_ms": 0,
            "event_type": "run.accepted",
            "actor": "gateway",
            "payload": {
                **ACCEPTED_PAYLOAD,
                "original_messages": [{"role": "user", "content": "hello"}],
            },
        }
    )
    view = trace_view_from(FRESH_RECORD, [accepted])

    assert view["timing"] == {
        "first_model_action_ms": None,
        "active_ms": None,
        "waiting_ms": None,
    }
    assert view["final_answer"] is None
    assert view["active_model"] is None
    assert view["prompt"] == [{"role": "user", "content": "hello"}]


def test_a_run_stored_before_the_prompt_was_recorded_says_so():
    """Section 9 (spec 0.20): no original_messages means the prompt is unknown, not empty."""
    accepted = TraceEvent(
        run_id="run_1",
        seq=1,
        timestamp="2026-10-20T03:00:00Z",
        elapsed_ms=0,
        event_type="run.accepted",
        actor="gateway",
        payload={k: v for k, v in ACCEPTED_PAYLOAD.items() if k != "original_messages"},
    )
    view = trace_view_from(FRESH_RECORD, [accepted])

    assert view["prompt"] is None


def test_the_view_carries_the_recorded_answer_options(tmp_path):
    """RM19, RM23, RM25, spec 0.22 sections 8 and 9: the options shown are the stored ones."""
    gateway = health_gateway(tmp_path)
    outcome = run_to_rest(
        gateway,
        final_options=GenerationOptions(max_output_tokens=60, temperature=0.4, top_p=0.9),
    )
    view = view_of(gateway, outcome)

    assert view["answer_options"] == {
        "max_completion_tokens": 60,
        "temperature": 0.4,
        "top_p": 0.9,
    }


def test_a_run_without_options_shows_none_not_unrecorded(tmp_path):
    """Spec 0.22 section 9: an empty record is a run without options."""
    gateway = health_gateway(tmp_path)
    view = view_of(gateway, run_to_rest(gateway))

    assert view["answer_options"] == {}


def test_a_run_stored_before_the_options_were_recorded_stays_readable(tmp_path):
    """RM22, spec 0.22 section 8: no answer_options means not recorded, not none."""
    with RunStore(tmp_path / "runs.sqlite") as store:
        run_id = store.create_run(
            "accepted", accepted=(EventType.RUN_ACCEPTED, "gateway", ACCEPTED_PAYLOAD)
        ).run_id
        assert "answer_options" not in store.events(run_id)[0].payload

        view = build_trace_view(store, run_id)

    assert view["answer_options"] is None
    assert view["prompt"] == ACCEPTED_PAYLOAD["original_messages"]


# -- escalation and the active model ---------------------------------------------------


def test_an_external_run_shows_the_escalation_and_the_external_model(tmp_path):
    """RM13, RM23: the handoff reason is shown, and the external model is the active one."""
    gateway = external_health_gateway(tmp_path)
    outcome = run_to_rest(gateway, execution_mode="external_only")
    view = view_of(gateway, outcome)

    assert view["execution_mode"] == {"requested": "external_only", "effective": "external_only"}
    assert view["escalation"]["reason_code"] == "execution_mode"
    assert view["escalation"]["model"] == EXTERNAL_MODEL_ID
    assert view["active_model"] == EXTERNAL_MODEL_ID
    assert view["final_answer"]["model"] == EXTERNAL_MODEL_ID
    assert {c["actor"] for c in view["model_calls"]} == {"external"}
    assert view["skill"] is None


def test_a_run_that_needs_an_external_model_shows_why(tmp_path):
    """RM04, RM23: a failed run shows its error code and reason code as events recorded them."""
    gateway = health_gateway(tmp_path, external_turns=None)
    outcome = run_to_rest(gateway, "Why is staging-api slow?", execution_mode="local_only")
    view = view_of(gateway, outcome)

    assert view["state"] == outcome.state.value
    if outcome.state.value == "failed":
        assert view["error_code"] == outcome.error_code
        assert view["reason_code"] == outcome.reason_code
        assert view["final_answer"] is None


# -- failed operations ------------------------------------------------------------------


def test_a_failed_operation_is_listed_with_its_error(tmp_path):
    """RM10, RM23: a user can inspect failed operations."""
    gateway = health_gateway(tmp_path, external_turns=None)

    def broken(arguments):
        raise RuntimeError("sandbox unreachable")

    replace_handler(gateway, "get_service_health", broken)
    outcome = run_to_rest(gateway)
    view = view_of(gateway, outcome)

    assert view["failed_operations"], view["operations"]
    failed = view["failed_operations"][0]
    assert failed["status"] == "failed"
    assert failed["tool_name"] == "get_service_health"
    assert failed["error"]["code"] == "tool_failure"
    assert failed in view["operations"]
    assert all(op["status"] != "succeeded" for op in view["failed_operations"])


# -- clarification ------------------------------------------------------------------------


def test_a_clarification_shows_the_question_and_then_the_answer(tmp_path):
    """RM23, section 5: the question is shown while waiting, and the answer once given."""
    gateway = clarifying_gateway(tmp_path)
    waiting = run_to_rest(gateway, "Is the service healthy?")
    view = view_of(gateway, waiting)

    assert view["state"] == "waiting_input"
    (asked,) = view["clarifications"]
    assert asked["question"] == "Which service do you mean?"
    assert asked["missing"] == "skill_selection"
    assert asked["answer"] is None
    assert view["final_answer"] is None


# -- confirmation -----------------------------------------------------------------------


def test_a_pending_confirmation_shows_what_will_be_changed(tmp_path):
    """RM11, RM23: the tool, arguments, target, and expiry, and no token."""
    gateway = delete_gateway(tmp_path)
    waiting = run_to_rest(gateway, DELETE_PROMPT)
    view = view_of(gateway, waiting)
    confirmation = view["confirmation"]

    assert view["state"] == "waiting_confirmation"
    assert confirmation["status"] == "pending"
    assert confirmation["tool_name"] == "delete_demo_database"
    assert confirmation["arguments"] == DELETE_ARGS
    assert confirmation["target"] == "demo-orders-db"
    assert confirmation["expires_at"].endswith("Z")
    assert confirmation["approval_id"] == waiting.confirmation.approval_id
    assert confirmation["model"] == LOCAL_MODEL_ID
    assert waiting.confirmation.token not in repr(view)


def test_an_approved_confirmation_is_shown_as_approved(tmp_path):
    """RM11, RM23: the executed operation names the approval that authorized it."""
    gateway = delete_gateway(
        tmp_path,
        text_turn(DELETE_ANSWER),
        text_turn(DELETE_ANSWER),
    )
    waiting = run_to_rest(gateway, DELETE_PROMPT)
    request = waiting.confirmation
    version = gateway.store.get_run(waiting.run_id).version
    asyncio.run(
        gateway.orchestrator.resume_confirmation(
            waiting.run_id,
            version,
            approval_id=request.approval_id,
            token=request.token,
            approve=True,
        )
    )
    view = view_of(gateway, waiting)

    assert view["state"] == "completed"
    assert view["confirmation"]["status"] == "approved"
    assert view["confirmation"]["approval_id"] == request.approval_id
    assert [op["tool_name"] for op in view["operations"]] == [
        "list_demo_databases",
        "delete_demo_database",
    ]


def test_an_approved_operation_answered_from_its_record_is_approved(tmp_path):
    """RM10, RM11: a replayed operation has no tool.started; tool.finished names the approval."""
    gateway = delete_gateway(tmp_path, text_turn(DELETE_ANSWER), text_turn(DELETE_ANSWER))
    waiting = run_to_rest(gateway, DELETE_PROMPT)
    request = waiting.confirmation
    version = gateway.store.get_run(waiting.run_id).version
    asyncio.run(
        gateway.orchestrator.resume_confirmation(
            waiting.run_id,
            version,
            approval_id=request.approval_id,
            token=request.token,
            approve=True,
        )
    )
    record = gateway.store.get_run(waiting.run_id)
    # As a replay records it: no tool.started, a tool.finished marked replayed.
    events = [
        replace(e, payload={**e.payload, "replayed": True})
        if e.event_type.value == "tool.finished" and e.payload.get("approval_id")
        else e
        for e in gateway.store.events(waiting.run_id)
        if e.event_type.value != "tool.started"
    ]

    view = trace_view_from(record, events)
    assert view["confirmation"]["status"] == "approved"


def test_a_denied_confirmation_is_shown_as_denied_and_the_run_as_cancelled(tmp_path):
    """RM11, RM23: a denial cancels the run with user_denied; nothing was mutated."""
    gateway = delete_gateway(tmp_path)
    waiting = run_to_rest(gateway, DELETE_PROMPT)
    request = waiting.confirmation
    version = gateway.store.get_run(waiting.run_id).version
    asyncio.run(
        gateway.orchestrator.resume_confirmation(
            waiting.run_id,
            version,
            approval_id=request.approval_id,
            token=request.token,
            approve=False,
        )
    )
    view = view_of(gateway, waiting)

    assert view["state"] == "cancelled"
    assert view["cancel_reason"] == "user_denied"
    assert view["confirmation"]["status"] == "denied"
    assert [op["tool_name"] for op in view["operations"]] == ["list_demo_databases"]


def test_an_expired_confirmation_is_not_shown_as_pending(tmp_path):
    """RM11, RM17, section 5: a run that ended on an unused approval shows it as expired."""
    gateway = delete_gateway(tmp_path)
    waiting = run_to_rest(gateway, DELETE_PROMPT)
    gateway.clock.advance(301)
    gateway.orchestrator.expire_waiting()
    view = view_of(gateway, waiting)

    assert view["state"] == "failed"
    assert view["error_code"] == "approval_expired"
    assert view["confirmation"]["status"] == "expired"
    assert view["timing"]["waiting_ms"] >= 300_000


# -- the view is a copy ---------------------------------------------------------------------


def test_building_a_view_does_not_change_the_events(tmp_path):
    """RM23: the UI renders evidence; it changes none."""
    gateway = health_gateway(tmp_path)
    outcome = run_to_rest(gateway)
    before = copy.deepcopy([e.to_json_object() for e in gateway.store.events(outcome.run_id)])

    view = view_of(gateway, outcome)
    view["operations"][0]["data"]["status"] = "unavailable"

    after = [e.to_json_object() for e in gateway.store.events(outcome.run_id)]
    assert before == after


@pytest.mark.parametrize("missing", ["prompt", "skill", "route", "escalation", "final_answer"])
def test_absent_evidence_is_none_never_invented(tmp_path, missing):
    """RM23: a field the events do not contain is None, not a guess."""
    gateway = health_gateway(tmp_path)
    outcome = run_to_rest(gateway)
    record = gateway.store.get_run(outcome.run_id)
    kept = [
        e
        for e in gateway.store.events(outcome.run_id)
        if e.event_type.value
        not in {
            "prompt": set(),
            "skill": {"skill.loaded"},
            "route": {"route.selected"},
            "escalation": {"handoff.started"},
            "final_answer": {"answer.delta"},
        }[missing]
    ]
    if missing == "prompt":
        kept[0] = without_original_messages(kept[0])
    view = trace_view_from(record, kept)
    assert view[missing] is None


# -- a tool result stored by reference (spec 0.30 sections 8 and 9) -------------------


def _offloaded_run(tmp_path):
    """A local health run whose tool result the store keeps as an artifact (RM22)."""
    gateway = health_gateway(tmp_path)
    gateway.store.max_inline_result_bytes = 1
    outcome = run_to_rest(gateway, execution_mode="local_only")
    events = gateway.store.events(outcome.run_id)
    (finished,) = of_type(events, "tool.finished")
    assert "result" not in finished.payload
    return gateway, outcome, events, finished.payload["result_ref"]


def test_an_offloaded_tool_result_is_read_back_from_its_artifact(tmp_path):
    """RM22, RM23: the view shows the stored result and that it was stored by reference."""
    gateway, outcome, _, ref = _offloaded_run(tmp_path)
    view = view_of(gateway, outcome)
    (operation,) = view["operations"]
    assert operation["status"] == "succeeded"
    assert operation["data"]["status"] == "healthy"
    assert operation["result_ref"] == ref
    assert operation["result_retained"] is True
    html = render_trace(view, UiSettings(mode=DataMode.DOUBLE))
    assert "stored by reference" in html
    assert f"{ref['size_bytes']} bytes" in html


def test_an_offloaded_result_that_is_not_retained_shows_its_status_and_reference(tmp_path):
    """RM23: no invented result; the event's status and the reference are shown."""
    gateway, outcome, events, ref = _offloaded_run(tmp_path)
    record = gateway.store.get_run(outcome.run_id)
    for view in (
        trace_view_from(record, events),
        trace_view_from(record, events, read_artifact=lambda artifact_id: None),
    ):
        (operation,) = view["operations"]
        assert operation["status"] == "succeeded"
        assert operation["data"] is None and operation["error"] is None
        assert operation["result_ref"] == ref
        assert operation["result_retained"] is False
        html = render_trace(view, UiSettings(mode=DataMode.DOUBLE))
        assert "stored by reference" in html and "not retained" in html


def test_a_stored_result_is_read_once_per_view(tmp_path):
    """Spec 0.30 section 9: each distinct artifact is read back once for one view."""
    gateway, outcome, events, ref = _offloaded_run(tmp_path)
    reads = []

    def read(artifact_id):
        reads.append(artifact_id)
        return gateway.store.read_artifact(artifact_id)

    (finished,) = of_type(events, "tool.finished")
    record = gateway.store.get_run(outcome.run_id)
    view = trace_view_from(record, [*events, finished], read_artifact=read)
    assert [op["result_retained"] for op in view["operations"]] == [True, True]
    assert reads == [ref["artifact_id"]]


def test_an_inline_tool_result_has_no_reference(tmp_path):
    """RM23: a result kept in its event names no artifact."""
    gateway = health_gateway(tmp_path)
    outcome = run_to_rest(gateway, execution_mode="local_only")
    (operation,) = view_of(gateway, outcome)["operations"]
    assert operation["result_ref"] is None and operation["result_retained"] is True
