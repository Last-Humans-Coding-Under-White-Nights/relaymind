"""AC02: a complex diagnostic request after local metrics collection.

Spec section 13, AC02: the external request contains the original task
and the completed evidence, and completed calls are not replayed.
Requirements RM04, RM13, RM14 (with RM01, RM05, RM08 to RM12, RM16, RM20
for the rules the handoff must keep). Issue #18; deterministic, with
scripted model doubles. The live external model is not exercised here.

RM13: the handoff preserves the original request, the selected skill
version, the structured tool results, the completed operation IDs,
pending work, permitted tools, language, and remaining budget; local
assumptions stay proposals; evidence over the external context budget
fails with context_limit_exceeded. RM14: escalation revokes the local
actor's dispatch authority; only one actor dispatches an operation or
publishes the final answer.

The scenario needs a skill that collects metrics locally and then runs
out of local budget. The checked-in investigate-performance skill is
external-required (spec section 9), so the local-prep tests use a copy of
the demo skills with that one manifest flag changed, in tmp_path.
"""

import asyncio
import json
import shutil

import pytest

from gateway_helpers import (
    EXTERNAL_MODEL_ID,
    LOCAL_MODEL_ID,
    assert_needs_external,
    build_gateway,
    executed_tools,
    replace_handler,
    route_turn,
    run,
    text_turn,
    tool_turn,
)
from helpers import DEMO_SKILLS_ROOT, ScriptedAdapter, make_capabilities, of_type
from relaymind.adapters import GenerationFailed, Role, TextDelta
from relaymind.orchestrator.budgets import LocalBudgets
from relaymind.orchestrator.local_loop import ConfiguredModel
from relaymind.orchestrator.run_state import RunState
from relaymind.redaction import Redactor
from relaymind.tools import CallerContext
from relaymind.tools.sandbox import MUTATE_PERMISSION, READ_PERMISSION

PROMPT = "Why is staging-api slow right now?"
ANSWER = (
    "Observations:\n"
    "- get_latest_deployment: the latest deployment is recorded.\n"
    "- get_metrics (60 minutes): p95 latency was 175 ms and 186 ms.\n"
    "- get_recent_logs: connection pool at 85% of capacity.\n"
    "\n"
    "Hypotheses:\n"
    "- The connection pool may be close to its limit.\n"
)
NO_TOKENIZER = make_capabilities(tokenizer_available=False)


@pytest.fixture
def local_prep_skills(tmp_path):
    """The demo skills, with investigate-performance allowed to run locally first."""
    root = tmp_path / "skills"
    shutil.copytree(DEMO_SKILLS_ROOT, root)
    manifest = root / "investigate-performance" / "skill.manifest.json"
    data = json.loads(manifest.read_text("utf-8"))
    data["local_eligible"] = True
    manifest.write_text(json.dumps(data), "utf-8")
    return [root]


def local_collection():
    """Routing, then deployment and metrics, then a third tool the local budget refuses."""
    return [
        route_turn("investigate-performance"),
        tool_turn("get_latest_deployment", call_id="c1", service="staging-api"),
        tool_turn("get_metrics", call_id="c2", service="staging-api", window_minutes=60),
        tool_turn("get_recent_logs", call_id="c3", service="staging-api", limit=3),
    ]


def after_local_collection(tmp_path, local_prep_skills, external_turns, **options):
    options.setdefault("external_capabilities", NO_TOKENIZER)
    return build_gateway(
        tmp_path,
        local_collection(),
        skill_roots=local_prep_skills,
        external_turns=external_turns,
        **options,
    )


def logs_then_answer():
    return [
        tool_turn("get_recent_logs", call_id="e1", service="staging-api", limit=3),
        text_turn(ANSWER),
    ]


def handoff_block(request):
    """The decoded Handoff JSON in the external request's system message."""
    system = request.messages[0]
    assert system.role is Role.SYSTEM
    lines = system.content.split("\n")
    return json.loads(lines[lines.index("Handoff (JSON):") + 1])


def texts(request):
    return [m.content or "" for m in request.messages]


# --- AC02: the task and the completed evidence reach the external model --------


