"""AC18, prefetch: read-only calls a skill declares run before its first generation
(RM29, RM05, RM08 to RM10, RM12, RM14, RM22, spec 0.32 sections 4.2, 8, 9, 14.1; #123).

Under performance.prefetch = true, the gateway resolves each declared
call from the request by the RM05 rule, runs the resolved calls
concurrently through the executor with every check of a model proposal
(actor gateway, mechanism prefetch, model null), charges them to the
budgets, and puts the results into the conversation as tool exchanges.
When nothing is pending afterwards, the first skill generation offers no
tools. Under the default, false, the declarations stay idle and every
earlier test passes unchanged. Deterministic, with model doubles.
"""

import asyncio
import json
import threading

import pytest

from gateway_helpers import (
    PAYMENTS_OK,
    STAGING_OK,
    assert_needs_external,
    build_gateway,
    executed_tools,
    local_eligible_skills,
    purposes,
    replace_handler,
    route_turn,
    run,
    text_turn,
    tool_turn,
)
from helpers import of_type
from relaymind.adapters import Role
from relaymind.orchestrator.budgets import BudgetMeter, LocalBudgets
from relaymind.orchestrator.performance import PerformanceSettings
from relaymind.orchestrator.prefetch import PrefetchBudgetError, resolve_arguments
from relaymind.orchestrator.run_state import RunState
from relaymind.skills import PrefetchBinding, PrefetchCall
from relaymind.skills.registry import CONSTANT, REQUEST
from relaymind.tools import ToolUnavailableError
from relaymind.ui.trace_view import build_trace_view
from test_ac02_handoff import ANSWER

PREFETCH = PerformanceSettings(prefetch=True)
HEALTHY = "Is staging-api healthy right now?"
SLOW = "Why is staging-api slow right now?"


def prefetch_gateway(tmp_path, turns, **options):
    options.setdefault("performance", PREFETCH)
    options.setdefault("external_turns", [])
    return build_gateway(tmp_path, turns, **options)


def health_turns(*tail):
    return [route_turn("check-service-health"), *tail]


def proposals(events):
    return [e for e in of_type(events, "tool.proposed")]


def index_of(events, event_type, **match):
    for position, event in enumerate(events):
        if event.event_type.value == event_type and all(
            event.payload.get(k) == v for k, v in match.items()
        ):
            return position
    raise AssertionError(f"no {event_type} {match}")


# -- the resolved call runs before the first skill generation ---------------------------------


def test_the_declared_call_runs_before_the_first_skill_generation(tmp_path):
    """AC18, RM29: get_service_health runs first, as the gateway, with mechanism prefetch."""
    gateway = prefetch_gateway(tmp_path, health_turns(text_turn(STAGING_OK), text_turn(STAGING_OK)))
    outcome, events = run(gateway, HEALTHY)
    assert outcome.state is RunState.COMPLETED
    assert outcome.answer == STAGING_OK
    (proposed,) = proposals(events)
    assert proposed.actor == "gateway"
    assert proposed.payload["mechanism"] == "prefetch"
    assert proposed.payload["model"] is None
    assert proposed.payload["tool_name"] == "get_service_health"
    assert proposed.payload["arguments"] == {"service": "staging-api"}
    assert proposed.payload["admissible"] is True and proposed.payload["rejection"] is None
    assert proposed.payload["skill_id"] == "check-service-health"
    # The order of the trace: the skill loads, the call runs, then the first skill generation.
    first_skill = next(
        i
        for i, e in enumerate(events)
        if e.event_type.value == "model.started" and e.payload["purpose"] == "skill"
    )
    assert (
        index_of(events, "skill.loaded")
        < index_of(events, "tool.proposed")
        < index_of(events, "tool.started")
        < index_of(events, "tool.finished")
        < first_skill
    )
    for kind in ("tool.started", "tool.finished"):
        (event,) = of_type(events, kind)
        assert event.actor == "gateway"
        assert event.payload["model"] is None
        assert event.payload["proposal_id"] == proposed.payload["proposal_id"]
    assert executed_tools(events) == ["get_service_health"]


