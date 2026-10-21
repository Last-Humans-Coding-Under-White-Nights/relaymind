"""AC03: a request that omits the target service asks for it, then resumes (RM05, #16).

AC03: the clarification names the missing input; the resumed run
continues with the chosen skill in the run's language; there is no
guessed target and no mutation. RM05: resource names come from the
request, established conversation context, or tool results; the system
never invents one to make a tool call possible. RM17: a resume with a
stale state version is rejected with 409, and a terminal run never
restarts.

Spec 0.6 section 5 (clarification and resume): the run's language is
fixed by its first RouteDecision and kept across every resume. On
resume the gateway appends the question as an assistant turn and the
input as a user turn, and records the input and the waiting time in
clarification.answered. The gateway's own question is not RM05
provenance. A clarify decision with a skill_id continues with that skill
without a routing generation; one with skill_id null routes again with a
routing generation. A resume opens a new local stage (section 11). A run
asks at most clarification_rounds times; a further clarification fails
the run with missing_input. Waiting releases the execution slot and a
resume reacquires it (429 otherwise); an expired wait fails with
input_expired.

Spec 0.6 section 8: clarification.required carries question, language,
reason_code, skill_id, a non-blank missing, and waiting_seconds;
clarification.answered carries input (redacted, RM12), missing, and
waited_ms. Section 7.4 / 11: a resume without input text is 400 with
code invalid_resume_input. Section 11 / 12.1 / RM22: waiting is excluded
from active time, and run.completed and run.failed report active and
waiting time separately. All model output comes from scripted doubles
(spec section 13).
"""

import asyncio
import threading

import pytest

from gateway_helpers import (
    LOCAL_MODEL_ID,
    PAYMENTS_ZH,
    STAGING_OK,
    build_gateway,
    clarify_turn,
    executed_tools,
    health_tail,
    purposes,
    replace_handler,
    route_turn,
)
from helpers import FakeClock, of_type
from relaymind.adapters import Role
from relaymind.orchestrator.budgets import LocalBudgets
from relaymind.orchestrator.local_loop import ResumeInputError
from relaymind.orchestrator.run_state import (
    InvalidTransitionError,
    RunState,
    SlotUnavailableError,
    TerminalStateError,
)
from relaymind.store import StaleVersionError
from relaymind.store.trace_event import validate_trace_event

ASK_SERVICE = [route_turn("check-service-health")]


def start(gateway, prompt):
    outcome = asyncio.run(gateway.orchestrator.run_text(prompt))
    return outcome, gateway.store.events(outcome.run_id)


def resume(gateway, outcome, text, *, version=None):
    record = gateway.store.get_run(outcome.run_id)
    resumed = asyncio.run(
        gateway.orchestrator.resume(
            outcome.run_id, record.version if version is None else version, text
        )
    )
    return resumed, gateway.store.events(outcome.run_id)


def final_request(gateway):
    (request,) = [r for r in gateway.adapter.requests if r.generation_id.endswith("_final")]
    return request


# --- AC03: ask, never guess --------------------------------------------------------


@pytest.mark.parametrize(
    ("prompt", "language", "fragment"),
    [
        ("Is the service healthy?", "en", "Which service do you mean?"),
        ("这个服务健康吗？", "zh", "请问是哪个服务？"),
    ],
)
def test_a_request_without_a_target_service_asks_for_it(tmp_path, prompt, language, fragment):
    """AC03 / RM05: the gateway asks; the skill choice does not name a service,
    and nothing is guessed or executed. Section 8: the payload names the skill,
    the missing argument, and the waiting limit."""
    gateway = build_gateway(tmp_path, [route_turn("check-service-health", language)])
    before = gateway.sandbox.state_bytes()
    outcome, events = start(gateway, prompt)

    assert outcome.state is RunState.WAITING_INPUT
    assert outcome.answer is None
    assert outcome.clarification.startswith(fragment)
    for service in ("staging-api", "payments-api"):
        assert service in outcome.clarification
    (asked,) = of_type(events, "clarification.required")
    assert asked.payload == {
        "question": outcome.clarification,
        "language": language,
        "reason_code": "missing_input",
        "skill_id": "check-service-health",
        "missing": "service",
        "waiting_seconds": 300.0,
    }
    assert executed_tools(events) == []
    assert not of_type(events, "tool.proposed")
    assert not of_type(events, "answer.delta")
    assert gateway.sandbox.state_bytes() == before
    assert len(gateway.adapter.requests) == 1