def test_the_external_request_carries_the_task_and_the_completed_evidence(
    tmp_path, local_prep_skills
):
    """AC02, RM13: original task, completed operation IDs, structured results."""
    gateway = after_local_collection(tmp_path, local_prep_skills, logs_then_answer())
    outcome, events = run(gateway, PROMPT)

    assert outcome.state is RunState.COMPLETED
    first = gateway.external.requests[0]
    assert [m.content for m in first.messages if m.role is Role.USER] == [PROMPT]

    local_operations = gateway.store.tool_operations(outcome.run_id)[:2]
    assert [op["tool_name"] for op in local_operations] == ["get_latest_deployment", "get_metrics"]
    tool_messages = [m for m in first.messages if m.role is Role.TOOL]
    assert [m.tool_call_id for m in tool_messages] == [
        op["operation_id"] for op in local_operations
    ]
    for message, operation in zip(tool_messages, local_operations, strict=True):
        assert json.loads(message.content) == operation["result"]
    calls = [c for m in first.messages for c in m.tool_calls]
    assert [(c.tool_name, dict(c.arguments)) for c in calls] == [
        (op["tool_name"], op["arguments"]) for op in local_operations
    ]

    block = handoff_block(first)
    assert block["completed_operation_ids"] == [op["operation_id"] for op in local_operations]
    assert block["reason_code"] == "local_budget"
    assert block["language"] == "en"
    assert block["skill"]["name"] == "investigate-performance"
    assert block["skill"]["version"] == gateway.skills.get("investigate-performance").version
    assert block["pending_work"] == [
        {"kind": "tool_call", "tool_name": "get_recent_logs"},
        {"kind": "final_answer", "tool_name": None},
    ]
    assert sorted(block["allowed_tools"]) == [
        "get_latest_deployment",
        "get_metrics",
        "get_recent_logs",
    ]
    assert block["remaining_budgets"]["run_generations"] == 4  # 8 - 4 local generations
    assert block["remaining_budgets"]["run_tool_executions"] == 4
    assert [t.name for t in first.tool_schemas] == block["allowed_tools"]


def test_the_external_model_does_not_replay_completed_operations(tmp_path, local_prep_skills):
    """AC02, RM10, RM14: a repeated proposal gets the recorded result, no handler runs."""
    gateway = after_local_collection(
        tmp_path,
        local_prep_skills,
        [
            tool_turn("get_metrics", call_id="e1", service="staging-api", window_minutes=60),
            tool_turn("get_recent_logs", call_id="e2", service="staging-api", limit=3),
            text_turn(ANSWER),
        ],
    )
    runs = {"get_metrics": 0}
    original = gateway.tools.get("get_metrics").declaration.handler

    def counting(arguments):
        runs["get_metrics"] += 1
        return original(arguments)

    replace_handler(gateway, "get_metrics", counting)
    outcome, events = run(gateway, PROMPT)

    assert outcome.state is RunState.COMPLETED
    assert runs["get_metrics"] == 1  # the local run, once
    assert executed_tools(events) == ["get_latest_deployment", "get_metrics", "get_recent_logs"]
    replays = [e for e in of_type(events, "tool.finished") if e.payload["replayed"]]
    assert [e.payload["tool_name"] for e in replays] == ["get_metrics"]
    recorded = gateway.store.tool_operations(outcome.run_id)[1]
    assert replays[0].payload["operation_id"] == recorded["operation_id"]
    assert len(gateway.store.tool_operations(outcome.run_id)) == 3
    # The external model saw the recorded result for its repeated proposal.
    second = gateway.external.requests[1]
    last_exchange = [m for m in second.messages if m.role is Role.TOOL][-1]
    assert json.loads(last_exchange.content)["operation_id"] == recorded["operation_id"]


def test_local_assumptions_stay_labeled_proposals(tmp_path, local_prep_skills):
    """RM13: the call the local budget refused is a proposal, never a completed fact."""
    gateway = after_local_collection(tmp_path, local_prep_skills, logs_then_answer())
    outcome, events = run(gateway, PROMPT)

    block = handoff_block(gateway.external.requests[0])
    (proposal,) = block["local_proposals"]
    assert proposal["label"] == "proposal"
    assert proposal["kind"] == "tool_call"
    assert proposal["tool_name"] == "get_recent_logs"
    assert proposal["arguments"] == {"service": "staging-api", "limit": 3}
    assert proposal["rejection_code"] is None
    # It is not among the completed operations or the evidence.
    first = gateway.external.requests[0]
    assert "get_recent_logs" not in [c.tool_name for m in first.messages for c in m.tool_calls]