def test_the_first_skill_request_holds_the_exchange_and_offers_no_tools(tmp_path):
    """AC18, RM29, RM12: nothing is pending, so the first generation is an answer generation."""
    gateway = prefetch_gateway(tmp_path, health_turns(text_turn(STAGING_OK), text_turn(STAGING_OK)))
    outcome, events = run(gateway, HEALTHY)
    first = gateway.adapter.requests[1]  # after the routing request
    assert first.tool_schemas == ()
    (started,) = [e for e in of_type(events, "model.started") if e.payload["purpose"] == "skill"]
    assert started.payload["tools_offered"] == []
    # The tool exchange is data in the conversation: the call, then its result.
    messages = list(first.messages)
    call_message = next(m for m in messages if m.role is Role.ASSISTANT and m.tool_calls)
    (call,) = call_message.tool_calls
    assert (call.tool_name, dict(call.arguments)) == (
        "get_service_health",
        {"service": "staging-api"},
    )
    result = messages[messages.index(call_message) + 1]
    assert result.role is Role.TOOL and result.tool_call_id == call.call_id
    body = json.loads(result.content)
    assert body["tool_name"] == "get_service_health" and body["status"] == "succeeded"
    (operation,) = gateway.store.tool_operations(outcome.run_id)
    assert body["operation_id"] == operation["operation_id"]
    # Later generations are unchanged: the final pass still runs under the default answer pass.
    assert purposes(events) == ["routing", "skill", "final"]


def test_a_run_completes_with_one_generation_fewer_than_without_prefetch(tmp_path):
    """RM29, section 12.1: the round trip for the tool call is gone."""
    plain = build_gateway(
        tmp_path / "plain",
        health_turns(
            tool_turn("get_service_health", service="staging-api"),
            text_turn(STAGING_OK),
            text_turn(STAGING_OK),
        ),
        external_turns=[],
    )
    _, plain_events = run(plain, HEALTHY)
    gateway = prefetch_gateway(
        tmp_path / "pre", health_turns(text_turn(STAGING_OK), text_turn(STAGING_OK))
    )
    _, events = run(gateway, HEALTHY)
    assert purposes(plain_events) == ["routing", "skill", "skill", "final"]
    assert purposes(events) == ["routing", "skill", "final"]
    assert executed_tools(events) == executed_tools(plain_events)


def test_the_call_is_charged_to_the_budgets(tmp_path):
    """RM03, RM29: a prefetch call counts as an executed tool call."""
    gateway = prefetch_gateway(tmp_path, health_turns(text_turn(STAGING_OK), text_turn(STAGING_OK)))
    _, events = run(gateway, HEALTHY)
    usage = of_type(events, "run.completed")[0].payload["budget_usage"]
    assert usage["tool_calls"] == 1


def test_the_option_and_the_declarations_are_recorded(tmp_path):
    """RM29, section 8: run.accepted names prefetch and the offered skills' declarations."""
    gateway = prefetch_gateway(tmp_path, health_turns(text_turn(STAGING_OK), text_turn(STAGING_OK)))
    _, events = run(gateway, HEALTHY)
    accepted = of_type(events, "run.accepted")[0].payload
    assert accepted["prefetch"] is True
    declarations = accepted["prefetch_declarations"]
    assert set(declarations) == {entry["name"] for entry in accepted["skills_offered"]}
    assert declarations["check-service-health"] == [
        {"tool": "get_service_health", "arguments": {"service": {"from": "request"}}}
    ]
    assert declarations["investigate-performance"] == [
        {"tool": "get_latest_deployment", "arguments": {"service": {"from": "request"}}},
        {
            "tool": "get_recent_logs",
            "arguments": {"service": {"from": "request"}, "limit": {"constant": 50}},
        },
    ]
    # A skill with none has an empty array (spec 0.32).
    assert declarations["summarize-incident-counts"] == []
    assert declarations["delete-demo-database"] == []


# -- what the request must say (RM05) -------------------------------------------------------


def test_a_request_that_names_two_services_prefetches_nothing(tmp_path):
    """RM05, RM29: more than one allowed value in the user turns is ambiguous, so the call is
    skipped and the model proposes it as before."""
    gateway = prefetch_gateway(
        tmp_path,
        health_turns(
            tool_turn("get_service_health", service="staging-api"),
            text_turn(STAGING_OK),
            text_turn(STAGING_OK),
        ),
    )
    outcome, events = run(gateway, "Is staging-api healthy, or payments-api?")
    assert outcome.state is RunState.COMPLETED
    (proposed,) = proposals(events)
    assert proposed.actor == "local" and "mechanism" not in proposed.payload
    assert purposes(events) == ["routing", "skill", "skill", "final"]
    # The first skill request still offers the skill's tool.
    assert [t.name for t in gateway.adapter.requests[1].tool_schemas] == ["get_service_health"]