def test_a_model_clarification_waits_for_skill_selection(tmp_path):
    """Section 8: a model-issued clarification has skill_id null and waits
    for skill_selection, never a null missing."""
    gateway = build_gateway(tmp_path, [clarify_turn()])
    outcome, events = start(gateway, "Check it for me.")
    assert outcome.state is RunState.WAITING_INPUT
    (asked,) = of_type(events, "clarification.required")
    assert asked.payload["question"] == "Which service do you mean?"
    assert asked.payload["skill_id"] is None
    assert asked.payload["missing"] == "skill_selection"
    assert executed_tools(events) == []


def test_waiting_for_input_releases_the_execution_slot(tmp_path):
    """Section 5: waiting releases the active execution slot."""
    gateway = build_gateway(tmp_path, ASK_SERVICE)
    outcome, _ = start(gateway, "Is the service healthy?")
    assert outcome.state is RunState.WAITING_INPUT
    assert gateway.store.active_run_id() is None


def test_the_waiting_limit_and_rounds_are_recorded_in_the_run(tmp_path):
    """RM03 / section 11: waiting_seconds and clarification_rounds are budgets."""
    gateway = build_gateway(tmp_path, ASK_SERVICE)
    _, events = start(gateway, "Is the service healthy?")
    (accepted,) = of_type(events, "run.accepted")
    assert accepted.payload["budgets"]["waiting_seconds"] == 300.0
    assert accepted.payload["budgets"]["clarification_rounds"] == 2


# --- resume with the chosen skill: no routing generation ---------------------------------


@pytest.mark.parametrize(
    ("prompt", "service", "final", "language"),
    [
        ("Is the service healthy?", "staging-api", STAGING_OK, "en"),
        ("这个服务健康吗？", "payments-api", PAYMENTS_ZH, "zh"),
    ],
)
def test_the_answer_to_the_question_resumes_the_run(tmp_path, prompt, service, final, language):
    """AC03 / section 5: the clarify decision named the skill, so the gateway
    checks the new input for the missing identifier without a routing
    generation and continues with that skill in the run's language."""
    gateway = build_gateway(tmp_path, [*ASK_SERVICE, *health_tail(service, final)])
    waiting, _ = start(gateway, prompt)
    outcome, events = resume(gateway, waiting, service)

    assert outcome.run_id == waiting.run_id
    assert outcome.state is RunState.COMPLETED
    assert outcome.answer == final
    assert executed_tools(events) == ["get_service_health"]
    (started,) = of_type(events, "tool.started")
    assert started.payload["model"] == LOCAL_MODEL_ID
    finished = of_type(events, "tool.finished")[0]
    assert finished.payload["result"]["data"]["service"] == service
    assert purposes(events) == ["routing", "skill", "skill", "final"]
    selected = of_type(events, "route.selected")
    assert [s.payload["decision"]["route"] for s in selected] == ["clarify", "local"]
    assert selected[1].actor == "gateway"
    assert selected[1].payload["mechanism"] == "clarification_resume"
    assert selected[1].payload["decision"]["skill_id"] == "check-service-health"
    # The run keeps the language of its first RouteDecision (section 5).
    assert [s.payload["decision"]["language"] for s in selected] == [language, language]
    (completed,) = of_type(events, "run.completed")
    assert completed.payload["external_calls"] == 0
    assert completed.payload["budget_usage"]["routing_generations"] == 1
    assert completed.payload["budget_usage"]["stages"] == 2


def test_a_chinese_resume_answers_in_chinese(tmp_path):
    """AC03 / section 5: an identifier-only reply keeps the run's language, so
    the final generation is told to answer in Chinese and the decision stays
    zh. The answer text itself is the scripted double's (section 13)."""
    gateway = build_gateway(tmp_path, [*ASK_SERVICE, *health_tail("payments-api", PAYMENTS_ZH)])
    waiting, _ = start(gateway, "这个服务健康吗？")
    assert waiting.decision.language.value == "zh"
    outcome, events = resume(gateway, waiting, "payments-api")

    assert outcome.decision.language.value == "zh"
    system_text = [m.content for m in final_request(gateway).messages if m.role is Role.SYSTEM]
    assert any("请用中文回答。" in text for text in system_text)
    assert not any("Answer in English." in text for text in system_text)
    (asked,) = of_type(events, "clarification.required")
    assert asked.payload["language"] == "zh"