def test_a_rejected_local_draft_answer_is_carried_as_a_proposal(tmp_path):
    """RM13: unverified local text is labeled, not recast as evidence."""
    gateway = build_gateway(
        tmp_path,
        [
            route_turn("check-service-health"),
            tool_turn("get_service_health", service="staging-api"),
            text_turn("It looks fine to me."),
            text_turn("Still fine."),
        ],
        external_turns=[text_turn("staging-api is healthy (checked at 2026-10-20T02:58:00Z).")],
    )
    outcome, events = run(gateway)

    assert outcome.state is RunState.COMPLETED
    block = handoff_block(gateway.external.requests[0])
    assert block["reason_code"] == "invalid_output"
    drafts = [p for p in block["local_proposals"] if p["kind"] == "answer_draft"]
    assert [(p["label"], p["text"]) for p in drafts] == [
        ("proposal", "It looks fine to me."),
        ("proposal", "Still fine."),
    ]
    assert len(block["completed_operation_ids"]) == 1


# --- the trace, dispatch authority, and the final answer -------------------------


def test_the_handoff_is_traced_and_the_external_model_owns_the_answer(tmp_path, local_prep_skills):
    """RM14, RM16, RM20, RM22: handoff.started, one dispatcher, one answer owner."""
    gateway = after_local_collection(tmp_path, local_prep_skills, logs_then_answer())
    outcome, events = run(gateway, PROMPT)

    assert outcome.answer == ANSWER
    kinds = [e.event_type.value for e in events]
    (handoff,) = of_type(events, "handoff.started")
    operations = gateway.store.tool_operations(outcome.run_id)
    assert handoff.actor == "gateway"
    assert handoff.payload["reason_code"] == "local_budget"
    assert handoff.payload["model"] == EXTERNAL_MODEL_ID
    assert handoff.payload["operation_ids"] == [op["operation_id"] for op in operations[:2]]
    assert handoff.payload["skill"]["name"] == "investigate-performance"
    assert handoff.payload["request"]["method"] == "utf8_bytes"
    assert 0 < handoff.payload["request"]["size"] <= handoff.payload["request"]["limit"]

    # Nothing local happens after the handoff; everything after is external or gateway.
    after = events[kinds.index("handoff.started") + 1 :]
    local_after = [e for e in after if e.actor == "local"]
    assert local_after == []
    assert len(gateway.adapter.requests) == 4  # routing and three skill generations
    assert [e.actor for e in of_type(after, "model.started")] == ["external", "external"]
    proposals = of_type(after, "tool.proposed")
    assert [(e.actor, e.payload["model"]) for e in proposals] == [("external", EXTERNAL_MODEL_ID)]
    (started,) = [e for e in of_type(after, "tool.started")]
    assert started.payload["model"] == EXTERNAL_MODEL_ID

    # One final-answer owner (RM20): the external model, in the record and the trace.
    deltas = of_type(events, "answer.delta")
    assert {e.actor for e in deltas} == {"external"}
    assert {e.payload["model"] for e in deltas} == {EXTERNAL_MODEL_ID}
    assert "".join(e.payload["text"] for e in deltas) == ANSWER
    (completed,) = of_type(events, "run.completed")
    assert completed.payload["final_answer_model"] == EXTERNAL_MODEL_ID
    assert completed.payload["external_calls"] == len(gateway.external.requests) == 2
    assert [c["actor"] for c in completed.payload["model_calls"]].count("local") == 4
    assert completed.payload["budget_usage"]["external_generations"] == 2
    assert gateway.store.get_run(outcome.run_id).state == "completed"
    assert gateway.store.active_run_id() is None


def test_the_run_is_external_running_while_the_external_model_works(tmp_path, local_prep_skills):
    """Section 5: local_running escalates to external_running, then completes."""
    states = []
    holder = {}

    def on_external(request):
        states.append(holder["gateway"].store.get_run(request.generation_id.split("_gen")[0]).state)

    gateway = after_local_collection(tmp_path, local_prep_skills, logs_then_answer())
    holder["gateway"] = gateway
    gateway.external.on_generate = on_external
    outcome, _ = run(gateway, PROMPT)
    assert outcome.state is RunState.COMPLETED
    assert states == ["external_running", "external_running"]


