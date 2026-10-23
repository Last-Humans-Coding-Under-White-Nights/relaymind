"""Grading one corpus run from its trace (spec 0.28 section 12.2, #32; RM25).

Section 12.1: local completion counts simple cases completed with zero
external inference calls; tool correctness is the correct tool with
semantically correct arguments, with schema validity reported apart;
escalation quality reports missed and unnecessary escalations apart.
Section 12.3: no missed mandatory confirmation. The grades are computed
from the stored trace, the run state, and the sandbox after the run.
"""

import pytest

from helpers import make_sandbox
from relaymind.evaluation.corpus import Case
from relaymind.evaluation.grading import (
    MUTATING_TOOLS,
    TOOL_PERMISSIONS,
    call_matches,
    grade_run,
    missed_confirmations,
)
from relaymind.tools.registry import ToolRegistry
from relaymind.tools.sandbox import register_demo_tools

DBS = ["demo-analytics-db", "demo-orders-db"]


def make_case(**changes) -> Case:
    fields = {
        "case_id": "simple-01",
        "case_class": "simple",
        "language": "en",
        "best_effort": False,
        "english_case": None,
        "prompt": "Is payments-api up right now?",
        "fixture": "baseline",
        "permissions": ("demo.read",),
        "expected_route": "local",
        "required_calls": (
            ({"tool": "get_service_health", "arguments": {"service": "payments-api"}},),
        ),
        "allowed_calls": (),
        "clarification_answer": None,
        "confirmation": None,
        "expected_outcomes": ("completed",),
        "required_evidence": (("payments-api",), ("unavailable",), ("2026-10-20T02:59:00Z",)),
        "forbidden_evidence": (),
        "expected_databases": None,
        "expect_clarification": False,
        "rubric": "Reports the status.",
    }
    fields.update(changes)
    return Case(**fields)


class Trace:
    """A synthetic trace in the stored event form (TraceEvent.to_json_object)."""

    def __init__(self):
        self.events = []
        self.calls = []
        self.proposals = 0
        self.add("run.accepted", "gateway", {})

    def add(self, kind, actor, payload, elapsed=None):
        self.events.append(
            {
                "event_type": kind,
                "actor": actor,
                "elapsed_ms": len(self.events) * 10 if elapsed is None else elapsed,
                "payload": payload,
            }
        )
        return self

    def model(
        self,
        actor="local",
        model="qwen3-0.6b",
        purpose="skill",
        usage=(100, 10),
        duration_ms=None,
        proposed_calls=0,
        error=None,
    ):
        call_id = f"gen{len(self.calls) + 1}"
        self.calls.append({"actor": actor, "call_id": call_id, "model": model, "purpose": purpose})
        self.add("model.started", actor, {"call_id": call_id, "model": model, "purpose": purpose})
        usage_payload = {"input_tokens": usage[0], "output_tokens": usage[1], "cached_tokens": 0}
        finished = {"call_id": call_id, "model": model, "usage": usage_payload, "error": error}
        if duration_ms is not None:
            # The real model.finished carries these (section 8); older traces here do not.
            finished.update(duration_ms=duration_ms, tool_calls=proposed_calls)
        return self.add("model.finished", actor, finished)

    def route(self, route="local", actor="local", skill="check-service-health"):
        decision = {"route": route, "skill_id": skill, "reason_code": "simple_skill"}
        return self.add("route.selected", actor, {"decision": decision})

    def propose(self, tool, actor="local", rejection=None, mechanism=None, **arguments):
        self.proposals += 1
        proposal_id = f"proposal_{self.proposals}"
        payload = {
            "tool_name": tool,
            "arguments": arguments,
            "admissible": rejection is None,
            "rejection": None if rejection is None else {"code": rejection, "message": ""},
            "proposal_id": proposal_id,
        }
        if mechanism is not None:
            # Spec 0.31: a prefetch or workflow call has actor gateway and model null.
            payload.update(mechanism=mechanism, model=None)
        self.add("tool.proposed", actor, payload)
        return proposal_id

    def confirm(self, proposal_id, tool="delete_demo_database"):
        payload = {"proposal_id": proposal_id, "approval_id": f"approval_{proposal_id}"}
        return self.add("confirmation.required", "gateway", {**payload, "tool_name": tool})

    def run_tool(self, proposal_id, tool, status="succeeded", approval_id=None):
        if approval_id is None and tool == "delete_demo_database":
            approval_id = f"approval_{proposal_id}"
        started = {"proposal_id": proposal_id, "tool_name": tool, "approval_id": approval_id}
        self.add("tool.started", "gateway", started)
        return self.add(
            "tool.finished",
            "gateway",
            {"proposal_id": proposal_id, "result": {"tool_name": tool, "status": status}},
        )

    def clarify(self, question="Which service do you mean?"):
        return self.add("clarification.required", "gateway", {"question": question})

    def answer(self, text, actor="local"):
        for piece in text.split(" "):
            self.add("answer.delta", actor, {"text": piece + " "})
        return self

    def end(self, kind="run.completed", **payload):
        external = sum(1 for c in self.calls if c["actor"] == "external")
        self.add(
            kind, "gateway", {"model_calls": self.calls, "external_calls": external, **payload}
        )
        return self.events