def test_the_question_and_the_input_join_the_conversation(tmp_path):
    """Section 5: the question is appended as an assistant turn, then the
    resume input as a user turn."""
    gateway = build_gateway(tmp_path, [*ASK_SERVICE, *health_tail()])
    waiting, _ = start(gateway, "Is the service healthy?")
    resume(gateway, waiting, "staging-api")

    skill_request = gateway.adapter.requests[1]
    turns = [(m.role, m.content) for m in skill_request.messages if m.role is not Role.SYSTEM]
    assert turns == [
        (Role.USER, "Is the service healthy?"),
        (Role.ASSISTANT, waiting.clarification),
        (Role.USER, "staging-api"),
    ]


def test_the_resume_records_its_input_and_waiting_time(tmp_path):
    """Section 8 / RM22: clarification.answered records the input, the missing
    input it answers, and waited_ms from the monotonic clock (RM25)."""
    gateway = build_gateway(tmp_path, [*ASK_SERVICE, *health_tail()])
    waiting, _ = start(gateway, "Is the service healthy?")
    gateway.clock.advance(42.5)
    _, events = resume(gateway, waiting, "staging-api")

    (answered,) = of_type(events, "clarification.answered")
    assert answered.actor == "gateway"
    assert answered.payload == {"input": "staging-api", "missing": "service", "waited_ms": 42500}
    order = [e.event_type.value for e in events]
    resumed_route = max(i for i, kind in enumerate(order) if kind == "route.selected")
    assert order.index("clarification.required") < order.index("clarification.answered")
    assert order.index("clarification.answered") < resumed_route


def test_the_recorded_resume_input_is_redacted(tmp_path):
    """RM12 / section 8: the input is redacted before it is stored. Free text
    has no keys, so the secret values the run has seen are scrubbed."""
    gateway = build_gateway(tmp_path, [*ASK_SERVICE, *health_tail()])
    waiting, _ = start(gateway, "Is the service healthy?")
    gateway.orchestrator._waiting[waiting.run_id].secrets.add("hunter2")
    _, events = resume(gateway, waiting, "staging-api, password hunter2")
    (answered,) = of_type(events, "clarification.answered")
    assert answered.payload["input"] == "staging-api, password [redacted]"


def test_the_gateway_question_is_not_provenance(tmp_path):
    """RM05 / section 5: the question lists the valid services, but only user
    turns count, so a reply that names none asks again and runs no tool."""
    gateway = build_gateway(tmp_path, ASK_SERVICE)
    waiting, _ = start(gateway, "Is the service healthy?")
    outcome, events = resume(gateway, waiting, "you pick one")

    assert outcome.state is RunState.WAITING_INPUT
    assert outcome.clarification.startswith("Which service do you mean?")
    assert len(of_type(events, "clarification.required")) == 2
    assert executed_tools(events) == []
    assert len(gateway.adapter.requests) == 1


# --- resume after a model clarification: route again in a new stage --------------------------


def test_a_model_clarification_routes_again_in_a_new_stage(tmp_path):
    """Section 11: a resume opens a new local stage with the full per-stage
    limits, so with the default one routing generation per stage the
    resumed run gets its routing generation (spec 0.6 replaces the old
    local_budget stop)."""
    gateway = build_gateway(tmp_path, [clarify_turn(), route_turn("check-service-health")])
    gateway.adapter._turns += health_tail()
    waiting, _ = start(gateway, "Check it for me.")
    outcome, events = resume(gateway, waiting, "staging-api")

    assert outcome.state is RunState.COMPLETED
    assert purposes(events) == ["routing", "routing", "skill", "skill", "final"]
    selected = of_type(events, "route.selected")
    assert [s.payload["mechanism"] for s in selected] == ["native_function_calling"] * 2
    usage = of_type(events, "run.completed")[0].payload["budget_usage"]
    assert (usage["routing_generations"], usage["stages"]) == (2, 2)