def test_the_external_stage_obeys_the_same_tool_rules(tmp_path, local_prep_skills):
    """RM05, RM08, RM09: an unpermitted tool, bad arguments, or a guessed ID never run."""
    gateway = after_local_collection(
        tmp_path,
        local_prep_skills,
        [
            tool_turn("delete_demo_database", call_id="e1", database_id="demo-orders-db"),
            tool_turn("get_recent_logs", call_id="e2", service="staging-api", limit=500),
            tool_turn("get_recent_logs", call_id="e3", service="payments-api", limit=3),
            tool_turn("get_recent_logs", call_id="e4", service="staging-api", limit=3),
            text_turn(ANSWER),
        ],
        budgets=LocalBudgets(run_generations=12),
    )
    outcome, events = run(gateway, PROMPT)

    assert outcome.state is RunState.COMPLETED
    assert executed_tools(events) == ["get_latest_deployment", "get_metrics", "get_recent_logs"]
    external_proposals = [e for e in of_type(events, "tool.proposed") if e.actor == "external"]
    assert [
        e.payload["rejection"] and e.payload["rejection"]["code"] for e in external_proposals
    ] == [
        "tool_not_permitted",
        "invalid_arguments",
        "unsupported_identifier",
        None,
    ]
    assert [d["database_id"] for d in gateway.sandbox.list_databases()] == [
        "demo-orders-db",
        "demo-analytics-db",
    ]
    # The rejections went back to the model as data, and the budget held.
    assert len(gateway.external.requests) == 5


def test_secret_fields_never_reach_the_external_request(tmp_path, local_prep_skills):
    """RM12, spec section 3: redacted evidence stays redacted in the envelope."""
    gateway = after_local_collection(
        tmp_path,
        local_prep_skills,
        logs_then_answer(),
        redactor=Redactor(secret_fields={"deployment_id"}),
    )
    outcome, events = run(gateway, PROMPT)

    assert outcome.state is RunState.COMPLETED
    for request in gateway.external.requests:
        assert all("dep-staging-0142" not in text for text in texts(request))
    assert any("[redacted]" in text for text in texts(gateway.external.requests[0]))


# --- when the handoff cannot happen -------------------------------------------------


def test_evidence_over_the_external_context_budget_fails_explicitly(tmp_path, local_prep_skills):
    """RM13, AC02: context_limit_exceeded, before any external call, evidence kept."""
    gateway = after_local_collection(
        tmp_path,
        local_prep_skills,
        logs_then_answer(),
        external_capabilities=make_capabilities(tokenizer_available=False, context_limit=600),
    )
    outcome, events = run(gateway, PROMPT)

    assert outcome.state is RunState.FAILED
    assert outcome.error_code == "context_limit_exceeded"
    assert outcome.reason_code == "context_limit"
    assert outcome.answer is None
    assert gateway.external.requests == []
    assert not of_type(events, "handoff.started")
    assert not of_type(events, "answer.delta")
    (failed,) = of_type(events, "run.failed")
    assert failed.payload["error_code"] == "context_limit_exceeded"
    assert failed.payload["reason_code"] == "context_limit"
    assert failed.payload["request"]["limit"] == 600
    assert failed.payload["request"]["size"] > 600
    assert failed.payload["request"]["method"] == "utf8_bytes"
    assert failed.payload["external_calls"] == 0
    # The partial evidence is still in the store (section 5).
    assert len(gateway.store.tool_operations(outcome.run_id)) == 2
    record = gateway.store.get_run(outcome.run_id)
    assert (record.state, record.error_code) == ("failed", "context_limit_exceeded")
    assert gateway.store.active_run_id() is None


def test_the_tokenizer_is_used_when_the_external_adapter_has_one(tmp_path, local_prep_skills):
    """RM13: a counted request is measured in tokens, and says so."""
    gateway = after_local_collection(
        tmp_path,
        local_prep_skills,
        logs_then_answer(),
        external_capabilities=make_capabilities(tokenizer_available=True, context_limit=100),
    )
    outcome, events = run(gateway, PROMPT)

    assert outcome.error_code == "context_limit_exceeded"
    (failed,) = of_type(events, "run.failed")
    assert failed.payload["request"]["method"] == "tokenizer"
    assert gateway.external.counted  # the adapter's own count was asked for
    assert gateway.external.requests == []


def test_the_context_budget_is_checked_again_as_tool_results_accumulate(
    tmp_path, local_prep_skills
):
    """RM13: a result that grows the request past the limit stops the next generation."""

    class GrowingAdapter(ScriptedAdapter):
        async def count_tokens(self, messages, tool_schemas=()):
            return 100 * len(messages)

    gateway = after_local_collection(tmp_path, local_prep_skills, logs_then_answer())
    external = GrowingAdapter(
        turns=logs_then_answer(),
        locality="external",
        capabilities=make_capabilities(tokenizer_available=True, context_limit=700),
    )
    gateway.orchestrator.external = ConfiguredModel(adapter=external, model_id=EXTERNAL_MODEL_ID)
    outcome, events = run(gateway, PROMPT)

    assert outcome.error_code == "context_limit_exceeded"
    assert len(external.requests) == 1  # the first fit, the second did not
    assert executed_tools(events)[-1] == "get_recent_logs"
    (failed,) = of_type(events, "run.failed")
    assert failed.payload["external_calls"] == 1
    assert failed.payload["request"]["size"] > 700
    assert not of_type(events, "answer.delta")