def test_a_request_without_a_service_asks_first_and_prefetches_after_the_answer(tmp_path):
    """RM05, RM29, section 5: nothing is prefetched for a clarification; the answer names the
    service, and only the user's turn counts, not the question that lists both services."""
    gateway = prefetch_gateway(
        tmp_path,
        [route_turn("check-service-health"), text_turn(PAYMENTS_OK), text_turn(PAYMENTS_OK)],
    )
    outcome = asyncio.run(gateway.orchestrator.run_text("Is the service healthy?"))
    assert outcome.state is RunState.WAITING_INPUT
    events = gateway.store.events(outcome.run_id)
    assert not proposals(events)
    assert executed_tools(events) == []
    record = gateway.store.get_run(outcome.run_id)
    resumed = asyncio.run(
        gateway.orchestrator.resume(outcome.run_id, record.version, "payments-api")
    )
    events = gateway.store.events(outcome.run_id)
    assert resumed.state is RunState.COMPLETED
    (proposed,) = proposals(events)
    assert proposed.payload["arguments"] == {"service": "payments-api"}
    assert proposed.payload["mechanism"] == "prefetch"
    # The resumed stage ran without a tool generation: routing, then the answer generations.
    assert purposes(events) == ["routing", "skill", "final"]


def test_an_ambiguous_request_skips_the_calls_not_the_skill(tmp_path, skills):
    """RM29: a request naming two services resolves neither identifier. The calls are skipped,
    no manifest error is raised, and the skill runs with all its tools offered."""
    gateway = build_gateway(
        tmp_path,
        [route_turn("investigate-performance"), text_turn(""), text_turn("")],
        skill_roots=skills,
        performance=PREFETCH,
    )
    outcome, _ = run(gateway, "Why is it slow? Compare staging-api and payments-api.")
    assert gateway.store.tool_operations(outcome.run_id) == []
    assert [t.name for t in gateway.adapter.requests[1].tool_schemas] == [
        "get_latest_deployment",
        "get_metrics",
        "get_recent_logs",
    ]


# -- several declared calls ---------------------------------------------------------------------


@pytest.fixture
def skills(tmp_path):
    return local_eligible_skills(tmp_path)


def test_the_declared_calls_run_concurrently_and_leave_the_pending_tool(tmp_path, skills):
    """RM29: two resolved calls run together; get_metrics stays pending, so tools are offered."""
    barrier = threading.Barrier(2, timeout=1.5)
    gateway = build_gateway(
        tmp_path,
        [route_turn("investigate-performance"), text_turn(ANSWER), text_turn(ANSWER)],
        skill_roots=skills,
        performance=PREFETCH,
        external_turns=[],
    )
    for name in ("get_latest_deployment", "get_recent_logs"):
        handler = gateway.tools.get(name).declaration.handler

        def waiting(arguments, handler=handler):
            barrier.wait()  # both handlers must be running at once to pass
            return handler(arguments)

        replace_handler(gateway, name, waiting)
    outcome, events = run(gateway, SLOW)
    assert outcome.state is RunState.COMPLETED
    assert sorted(executed_tools(events)) == ["get_latest_deployment", "get_recent_logs"]
    first, second = proposals(events)
    # Proposals follow the declared order; the logs call has the documented constant limit.
    assert (first.payload["tool_name"], second.payload["tool_name"]) == (
        "get_latest_deployment",
        "get_recent_logs",
    )
    assert second.payload["arguments"] == {"service": "staging-api", "limit": 50}
    assert {e.payload["mechanism"] for e in proposals(events)} == {"prefetch"}
    # The exchange keeps the declared order whatever finished first.
    request = gateway.adapter.requests[1]
    assistant = next(m for m in request.messages if m.role is Role.ASSISTANT and m.tool_calls)
    assert [c.tool_name for c in assistant.tool_calls] == [
        "get_latest_deployment",
        "get_recent_logs",
    ]
    answers = [m for m in request.messages if m.role is Role.TOOL]
    assert [json.loads(m.content)["tool_name"] for m in answers] == [
        "get_latest_deployment",
        "get_recent_logs",
    ]
    # get_metrics is not prefetched (its window is semantic), so it is still offered.
    assert [t.name for t in request.tool_schemas] == [
        "get_latest_deployment",
        "get_metrics",
        "get_recent_logs",
    ]


# -- the local actor keeps the run (RM14) ---------------------------------------------------------