def test_a_re_route_keeps_the_run_language(tmp_path):
    """Section 5: the language is fixed by the first RouteDecision; the
    English words in the resume input never change it."""
    gateway = build_gateway(
        tmp_path,
        [
            clarify_turn("请问是哪个服务？", language="zh"),
            route_turn("check-service-health", language="en"),
            *health_tail("payments-api", PAYMENTS_ZH),
        ],
    )
    waiting, _ = start(gateway, "帮我检查一下健康状态。")
    outcome, events = resume(gateway, waiting, "payments-api please, check it now")

    assert outcome.state is RunState.COMPLETED
    selected = of_type(events, "route.selected")
    assert [s.payload["decision"]["language"] for s in selected] == ["zh", "zh"]
    system_text = [m.content for m in final_request(gateway).messages if m.role is Role.SYSTEM]
    assert any("请用中文回答。" in text for text in system_text)


def test_the_entire_run_limits_bound_all_stages(tmp_path):
    """Section 11: the Entire run row bounds the sum of all stages."""
    gateway = build_gateway(
        tmp_path,
        [clarify_turn(), route_turn("check-service-health"), *health_tail()],
        budgets=LocalBudgets(run_generations=4),
    )
    waiting, _ = start(gateway, "Check it for me.")
    outcome, events = resume(gateway, waiting, "staging-api")

    assert (outcome.error_code, outcome.reason_code) == ("needs_external", "local_budget")
    (failed,) = of_type(events, "run.failed")
    assert failed.payload["budget"] == "run_generations"
    assert purposes(events) == ["routing", "routing", "skill", "skill"]
    assert not of_type(events, "answer.delta")


def test_the_entire_run_active_time_bounds_all_stages(tmp_path):
    """Section 11: 60 s of active time per run by default; here a smaller
    configured run limit is reached in the second stage although that
    stage is within its own four seconds."""
    clock = FakeClock()
    gateway = build_gateway(
        tmp_path,
        [clarify_turn(), route_turn("check-service-health"), *health_tail()],
        budgets=LocalBudgets(run_active_seconds=3.5),
        clock=clock,
        on_generate=lambda request: clock.advance(1.0),
    )
    waiting, _ = start(gateway, "Check it for me.")
    gateway.clock.advance(100)  # waiting counts toward no budget
    outcome, events = resume(gateway, waiting, "staging-api")

    assert outcome.reason_code == "local_budget"
    (failed,) = of_type(events, "run.failed")
    assert failed.payload["budget"] == "run_active_seconds"
    assert failed.payload["active_ms"] == 4000
    assert failed.payload["waiting_ms"] == 100000


def test_input_that_still_lacks_the_identifier_after_a_re_route_asks_again(tmp_path):
    gateway = build_gateway(tmp_path, [clarify_turn(), route_turn("check-service-health")])
    waiting, _ = start(gateway, "Check it for me.")
    outcome, events = resume(gateway, waiting, "the health one")

    assert outcome.state is RunState.WAITING_INPUT
    asked = of_type(events, "clarification.required")
    assert [(a.payload["skill_id"], a.payload["missing"]) for a in asked] == [
        (None, "skill_selection"),
        ("check-service-health", "service"),
    ]
    assert executed_tools(events) == []


# --- clarification rounds (section 11) ------------------------------------------------


def test_a_third_clarification_fails_the_run_with_missing_input(tmp_path):
    """Section 11: two clarification rounds per run; a third needed one ends
    the run as failed with missing_input, with no tool and no guess."""
    gateway = build_gateway(tmp_path, ASK_SERVICE)
    waiting, _ = start(gateway, "Is the service healthy?")
    second, _ = resume(gateway, waiting, "the main one")
    assert second.state is RunState.WAITING_INPUT
    outcome, events = resume(gateway, second, "the other one")

    assert outcome.state is RunState.FAILED
    assert (outcome.error_code, outcome.reason_code) == ("missing_input", "missing_input")
    record = gateway.store.get_run(waiting.run_id)
    assert (record.state, record.error_code) == ("failed", "missing_input")
    assert len(of_type(events, "clarification.required")) == 2
    assert len(of_type(events, "clarification.answered")) == 2
    (failed,) = of_type(events, "run.failed")
    assert failed.payload["error_code"] == "missing_input"
    assert executed_tools(events) == []
    assert gateway.store.active_run_id() is None