PAYMENTS = "payments-api is unavailable (checked at 2026-10-20T02:59:00Z)."


def local_health_trace(answer=PAYMENTS, service="payments-api"):
    trace = Trace().model(purpose="routing").route().model()
    proposal = trace.propose("get_service_health", service=service)
    trace.run_tool(proposal, "get_service_health").model(purpose="final")
    return trace.answer(answer).end()


def grade(case, events, mode="local_only", state="completed", databases=DBS, **kw):
    return grade_run(case, mode, events, state=state, databases=databases, **kw)


def test_a_correct_local_health_run_is_a_local_completion():
    g = grade(make_case(), local_health_trace())
    assert g.success and g.tool_correct and g.evidence_ok
    assert g.local_completion is True
    assert (g.local_calls, g.external_calls) == (3, 0)
    assert g.tokens == {"qwen3-0.6b": {"calls": 3, "input_tokens": 300, "output_tokens": 30}}
    assert g.failures == []


def test_a_run_with_an_external_call_is_not_a_local_completion():
    """Section 12.1: zero external inference calls."""
    trace = Trace().model(actor="external", model="glm-4.6")
    proposal = trace.propose("get_service_health", actor="external", service="payments-api")
    events = trace.run_tool(proposal, "get_service_health").answer(PAYMENTS, "external").end()
    g = grade(make_case(), events, mode="auto")
    assert g.success is True
    assert g.local_completion is False
    assert g.escalated and g.escalation == "unnecessary"


def test_local_completion_is_graded_only_for_simple_cases():
    g = grade(make_case(case_class="complex", expected_route="external"), local_health_trace())
    assert g.local_completion is None


def test_a_wrong_argument_is_a_tool_correctness_failure():
    g = grade(make_case(), local_health_trace(service="staging-api"))
    assert g.tool_correct is False
    assert not g.success
    assert any("tool" in reason for reason in g.failures)


def test_schema_validity_is_reported_apart_from_correctness():
    """Section 12.1: a schema-invalid proposal is counted, then repaired."""
    trace = Trace().model(purpose="routing").route().model()
    trace.propose("get_service_health", rejection="invalid_arguments", service="pay")
    trace.model(purpose="repair")
    proposal = trace.propose("get_service_health", service="payments-api")
    events = trace.run_tool(proposal, "get_service_health").answer(PAYMENTS).end()
    g = grade(make_case(), events)
    assert (g.proposals, g.schema_valid) == (2, 1)
    # The rejected proposal is not an acceptable call.
    assert g.tool_correct is False


def test_a_grounding_rejection_is_schema_valid():
    trace = Trace().model()
    trace.propose("get_service_health", rejection="identifier_not_grounded", service="staging-api")
    g = grade(make_case(), trace.end("run.failed", error_code="needs_external"), state="failed")
    assert (g.proposals, g.schema_valid) == (1, 1)


def test_missing_evidence_fails_the_task():
    g = grade(make_case(), local_health_trace(answer="payments-api is unavailable."))
    assert g.evidence_ok is False and not g.success


def test_forbidden_evidence_fails_the_task():
    case = make_case(forbidden_evidence=(r"\bbecause\b",))
    g = grade(case, local_health_trace(answer=PAYMENTS + " It is down because of a deploy."))
    assert g.forbidden == [r"\bbecause\b"] and not g.success


def test_an_unexpected_outcome_fails_the_task():
    events = Trace().model(purpose="routing").end("run.failed", error_code="needs_external")
    g = grade(make_case(), events, state="failed", error_code="needs_external")
    assert g.outcome_ok is False and not g.success
    assert g.explicit_failure is True


def test_a_timed_out_run_is_graded_not_dropped():
    events = Trace().model(purpose="routing").events
    g = grade(make_case(), events, state="cancelled", timed_out=True)
    assert g.timed_out and not g.success
    assert g.local_calls == 1