def test_the_local_actor_holds_dispatch_authority_after_prefetch(tmp_path, skills):
    """RM14, RM29: prefetch does not move authority; the local model proposes and answers."""
    gateway = build_gateway(
        tmp_path,
        [
            route_turn("investigate-performance"),
            tool_turn("get_metrics", service="staging-api", window_minutes=60),
            text_turn(ANSWER),
            text_turn(ANSWER),
        ],
        skill_roots=skills,
        performance=PREFETCH,
        budgets=LocalBudgets(tool_calls=3, local_generations=4),
        external_turns=[],
    )
    outcome, events = run(gateway, SLOW)
    assert outcome.state is RunState.COMPLETED
    assert gateway.external.requests == []
    assert {e.actor for e in of_type(events, "answer.delta")} == {"local"}
    assert of_type(events, "run.completed")[0].payload["final_answer_model"] == "double-qwen3-0.6b"
    by_actor = {(e.actor, e.payload["tool_name"]) for e in proposals(events)}
    assert by_actor == {
        ("gateway", "get_latest_deployment"),
        ("gateway", "get_recent_logs"),
        ("local", "get_metrics"),
    }
    assert of_type(events, "run.completed")[0].payload["external_calls"] == 0


def test_a_later_handoff_carries_the_prefetched_operations(tmp_path, skills):
    """RM13, RM29: prefetched operations are completed; the external model replays them."""
    gateway = build_gateway(
        tmp_path,
        [
            route_turn("investigate-performance"),
            # The third tool of the stage is over the per-stage limit of 2.
            tool_turn("get_metrics", service="staging-api", window_minutes=60),
        ],
        skill_roots=skills,
        performance=PREFETCH,
        external_turns=[
            tool_turn("get_recent_logs", call_id="e1", service="staging-api", limit=50),
            text_turn(ANSWER),
        ],
    )
    outcome, events = run(gateway, SLOW)
    assert outcome.state is RunState.COMPLETED
    operations = gateway.store.tool_operations(outcome.run_id)
    prefetched = [op for op in operations if op["tool_name"] != "get_metrics"]
    assert [op["tool_name"] for op in prefetched] == ["get_latest_deployment", "get_recent_logs"]
    assert {op["model"] for op in prefetched} == {"gateway"}
    (handoff,) = of_type(events, "handoff.started")
    assert handoff.payload["operation_ids"] == [op["operation_id"] for op in prefetched]
    # The external model's repeat of the logs call is a replay with its recorded result.
    replays = [e for e in of_type(events, "tool.finished") if e.payload["replayed"]]
    assert [e.payload["tool_name"] for e in replays] == ["get_recent_logs"]
    assert executed_tools(events).count("get_recent_logs") == 1


def test_a_failed_prefetch_call_escalates_like_a_failed_proposal(tmp_path):
    """RM29, section 11: a local tool failure ends the local stage, after the one retry."""
    gateway = build_gateway(
        tmp_path,
        health_turns(),
        performance=PREFETCH,
    )

    def unavailable(arguments):
        raise ToolUnavailableError("the sandbox service is down")

    replace_handler(gateway, "get_service_health", unavailable)
    outcome, events = run(gateway, HEALTHY)
    assert_needs_external(gateway, outcome, events, "tool_failure")
    assert len([e for e in of_type(events, "tool.started")]) == 2  # the attempt and its retry
    assert not of_type(events, "answer.delta")


def test_a_prefetch_call_that_is_not_permitted_is_recorded_as_rejected(tmp_path, skills):
    """RM08, RM29: every check of a model proposal applies; a refusal is on the record, not run."""
    from relaymind.tools import OperatorPolicy

    gateway = build_gateway(
        tmp_path,
        [route_turn("investigate-performance"), text_turn(""), text_turn("")],
        skill_roots=skills,
        performance=PREFETCH,
    )
    gateway.orchestrator.policy = OperatorPolicy(
        enabled_tools=frozenset({"get_latest_deployment", "get_metrics"})
    )
    outcome, events = run(gateway, SLOW)
    declared = [e for e in proposals(events) if e.payload.get("mechanism") == "prefetch"]
    by_tool = {e.payload["tool_name"]: e.payload for e in declared}
    assert by_tool["get_latest_deployment"]["admissible"] is True
    assert by_tool["get_recent_logs"]["admissible"] is False
    assert by_tool["get_recent_logs"]["rejection"]["code"] == "tool_not_permitted"
    assert executed_tools(events) == ["get_latest_deployment"]


def test_prefetch_does_nothing_when_the_budget_has_no_room(tmp_path, monkeypatch):
    """RM03, RM29: a prefetch the tool budget cannot finish is not started."""
    monkeypatch.setattr(BudgetMeter, "tool_room", lambda self: 0)
    gateway = prefetch_gateway(
        tmp_path, health_turns(text_turn(""), text_turn("")), external_turns=None
    )
    outcome, events = run(gateway, HEALTHY)
    assert gateway.store.tool_operations(outcome.run_id) == []
    assert not proposals(events)