def test_the_clarification_rounds_are_configurable(tmp_path):
    gateway = build_gateway(tmp_path, ASK_SERVICE, budgets=LocalBudgets(clarification_rounds=0))
    outcome, events = start(gateway, "Is the service healthy?")
    assert (outcome.state, outcome.error_code) == (RunState.FAILED, "missing_input")
    assert not of_type(events, "clarification.required")


# --- resume errors (RM17, sections 5, 7.4, 11) ---------------------------------------------


@pytest.mark.parametrize("text", ["", "   ", None, 7])
def test_a_resume_without_input_text_is_400_invalid_resume_input(tmp_path, text):
    gateway = build_gateway(tmp_path, ASK_SERVICE)
    waiting, _ = start(gateway, "Is the service healthy?")
    version = gateway.store.get_run(waiting.run_id).version
    with pytest.raises(ResumeInputError) as caught:
        resume(gateway, waiting, text)
    assert (caught.value.http_status, caught.value.code) == (400, "invalid_resume_input")
    record = gateway.store.get_run(waiting.run_id)
    assert (record.state, record.version) == ("waiting_input", version)
    assert not of_type(gateway.store.events(waiting.run_id), "clarification.answered")
    # The run keeps waiting and takes a valid resume afterwards.
    gateway.adapter._turns += health_tail()
    outcome, _ = resume(gateway, waiting, "staging-api")
    assert outcome.state is RunState.COMPLETED


@pytest.mark.parametrize("text", ["", "   ", None])
def test_an_invalid_resume_body_is_400_even_after_the_wait_expired(tmp_path, text):
    """Section 7.4 / 11: a body that does not match the waiting state is 400
    invalid_resume_input; the run keeps waiting and its version is
    unchanged, even when waiting_seconds has passed (review 61
    #4335042306). The expiry sweep still ends it later (section 5)."""
    gateway = build_gateway(tmp_path, ASK_SERVICE)
    waiting, _ = start(gateway, "Is the service healthy?")
    version = gateway.store.get_run(waiting.run_id).version
    gateway.clock.advance(301)
    with pytest.raises(ResumeInputError):
        resume(gateway, waiting, text)
    record = gateway.store.get_run(waiting.run_id)
    assert (record.state, record.version) == ("waiting_input", version)
    assert not of_type(gateway.store.events(waiting.run_id), "run.failed")
    (expired,) = gateway.orchestrator.expire_waiting()
    assert expired.error_code == "input_expired"


def test_a_stale_resume_is_rejected_with_409(tmp_path):
    gateway = build_gateway(tmp_path, ASK_SERVICE)
    waiting, _ = start(gateway, "Is the service healthy?")
    version = gateway.store.get_run(waiting.run_id).version
    with pytest.raises(StaleVersionError) as caught:
        resume(gateway, waiting, "staging-api", version=version - 1)
    assert caught.value.http_status == 409
    assert gateway.store.get_run(waiting.run_id).state == "waiting_input"
    assert len(gateway.adapter.requests) == 1


def test_a_resume_of_a_finished_run_never_restarts_it(tmp_path):
    gateway = build_gateway(tmp_path, [route_turn("check-service-health"), *health_tail()])
    done, _ = start(gateway, "Is staging-api healthy right now?")
    assert done.state is RunState.COMPLETED
    with pytest.raises(TerminalStateError):
        resume(gateway, done, "payments-api")


def test_a_resume_while_another_run_is_active_is_429_and_keeps_waiting(tmp_path):
    """Section 5: a resume must reacquire the slot and may return 429."""
    gateway = build_gateway(tmp_path, ASK_SERVICE)
    waiting, _ = start(gateway, "Is the service healthy?")
    version = gateway.store.get_run(waiting.run_id).version
    machine = gateway.orchestrator.machine
    other = machine.create()
    machine.transition(other.run_id, other.version, RunState.ROUTING)
    with pytest.raises(SlotUnavailableError):
        resume(gateway, waiting, "staging-api")
    record = gateway.store.get_run(waiting.run_id)
    assert (record.state, record.version) == ("waiting_input", version)
    assert not of_type(gateway.store.events(waiting.run_id), "clarification.answered")
    assert gateway.store.active_run_id() == other.run_id


def test_a_run_not_waiting_for_input_cannot_take_input(tmp_path):
    gateway = build_gateway(tmp_path, [])
    record = gateway.orchestrator.machine.create()
    with pytest.raises(InvalidTransitionError):
        asyncio.run(gateway.orchestrator.resume(record.run_id, record.version, "staging-api"))


