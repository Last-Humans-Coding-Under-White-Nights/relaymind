"""AC18, admitted calls: several read-only calls from one generation (RM05, RM08 to RM11,
spec 0.32 sections 4.3, 8, 11, 14.1; #122).

Under performance.calls_per_generation = many, up to the per-stage tool
limit of read-only calls are admitted from one generation in a local
stage, and up to the remaining entire-run tool limit in the external
stage. Each call is validated on its own, runs in proposal order through
the executor, and is answered as its own tool message. A mutating call
is admitted only alone; a generation with more calls than the limit
allows, or one invalid call, is rejected as a whole without executing
anything. Under the default, one, every earlier test passes unchanged
(tests/test_local_loop.py is the gate). Deterministic, with model doubles.
"""

import json

import pytest

from gateway_helpers import (
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
from relaymind.adapters import Completed, Role, ToolCall
from relaymind.orchestrator.budgets import LocalBudgets
from relaymind.orchestrator.performance import PerformanceSettings
from relaymind.orchestrator.run_state import RunState
from relaymind.tools import CallerContext, ToolUnavailableError
from relaymind.tools.sandbox import MUTATE_PERMISSION, READ_PERMISSION
from test_ac02_handoff import ANSWER

MANY = PerformanceSettings(calls_per_generation="many")
PROMPT = "Why is staging-api slow right now?"


def call(call_id: str, tool: str, **arguments) -> ToolCall:
    return ToolCall(call_id=call_id, tool_name=tool, arguments=arguments)


def calls_turn(*calls: ToolCall) -> list:
    """One generation that proposes several tool calls."""
    return [*calls, Completed(finish_reason="tool_calls")]


LOGS = call("c_logs", "get_recent_logs", service="staging-api", limit=3)
DEPLOYMENT = call("c_deploy", "get_latest_deployment", service="staging-api")
METRICS = call("c_metrics", "get_metrics", service="staging-api", window_minutes=60)


@pytest.fixture
def skills(tmp_path):
    """The demo skills with investigate-performance allowed to run locally (three read tools)."""
    return local_eligible_skills(tmp_path)


def local_gateway(tmp_path, skills, turns, **options):
    options.setdefault("performance", MANY)
    options.setdefault("external_turns", [])
    return build_gateway(
        tmp_path,
        [route_turn("investigate-performance"), *turns],
        skill_roots=skills,
        **options,
    )


def proposals(events):
    return [e.payload for e in of_type(events, "tool.proposed")]


def tool_messages(request):
    return [m for m in request.messages if m.role is Role.TOOL]


def last_assistant_calls(request):
    return [m for m in request.messages if m.role is Role.ASSISTANT and m.tool_calls][-1]


# -- the admitted set -----------------------------------------------------------------


def test_two_reads_run_in_proposal_order_with_their_own_events(tmp_path, skills):
    """AC18, RM05, RM09, section 4.3: both execute, in the order proposed, each as its own call."""
    gateway = local_gateway(
        tmp_path, skills, [calls_turn(LOGS, DEPLOYMENT), text_turn(ANSWER), text_turn(ANSWER)]
    )
    outcome, events = run(gateway, PROMPT)
    assert outcome.state is RunState.COMPLETED
    assert executed_tools(events) == ["get_recent_logs", "get_latest_deployment"]
    proposed = proposals(events)
    assert [p["call_id"] for p in proposed] == ["c_logs", "c_deploy"]
    assert all(p["admissible"] is True and p["rejection"] is None for p in proposed)
    assert len({p["proposal_id"] for p in proposed}) == 2
    # Each call has its own proposal, start, and finish, in proposal order.
    for kind in ("tool.started", "tool.finished"):
        assert [e.payload["proposal_id"] for e in of_type(events, kind)] == [
            p["proposal_id"] for p in proposed
        ]
    # The proposals come before the first execution: the set was admitted as a whole.
    first_started = of_type(events, "tool.started")[0].seq
    assert all(e.seq < first_started for e in of_type(events, "tool.proposed"))
    # Each execution is charged (RM03), and the model made one generation for both.
    completed = of_type(events, "run.completed")[0].payload
    assert completed["budget_usage"]["tool_calls"] == 2
    assert purposes(events) == ["routing", "skill", "skill", "final"]


def test_the_set_is_one_assistant_message_then_one_tool_message_per_call(tmp_path, skills):
    """Section 4.3: each call is answered as its own tool message, in order (RM12)."""
    gateway = local_gateway(
        tmp_path, skills, [calls_turn(LOGS, DEPLOYMENT), text_turn(ANSWER), text_turn(ANSWER)]
    )
    outcome, events = run(gateway, PROMPT)
    request = gateway.adapter.requests[2]
    assistant = last_assistant_calls(request)
    assert [c.call_id for c in assistant.tool_calls] == ["c_logs", "c_deploy"]
    answers = tool_messages(request)
    assert [m.tool_call_id for m in answers] == ["c_logs", "c_deploy"]
    assert [json.loads(m.content)["tool_name"] for m in answers] == [
        "get_recent_logs",
        "get_latest_deployment",
    ]
    # The assistant message comes right before its tool messages.
    messages = list(request.messages)
    start = messages.index(assistant)
    assert messages[start + 1 : start + 3] == answers
    # The operation IDs are the recorded ones.
    operations = gateway.store.tool_operations(outcome.run_id)
    assert [json.loads(m.content)["operation_id"] for m in answers] == [
        op["operation_id"] for op in operations
    ]


def test_the_option_is_recorded_in_run_accepted(tmp_path, skills):
    """Section 8: run.accepted names calls_per_generation, so a run states its configuration."""
    gateway = local_gateway(
        tmp_path, skills, [calls_turn(LOGS), text_turn(ANSWER), text_turn(ANSWER)]
    )
    _, events = run(gateway, PROMPT)
    assert of_type(events, "run.accepted")[0].payload["calls_per_generation"] == "many"


def test_one_call_runs_as_before_under_many(tmp_path, skills):
    """Section 4.3: a generation with a single call is the one-call path."""
    gateway = local_gateway(
        tmp_path, skills, [calls_turn(LOGS), text_turn(ANSWER), text_turn(ANSWER)]
    )
    outcome, events = run(gateway, PROMPT)
    assert outcome.state is RunState.COMPLETED
    assert executed_tools(events) == ["get_recent_logs"]
    assistant = last_assistant_calls(gateway.adapter.requests[2])
    assert [c.call_id for c in assistant.tool_calls] == ["c_logs"]


def test_a_repeated_call_in_a_set_replays_and_is_charged_once(tmp_path, skills):
    """RM10: the second identical call is the same operation, so it runs no handler."""
    gateway = local_gateway(
        tmp_path,
        skills,
        [
            calls_turn(LOGS, call("c_again", "get_recent_logs", service="staging-api", limit=3)),
            text_turn(ANSWER),
            text_turn(ANSWER),
        ],
    )
    seen = []
    original = gateway.tools.get("get_recent_logs").declaration.handler
    replace_handler(gateway, "get_recent_logs", lambda a: seen.append(a) or original(a))
    outcome, events = run(gateway, PROMPT)
    assert outcome.state is RunState.COMPLETED
    assert len(seen) == 1
    finished = of_type(events, "tool.finished")
    assert [e.payload["replayed"] for e in finished] == [False, True]
    assert of_type(events, "run.completed")[0].payload["budget_usage"]["tool_calls"] == 1
    answers = tool_messages(gateway.adapter.requests[2])
    assert [m.tool_call_id for m in answers] == ["c_logs", "c_again"]
    assert answers[0].content == answers[1].content


# -- rejection of the whole generation ---------------------------------------------------


def assert_every_call_answered(request, rejection_code, *, offending=0):
    """A rejection exchange answers every call: the offending one with the error, the rest not run.

    offending is the position of the call that was rejected; a set-level
    rejection (limit, mutation, budget) answers the first call.
    """
    assistant = last_assistant_calls(request)
    answers = tool_messages(request)[-len(assistant.tool_calls) :]
    assert [m.tool_call_id for m in answers] == [c.call_id for c in assistant.tool_calls]
    bodies = [json.loads(m.content) for m in answers]
    assert bodies[offending]["error"]["code"] == rejection_code
    others = bodies[:offending] + bodies[offending + 1 :]
    assert {b["error"]["code"] for b in others} <= {"not_executed"}
    assert all("error" in b for b in bodies)


def test_three_reads_over_the_stage_limit_are_rejected_without_execution(tmp_path, skills):
    """AC18, section 4.3: more calls than the per-stage limit of 2 is multiple_tool_calls."""
    gateway = local_gateway(
        tmp_path,
        skills,
        [
            calls_turn(LOGS, DEPLOYMENT, METRICS),
            calls_turn(LOGS, DEPLOYMENT),
            text_turn(ANSWER),
            text_turn(ANSWER),
        ],
        budgets=LocalBudgets(local_generations=5),
    )
    outcome, events = run(gateway, PROMPT)
    assert outcome.state is RunState.COMPLETED
    rejected = [p for p in proposals(events) if p["admissible"] is False]
    assert [p["call_id"] for p in rejected] == ["c_logs", "c_deploy", "c_metrics"]
    assert {p["rejection"]["code"] for p in rejected} == {"multiple_tool_calls"}
    # Nothing ran for the rejected generation; the repair generation's pair did.
    assert executed_tools(events) == ["get_recent_logs", "get_latest_deployment"]
    assert purposes(events)[:3] == ["routing", "skill", "repair"]
    assert_every_call_answered(gateway.adapter.requests[2], "multiple_tool_calls")


def test_a_set_the_remaining_stage_budget_cannot_finish_is_not_started(tmp_path, skills):
    """Section 4.3, 11: a set never runs in part because the tool budget ran out in the middle."""
    gateway = local_gateway(
        tmp_path,
        skills,
        [
            calls_turn(LOGS),
            calls_turn(DEPLOYMENT, METRICS),
            calls_turn(DEPLOYMENT),
            text_turn(ANSWER),
            text_turn(ANSWER),
        ],
        budgets=LocalBudgets(local_generations=5),
    )
    outcome, events = run(gateway, PROMPT)
    assert outcome.state is RunState.COMPLETED
    rejected = [p for p in proposals(events) if p["admissible"] is False]
    assert [p["call_id"] for p in rejected] == ["c_deploy", "c_metrics"]
    assert {p["rejection"]["code"] for p in rejected} == {"multiple_tool_calls"}
    # Only the single calls ran; neither call of the refused pair did.
    assert executed_tools(events) == ["get_recent_logs", "get_latest_deployment"]
    assert_every_call_answered(gateway.adapter.requests[3], "multiple_tool_calls")


def test_a_mutating_call_with_other_calls_is_rejected_without_execution(tmp_path):
    """AC18, section 4.3, RM11: a mutating call is admitted only as the generation's single call."""
    delete = call(
        "c_delete", "delete_demo_database", database_id="demo-orders-db", expected_revision=3
    )
    gateway = build_gateway(
        tmp_path,
        [
            route_turn("delete-demo-database"),
            calls_turn(call("c_list", "list_demo_databases"), delete),
            calls_turn(delete, call("c_list", "list_demo_databases")),
        ],
        caller=CallerContext(permissions=frozenset({READ_PERMISSION, MUTATE_PERMISSION})),
        performance=MANY,
    )
    outcome, events = run(gateway, "Delete the demo-orders-db sandbox database.")
    # The second rejection finds no repair left, so the local stage stops (RM04).
    assert_needs_external(gateway, outcome, events, "invalid_output")
    assert executed_tools(events) == []
    assert not of_type(events, "confirmation.required")
    assert {p["rejection"]["code"] for p in proposals(events)} == {"multiple_tool_calls"}
    assert len(proposals(events)) == 4


@pytest.mark.parametrize(
    ("bad", "code"),
    [
        pytest.param(
            call("c_bad", "get_recent_logs", service="staging-api", limit=500),
            "invalid_arguments",
            id="schema",
        ),
        pytest.param(
            call("c_bad", "invented_tool", service="staging-api"), "unknown_tool", id="name"
        ),
        pytest.param(
            call("c_bad", "get_recent_logs", service="payments-api", limit=3),
            "unsupported_identifier",
            id="provenance",
        ),
    ],
)
def test_one_invalid_call_rejects_the_whole_generation(tmp_path, skills, bad, code):
    """AC18, RM05, RM09, section 4.3: no call of the set runs; the first rejection is reported.

    The valid call comes first and the invalid one second, and the whole
    generation is refused, which goes back through the format repair
    (section 11). The rejection belongs to the call that failed: the
    valid call is traced and answered as not executed, never as malformed.
    """
    gateway = local_gateway(
        tmp_path,
        skills,
        [
            calls_turn(DEPLOYMENT, bad),
            calls_turn(DEPLOYMENT, LOGS),
            text_turn(ANSWER),
            text_turn(ANSWER),
        ],
        budgets=LocalBudgets(local_generations=5),
    )
    outcome, events = run(gateway, PROMPT)
    assert outcome.state is RunState.COMPLETED
    refused = [p for p in proposals(events) if p["admissible"] is False]
    assert [p["call_id"] for p in refused] == ["c_deploy", "c_bad"]
    assert [p["rejection"]["code"] for p in refused] == ["not_executed", code]
    assert purposes(events)[:3] == ["routing", "skill", "repair"]
    # The refused generation ran nothing; the repair's set ran in full.
    assert executed_tools(events) == ["get_latest_deployment", "get_recent_logs"]
    assert_every_call_answered(gateway.adapter.requests[2], code, offending=1)


def test_the_first_rejection_of_the_set_is_the_one_reported(tmp_path, skills):
    """Section 4.3: with two invalid calls, the first in proposal order is reported."""
    first = call("c_first", "get_recent_logs", service="staging-api", limit=500)
    second = call("c_second", "invented_tool", service="staging-api")
    gateway = local_gateway(
        tmp_path,
        skills,
        [calls_turn(first, second), calls_turn(LOGS), text_turn(ANSWER), text_turn(ANSWER)],
        budgets=LocalBudgets(local_generations=5),
    )
    _, events = run(gateway, PROMPT)
    answers = tool_messages(gateway.adapter.requests[2])
    assert json.loads(answers[0].content)["error"]["code"] == "invalid_arguments"
    assert json.loads(answers[1].content)["error"]["code"] == "not_executed"
    assert [p["rejection"]["code"] for p in proposals(events)[:2]] == [
        "invalid_arguments",
        "not_executed",
    ]
    assert executed_tools(events) == ["get_recent_logs"]


def test_a_retry_can_use_the_last_of_the_budget_in_the_middle_of_a_set(tmp_path, skills):
    """AC18, RM03, section 11: the room counts one execution per call; a retry is charged too.

    Two reads fit the default tool budget of 2, so the set is admitted. The
    first read fails once and its retry is a second execution, so the
    second read finds the budget used up: the local stage stops on the
    tool budget with the second call not run, as it would for a single
    call, and the run hands off. The set is documented not to reserve
    room for retries.
    """
    gateway = local_gateway(
        tmp_path,
        skills,
        [calls_turn(LOGS, DEPLOYMENT)],
        external_turns=[calls_turn(DEPLOYMENT, METRICS), text_turn(ANSWER)],
    )
    attempts = []
    original = gateway.tools.get("get_recent_logs").declaration.handler

    def flaky(arguments):
        attempts.append(arguments)
        if len(attempts) == 1:
            raise ToolUnavailableError("down once")
        return original(arguments)

    replace_handler(gateway, "get_recent_logs", flaky)
    outcome, events = run(gateway, PROMPT)
    assert len(attempts) == 2
    (handoff,) = of_type(events, "handoff.started")
    assert handoff.payload["reason_code"] == "local_budget"
    # The local stage ran the failed read and its retry; the second call never started there.
    local = [e.payload["tool_name"] for e in of_type(events, "tool.started") if e.seq < handoff.seq]
    assert local == ["get_recent_logs", "get_recent_logs"]


# -- the external stage ------------------------------------------------------------------------


def external_gateway(tmp_path, turns, **options):
    options.setdefault("performance", MANY)
    return build_gateway(
        tmp_path,
        [route_turn("investigate-performance")],
        external_turns=turns,
        **options,
    )


def test_the_external_limit_is_the_remaining_run_tool_limit(tmp_path):
    """AC18, section 4.3: three reads pass the run limit though not the local limit of 2."""
    gateway = external_gateway(tmp_path, [calls_turn(LOGS, DEPLOYMENT, METRICS), text_turn(ANSWER)])
    outcome, events = run(gateway, PROMPT)
    assert outcome.state is RunState.COMPLETED
    assert executed_tools(events) == ["get_recent_logs", "get_latest_deployment", "get_metrics"]
    assert {e.actor for e in of_type(events, "tool.proposed")} == {"external"}
    request = gateway.external.requests[1]
    assistant = last_assistant_calls(request)
    assert [c.call_id for c in assistant.tool_calls][-3:] == ["c_logs", "c_deploy", "c_metrics"]
    assert [m.tool_call_id for m in tool_messages(request)][-3:] == [
        "c_logs",
        "c_deploy",
        "c_metrics",
    ]


def test_an_external_set_over_the_remaining_run_limit_is_rejected(tmp_path):
    """AC18, section 4.3, 11: with 2 executions left, a set of 3 runs nothing."""
    gateway = external_gateway(
        tmp_path,
        [calls_turn(LOGS, DEPLOYMENT, METRICS), calls_turn(LOGS, DEPLOYMENT), text_turn(ANSWER)],
        budgets=LocalBudgets(run_tool_executions=2),
    )
    outcome, events = run(gateway, PROMPT)
    assert outcome.state is RunState.COMPLETED
    rejected = [p for p in proposals(events) if p["admissible"] is False]
    assert [p["call_id"] for p in rejected] == ["c_logs", "c_deploy", "c_metrics"]
    assert {p["rejection"]["code"] for p in rejected} == {"multiple_tool_calls"}
    assert executed_tools(events) == ["get_recent_logs", "get_latest_deployment"]
    assert_every_call_answered(gateway.external.requests[1], "multiple_tool_calls")


def test_an_external_set_counts_the_tools_the_local_stage_ran(tmp_path, skills):
    """Section 4.3, 11: the external limit is what the entire run has left after the local stage."""
    gateway = build_gateway(
        tmp_path,
        [
            route_turn("investigate-performance"),
            calls_turn(LOGS, DEPLOYMENT),
            calls_turn(METRICS),
        ],
        skill_roots=skills,
        external_turns=[calls_turn(METRICS, LOGS), calls_turn(METRICS), text_turn(ANSWER)],
        performance=MANY,
        budgets=LocalBudgets(local_generations=2, run_tool_executions=3),
    )
    outcome, events = run(gateway, PROMPT)
    assert outcome.state is RunState.COMPLETED
    # The local stage ran two tools and stopped at the per-stage limit on the third. The
    # entire run has one execution left, so the external set of two is refused whole.
    assert executed_tools(events) == ["get_recent_logs", "get_latest_deployment", "get_metrics"]
    rejected = [e for e in of_type(events, "tool.proposed") if e.payload["admissible"] is False]
    assert [e.actor for e in rejected] == ["external", "external"]
    assert [e.payload["call_id"] for e in rejected] == ["c_metrics", "c_logs"]
    assert "at most 1 tool call" in rejected[0].payload["rejection"]["message"]


# -- the default and the other paths ------------------------------------------------------------


def test_under_one_two_reads_are_still_rejected_with_a_single_call_exchange(tmp_path, skills):
    """Section 14.1: the default keeps the one-call rule and its one-call exchange, unchanged."""
    gateway = local_gateway(
        tmp_path,
        skills,
        [calls_turn(LOGS, DEPLOYMENT), calls_turn(LOGS), text_turn(ANSWER), text_turn(ANSWER)],
        performance=PerformanceSettings(),
        budgets=LocalBudgets(local_generations=5),
    )
    outcome, events = run(gateway, PROMPT)
    assert outcome.state is RunState.COMPLETED
    assert {p["rejection"]["code"] for p in proposals(events)[:2]} == {"multiple_tool_calls"}
    assert executed_tools(events) == ["get_recent_logs"]
    request = gateway.adapter.requests[2]
    assert [c.call_id for c in last_assistant_calls(request).tool_calls][:1] == ["c_logs"]
    assert len(last_assistant_calls(request).tool_calls) == 1
    assert len(tool_messages(request)) == 1


def test_the_client_owned_alias_keeps_its_one_call_rule(tmp_path):
    """Section 7.2, 4.3: the client-owned alias exposes one call under many too."""
    from test_client_owned_tools import client_app, events_of, message_of, request

    two = [
        call("c1", "get_weather", city="Oslo"),
        call("c2", "get_weather", city="Rome"),
        Completed(finish_reason="tool_calls"),
    ]
    gateway, app = client_app(tmp_path, [two], performance=MANY)
    response = request(app)
    (exposed,) = message_of(response)["tool_calls"]
    assert exposed["id"] == "c1"
    (proposed,) = of_type(events_of(gateway, response), "tool.proposed")
    assert proposed.payload["proposal_id"] == "c1"


def test_a_single_call_proposal_is_unchanged_in_the_local_loop(tmp_path):
    """Section 14.1: under many a lone call takes the same steps as under one."""
    turns = [
        route_turn("check-service-health"),
        tool_turn("get_service_health", service="staging-api"),
        text_turn("staging-api is healthy (checked at 2026-10-20T02:58:00Z)."),
        text_turn("staging-api is healthy (checked at 2026-10-20T02:58:00Z)."),
    ]
    traces = []
    for performance in (PerformanceSettings(), MANY):  # the double copies the script
        gateway = build_gateway(
            tmp_path / performance.calls_per_generation,
            turns,
            performance=performance,
            external_turns=[],
        )
        outcome, events = run(gateway)
        assert outcome.state is RunState.COMPLETED
        traces.append(
            [(e.event_type.value, e.actor) for e in events if e.event_type.value != "run.accepted"]
        )
    assert traces[0] == traces[1]