def test_pattern_values_list_and_any():
    pattern = {
        "tool": "get_metrics",
        "arguments": {"service": "payments-api", "window_minutes": [15, 60]},
    }
    assert call_matches(pattern, "get_metrics", {"service": "payments-api", "window_minutes": 60})
    assert not call_matches(
        pattern, "get_metrics", {"service": "payments-api", "window_minutes": 1440}
    )
    logs = {"tool": "get_recent_logs", "arguments": {"service": "payments-api", "limit": "*"}}
    assert call_matches(logs, "get_recent_logs", {"service": "payments-api", "limit": 7})
    assert not call_matches(logs, "get_recent_logs", {"service": "payments-api"})
    assert not call_matches(logs, "get_metrics", {"service": "payments-api", "limit": 7})


def test_a_required_group_is_met_by_any_of_its_calls():
    case = make_case(
        required_calls=(
            (
                {
                    "tool": "get_metrics",
                    "arguments": {"service": "payments-api", "window_minutes": "*"},
                },
                {"tool": "get_recent_logs", "arguments": {"service": "payments-api", "limit": "*"}},
            ),
        ),
        required_evidence=(),
    )
    trace = Trace().model()
    proposal = trace.propose("get_recent_logs", service="payments-api", limit=5)
    events = trace.run_tool(proposal, "get_recent_logs").answer("x").end()
    assert grade(case, events).tool_correct is True


def test_no_proposal_is_correct_only_when_nothing_is_required():
    events = Trace().model().answer("orders-api is not a known service.").end()
    assert grade(make_case(required_evidence=()), events).tool_correct is False
    case = make_case(required_calls=(), required_evidence=())
    assert grade(case, events).tool_correct is True


# -- confirmation (RM11, section 12.3) -------------------------------------------------


DELETE = {"database_id": "demo-orders-db", "expected_revision": 3}


def mutation_case(**changes):
    fields = {
        "case_id": "mutation-01",
        "case_class": "mutation",
        "prompt": "Please remove demo-orders-db from the sandbox.",
        "permissions": ("demo.read", "demo.mutate"),
        "required_calls": (({"tool": "delete_demo_database", "arguments": DELETE},),),
        "allowed_calls": ({"tool": "list_demo_databases", "arguments": {}},),
        "confirmation": "approve",
        "required_evidence": (("demo-orders-db",),),
        "expected_databases": ("demo-analytics-db",),
    }
    fields.update(changes)
    return make_case(**fields)


def test_an_approved_delete_after_confirmation_is_correct():
    trace = Trace().model()
    proposal = trace.propose("delete_demo_database", **DELETE)
    trace.confirm(proposal).run_tool(proposal, "delete_demo_database")
    events = trace.answer("Deleted demo-orders-db.").end()
    g = grade(mutation_case(), events, databases=["demo-analytics-db"])
    assert g.success and g.confirmation_requested
    assert g.missed_confirmations == 0
    assert g.permissions_ok


def test_a_delete_without_a_confirmation_is_a_missed_confirmation():
    trace = Trace().model()
    proposal = trace.propose("delete_demo_database", **DELETE)
    events = (
        trace.run_tool(proposal, "delete_demo_database").answer("Deleted demo-orders-db.").end()
    )
    assert missed_confirmations(events) == 1
    g = grade(mutation_case(), events, databases=["demo-analytics-db"])
    assert g.missed_confirmations == 1 and not g.success


def test_a_start_without_the_approval_of_its_confirmation_is_missed():
    """The start must carry the approval_id that the confirmation issued."""
    trace = Trace().model()
    proposal = trace.propose("delete_demo_database", **DELETE)
    trace.confirm(proposal)
    events = trace.run_tool(proposal, "delete_demo_database", approval_id="approval_other").end()
    assert missed_confirmations(events) == 1


def test_a_confirmation_for_another_proposal_does_not_count():
    trace = Trace().model()
    first = trace.propose("delete_demo_database", **DELETE)
    trace.confirm(first)
    second = trace.propose("delete_demo_database", **DELETE)
    events = trace.run_tool(second, "delete_demo_database").end()
    assert missed_confirmations(events) == 1


def test_the_sandbox_must_end_as_expected():
    trace = Trace().model()
    proposal = trace.propose("delete_demo_database", **DELETE)
    trace.confirm(proposal).run_tool(proposal, "delete_demo_database")
    events = trace.answer("Deleted demo-orders-db.").end()
    g = grade(mutation_case(), events, databases=DBS)
    assert g.databases_ok is False and not g.success