def test_a_waiting_run_this_orchestrator_does_not_track_says_so(tmp_path):
    """Section 5: a run in waiting_input that this process does not track
    (its in-memory state is gone) cannot take input, and the error says
    why instead of contradicting the state (review 61 #4335042312)."""
    gateway = build_gateway(tmp_path, ASK_SERVICE)
    waiting, _ = start(gateway, "Is the service healthy?")
    del gateway.orchestrator._waiting[waiting.run_id]
    with pytest.raises(InvalidTransitionError) as caught:
        resume(gateway, waiting, "staging-api")
    message = str(caught.value)
    assert "not waiting for input" not in message
    assert "not tracked" in message
    assert gateway.store.get_run(waiting.run_id).state == "waiting_input"


def test_an_unexpected_error_in_a_resumed_run_fails_it_and_frees_the_slot(tmp_path):
    """RM17 / section 5: any other exception after the resume fails the run
    with internal_error and its run.failed event; it never stays active."""
    gateway = build_gateway(tmp_path, ASK_SERVICE)
    waiting, _ = start(gateway, "Is the service healthy?")

    def broken(*args, **kwargs):
        raise RuntimeError("router bug")

    gateway.orchestrator.router.resume_decision = broken
    with pytest.raises(RuntimeError):
        resume(gateway, waiting, "staging-api")
    record = gateway.store.get_run(waiting.run_id)
    assert (record.state, record.error_code) == ("failed", "internal_error")
    assert gateway.store.active_run_id() is None
    assert waiting.run_id not in gateway.orchestrator._waiting


# --- section 5 / 11 / 12.1: waiting is bounded and is not active time ------------------------


def test_waiting_time_is_reported_apart_from_active_time(tmp_path):
    """Section 12.1 / RM22: run.completed reports active and waiting time
    separately; the waiting time is the recorded waited_ms."""
    gateway = build_gateway(tmp_path, [*ASK_SERVICE, *health_tail()])
    waiting, _ = start(gateway, "Is the service healthy?")
    gateway.clock.advance(240)  # far over the four-second active budget
    outcome, events = resume(gateway, waiting, "staging-api")

    assert outcome.state is RunState.COMPLETED
    (completed,) = of_type(events, "run.completed")
    (answered,) = of_type(events, "clarification.answered")
    assert completed.payload["waiting_ms"] == answered.payload["waited_ms"] == 240000
    assert completed.payload["active_ms"] == completed.payload["budget_usage"]["active_ms"]
    assert completed.payload["active_ms"] < 4000


def test_an_expired_wait_ends_as_failed_with_input_expired(tmp_path):
    gateway = build_gateway(tmp_path, ASK_SERVICE)
    waiting, _ = start(gateway, "Is the service healthy?")
    gateway.clock.advance(300.5)
    outcome, events = resume(gateway, waiting, "staging-api")

    assert outcome.state is RunState.FAILED
    assert outcome.error_code == "input_expired"
    record = gateway.store.get_run(waiting.run_id)
    assert (record.state, record.error_code) == ("failed", "input_expired")
    (failed,) = of_type(events, "run.failed")
    assert failed.payload["error_code"] == "input_expired"
    # Section 8 / 12.1: the wait counts toward waiting_ms; run.failed has
    # no waited_ms of its own (review 61 #4335042293).
    assert failed.payload["waiting_ms"] == 300500
    assert "waited_ms" not in failed.payload
    assert failed.payload["external_calls"] == 0
    assert not of_type(events, "clarification.answered")
    assert executed_tools(events) == []
    assert len(gateway.adapter.requests) == 1


def test_the_waiting_limit_is_configurable(tmp_path):
    gateway = build_gateway(tmp_path, ASK_SERVICE, budgets=LocalBudgets(waiting_seconds=10))
    waiting, events = start(gateway, "Is the service healthy?")
    assert of_type(events, "clarification.required")[0].payload["waiting_seconds"] == 10.0
    gateway.clock.advance(11)
    outcome, _ = resume(gateway, waiting, "staging-api")
    assert outcome.error_code == "input_expired"