def test_local_only_never_dispatches_externally(tmp_path, local_prep_skills):
    """Section 7.4, RM04: local_only returns needs_external with partial evidence."""
    gateway = after_local_collection(tmp_path, local_prep_skills, logs_then_answer())
    outcome, events = run(gateway, PROMPT, execution_mode="local_only")

    assert_needs_external(gateway, outcome, events, "local_budget")
    assert len(gateway.store.tool_operations(outcome.run_id)) == 2


def test_an_unconfigured_external_model_returns_needs_external(tmp_path, local_prep_skills):
    """RM04: with no external model the run ends needs_external, as before."""
    gateway = build_gateway(tmp_path, local_collection(), skill_roots=local_prep_skills)
    outcome, events = run(gateway, PROMPT)

    assert_needs_external(gateway, outcome, events, "local_budget")


def test_no_run_budget_left_for_an_external_generation_is_needs_external(
    tmp_path, local_prep_skills
):
    """Section 11: the entire-run limits hold; nothing was dispatched."""
    gateway = after_local_collection(
        tmp_path, local_prep_skills, logs_then_answer(), budgets=LocalBudgets(run_generations=4)
    )
    outcome, events = run(gateway, PROMPT)

    assert outcome.error_code == "needs_external"
    assert outcome.reason_code == "local_budget"
    assert gateway.external.requests == []
    assert not of_type(events, "handoff.started")


def test_an_operation_without_a_known_outcome_blocks_the_handoff(tmp_path):
    """RM14: a write with an uncertain outcome must not be continued by a second actor."""
    holder = {}

    def leave_unknown(request):
        if request.generation_id.endswith("gen3_skill"):
            run_id = request.generation_id.split("_gen")[0]
            store = holder["gateway"].store
            store.record_tool_operation(
                run_id,
                operation_id="op_write_1",
                proposal_id=None,
                tool_name="delete_demo_database",
                model=LOCAL_MODEL_ID,
                arguments={"database_id": "demo-orders-db"},
            )
            store.finish_tool_operation("op_write_1", status="unknown_outcome", result={})

    gateway = build_gateway(
        tmp_path,
        [
            route_turn("check-service-health"),
            tool_turn("get_service_health", service="staging-api"),
            text_turn("not an answer"),
            text_turn("still not an answer"),
        ],
        external_turns=[text_turn("staging-api is healthy (checked at 2026-10-20T02:58:00Z).")],
        on_generate=leave_unknown,
    )
    holder["gateway"] = gateway
    outcome, events = run(gateway)

    assert outcome.state is RunState.FAILED
    assert outcome.error_code == "needs_external"
    assert outcome.reason_code == "tool_failure"
    assert gateway.external.requests == []
    assert not of_type(events, "handoff.started")
    (failed,) = of_type(events, "run.failed")
    assert failed.payload["operation_id"] == "op_write_1"


# --- external failure --------------------------------------------------------------


def test_an_external_generation_failure_ends_the_run_without_a_result(tmp_path, local_prep_skills):
    """AC07, RM16: explicit failure, partial evidence kept, no local answer, no invented result."""
    gateway = after_local_collection(
        tmp_path,
        local_prep_skills,
        [[GenerationFailed(code="unreachable", message="no route to host")]],
    )
    outcome, events = run(gateway, PROMPT)

    assert outcome.state is RunState.FAILED
    assert outcome.error_code == "external_failed"
    assert outcome.answer is None
    (failed,) = of_type(events, "run.failed")
    assert failed.payload["error_code"] == "external_failed"
    assert failed.payload["external_calls"] == 1
    assert not of_type(events, "answer.delta")
    assert len(gateway.store.tool_operations(outcome.run_id)) == 2
    assert gateway.store.active_run_id() is None
    assert len(gateway.adapter.requests) == 4  # the local model was not asked again