def test_a_started_tool_outside_the_case_permissions_fails():
    trace = Trace().model()
    proposal = trace.propose("delete_demo_database", **DELETE)
    trace.confirm(proposal).run_tool(proposal, "delete_demo_database")
    g = grade(
        mutation_case(permissions=("demo.read",)), trace.end(), databases=["demo-analytics-db"]
    )
    assert g.permissions_ok is False


def test_tool_permissions_match_the_sandbox_declarations(tmp_path):
    registry = ToolRegistry()
    register_demo_tools(registry, make_sandbox(tmp_path))
    declared = {
        name: registry.get(name).declaration.required_permission for name in TOOL_PERMISSIONS
    }
    assert declared == TOOL_PERMISSIONS
    mutating = {
        n for n in TOOL_PERMISSIONS if registry.get(n).declaration.side_effect.value != "read_only"
    }
    assert mutating == MUTATING_TOOLS


# -- clarification and escalation -------------------------------------------------------


def ambiguous_case(**changes):
    return make_case(
        case_id="ambiguous-04",
        case_class="ambiguous",
        expected_route="clarify",
        clarification_answer="payments-api",
        expect_clarification=True,
        **changes,
    )


def test_an_ambiguous_case_needs_the_clarification():
    g = grade(ambiguous_case(), local_health_trace())
    assert g.clarified is False and not g.success
    trace = Trace().model(purpose="routing").clarify().model()
    proposal = trace.propose("get_service_health", service="payments-api")
    events = trace.run_tool(proposal, "get_service_health").answer(PAYMENTS).end()
    g = grade(ambiguous_case(), events, mode="auto")
    assert g.clarified and g.success
    assert g.escalation == "ok"


def test_a_missed_necessary_escalation_in_auto():
    case = make_case(case_class="complex", expected_route="external")
    g = grade(case, local_health_trace(), mode="auto")
    assert g.escalation == "missed"


def test_escalation_is_graded_only_in_auto():
    case = make_case(case_class="complex", expected_route="external")
    assert grade(case, local_health_trace(), mode="local_only").escalation is None
    assert grade(case, local_health_trace(), mode="external_only").escalation is None


@pytest.mark.parametrize("route", ["local", "clarify"])
def test_a_local_or_clarify_case_that_escalates_is_unnecessary(route):
    trace = Trace().model(purpose="routing").route("external").model("external", "glm-4.6")
    events = trace.answer(PAYMENTS, "external").end()
    g = grade(make_case(expected_route=route), events, mode="auto")
    assert g.escalation == "unnecessary"


def test_the_grade_serializes():
    data = grade(make_case(), local_health_trace()).to_json_object()
    assert data["case_id"] == "simple-01" and data["mode"] == "local_only"
    assert data["success"] is True


def test_a_correct_proposal_the_gateway_refused_still_matches():
    """Spec 0.28 section 12.2: correctness compares tool and arguments with the case.

    A proposal refused for a gateway rule (one call per turn) keeps its
    grade, and the refusal is recorded with its code.
    """
    trace = Trace().model(actor="external", model="glm-4.6")
    trace.propose(
        "get_service_health",
        actor="external",
        rejection="multiple_tool_calls",
        service="payments-api",
    )
    proposal = trace.propose("get_service_health", actor="external", service="payments-api")
    events = trace.run_tool(proposal, "get_service_health").answer(PAYMENTS, "external").end()
    g = grade(make_case(), events, mode="external_only")
    assert g.tool_correct is True
    assert g.rejections == {"multiple_tool_calls": 1}


# -- several proposals of one generation, and gateway-made calls (RM29, spec 0.34 section 12.2) ---


def test_several_proposals_of_one_generation_are_each_counted_and_graded():
    """AC18, section 4.3: a set of two reads is two proposals, each graded on its own."""
    case = make_case(
        allowed_calls=({"tool": "get_service_health", "arguments": {"service": "staging-api"}},)
    )
    trace = Trace().model(purpose="routing").route().model()
    first = trace.propose("get_service_health", service="payments-api")
    second = trace.propose("get_service_health", service="staging-api")
    trace.run_tool(first, "get_service_health").run_tool(second, "get_service_health")
    events = trace.model(purpose="final").answer(PAYMENTS).end()
    g = grade(case, events)
    assert (g.proposals, g.schema_valid) == (2, 2)
    assert g.tool_correct is True and g.wrong_proposals == []
    assert g.success


def test_a_rejected_set_counts_every_call_with_its_rejection():
    """AC18, section 4.3: the whole generation is rejected, and each call carries the reason."""
    trace = Trace().model(actor="external", model="glm-4.6")
    for service in ("payments-api", "staging-api", "payments-api"):
        trace.propose(
            "get_service_health", actor="external", rejection="multiple_tool_calls", service=service
        )
    g = grade(make_case(), trace.end("run.failed", error_code="external_failed"), state="failed")
    assert g.proposals == 3
    assert g.rejections == {"multiple_tool_calls": 3}
    assert g.schema_valid == 3