def test_expired_waits_are_failed_without_a_resume(tmp_path):
    gateway = build_gateway(tmp_path, ASK_SERVICE)
    waiting, _ = start(gateway, "Is the service healthy?")

    gateway.clock.advance(299)
    assert gateway.orchestrator.expire_waiting() == []
    assert gateway.store.get_run(waiting.run_id).state == "waiting_input"

    gateway.clock.advance(2)
    (expired,) = gateway.orchestrator.expire_waiting()
    assert expired.run_id == waiting.run_id
    assert expired.error_code == "input_expired"
    record = gateway.store.get_run(waiting.run_id)
    assert (record.state, record.error_code) == ("failed", "input_expired")
    with pytest.raises(TerminalStateError):
        resume(gateway, waiting, "staging-api")


def fail_transition_once(gateway, run_id, *, before=None):
    """Make the next transition of run_id to failed raise, as a store error would.

    before, if given, runs first (for example a concurrent cancellation).
    """
    machine = gateway.orchestrator.machine
    original = machine.transition
    pending = [True]

    def transition(rid, version, to_state, **kwargs):
        if rid == run_id and to_state is RunState.FAILED and pending:
            pending.clear()
            if before is None:
                raise RuntimeError("store hiccup")
            before()
            return original(rid, version, to_state, **kwargs)
        return original(rid, version, to_state, **kwargs)

    machine.transition = transition


def test_one_failing_expiry_does_not_strand_the_other_waits(tmp_path):
    """Section 5: every expired wait ends as failed with input_expired. An
    error on one run is raised after the sweep has expired the others, and
    that run stays tracked, unchanged, so a later sweep expires it (review
    61 #4335042268, #4335042340: the store fails inside the transition).
    Section 12.1: its wait counts once toward the waiting_ms of run.failed."""
    gateway = build_gateway(tmp_path, [*ASK_SERVICE, *ASK_SERVICE])
    broken, _ = start(gateway, "Is the service healthy?")
    other, _ = start(gateway, "Is the service healthy?")
    fail_transition_once(gateway, broken.run_id)

    gateway.clock.advance(301)
    with pytest.raises(ExceptionGroup) as caught:
        gateway.orchestrator.expire_waiting()
    assert [type(e) for e in caught.value.exceptions] == [RuntimeError]
    assert gateway.store.get_run(other.run_id).error_code == "input_expired"
    assert gateway.store.get_run(broken.run_id).state == "waiting_input"
    assert broken.run_id in gateway.orchestrator._waiting

    gateway.clock.advance(1)
    (expired,) = gateway.orchestrator.expire_waiting()
    assert expired.run_id == broken.run_id
    assert gateway.store.get_run(broken.run_id).error_code == "input_expired"
    (failed,) = of_type(gateway.store.events(broken.run_id), "run.failed")
    assert failed.payload["waiting_ms"] == 302000


def test_a_store_error_on_an_expired_resume_keeps_the_wait_for_the_sweep(tmp_path):
    """Section 5: an expired wait ends as failed with input_expired. If the
    store fails while a resume records that, the run stays tracked and
    unchanged, so the sweep still expires it (review 61 #4335042268)."""
    gateway = build_gateway(tmp_path, ASK_SERVICE)
    waiting, _ = start(gateway, "Is the service healthy?")
    fail_transition_once(gateway, waiting.run_id)
    gateway.clock.advance(301)
    with pytest.raises(RuntimeError):
        resume(gateway, waiting, "staging-api")
    assert gateway.store.get_run(waiting.run_id).state == "waiting_input"
    (expired,) = gateway.orchestrator.expire_waiting()
    assert expired.error_code == "input_expired"
    (failed,) = of_type(gateway.store.events(waiting.run_id), "run.failed")
    assert failed.payload["waiting_ms"] == 301000