def test_an_external_answer_that_misses_the_predicate_is_not_published(tmp_path, local_prep_skills):
    """RM06, RM20: the external answer meets the skill's evidence rules or the run fails."""
    gateway = after_local_collection(
        tmp_path,
        local_prep_skills,
        [text_turn("It is slow."), text_turn("Probably the database.")],
        budgets=LocalBudgets(external_generations=2),
    )
    outcome, events = run(gateway, PROMPT)

    assert outcome.error_code == "external_failed"
    assert len(gateway.external.requests) == 2  # bounded by external_generations
    assert not of_type(events, "answer.delta")
    corrective = [m.content for m in gateway.external.requests[1].messages if m.role is Role.USER]
    assert any("completion criteria" in text for text in corrective)


# --- no selected skill and a skill never loaded locally --------------------------------


def test_external_only_hands_over_without_a_local_generation(tmp_path):
    """Section 7.4, RM13: external_only carries skill null, the catalog, and no evidence."""
    gateway = build_gateway(
        tmp_path,
        [],
        external_turns=[
            tool_turn("get_service_health", call_id="e1", service="staging-api"),
            text_turn("staging-api is healthy (checked at 2026-10-20T02:58:00Z)."),
        ],
    )
    outcome, events = run(
        gateway, "Is staging-api healthy right now?", execution_mode="external_only"
    )

    assert outcome.state is RunState.COMPLETED
    assert gateway.adapter.requests == []
    (handoff,) = of_type(events, "handoff.started")
    assert handoff.payload["reason_code"] == "execution_mode"
    assert handoff.payload["skill"] is None
    assert handoff.payload["operation_ids"] == []
    block = handoff_block(gateway.external.requests[0])
    assert block["skill"] is None
    assert "check-service-health:" in gateway.external.requests[0].messages[0].content
    assert "get_service_health" in block["allowed_tools"]
    assert not of_type(events, "skill.loaded")
    (completed,) = of_type(events, "run.completed")
    assert completed.payload["final_answer_model"] == EXTERNAL_MODEL_ID
    assert completed.payload["external_calls"] == 2
    assert [c["actor"] for c in completed.payload["model_calls"]] == ["external", "external"]


def test_a_complex_skill_is_loaded_for_the_external_model(tmp_path):
    """RM06, RM13: an external-required skill is loaded at its pinned version on handoff."""
    gateway = build_gateway(
        tmp_path,
        [route_turn("investigate-performance")],
        external_turns=[
            tool_turn("get_latest_deployment", call_id="e1", service="staging-api"),
            tool_turn("get_metrics", call_id="e2", service="staging-api", window_minutes=60),
            tool_turn("get_recent_logs", call_id="e3", service="staging-api", limit=3),
            text_turn(ANSWER),
        ],
    )
    outcome, events = run(gateway, PROMPT)

    assert outcome.state is RunState.COMPLETED
    (loaded,) = of_type(events, "skill.loaded")
    skill = gateway.skills.get("investigate-performance")
    assert (loaded.payload["name"], loaded.payload["version"]) == (skill.name, skill.version)
    assert loaded.payload["content_hash"] == skill.content_hash
    assert skill.body in gateway.external.requests[0].messages[0].content
    (handoff,) = of_type(events, "handoff.started")
    assert handoff.payload["reason_code"] == "complex_skill"
    assert handoff.payload["operation_ids"] == []
    assert handoff.payload["skill"]["content_hash"] == skill.content_hash
    assert executed_tools(events) == ["get_latest_deployment", "get_metrics", "get_recent_logs"]
    assert len(gateway.adapter.requests) == 1  # only the routing generation was local
    (completed,) = of_type(events, "run.completed")
    assert completed.payload["final_answer_model"] == EXTERNAL_MODEL_ID
    assert completed.payload["external_calls"] == 4
    assert completed.payload["model_calls"][0]["actor"] == "local"


# --- authority, capabilities, and time in the external stage -----------------------


def test_a_local_dispatch_after_the_handoff_is_refused(tmp_path, local_prep_skills, monkeypatch):
    """RM14: escalation revokes the local actor's dispatch authority; nothing runs."""
    from relaymind.orchestrator.handoff import DispatchRevokedError
    from relaymind.orchestrator.local_loop import _Run

    async def stray_local_dispatch(self, conversation, schemas, available, skill, capabilities):
        call = self.o.executor.validate(
            self.run_id,
            "get_recent_logs",
            {"service": "staging-api", "limit": 3},
            available=available,
        )
        await self._dispatch(call, "proposal_stray", "local")
        raise AssertionError("the local actor dispatched after the handoff")

    monkeypatch.setattr(_Run, "_external", stray_local_dispatch)
    gateway = after_local_collection(tmp_path, local_prep_skills, logs_then_answer())
    with pytest.raises(DispatchRevokedError, match="local"):
        run(gateway, PROMPT)

    run_id = gateway.adapter.requests[0].generation_id.split("_gen")[0]
    assert gateway.store.active_run_id() is None  # the run ended and freed the slot
    assert gateway.store.get_run(run_id).error_code == "internal_error"
    assert len(gateway.store.tool_operations(run_id)) == 2  # no third operation