# -- the default, and the views -------------------------------------------------------------------


def test_under_false_the_declarations_stay_idle(tmp_path):
    """Section 14.1: the baseline runs exactly as before; the declarations are only recorded."""
    gateway = build_gateway(
        tmp_path,
        health_turns(
            tool_turn("get_service_health", service="staging-api"),
            text_turn(STAGING_OK),
            text_turn(STAGING_OK),
        ),
        external_turns=[],
    )
    outcome, events = run(gateway, HEALTHY)
    assert outcome.state is RunState.COMPLETED
    (proposed,) = proposals(events)
    assert proposed.actor == "local" and "mechanism" not in proposed.payload
    assert purposes(events) == ["routing", "skill", "skill", "final"]
    accepted = of_type(events, "run.accepted")[0].payload
    assert accepted["prefetch"] is False
    assert accepted["prefetch_declarations"]["check-service-health"]
    assert [t.name for t in gateway.adapter.requests[1].tool_schemas] == ["get_service_health"]


def test_the_trace_view_shows_a_prefetch_proposal_as_the_gateway_s(tmp_path):
    """RM23: the UI renders the evidence; a prefetch call names no model and is no model action."""
    gateway = prefetch_gateway(tmp_path, health_turns(text_turn(STAGING_OK), text_turn(STAGING_OK)))
    outcome, events = run(gateway, HEALTHY)
    view = build_trace_view(gateway.store, outcome.run_id)
    (proposal,) = view["proposals"]
    assert proposal["actor"] == "gateway" and proposal["model"] is None
    assert proposal["mechanism"] == "prefetch"
    route = next(e for e in events if e.event_type.value == "route.selected")
    assert view["timing"]["first_model_action_ms"] == route.elapsed_ms


# -- the declared calls against the configured limits (RM29, section 11) -------------------------


def test_a_skill_whose_prefetch_exceeds_the_stage_limit_is_a_startup_error(tmp_path, skills):
    """RM29: investigate-performance declares two calls; a per-stage limit of 1 cannot hold them."""
    with pytest.raises(PrefetchBudgetError, match=r"investigate-performance.*tool_calls is 1"):
        build_gateway(
            tmp_path,
            [],
            skill_roots=skills,
            performance=PREFETCH,
            budgets=LocalBudgets(tool_calls=1),
        )


def test_a_skill_whose_prefetch_exceeds_the_run_limit_is_a_startup_error(tmp_path):
    """RM29, section 11: the entire-run limit applies to every skill, local or external."""
    with pytest.raises(PrefetchBudgetError, match="run_tool_executions is 1"):
        build_gateway(
            tmp_path,
            [],
            performance=PREFETCH,
            budgets=LocalBudgets(tool_calls=1, run_tool_executions=1),
        )


def test_an_external_required_skill_is_checked_against_the_run_limit_only(tmp_path):
    """RM29: investigate-performance has no local stage, so tool_calls does not bind it."""
    build_gateway(
        tmp_path,
        [],
        performance=PREFETCH,
        budgets=LocalBudgets(tool_calls=2, run_tool_executions=2),
    )
    with pytest.raises(PrefetchBudgetError, match="skill 'check-service-health'"):
        build_gateway(
            tmp_path / "again",
            [],
            performance=PREFETCH,
            budgets=LocalBudgets(tool_calls=0, run_tool_executions=6),
        )


def test_the_limits_are_not_checked_while_the_declarations_are_idle(tmp_path):
    """Section 14.1: under false (the default) the budgets are free of the declarations."""
    build_gateway(tmp_path, [], budgets=LocalBudgets(tool_calls=0, run_tool_executions=0))


def test_the_resolution_follows_the_declaration_and_the_rm05_rule():
    """RM05, RM29: exactly one allowed value in the user turns; constants as documented."""
    declared = PrefetchCall(
        "get_recent_logs",
        (
            PrefetchBinding("service", REQUEST),
            PrefetchBinding("limit", CONSTANT, 50),
        ),
    )
    schema = {"properties": {"service": {"enum": ["staging-api", "payments-api"]}}}
    assert resolve_arguments(declared, schema, "slow staging-api?") == {
        "service": "staging-api",
        "limit": 50,
    }
    # A longer ID that contains an allowed value is another resource (RM05).
    assert resolve_arguments(declared, schema, "staging-api.backup is slow") is None
    assert resolve_arguments(declared, schema, "slow?") is None
    assert resolve_arguments(declared, schema, "staging-api and payments-api") is None
    assert resolve_arguments(declared, schema, "staging-api, again staging-api") == {
        "service": "staging-api",
        "limit": 50,
    }
