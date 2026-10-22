"""AC07 and the failed-run half of the error contract (RM03, RM04, RM16; #21).

AC07: local generation exceeds its budget or external generation fails.
Required: bounded escalation or explicit failure, and no invented result.
Section 11 maps the failed runs to HTTP statuses: needs_external 422,
context_limit_exceeded 400, external_failed 503, an exhausted time limit
504, workflow_unavailable 503, internal_error 500. Deterministic, with
scripted model doubles.
"""

import asyncio

import pytest

from gateway_helpers import (
    assert_needs_external,
    build_gateway,
    local_eligible_skills,
    route_turn,
    run,
    text_turn,
    tool_turn,
)
from helpers import ScriptedAdapter, make_capabilities, of_type
from relaymind.adapters import GenerationFailed, TextDelta
from relaymind.api.errors import ApiError, error_from_outcome
from relaymind.orchestrator.budgets import LocalBudgets
from relaymind.orchestrator.local_loop import ConfiguredModel, RunOutcome
from relaymind.orchestrator.run_state import RunState

PROMPT = "Why is staging-api slow right now?"
NO_TOKENIZER = make_capabilities(tokenizer_available=False)
UNREACHABLE = [GenerationFailed(code="unreachable", message="no route to host")]


def local_collection():
    """Routing, two tools, then a third tool the local budget refuses."""
    return [
        route_turn("investigate-performance"),
        tool_turn("get_latest_deployment", call_id="c1", service="staging-api"),
        tool_turn("get_metrics", call_id="c2", service="staging-api", window_minutes=60),
        tool_turn("get_recent_logs", call_id="c3", service="staging-api", limit=3),
    ]


def exhausted_local_budget(tmp_path, external_turns, **options):
    """A run whose local budget runs out after two tools (RM03, RM04)."""
    options.setdefault("external_capabilities", NO_TOKENIZER)
    return build_gateway(
        tmp_path,
        local_collection(),
        skill_roots=local_eligible_skills(tmp_path),
        external_turns=external_turns,
        **options,
    )


def assert_no_invented_result(gateway, outcome, events):
    """AC07: nothing published, no answer, the evidence kept, the slot released."""
    assert outcome.state is RunState.FAILED
    assert outcome.answer is None
    assert not of_type(events, "answer.delta")
    assert not of_type(events, "run.completed")
    assert len(gateway.store.tool_operations(outcome.run_id)) == 2
    assert gateway.store.active_run_id() is None


# --- AC07: local budget exhaustion with a failed external generation -----------------


def test_an_exhausted_local_budget_with_a_failed_external_generation_fails_explicitly(tmp_path):
    """AC07, RM04: bounded escalation, then explicit failure, and no local answer."""
    gateway = exhausted_local_budget(tmp_path, [UNREACHABLE])
    outcome, events = run(gateway, PROMPT)

    assert outcome.error_code == "external_failed"
    assert outcome.reason_code == "local_budget"
    assert_no_invented_result(gateway, outcome, events)
    assert len(gateway.external.requests) == 1  # one escalation, not a retry loop
    assert len(gateway.adapter.requests) == 4  # the local model was not asked again
    error = error_from_outcome(outcome)
    assert (error.status, error.code) == (503, "external_failed")
    assert (error.run_id, error.reason_code) == (outcome.run_id, "local_budget")


def test_the_external_stage_is_bounded_by_external_generations(tmp_path):
    """AC07, RM03: wrong external answers end the run after the configured number."""
    gateway = exhausted_local_budget(
        tmp_path,
        [text_turn("It is slow."), text_turn("Probably the database.")],
        budgets=LocalBudgets(external_generations=2),
    )
    outcome, events = run(gateway, PROMPT)

    assert outcome.error_code == "external_failed"
    assert_no_invented_result(gateway, outcome, events)
    assert len(gateway.external.requests) == 2
    assert error_from_outcome(outcome).status == 503


def test_an_exhausted_local_budget_without_external_dispatch_returns_needs_external(tmp_path):
    """AC07, RM04: no external model configured, so the run returns the partial evidence."""
    gateway = build_gateway(
        tmp_path,
        local_collection(),
        skill_roots=local_eligible_skills(tmp_path),
    )
    outcome, events = run(gateway, PROMPT)

    assert_needs_external(gateway, outcome, events, "local_budget")
    assert len(gateway.store.tool_operations(outcome.run_id)) == 2
    # RunOutcome.budget names the limit that stopped the run, as run.failed does.
    (failed,) = of_type(events, "run.failed")
    assert outcome.budget is not None
    assert outcome.budget == failed.payload["budget"]
    error = error_from_outcome(outcome)
    assert (error.status, error.code, error.reason_code) == (422, "needs_external", "local_budget")


def test_local_only_never_dispatches_and_returns_needs_external(tmp_path):
    """AC07, section 7.4: local_only returns needs_external with the partial results."""
    gateway = exhausted_local_budget(tmp_path, [text_turn("unused")])
    outcome, events = run(gateway, PROMPT, execution_mode="local_only")

    assert_needs_external(gateway, outcome, events, "local_budget")
    assert error_from_outcome(outcome).status == 422


# --- the mapping of failed runs ------------------------------------------------------------