def test_an_external_model_that_cannot_call_tools_is_not_sent_a_tool_task(
    tmp_path, local_prep_skills
):
    """RM18: unsupported capabilities are rejected, not worked around."""
    gateway = after_local_collection(
        tmp_path,
        local_prep_skills,
        logs_then_answer(),
        external_capabilities=make_capabilities(tool_calling=False, tokenizer_available=False),
    )
    outcome, events = run(gateway, PROMPT)

    assert outcome.error_code == "external_failed"
    assert gateway.external.requests == []
    assert not of_type(events, "handoff.started")


def test_the_external_stage_is_bounded_by_the_entire_run_active_time(tmp_path, local_prep_skills):
    """Section 11: external work is bounded by the remaining run budget."""
    gateway = None

    def slow(request):
        gateway.clock.advance(61)

    gateway = after_local_collection(tmp_path, local_prep_skills, logs_then_answer())
    gateway.external.on_generate = slow
    outcome, events = run(gateway, PROMPT)

    assert outcome.error_code == "external_failed"
    (failed,) = of_type(events, "run.failed")
    assert failed.payload["budget"] == "run_active_seconds"
    assert not of_type(events, "answer.delta")


# --- review of #18: evidence recorded before a local stop -----------------------------


def slow_first_tool(tmp_path, local_prep_skills, external_turns):
    """The first local tool succeeds and is recorded, then the stage time is spent."""
    gateway = build_gateway(
        tmp_path,
        [
            route_turn("investigate-performance"),
            tool_turn("get_latest_deployment", call_id="c1", service="staging-api"),
        ],
        skill_roots=local_prep_skills,
        external_turns=external_turns,
        external_capabilities=NO_TOKENIZER,
    )
    original = gateway.tools.get("get_latest_deployment").declaration.handler

    def slow(arguments):
        gateway.clock.advance(5)  # past the 4-second local stage limit
        return original(arguments)

    replace_handler(gateway, "get_latest_deployment", slow)
    return gateway


def test_evidence_recorded_before_a_local_stop_counts_in_the_external_stage(
    tmp_path, local_prep_skills
):
    """RM13, RM06: the external stage judges the answer on the envelope's evidence.

    The tool result is in the store and in the envelope, so the
    completion predicate must see it too.
    """
    gateway = slow_first_tool(tmp_path, local_prep_skills, [text_turn(ANSWER)])
    outcome, events = run(gateway, PROMPT)

    (handoff,) = of_type(events, "handoff.started")
    assert handoff.payload["reason_code"] == "local_budget"
    assert len(handoff.payload["operation_ids"]) == 1
    assert outcome.state is RunState.COMPLETED
    assert len(gateway.external.requests) == 1


def test_a_proposal_that_produced_an_operation_is_not_also_a_local_proposal(
    tmp_path, local_prep_skills
):
    """RM13: a call that ran is a completed operation, never also an unverified proposal."""
    gateway = slow_first_tool(tmp_path, local_prep_skills, [text_turn(ANSWER)])
    run(gateway, PROMPT)

    block = handoff_block(gateway.external.requests[0])
    assert len(block["completed_operation_ids"]) == 1
    assert block["local_proposals"] == []


def test_a_token_count_failure_in_the_external_stage_is_external_failed(
    tmp_path, local_prep_skills
):
    """Section 11, RM13: a request that cannot be measured again ends external_failed."""
    from relaymind.adapters import TokenCountError

    class FailingCount(ScriptedAdapter):
        async def count_tokens(self, messages, tool_schemas=()):
            self.counted.append((tuple(messages), tuple(tool_schemas)))
            if len(self.counted) > 1:
                raise TokenCountError("the tokenizer is gone")
            return 10

    gateway = after_local_collection(tmp_path, local_prep_skills, logs_then_answer())
    external = FailingCount(
        turns=logs_then_answer(),
        locality="external",
        capabilities=make_capabilities(tokenizer_available=True, context_limit=100_000),
    )
    gateway.orchestrator.external = ConfiguredModel(adapter=external, model_id=EXTERNAL_MODEL_ID)
    outcome, events = run(gateway, PROMPT)

    assert outcome.state is RunState.FAILED
    assert outcome.error_code == "external_failed"
    assert len(external.requests) == 1
    assert not of_type(events, "answer.delta")