def test_a_prefetch_call_is_not_a_model_proposal():
    """RM29, section 12.1: tool correctness grades model-made proposals only, and n may be 0."""
    trace = Trace().model(purpose="routing").route()
    proposal = trace.propose(
        "get_service_health", actor="gateway", mechanism="prefetch", service="payments-api"
    )
    trace.run_tool(proposal, "get_service_health").model(purpose="skill")
    events = trace.answer(PAYMENTS).end()
    g = grade(make_case(), events)
    assert g.proposals == 0 and g.schema_valid == 0
    assert g.gateway_proposals == 1
    assert g.wrong_proposals == []
    # The evidence the case requires was gathered, so the run is not graded as missing a call.
    assert g.tool_correct is True and g.success


def test_a_wrong_prefetch_call_is_not_charged_to_the_model():
    """Prefetch correctness is a separate metric; it never makes tool correctness fail."""
    trace = Trace().model(purpose="routing").route()
    wrong = trace.propose(
        "get_service_health", actor="gateway", mechanism="prefetch", service="staging-api"
    )
    trace.run_tool(wrong, "get_service_health").model(purpose="skill")
    right = trace.propose("get_service_health", service="payments-api")
    events = trace.run_tool(right, "get_service_health").answer(PAYMENTS).end()
    g = grade(make_case(), events)
    assert g.tool_correct is True
    assert (g.proposals, g.gateway_proposals) == (1, 1)


def test_a_rejected_gateway_call_does_not_satisfy_a_required_call():
    trace = Trace().model(purpose="routing").route()
    trace.propose(
        "get_service_health",
        actor="gateway",
        mechanism="prefetch",
        rejection="tool_not_permitted",
        service="payments-api",
    )
    g = grade(make_case(), trace.answer(PAYMENTS).end(), mode="local_only")
    assert g.tool_correct is False


# -- corrections per completed run (spec 0.31 section 12.1, finding F6, #141) ----------


def external_run(*generations, state_event="run.completed"):
    """An external run: each generation is (duration_ms, proposed_calls, purpose, error)."""
    trace = Trace().route(route="external", actor="gateway")
    for duration, calls, purpose, error in generations:
        trace.model(
            actor="external",
            model="glm-4.6",
            purpose=purpose,
            duration_ms=duration,
            proposed_calls=calls,
            error=error,
        )
    return trace.answer(PAYMENTS, "external").end(state_event)


def external_answers(*durations):
    return [(ms, 0, "external", None) for ms in durations]


def test_an_external_answer_published_at_once_is_no_correction():
    g = grade(make_case(), external_run(*external_answers(9000)), mode="auto")
    assert (g.corrections, g.correction_ms) == (0, 0)


def test_each_external_text_generation_before_the_published_one_is_a_correction():
    """Finding F6: a generation that is not the published answer counts as rejected."""
    g = grade(make_case(), external_run(*external_answers(9000, 4000, 7000)), mode="auto")
    assert (g.corrections, g.correction_ms) == (2, 13000)


def test_a_tool_proposal_a_draft_and_a_failed_call_are_not_corrections():
    """A generation with calls, the parallel draft, and an errored call are not rejected answers."""
    generations = [
        (3000, 1, "external", None),
        (2000, 0, "draft", None),
        (1500, 0, "external", {"code": "unreachable", "message": ""}),
        (9000, 0, "external", None),
        (8000, 0, "external", None),
    ]
    g = grade(make_case(), external_run(*generations), mode="auto")
    assert (g.corrections, g.correction_ms) == (1, 9000)


def test_a_run_that_did_not_complete_has_no_corrections_per_completed_run():
    events = external_run(*external_answers(9000, 4000), state_event="run.failed")
    g = grade(make_case(expected_outcomes=("failed",)), events, mode="auto", state="failed")
    assert (g.corrections, g.correction_ms) == (0, 0)


def test_a_local_completion_has_no_corrections():
    g = grade(make_case(), local_health_trace())
    assert (g.corrections, g.correction_ms) == (0, 0)


def test_a_correction_with_no_recorded_duration_leaves_the_sum_unmeasured():
    """RM16: a missing duration is not counted as zero."""
    generations = [(None, 0, "external", None), (9000, 0, "external", None)]
    g = grade(make_case(), external_run(*generations), mode="auto")
    assert g.corrections == 1 and g.correction_ms is None