def test_evidence_over_the_external_context_limit_is_400(tmp_path):
    """RM13, AC16: oversized context is an explicit error, with no external call."""
    gateway = exhausted_local_budget(
        tmp_path,
        [text_turn("unused")],
        external_capabilities=make_capabilities(tokenizer_available=False, context_limit=600),
    )
    outcome, _ = run(gateway, PROMPT)

    assert outcome.error_code == "context_limit_exceeded"
    error = error_from_outcome(outcome)
    assert (error.status, error.code, error.reason_code) == (
        400,
        "context_limit_exceeded",
        "context_limit",
    )
    assert gateway.external.requests == []


def test_an_external_generation_over_external_seconds_is_504(tmp_path):
    """Section 11: an exhausted deadline is 504; the run keeps external_failed."""

    class Stalled(ScriptedAdapter):
        async def generate(self, request):
            self.requests.append(request)
            await asyncio.sleep(1)
            yield TextDelta(text="too late")

    gateway = exhausted_local_budget(tmp_path, [], budgets=LocalBudgets(external_seconds=0.05))
    gateway.orchestrator.external = ConfiguredModel(
        adapter=Stalled(locality="external", capabilities=NO_TOKENIZER), model_id="stalled"
    )
    outcome, events = run(gateway, PROMPT)

    assert outcome.error_code == "external_failed"
    assert outcome.budget == "external_seconds"
    (failed,) = of_type(events, "run.failed")
    assert failed.payload["error_code"] == "external_failed"
    error = error_from_outcome(outcome)
    assert (error.status, error.code) == (504, "deadline_exceeded")
    assert "external_seconds" in error.message


def test_a_failing_provider_cancel_never_hides_an_exhausted_deadline(tmp_path):
    """RM03, RM14, section 11, spec 0.19 section 7.4: the cancel after a timeout is
    best effort; its own error never turns the 504 into an internal error."""

    class StalledFailingCancel(ScriptedAdapter):
        async def generate(self, request):
            self.requests.append(request)
            await asyncio.sleep(1)
            yield TextDelta(text="too late")

        async def cancel(self, generation_id):
            await super().cancel(generation_id)
            raise RuntimeError("provider cancel endpoint unreachable")

    stalled = StalledFailingCancel(locality="external", capabilities=NO_TOKENIZER)
    gateway = exhausted_local_budget(tmp_path, [], budgets=LocalBudgets(external_seconds=0.05))
    gateway.orchestrator.external = ConfiguredModel(adapter=stalled, model_id="stalled")
    outcome, events = run(gateway, PROMPT)

    assert outcome.error_code == "external_failed"
    assert outcome.budget == "external_seconds"
    assert error_from_outcome(outcome).code == "deadline_exceeded"
    (call,) = of_type(events, "model.started")[-1:]
    assert stalled.cancelled == [call.payload["call_id"]]


def test_an_exhausted_entire_run_time_limit_in_the_external_stage_is_504(tmp_path):
    """Section 11: the entire-run active time is a deadline too."""
    gateway = exhausted_local_budget(
        tmp_path, [text_turn("slow")], budgets=LocalBudgets(run_active_seconds=60)
    )
    gateway.external.on_generate = lambda request: gateway.clock.advance(61)
    outcome, _ = run(gateway, PROMPT)

    assert outcome.error_code == "external_failed"
    assert outcome.budget == "run_active_seconds"
    assert error_from_outcome(outcome).status == 504


def test_an_exhausted_generation_count_is_503_not_504(tmp_path):
    """Only a time limit is a deadline; a count limit is an external failure."""
    gateway = exhausted_local_budget(
        tmp_path, [text_turn("It is slow.")], budgets=LocalBudgets(external_generations=1)
    )
    outcome, _ = run(gateway, PROMPT)

    assert outcome.budget is None or outcome.budget not in (
        "run_active_seconds",
        "external_seconds",
    )
    assert error_from_outcome(outcome).status == 503


@pytest.mark.parametrize(
    ("error_code", "status"),
    [("workflow_unavailable", 503), ("internal_error", 500)],
)
def test_other_failed_runs_map_to_their_status(error_code, status):
    outcome = RunOutcome("run_1", RunState.FAILED, error_code=error_code)
    error = error_from_outcome(outcome)
    assert (error.status, error.code, error.run_id) == (status, error_code, "run_1")


@pytest.mark.parametrize(
    "error_code", ["missing_input", "input_expired", "approval_expired", "interrupted"]
)
def test_a_failure_reported_as_run_state_is_not_an_error_response(error_code):
    assert error_from_outcome(RunOutcome("run_1", RunState.FAILED, error_code=error_code)) is None


def test_runs_that_did_not_fail_are_not_error_responses():
    for state in (RunState.COMPLETED, RunState.WAITING_INPUT, RunState.CANCELLED):
        assert error_from_outcome(RunOutcome("run_1", state)) is None


def test_an_undocumented_failure_code_is_refused_not_guessed():
    with pytest.raises(ValueError):
        error_from_outcome(RunOutcome("run_1", RunState.FAILED, error_code="surprise"))


def test_the_error_is_an_api_error():
    outcome = RunOutcome("run_1", RunState.FAILED, error_code="needs_external", reason_code="x")
    assert isinstance(error_from_outcome(outcome), ApiError)