def test_external_capabilities_are_fetched_once_per_handoff(tmp_path, local_prep_skills):
    """RM18: capabilities are read once and reused for every measurement."""

    class CountingCapabilities(ScriptedAdapter):
        fetched = 0

        async def capabilities(self):
            self.fetched += 1
            return await super().capabilities()

    gateway = after_local_collection(tmp_path, local_prep_skills, logs_then_answer())
    external = CountingCapabilities(
        turns=logs_then_answer(), locality="external", capabilities=NO_TOKENIZER
    )
    gateway.orchestrator.external = ConfiguredModel(adapter=external, model_id=EXTERNAL_MODEL_ID)
    outcome, _ = run(gateway, PROMPT)

    assert outcome.state is RunState.COMPLETED
    assert len(external.requests) == 2
    assert external.fetched == 1


# --- review of #18, round 2: no futile handoff, honest timeout evidence ------------


def test_a_mutating_tool_that_needs_confirmation_does_not_hand_off(tmp_path):
    """RM11, RM04, section 4.4: the external stage cannot run the mutation either.

    The local loop stops on confirmation_required. A handoff would send
    the same proposal to the external model, which the gateway rejects
    the same way, so the run ends as needs_external with no external call.
    """
    gateway = build_gateway(
        tmp_path,
        [
            route_turn("delete-demo-database"),
            tool_turn("list_demo_databases", call_id="call_list"),
            tool_turn(
                "delete_demo_database",
                call_id="call_delete",
                database_id="demo-orders-db",
                expected_revision=3,
            ),
        ],
        caller=CallerContext(permissions=frozenset({READ_PERMISSION, MUTATE_PERMISSION})),
        external_turns=[text_turn("Deleted demo-orders-db.")],
    )
    outcome, events = run(gateway, "Delete the demo-orders-db sandbox database.")

    assert_needs_external(gateway, outcome, events, "capability_missing")
    assert not of_type(events, "handoff.started")
    assert "delete_demo_database" not in executed_tools(events)


@pytest.mark.parametrize(
    ("skill", "prompt"),
    [
        ("check-service-health", "Is staging-api healthy right now?"),
        ("investigate-performance", PROMPT),
    ],
    ids=["local-skill", "external-required-skill"],
)
def test_a_skill_with_no_permitted_tools_does_not_hand_off(tmp_path, skill, prompt):
    """RM08, RM04, section 4.4: no tool of the skill is permitted to the caller.

    The external stage gets the same permitted tools, none, so it could
    never collect the evidence the skill's completion predicate needs.
    """
    gateway = build_gateway(
        tmp_path,
        [route_turn(skill)],
        caller=CallerContext(permissions=frozenset()),
        external_turns=[text_turn("staging-api is healthy.")],
    )
    outcome, events = run(gateway, prompt)

    assert_needs_external(gateway, outcome, events, "capability_missing")
    assert not of_type(events, "handoff.started")


def test_an_external_generation_timeout_is_not_traced_as_local_time(tmp_path, local_prep_skills):
    """RM16, RM03, section 11: the trace names the limit that stopped the generation."""

    class Stalled(ScriptedAdapter):
        async def generate(self, request):
            self.requests.append(request)
            await asyncio.sleep(1)
            yield TextDelta(text="too late")

    gateway = after_local_collection(
        tmp_path, local_prep_skills, [], budgets=LocalBudgets(external_seconds=0.05)
    )
    external = Stalled(locality="external", capabilities=NO_TOKENIZER)
    gateway.orchestrator.external = ConfiguredModel(adapter=external, model_id=EXTERNAL_MODEL_ID)
    outcome, events = run(gateway, PROMPT)

    assert outcome.error_code == "external_failed"
    (failed,) = of_type(events, "run.failed")
    assert failed.payload["budget"] == "external_seconds"
    assert "local" not in failed.payload["detail"]
    finished = [e for e in of_type(events, "model.finished") if e.actor == "external"]
    assert len(finished) == 1
    error = finished[0].payload["error"]
    assert error["code"] != "local_budget"
    assert "local" not in error["message"]
    assert "external_seconds" in error["message"]