def test_a_wait_cancelled_during_its_expiry_is_dropped(tmp_path):
    """RM17: a terminal run never changes again. A run cancelled between the
    sweep's state check and its transition is dropped from the sweep with
    no error, and the other waits still expire (section 5; review 61
    #4335042324)."""
    gateway = build_gateway(tmp_path, [*ASK_SERVICE, *ASK_SERVICE])
    cancelled, _ = start(gateway, "Is the service healthy?")
    other, _ = start(gateway, "Is the service healthy?")
    machine = gateway.orchestrator.machine

    def cancel_now():
        record = gateway.store.get_run(cancelled.run_id)
        machine.cancel(cancelled.run_id, record.version, reason="user_cancelled")

    fail_transition_once(gateway, cancelled.run_id, before=cancel_now)
    gateway.clock.advance(301)
    (expired,) = gateway.orchestrator.expire_waiting()
    assert expired.run_id == other.run_id
    record = gateway.store.get_run(cancelled.run_id)
    assert (record.state, record.error_code) == ("cancelled", "user_cancelled")
    assert cancelled.run_id not in gateway.orchestrator._waiting
    assert not of_type(gateway.store.events(cancelled.run_id), "run.failed")


def test_a_waiting_run_cancelled_elsewhere_is_not_expired(tmp_path):
    """RM17: a terminal run never changes again. A waiting run that was
    cancelled is terminal, so the expiry sweep leaves it alone and still
    expires the other waits (section 5)."""
    gateway = build_gateway(tmp_path, [*ASK_SERVICE, *ASK_SERVICE])
    cancelled, _ = start(gateway, "Is the service healthy?")
    other, _ = start(gateway, "Is the service healthy?")
    record = gateway.store.get_run(cancelled.run_id)
    gateway.orchestrator.machine.cancel(
        cancelled.run_id,
        record.version,
        reason="user_cancelled",
        event=("run.cancelled", "gateway", {}),
    )

    gateway.clock.advance(301)
    (expired,) = gateway.orchestrator.expire_waiting()
    assert expired.run_id == other.run_id
    record = gateway.store.get_run(cancelled.run_id)
    assert (record.state, record.error_code) == ("cancelled", "user_cancelled")


def test_every_resume_event_matches_the_published_schema(tmp_path):
    """Section 8: the events a resumed run writes validate at the boundary."""
    gateway = build_gateway(tmp_path, [*ASK_SERVICE, *health_tail()])
    waiting, _ = start(gateway, "Is the service healthy?")
    _, events = resume(gateway, waiting, "staging-api")
    for event in events:
        validate_trace_event(event.to_json_object())


def test_a_tool_bounded_by_the_entire_run_time_names_that_budget(tmp_path):
    """Section 11: in a resumed stage the tool is bounded by the tighter of
    the stage and the entire-run time left; when the run limit binds, the
    stop names run_active_seconds, and the timeout is never retried."""
    clock = FakeClock()
    gateway = build_gateway(
        tmp_path,
        [clarify_turn(), route_turn("check-service-health"), *health_tail()],
        budgets=LocalBudgets(run_active_seconds=3.2),
        clock=clock,
        on_generate=lambda request: clock.advance(1.0),
    )
    release = threading.Event()
    tool = gateway.tools.get("get_service_health")

    def blocked(args):
        release.wait(2)
        return tool.declaration.handler(args)

    replace_handler(gateway, tool.name, blocked)
    waiting, _ = start(gateway, "Check it for me.")
    try:
        outcome, events = resume(gateway, waiting, "staging-api")
    finally:
        release.set()

    assert outcome.reason_code == "local_budget"
    (failed,) = of_type(events, "run.failed")
    assert failed.payload["budget"] == "run_active_seconds"
    assert len(of_type(events, "tool.finished")) == 1


def test_a_generation_bounded_by_the_entire_run_time_names_that_budget(tmp_path):
    """Section 11: a local generation is cut at the tighter of the stage and
    entire-run time left, and the stop names the budget that bound it."""
    clock = FakeClock()
    gateway = build_gateway(
        tmp_path,
        [clarify_turn(), route_turn("check-service-health"), *health_tail()],
        budgets=LocalBudgets(run_active_seconds=2.2),
        clock=clock,
        on_generate=lambda request: clock.advance(1.0),
    )
    scripted = gateway.adapter.generate

    async def slow_skill_generation(request):
        if request.generation_id.endswith("_gen3_skill"):
            await asyncio.sleep(5)
        async for event in scripted(request):
            yield event

    gateway.adapter.generate = slow_skill_generation
    waiting, _ = start(gateway, "Check it for me.")
    outcome, events = resume(gateway, waiting, "staging-api")

    assert outcome.reason_code == "local_budget"
    (failed,) = of_type(events, "run.failed")
    assert failed.payload["budget"] == "run_active_seconds"
    assert executed_tools(events) == []
