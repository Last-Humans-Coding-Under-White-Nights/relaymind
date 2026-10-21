"""Bounded local generation loop (RM01-RM05, RM12, RM16, sections 4.1, 7.3, 11).

One run, start to end, with the local model only:

1. accepted: record the section 8 run.accepted payload: the requested
   and effective execution mode, the model alias, the budgets (RM03),
   the skill roots, and the skills offered (section 4.2). That snapshot
   is the one RM07 pins: the selected skill is loaded at exactly that
   version and hash.
2. routing: in external_only the gateway records its own decision
   (route external, reason execution_mode, section 7.4) and runs no
   generation. Otherwise check the adapter's capabilities, measure the
   router input with the local tokenizer, and either take the bypass
   decision for an oversized input (section 4.1) or run the single
   routing generation (RM02, ``relaymind.orchestrator.router``).
   A non-local route returns needs_external. The run keeps the language
   of this first RouteDecision across every resume (section 5).
2a. Clarify suspends the run in waiting_input with clarification.required
   (RM05; spec 0.6 sections 5 and 8): question, language, reason_code,
   skill_id, the missing input, and waiting_seconds. The stage ends and
   the wait is not active time (section 11). ``LocalOrchestrator.resume``
   takes explicit new input text: it records clarification.answered
   (input redacted, missing, waited_ms), appends the question as an
   assistant turn and the input as a user turn, and opens a new local
   stage with the full per-stage budgets, bounded by the entire-run
   limits. When the clarify decision named a skill, the gateway checks
   the new input for the missing identifier without a routing
   generation; when its skill_id was null, the run routes again with a
   routing generation. At most ``clarification_rounds`` clarifications
   are asked; a further one fails the run with missing_input. A wait
   longer than ``waiting_seconds`` fails with input_expired.
3. local_running: load the selected skill (skill.loaded, RM06) and loop.
   Each generation returns a tool proposal or a finalize proposal (an
   answer text). A tool proposal is validated once (RM09) and checked
   for identifiers that no user turn or tool result named (RM05) before
   the executor runs it (RM10). A mutating tool is never executed: the
   confirmation flow (RM11, #19) is not wired into this loop yet, so
   such a proposal stops the run with reason code capability_missing
   and nothing is mutated. A failed or unknown tool outcome stops the
   run with reason code tool_failure, after the one read-only retry
   section 11 allows for a transient failure. A finalize proposal must
   meet the skill's completion predicate; then a final generation runs
   with tools disabled (section 7.3), its answer is checked by the same
   predicate, and only then published as answer.delta events, in one
   batch.
4. An invalid proposal gets at most the configured format-repair
   generations, inside the generation limit (RM03).

Every generation is charged by its purpose in one place, ``_generate``,
before it runs, and every tool execution is charged when the executor
is about to run it (a replay is not an execution). Exceeding a budget
is an explicit stop with reason code local_budget, never silent
continuation (RM04). A tool is bounded by its timeout or the remaining
active budget, whichever is shorter.

5. Escalation (RM04, RM13, RM14). Every stop above is an Escalation with
   an RM04 reason code. If external dispatch is permitted (an external
   model is configured, the mode is not local_only, and the entire-run
   limits leave room), ``_escalate`` builds the HandoffEnvelope from the
   run store (``relaymind.orchestrator.handoff``), measures the external
   request against the external context limit, records handoff.started,
   and moves the run to external_running. The local actor's dispatch
   authority is revoked at that point; the external model then proposes
   tools that run through the same validation, provenance check, and
   executor, and it owns the final answer (RM14, RM20). A proposal that
   repeats a completed operation gets the recorded result and runs no
   handler (RM10). Evidence over the external context limit fails the
   run with context_limit_exceeded before any external call (RM13); an
   external stage that cannot finish fails with external_failed
   (section 11). If dispatch is not permitted, the run ends as failed
   with error code needs_external and the RM04 reason code; partial
   evidence stays in the trace (section 5).

Any other exception, in the first stage or after a resume, fails the
run with error code internal_error and its run.failed event, which
releases the execution slot, and then propagates; a cancelled run ends
as cancelled. A terminal run never changes again (RM17).

The trace proves locality (RM01, RM16): every model call is labelled
with the locality its adapter declares, and run.completed lists every
model call and the number of them that were external. run.completed and
run.failed report active_ms and waiting_ms separately (RM22, section
12.1); waiting_ms is the sum of the recorded waits.

Tool arguments, tool results, and model text are redacted of configured
secret fields before they are persisted (RM12, section 8).
"""

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from relaymind.adapters import (
    AdapterCapabilities,
    Completed,
    GenerationFailed,
    GenerationOptions,
    GenerationRequest,
    Message,
    ModelAdapter,
    Role,
    TextDelta,
    TokenCountError,
    ToolCall,
    ToolSchema,
    UnsupportedCapabilityError,
    Usage,
)
from relaymind.orchestrator import identifiers
from relaymind.orchestrator.budgets import BudgetExceededError, BudgetMeter, LocalBudgets
from relaymind.orchestrator.handoff import (
    EXTERNAL_ACTOR,
    LOCAL_ACTOR,
    DispatchAuthority,
    RequestSize,
    build_handoff_envelope,
    external_messages,
    measure_request,
    unsettled_operations,
)
from relaymind.orchestrator.route_decision import (
    REASON_CODES,
    ExecutionMode,
    Language,
    Route,
    RouteDecision,
    RouteDecisionError,
)
from relaymind.orchestrator.router import Router, Routing, request_language
from relaymind.orchestrator.run_state import (
    TERMINAL_STATES,
    InvalidTransitionError,
    RunState,
    RunStateMachine,
    SlotUnavailableError,
    TerminalStateError,
)
from relaymind.redaction import Redactor, redact_text
from relaymind.skills import Skill, SkillRegistry
from relaymind.skills.predicates import COMPLETION_PREDICATES, Predicate
from relaymind.store import RunRecord, RunStore, StaleVersionError, TraceClock
from relaymind.store.run_store import EventDraft
from relaymind.store.trace_event import EventType
from relaymind.tools import CallerContext, OperatorPolicy, ToolRegistry, ToolRejectedError
from relaymind.tools.executor import Execution, OperationInFlightError, ToolExecutor, ValidatedCall
from relaymind.tools.registry import SideEffect, ToolResult, ToolStatus

GATEWAY_ACTOR = "gateway"
LOCALITIES = (LOCAL_ACTOR, EXTERNAL_ACTOR)
NEEDS_EXTERNAL = "needs_external"
INTERNAL_ERROR = "internal_error"
INPUT_EXPIRED = "input_expired"
MISSING_INPUT = "missing_input"
INVALID_RESUME_INPUT = "invalid_resume_input"
CONTEXT_LIMIT_EXCEEDED = "context_limit_exceeded"
EXTERNAL_FAILED = "external_failed"
CONFIRMATION_REQUIRED = "confirmation_required"
ROUTING_MECHANISM = "native_function_calling"
RESUME_MECHANISM = "clarification_resume"

# Skill-stage layout. In development runs against the pinned model, it
# often wrote its tool call as plain JSON text instead of a native call
# when the skill body came first in the system prompt. With the skill
# description and the tool to call first, and the full body as a system
# message after the request, it made native calls (the recorded live run
# is in docs/EVIDENCE.md). The body is still loaded into the model
# context for every skill generation (RM06).
SKILL_INSTRUCTION = "Call the {tools} tool to get the data before you answer."
BODY_HEADER = "Skill instructions:"
FINAL_INSTRUCTION = "Write the final answer to the user now, from the tool results only."
# The run language (its first RouteDecision) picks the answer language.
ANSWER_LANGUAGE = {Language.EN: "Answer in English.", Language.ZH: "请用中文回答。"}
# Local generations are deterministic, like the routing generation.
LOCAL_OPTIONS = GenerationOptions(temperature=0)
# External providers differ in the sampling options they accept (RM18),
# so the external stage sets none.
EXTERNAL_OPTIONS = GenerationOptions()
CORRECTION = (
    "[gateway] The answer did not meet the skill's completion criteria. "
    "Answer again from the tool results only, following the skill rules."
)


@dataclass(frozen=True)
class ConfiguredModel:
    """A configured model: its adapter and its identity in traces.

    actor is the locality the adapter declares (RM16); an adapter that
    declares none is refused, because the trace could not prove where
    its calls ran.
    """

    adapter: ModelAdapter
    model_id: str

    def __post_init__(self) -> None:
        if getattr(self.adapter, "locality", None) not in LOCALITIES:
            raise ValueError(
                f"adapter {type(self.adapter).__name__} must declare locality "
                f"{' or '.join(LOCALITIES)}"
            )
        if not isinstance(self.model_id, str) or not self.model_id.strip():
            raise ValueError("model_id must be a non-blank string")

    @property
    def actor(self) -> str:
        return str(self.adapter.locality)


@dataclass(frozen=True)
class RunOutcome:
    """Where a run ended and what it produced."""

    run_id: str
    state: RunState
    answer: str | None = None
    clarification: str | None = None
    error_code: str | None = None
    reason_code: str | None = None
    decision: RouteDecision | None = None


class ResumeInputError(ValueError):
    """A resume body that does not match the waiting state (400, sections 7.4, 11).

    In waiting_input that is missing or blank input text. The run keeps
    waiting and its version is unchanged.
    """

    http_status = 400
    code = INVALID_RESUME_INPUT


class Escalation(Exception):
    """End the run as needs_external with an RM04 reason code.

    The reason code must be one of the section 8 operational labels;
    anything else is a gateway bug, refused before it reaches a trace.
    Raised inside a run, that refusal is an unexpected exception, so the
    run fails with internal_error and never stays active.

    hand_off is False for a stop the external stage could not resolve
    either, such as a mutation that needs the confirmation flow (RM11);
    such a stop ends as needs_external without a handoff (section 4.4).
    """

    def __init__(
        self, reason_code: str, detail: str, *, hand_off: bool = True, **extra: Any
    ) -> None:
        if reason_code not in REASON_CODES:
            raise ValueError(f"{reason_code!r} is not a section 8 reason code")
        super().__init__(detail)
        self.reason_code = reason_code
        self.detail = detail
        self.hand_off = hand_off
        self.extra = extra


class ExternalFailure(Exception):
    """The external stage cannot finish (error code external_failed, section 11)."""

    def __init__(self, detail: str, **extra: Any) -> None:
        super().__init__(detail)
        self.detail = detail
        self.extra = extra


class ContextLimitExceeded(Exception):
    """The external request does not fit the external context limit (RM13)."""

    def __init__(self, size: RequestSize) -> None:
        super().__init__(f"external request of {size.size} exceeds the limit {size.limit}")
        self.size = size


@dataclass(frozen=True)
class _Pending:
    """The clarification a waiting run asked (section 8 payload fields)."""

    question: str
    missing: str
    skill_id: str | None


@dataclass
class _Generation:
    calls: list[ToolCall] = field(default_factory=list)
    pieces: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "".join(self.pieces)


class LocalOrchestrator:
    """Runs managed requests through the bounded local loop."""

    def __init__(
        self,
        model: ConfiguredModel,
        *,
        skills: SkillRegistry,
        tools: ToolRegistry,
        store: RunStore,
        policy: OperatorPolicy,
        caller: CallerContext,
        budgets: LocalBudgets | None = None,
        clock: TraceClock | None = None,
        redactor: Redactor | None = None,
        enabled_workflows: Collection[str] = (),
        parallel_in_auto: bool = False,
        external: ConfiguredModel | None = None,
    ) -> None:
        self.model = model
        self.external = external
        self.skills = skills
        self.tools = tools
        self.store = store
        self.policy = policy
        self.caller = caller
        self.budgets = budgets or LocalBudgets()
        # One TraceClock for budgets, durations, and the store (RM25).
        self.clock = clock or store.clock
        self.redactor = redactor or Redactor()
        self.router = Router(
            skills,
            tools,
            budgets=self.budgets,
            enabled_workflows=enabled_workflows,
            parallel_in_auto=parallel_in_auto,
        )
        self.executor = ToolExecutor(tools, store, clock=self.clock, redactor=self.redactor)
        self.machine = RunStateMachine(store)
        # Runs this orchestrator suspended in waiting_input. A process
        # restart fails unfinished runs as interrupted (section 5), so
        # nothing here has to outlive the process.
        self._waiting: dict[str, _Run] = {}

    async def run_text(self, text: str, **options: Any) -> RunOutcome:
        """Run one user request given as plain text."""
        return await self.run((Message(role=Role.USER, content=text),), **options)

    async def run(
        self,
        messages: Sequence[Message],
        *,
        execution_mode: str | None = None,
        model_alias: str | None = None,
    ) -> RunOutcome:
        """Run one managed request to a terminal or waiting state.

        execution_mode is the requested mode (None means auto, section
        7.4); operator policy checks belong to the API layer. model_alias
        is the chat request's virtual alias, or None for a native run.
        Raises SlotUnavailableError (429) when another run holds the
        execution slot; no run is created then.
        """
        run = _Run(self, tuple(messages), execution_mode, model_alias)
        return self._keep_if_waiting(run, await run.execute())

    async def resume(self, run_id: str, expected_version: int, text: object) -> RunOutcome:
        """Resume a run waiting for input with explicit new input (section 5, RM17).

        A terminal run never restarts (TerminalStateError) and a stale
        version is 409 (StaleVersionError). Missing or blank input text is
        400 invalid_resume_input (ResumeInputError) and leaves the run
        waiting with its version unchanged (sections 7.4, 11); the body is
        checked before the waiting limit, so a malformed resume never
        changes the run, even after the wait expired. A valid resume of a
        wait older than waiting_seconds ends the run as failed with
        input_expired. If another run holds the execution slot, the resume
        is 429 and the run keeps waiting (section 5).
        """
        record = self.store.get_run(run_id)
        state = RunState(record.state)
        if state in TERMINAL_STATES:
            self._waiting.pop(run_id, None)
            raise TerminalStateError(f"run {run_id} is {state}; terminal runs never restart")
        if record.version != expected_version:
            raise StaleVersionError(run_id, expected_version, record.version)
        if state is not RunState.WAITING_INPUT:
            raise InvalidTransitionError(f"run {run_id} is {state}, not waiting for input")
        run = self._waiting.get(run_id)
        if run is None:
            raise InvalidTransitionError(
                f"run {run_id} is waiting_input but is not tracked by this gateway process"
            )
        if not isinstance(text, str) or not text.strip():
            raise ResumeInputError("a resume in waiting_input needs non-blank input text")
        if run.wait_expired():
            # Untracked only once the expiry is recorded, so a store
            # error leaves the wait for the next sweep (section 5).
            outcome = run.expire()
            del self._waiting[run_id]
            return outcome
        run.reopen(expected_version)
        del self._waiting[run_id]
        return self._keep_if_waiting(run, await run.continue_with(text))

    def expire_waiting(self) -> list[RunOutcome]:
        """Fail every wait older than waiting_seconds with input_expired (section 5).

        A run that left waiting_input some other way (for example, it was
        cancelled) is dropped without a transition: a terminal run never
        changes again (RM17). That holds when it is cancelled during its
        own expiry too: the failed transition is then not an error. An
        error on one run does not stop the sweep:
        the other waits are still expired, the run that failed stays
        tracked for the next sweep, and the errors are raised together at
        the end. The expired runs are recorded in the store either way.
        """
        expired: list[RunOutcome] = []
        errors: list[Exception] = []
        for run_id, run in list(self._waiting.items()):
            try:
                if self.store.get_run(run_id).state != RunState.WAITING_INPUT.value:
                    del self._waiting[run_id]
                elif run.wait_expired():
                    expired.append(run.expire())
                    del self._waiting[run_id]
            except Exception as exc:
                if self._left_waiting(run_id):
                    self._waiting.pop(run_id, None)
                else:
                    errors.append(exc)
        if errors:
            raise ExceptionGroup("some expired waits could not be failed", errors)
        return expired

    def _left_waiting(self, run_id: str) -> bool:
        """True when the store shows the run is no longer in waiting_input."""
        try:
            return self.store.get_run(run_id).state != RunState.WAITING_INPUT.value
        except Exception:
            return False

    def _keep_if_waiting(self, run: "_Run", outcome: RunOutcome) -> RunOutcome:
        if outcome.state is RunState.WAITING_INPUT:
            self._waiting[run.run_id] = run
        return outcome


class _Run:
    """The state of one run while the loop drives it."""

    def __init__(
        self,
        owner: LocalOrchestrator,
        messages: tuple[Message, ...],
        requested_mode: str | None,
        model_alias: str | None,
    ) -> None:
        self.o = owner
        self.messages = messages
        self.requested_mode = requested_mode
        self.mode = ExecutionMode(requested_mode or ExecutionMode.AUTO)
        self.model_alias = model_alias
        self.meter = BudgetMeter(owner.budgets, clock=owner.clock)
        self.model_calls: list[dict[str, str]] = []
        self.record: RunRecord
        self.decision: RouteDecision | None = None
        # Fixed by the first RouteDecision and kept across resumes (section 5).
        self.language: Language | None = None
        # The clarification being waited on, its start, and the waits so far.
        self.pending: _Pending | None = None
        self.waiting_since: float | None = None
        self.waiting_ms = 0
        self.clarifications = 0
        # The run.accepted snapshot (section 8); RM07 pins skills to it.
        self.offered = owner.skills.offered_snapshot(owner.router.offered)
        # Secret values seen in tool results, scrubbed from model text (RM12).
        self.secrets: set[str] = set()
        self._calls = 0
        # RM14: who may dispatch and who owns the final answer.
        self.authority = DispatchAuthority()
        # The skill the local loop loaded, and every tool result of the run.
        self.skill: Skill | None = None
        self.results: list[ToolResult] = []
        # Local proposals that never produced an operation, and rejected
        # drafts; a handoff carries them labeled as proposals (RM13).
        self.local_proposals: dict[str, dict[str, Any]] = {}
        self.drafts: list[dict[str, Any]] = []

    # -- trace helpers ---------------------------------------------------------

    @property
    def run_id(self) -> str:
        return self.record.run_id

    @property
    def active_model(self) -> ConfiguredModel:
        """The model that holds dispatch authority now (RM14)."""
        if self.authority.holder == EXTERNAL_ACTOR:
            assert self.o.external is not None
            return self.o.external
        return self.o.model

    @property
    def actor(self) -> str:
        return self.active_model.actor

    def emit(self, event_type: EventType | str, actor: str, payload: Mapping[str, Any]) -> None:
        self.o.store.append_event(self.run_id, EventType(event_type), actor, payload)

    def move(self, state: RunState, **kwargs: Any) -> None:
        self.record = self.o.machine.transition(self.run_id, self.record.version, state, **kwargs)

    def scrub(self, text: str) -> str:
        return redact_text(text, self.secrets)

    def _summary(self) -> dict[str, Any]:
        """The run.completed / run.failed evidence (RM16, RM22, section 12.1)."""
        usage = self.meter.usage()
        return {
            "model_calls": list(self.model_calls),
            "external_calls": sum(1 for c in self.model_calls if c["actor"] == EXTERNAL_ACTOR),
            "budget_usage": usage,
            # Active and waiting time, reported separately.
            "active_ms": usage["active_ms"],
            "waiting_ms": self.waiting_ms,
        }

    # -- the run ---------------------------------------------------------------

    async def execute(self) -> RunOutcome:
        if self.o.machine.active_run_id() is not None:
            raise SlotUnavailableError("another run holds the execution slot (section 11)")
        # RM22: run.accepted is written with the run in one transaction,
        # so an invalid payload creates no run, and a run can always fail
        # or be cancelled from accepted (section 5).
        self.record = self.o.machine.create(
            accepted=(
                EventType.RUN_ACCEPTED,
                GATEWAY_ACTOR,
                {
                    "requested_execution_mode": self.requested_mode,
                    "effective_execution_mode": self.mode.value,
                    "model_alias": self.model_alias,
                    "budgets": self.o.budgets.to_json_object(),
                    **self.offered,
                },
            )
        )
        try:
            self.move(RunState.ROUTING)
        except SlotUnavailableError:
            # Another run took the slot between the check and here: this
            # run never started, so it leaves no record (429).
            self.o.machine.delete_run(self.run_id)
            raise
        except BaseException as exc:
            self._abort(exc)
            raise
        return await self._guarded(self._route)

    async def _guarded(self, route: Callable[[], Awaitable[Routing]]) -> RunOutcome:
        """Drive the run; any unexpected exception ends it (RM17, section 5)."""
        try:
            return await self._drive(route)
        except BaseException as exc:
            self._abort(exc)
            raise

    async def _drive(self, route: Callable[[], Awaitable[Routing]]) -> RunOutcome:
        """Route, then wait, stop, or run the local loop to completion."""
        try:
            routing = await route()
            decision = self.decision = routing.decision
            if self.language is None:
                self.language = decision.language
            if decision.route is Route.CLARIFY:
                return self._clarify(routing)
            if decision.route is not Route.LOCAL:
                raise Escalation(
                    decision.reason_code, f"route {decision.route.value} needs external"
                )
            self.move(RunState.LOCAL_RUNNING)
            answer = await self._local(decision)
        except Escalation as stop:
            return await self._escalate(stop)
        except BudgetExceededError as exceeded:
            return await self._escalate(
                Escalation(exceeded.reason_code, str(exceeded), budget=exceeded.budget)
            )
        self.move(
            RunState.COMPLETED,
            event=(
                EventType.RUN_COMPLETED,
                GATEWAY_ACTOR,
                {"final_answer_model": self.o.model.model_id, "answer": answer, **self._summary()},
            ),
        )
        return RunOutcome(self.run_id, RunState.COMPLETED, answer=answer, decision=decision)

    # -- waiting for input (spec 0.6 sections 5, 8, 11) ------------------------------

    def _clarify(self, routing: Routing) -> RunOutcome:
        """Suspend in waiting_input, or fail once the clarification rounds are spent."""
        decision = routing.decision
        assert decision.clarification is not None
        if not routing.missing or not routing.missing.strip():
            raise ValueError("a clarification must name the input it waits for (section 8)")
        if self.clarifications >= self.o.budgets.clarification_rounds:
            return self._fail(
                MISSING_INPUT,
                reason_code=MISSING_INPUT,
                detail=(
                    f"a clarification beyond the {self.o.budgets.clarification_rounds} "
                    "allowed rounds was needed"
                ),
                missing=routing.missing,
            )
        self.clarifications += 1
        # Section 11: the stage ends; waiting is not active time.
        self.meter.end_stage()
        self.move(
            RunState.WAITING_INPUT,
            event=(
                EventType.CLARIFICATION_REQUIRED,
                GATEWAY_ACTOR,
                {
                    "question": decision.clarification,
                    "language": decision.language.value,
                    "reason_code": decision.reason_code,
                    "skill_id": decision.skill_id,
                    "missing": routing.missing,
                    "waiting_seconds": float(self.o.budgets.waiting_seconds),
                },
            ),
        )
        self.pending = _Pending(decision.clarification, routing.missing, decision.skill_id)
        self.waiting_since = self.o.clock.monotonic()
        return RunOutcome(
            self.run_id,
            RunState.WAITING_INPUT,
            clarification=decision.clarification,
            reason_code=decision.reason_code,
            decision=decision,
        )

    def _waited_ms(self) -> int:
        assert self.waiting_since is not None
        return round((self.o.clock.monotonic() - self.waiting_since) * 1000)

    def wait_expired(self) -> bool:
        assert self.waiting_since is not None
        return self.o.clock.monotonic() - self.waiting_since > self.o.budgets.waiting_seconds

    def expire(self) -> RunOutcome:
        """An expired wait ends as failed with input_expired (section 5).

        Section 12.1: the wait counts toward the waiting_ms of run.failed,
        the only waiting field that event has (section 8). The wait is
        closed only once the transition succeeds: if the store fails, the
        run keeps its wait unchanged, so a later sweep can expire it.
        """
        waited_ms = self._waited_ms()
        self.record = self.o.store.get_run(self.run_id)
        self.waiting_ms += waited_ms
        try:
            outcome = self._fail(INPUT_EXPIRED, detail="no input arrived within the waiting limit")
        except BaseException:
            self.waiting_ms -= waited_ms
            raise
        self.waiting_since = None
        return outcome

    def reopen(self, expected_version: int) -> None:
        """Back to the state that suspended; reacquires the slot or raises 429 (section 5)."""
        self.record = self.o.machine.resume(self.run_id, expected_version)

    async def continue_with(self, text: str) -> RunOutcome:
        """Record the answer, open a new stage, and continue the run (section 5)."""

        async def route() -> Routing:
            pending = self.pending
            assert pending is not None
            waited_ms = self._waited_ms()
            self.waiting_ms += waited_ms
            self.emit(
                EventType.CLARIFICATION_ANSWERED,
                GATEWAY_ACTOR,
                {"input": self.scrub(text), "missing": pending.missing, "waited_ms": waited_ms},
            )
            self.pending = None
            self.waiting_since = None
            # Section 11: a new local stage with the full per-stage limits.
            self.meter.new_stage()
            self.messages = (
                *self.messages,
                Message(role=Role.ASSISTANT, content=pending.question),
                Message(role=Role.USER, content=text),
            )
            if pending.skill_id is None:
                # No skill was chosen: route again with a routing generation.
                return await self._route()
            return self._resume_route(pending.skill_id)

        return await self._guarded(route)

    def _resume_route(self, skill_id: str) -> Routing:
        """The clarify named a skill: check the new input, no generation (section 5)."""
        assert self.language is not None
        try:
            routing = self.o.router.resume_decision(
                skill_id, self.messages, mode=self.mode, language=self.language
            )
        except RouteDecisionError as exc:
            raise Escalation("invalid_output", f"resume decision rejected: {exc}") from exc
        self.emit(
            EventType.ROUTE_SELECTED,
            GATEWAY_ACTOR,
            {"decision": routing.decision.to_json_object(), "mechanism": RESUME_MECHANISM},
        )
        return routing

    def _fail(self, error_code: str, *, detail: str, **extra: Any) -> RunOutcome:
        """End the run as failed with error_code and its run.failed event."""
        self.move(
            RunState.FAILED,
            error_code=error_code,
            event=(
                EventType.RUN_FAILED,
                GATEWAY_ACTOR,
                {"error_code": error_code, "detail": detail, **extra, **self._summary()},
            ),
        )
        return RunOutcome(
            self.run_id,
            RunState.FAILED,
            error_code=error_code,
            reason_code=extra.get("reason_code"),
            decision=self.decision,
        )

    def _needs_external(self, stop: Escalation) -> RunOutcome:
        self.move(
            RunState.FAILED,
            error_code=NEEDS_EXTERNAL,
            event=(
                EventType.RUN_FAILED,
                GATEWAY_ACTOR,
                {
                    "error_code": NEEDS_EXTERNAL,
                    "reason_code": stop.reason_code,
                    "detail": stop.detail,
                    **stop.extra,
                    **self._summary(),
                },
            ),
        )
        return RunOutcome(
            self.run_id,
            RunState.FAILED,
            error_code=NEEDS_EXTERNAL,
            reason_code=stop.reason_code,
            decision=self.decision,
        )

    def _abort(self, exc: BaseException) -> None:
        """End a run an unexpected exception interrupted (RM17, section 5).

        The run fails with internal_error, or is cancelled when the task
        was cancelled; either way the slot is released. Only the
        exception type is recorded: a message may carry data (RM12). A
        failure to record this never hides the original exception.
        """
        try:
            self.record = self.o.machine.get(self.run_id)
            if RunState(self.record.state) in TERMINAL_STATES:
                return
            if isinstance(exc, asyncio.CancelledError):
                self.record = self.o.machine.cancel(
                    self.run_id,
                    self.record.version,
                    event=(EventType.RUN_CANCELLED, GATEWAY_ACTOR, self._summary()),
                )
                return
            self.move(
                RunState.FAILED,
                error_code=INTERNAL_ERROR,
                event=(
                    EventType.RUN_FAILED,
                    GATEWAY_ACTOR,
                    {
                        "error_code": INTERNAL_ERROR,
                        "detail": type(exc).__name__,
                        **self._summary(),
                    },
                ),
            )
        except Exception:
            pass

    # -- escalation and the external stage (RM04, RM13, RM14) ----------------------

    async def _escalate(self, stop: Escalation) -> RunOutcome:
        """Hand the run to the external model, or end it as needs_external (RM04).

        External dispatch needs a configured external model, an effective
        mode other than local_only, and room in the entire-run limits. An
        operation with no known outcome blocks the handoff (RM14). The
        envelope is measured against the external context limit before
        anything is sent (RM13). A stop the external stage could not
        resolve either does not hand off (section 4.4).
        """
        external = self.o.external
        if external is None or self.mode is ExecutionMode.LOCAL_ONLY or not stop.hand_off:
            return self._needs_external(stop)
        room = self.meter.external_room()
        if room is not None:
            return self._needs_external(
                Escalation(
                    stop.reason_code, stop.detail, **{**stop.extra, "dispatch_blocked": room.budget}
                )
            )
        operations = self.o.store.tool_operations(self.run_id)
        unsettled = unsettled_operations(operations)
        if unsettled:
            blocker = unsettled[0]
            return self._needs_external(
                Escalation(
                    "tool_failure",
                    f"operation {blocker['operation_id']} has no known outcome; it blocks handoff",
                    operation_id=blocker["operation_id"],
                    tool_status=blocker["status"],
                )
            )
        # The external stage works from the recorded evidence the envelope
        # carries, not from what the local loop kept: an operation can be
        # recorded just before a local stop (RM05, RM13).
        self.results[:] = [ToolResult.from_json_object(op["result"]) for op in operations]

        skill = self.skill
        if skill is None and self.decision is not None and self.decision.skill_id is not None:
            skill = self._load_skill(self.decision.skill_id)
        if skill is not None:
            allowlist: tuple[str, ...] = skill.allowed_tools
            catalog: tuple[tuple[str, str], ...] = ()
        else:
            offered = [self.o.skills.get(name) for name in self.o.router.offered]
            allowlist = tuple(dict.fromkeys(t for s in offered for t in s.allowed_tools))
            catalog = tuple((s.name, s.description) for s in offered)
        available = self.o.tools.available_tools(
            skill_allowlist=allowlist, policy=self.o.policy, caller=self.o.caller
        )
        schemas = self.o.tools.tool_schemas(name for name in allowlist if name in available)
        if skill is not None and not schemas:
            # RM08: the external stage gets the same permitted tools, none,
            # so it could not collect the evidence the skill needs.
            return self._needs_external(
                Escalation("capability_missing", f"no permitted tools for skill {skill.name!r}")
            )
        language = self.language or request_language(self.messages)
        done = {op["tool_name"] for op in operations}
        pending: list[dict[str, Any]] = (
            [{"kind": "tool_call", "tool_name": s.name} for s in schemas if s.name not in done]
            if skill is not None
            else []
        )
        pending.append({"kind": "final_answer", "tool_name": None})
        envelope = build_handoff_envelope(
            run_id=self.run_id,
            messages=self.messages,
            skill=None
            if skill is None
            else {"name": skill.name, "version": skill.version, "content_hash": skill.content_hash},
            operations=operations,
            proposals=[*self.local_proposals.values(), *self.drafts],
            allowed_tools=[s.name for s in schemas],
            pending_work=pending,
            reason_code=stop.reason_code,
            remaining_budgets=self.meter.remaining_budgets(),
            language=language.value,
        )
        conversation = list(
            external_messages(
                envelope,
                skill_body=None if skill is None else skill.body,
                catalog=catalog,
                answer_instruction=ANSWER_LANGUAGE[language],
            )
        )
        try:
            capabilities = await external.adapter.capabilities()
            if schemas:
                capabilities.require("tool_calling")
            size = await measure_request(external.adapter, capabilities, conversation, schemas)
        except (UnsupportedCapabilityError, TokenCountError) as exc:
            return self._fail(
                EXTERNAL_FAILED,
                detail=f"the external request could not be prepared: {exc}",
                reason_code=stop.reason_code,
            )
        if not size.fits:
            return self._fail_context(size)

        self.move(
            RunState.EXTERNAL_RUNNING,
            event=(
                EventType.HANDOFF_STARTED,
                GATEWAY_ACTOR,
                {
                    "reason_code": stop.reason_code,
                    "model": external.model_id,
                    "skill": None if envelope.skill is None else dict(envelope.skill),
                    "operation_ids": list(envelope.operation_ids),
                    "allowed_tools": list(envelope.allowed_tools),
                    "request": size.to_json_object(),
                },
            ),
        )
        # RM14: from here only the external actor dispatches or answers.
        self.authority.hand_off_to_external()
        self.meter.start_external()
        try:
            answer = await self._external(conversation, schemas, available, skill, capabilities)
        except ContextLimitExceeded as exceeded:
            return self._fail_context(exceeded.size)
        except ExternalFailure as failure:
            return self._fail(EXTERNAL_FAILED, detail=failure.detail, **failure.extra)
        except Escalation as stopped:
            # A stop inside the external stage has no further escalation (RM14).
            return self._fail(
                EXTERNAL_FAILED,
                detail=stopped.detail,
                reason_code=stopped.reason_code,
                **stopped.extra,
            )
        except BudgetExceededError as exceeded:
            return self._fail(EXTERNAL_FAILED, detail=str(exceeded), budget=exceeded.budget)
        self.move(
            RunState.COMPLETED,
            event=(
                EventType.RUN_COMPLETED,
                GATEWAY_ACTOR,
                {"final_answer_model": external.model_id, "answer": answer, **self._summary()},
            ),
        )
        return RunOutcome(self.run_id, RunState.COMPLETED, answer=answer, decision=self.decision)

    def _fail_context(self, size: RequestSize) -> RunOutcome:
        """Evidence over the external context budget fails explicitly (RM13)."""
        return self._fail(
            CONTEXT_LIMIT_EXCEEDED,
            reason_code="context_limit",
            detail=(
                f"the external request needs {size.size} ({size.method}) "
                f"and the external context limit is {size.limit}"
            ),
            request=size.to_json_object(),
        )

    async def _external(
        self,
        conversation: list[Message],
        schemas: tuple[ToolSchema, ...],
        available: frozenset[str],
        skill: Skill | None,
        capabilities: AdapterCapabilities,
    ) -> str:
        """The external model's bounded loop: propose tools, then answer (RM13, RM14).

        The caller measured the first request. Tool proposals take the
        local path's checks and the same executor. A proposal that
        repeats a completed operation is answered from the record and
        runs no handler (RM10). The answer is buffered, checked against
        the skill's completion predicate, and published once (RM20).
        """
        predicate = None if skill is None else COMPLETION_PREDICATES[skill.completion_predicate]
        while True:
            _, generation = await self._generate(
                "external", conversation, schemas, EXTERNAL_OPTIONS
            )
            if generation.calls:
                call, admitted, rejection, proposal_id = self._tool_turn(
                    generation, available, skill
                )
                if rejection is not None:
                    conversation += _tool_exchange(call, {"error": rejection})
                else:
                    assert admitted is not None
                    result = await self._run_admitted(admitted, proposal_id, EXTERNAL_ACTOR)
                    conversation += _tool_exchange(call, result.to_json_object())
            elif generation.text.strip() and (
                predicate is None or predicate(generation.text, self.results)
            ):
                return self._publish(generation, EXTERNAL_ACTOR)
            else:
                conversation += _correction_exchange(generation.text)
            # Tool results accumulate, so the request is measured again (RM13).
            try:
                size = await measure_request(
                    self.active_model.adapter, capabilities, conversation, schemas
                )
            except (UnsupportedCapabilityError, TokenCountError) as exc:
                # Section 11: an external stage that cannot finish is external_failed.
                raise ExternalFailure(f"the external request could not be measured: {exc}") from exc
            if not size.fits:
                raise ContextLimitExceeded(size)

    # -- routing ---------------------------------------------------------------

    async def _route(self) -> Routing:
        router = self.o.router
        if self.mode is ExecutionMode.EXTERNAL_ONLY:
            decision = router.execution_mode_decision(self.messages)
            self.emit(
                EventType.ROUTE_SELECTED,
                GATEWAY_ACTOR,
                {"decision": decision.to_json_object(), "mechanism": "execution_mode"},
            )
            return Routing(decision)

        adapter = self.o.model.adapter
        try:
            capabilities = await adapter.capabilities()
            capabilities.require("tool_calling", "tokenizer_available")
        except UnsupportedCapabilityError as exc:
            raise Escalation("capability_missing", str(exc)) from exc

        routing_messages = router.routing_messages(self.messages)
        functions = router.functions()
        try:
            input_tokens = await adapter.count_tokens(routing_messages, functions)
        except (TokenCountError, UnsupportedCapabilityError) as exc:
            raise Escalation(
                "capability_missing", f"router input could not be counted: {exc}"
            ) from exc

        if not router.within_input_cap(input_tokens):
            decision = router.bypass_decision(self.messages, self.mode, language=self.language)
            self.emit(
                EventType.ROUTE_SELECTED,
                GATEWAY_ACTOR,
                {
                    "decision": decision.to_json_object(),
                    "mechanism": "bypass",
                    "input_tokens": input_tokens,
                    "input_cap": self.o.budgets.routing_input_tokens,
                },
            )
            return Routing(decision)

        call_id, generation = await self._generate(
            "routing", routing_messages, functions, router.options()
        )
        try:
            routing = router.decide(
                generation.calls, self.messages, mode=self.mode, language=self.language
            )
        except RouteDecisionError as exc:
            raise Escalation("invalid_output", f"routing output rejected: {exc}") from exc
        self.emit(
            EventType.ROUTE_SELECTED,
            self.actor,
            {
                "decision": routing.decision.to_json_object(),
                "mechanism": ROUTING_MECHANISM,
                "model": self.o.model.model_id,
                "call_id": call_id,
                "input_tokens": input_tokens,
                "model_language": generation.calls[0].arguments.get("language"),
            },
        )
        return routing

    # -- generations -------------------------------------------------------------

    async def _generate(
        self,
        purpose: str,
        messages: Sequence[Message],
        tool_schemas: Sequence[ToolSchema],
        options: GenerationOptions = LOCAL_OPTIONS,
    ) -> tuple[str, _Generation]:
        """Charge, run, and trace one model call (RM03, RM16); the only budget path.

        The model is the one that holds dispatch authority (RM14).
        Returns the call ID and what the model produced.
        """
        model = self.active_model
        self.meter.charge_generation(purpose)
        self._calls += 1
        call_id = f"{self.run_id}_gen{self._calls}_{purpose}"
        request = GenerationRequest(
            generation_id=call_id,
            messages=tuple(messages),
            tool_schemas=tuple(tool_schemas),
            options=options,
        )
        model_id = model.model_id
        actor = model.actor
        call = {"call_id": call_id, "model": model_id, "actor": actor, "purpose": purpose}
        self.model_calls.append(call)
        self.emit(
            EventType.MODEL_STARTED,
            actor,
            {**call, "tools_offered": [t.name for t in request.tool_schemas]},
        )
        started = self.o.clock.monotonic()
        generation = _Generation()
        usage: Usage | None = None
        finish: str | None = None
        failure: GenerationFailed | None = None
        exhausted: BudgetExceededError | None = None
        adapter = model.adapter
        try:
            async with asyncio.timeout(max(self.meter.remaining_seconds(), 0.001)):
                async for event in adapter.generate(request):
                    if isinstance(event, ToolCall):
                        generation.calls.append(event)
                    elif isinstance(event, TextDelta):
                        generation.pieces.append(event.text)
                    elif isinstance(event, Usage):
                        usage = event
                    elif isinstance(event, Completed):
                        finish = event.finish_reason
                    elif isinstance(event, GenerationFailed):
                        failure = event
        except TimeoutError:
            # The trace names the limit that bounded this generation, local
            # or external, not always local time (RM03, RM16).
            exhausted = self.meter.time_exhausted()
            failure = GenerationFailed(code="time_budget", message=f"{exhausted.budget} spent")
            await adapter.cancel(call_id)
        except BaseException as exc:
            failure = GenerationFailed(
                code="cancelled" if isinstance(exc, asyncio.CancelledError) else "interrupted",
                message="model generation interrupted",
            )
            raise
        finally:
            redactor = self.o.redactor
            self.emit(
                EventType.MODEL_FINISHED,
                actor,
                {
                    "call_id": call_id,
                    "model": model_id,
                    "purpose": purpose,
                    "duration_ms": round((self.o.clock.monotonic() - started) * 1000),
                    "finish_reason": finish,
                    "tool_calls": len(generation.calls),
                    "proposed_calls": [
                        {"tool_name": c.tool_name, "arguments": redactor.redact(dict(c.arguments))}
                        for c in generation.calls
                    ],
                    "text": self.scrub(generation.text) or None,
                    "usage": None
                    if usage is None
                    else {
                        "input_tokens": usage.input_tokens,
                        "output_tokens": usage.output_tokens,
                        "cached_tokens": usage.cached_tokens,
                    },
                    "error": None
                    if failure is None
                    else {"code": failure.code, "message": self.scrub(failure.message)},
                },
            )
        if exhausted is not None:
            raise exhausted
        if failure is not None:
            if model is not self.o.model:
                raise ExternalFailure(
                    f"external generation failed: {failure.code}", generation_error=failure.code
                )
            raise Escalation("capability_missing", f"local generation failed: {failure.code}")
        self.meter.check_time()
        return call_id, generation

    # -- local skill loop ----------------------------------------------------------

    def _load_skill(self, name: str) -> Skill:
        """Load the skill at the version run.accepted recorded (RM06, RM07)."""
        pinned = next(s for s in self.offered["skills_offered"] if s["name"] == name)
        return self.o.skills.load_skill(
            name,
            emit=lambda event_type, payload: self.emit(event_type, GATEWAY_ACTOR, payload),
            version=pinned["version"],
            content_hash=pinned["content_hash"],
        )

    async def _local(self, decision: RouteDecision) -> str:
        assert decision.skill_id is not None
        skill = self.skill = self._load_skill(decision.skill_id)
        available = self.o.tools.available_tools(
            skill_allowlist=skill.allowed_tools, policy=self.o.policy, caller=self.o.caller
        )
        schemas = self.o.tools.tool_schemas(n for n in skill.allowed_tools if n in available)
        if not schemas:
            # RM08: nothing the skill allows is available to this caller.
            raise Escalation("capability_missing", f"no permitted tools for skill {skill.name!r}")
        predicate = COMPLETION_PREDICATES[skill.completion_predicate]
        tool_names = " or ".join(schema.name for schema in schemas)
        conversation: list[Message] = [
            Message(
                role=Role.SYSTEM,
                content=f"{skill.description}\n{SKILL_INSTRUCTION.format(tools=tool_names)}",
            ),
            *self.messages,
            Message(role=Role.SYSTEM, content=f"{BODY_HEADER}\n{skill.body}"),
        ]
        results = self.results
        purpose = "skill"

        while True:
            _, generation = await self._generate(purpose, conversation, schemas)
            purpose = "skill"

            if generation.calls:
                call, admitted, rejection, proposal_id = self._tool_turn(
                    generation, available, skill
                )
                if rejection is not None and rejection["code"] == CONFIRMATION_REQUIRED:
                    # RM11: the confirmation flow (#19) is not wired here, so
                    # a mutating tool is never executed by this loop. The
                    # external stage rejects it the same way, so no handoff.
                    raise Escalation("capability_missing", rejection["message"], hand_off=False)
                if rejection is not None:
                    purpose = self._repair_or_stop(rejection)
                    conversation += _tool_exchange(call, {"error": rejection})
                    continue
                assert admitted is not None
                result = await self._run_admitted(admitted, proposal_id, LOCAL_ACTOR)
                if result.status is not ToolStatus.SUCCEEDED:
                    raise Escalation(
                        "tool_failure",
                        f"tool {result.tool_name!r} ended {result.status.value}",
                        operation_id=result.operation_id,
                        tool_status=result.status.value,
                    )
                conversation += _tool_exchange(call, result.to_json_object())
                continue

            # A text answer is a finalize proposal (section 7.3).
            if generation.text.strip() and predicate(generation.text, results):
                return await self._final(conversation, predicate, results)
            self._keep_draft(generation.text)
            problem = {
                "code": "completion_predicate_unmet" if generation.text.strip() else "empty_output",
                "message": (
                    f"the answer does not meet the skill's completion criteria "
                    f"({skill.completion_predicate})"
                ),
            }
            purpose = self._repair_or_stop(problem)
            conversation += _correction_exchange(generation.text)

    def _keep_draft(self, text: str) -> None:
        """Keep a rejected local answer so a handoff can label it a proposal (RM13)."""
        if text.strip():
            self.drafts.append(
                {"kind": "answer_draft", "label": "proposal", "text": self.scrub(text)}
            )

    def _repair_or_stop(self, problem: Mapping[str, str]) -> str:
        """The next purpose, repair, if a repair is left; otherwise stop (RM04)."""
        if self.meter.may_repair():
            return "repair"
        raise Escalation("invalid_output", f"unrepaired output: {problem['code']}")

    def _tool_turn(
        self, generation: _Generation, available: frozenset[str], skill: Skill | None
    ) -> tuple[ToolCall, ValidatedCall | None, dict[str, str] | None, str]:
        """Admit a tool turn and record every proposed call (RM05, RM09, RM11, RM16).

        The same checks hold for the local and the external actor. Every
        proposed call is recorded, so a turn with several calls keeps all
        of them in the trace (RM16, RM23). Returns the first call, the
        admitted call or the rejection, and the first call's proposal ID.
        """
        call = generation.calls[0]
        admitted, rejection = self._admit(generation.calls, available, self.results)
        proposal_id = self._propose(call, skill, rejection)
        for extra in generation.calls[1:]:
            self._propose(extra, skill, rejection)
        return call, admitted, rejection, proposal_id

    async def _run_admitted(self, call: ValidatedCall, proposal_id: str, actor: str) -> ToolResult:
        """Dispatch an admitted call and keep its result as run evidence."""
        result = await self._dispatch(call, proposal_id, actor)
        self.results.append(result)
        return result

    def _admit(
        self,
        calls: Sequence[ToolCall],
        available: frozenset[str],
        results: Sequence[ToolResult],
    ) -> tuple[ValidatedCall | None, dict[str, str] | None]:
        """The validated call, or the rejection (RM05, RM09, RM11)."""
        if len(calls) != 1:
            return None, {
                "code": "multiple_tool_calls",
                "message": "propose one tool call at a time",
            }
        call = calls[0]
        try:
            admitted = self.o.executor.validate(
                self.run_id, call.tool_name, call.arguments, available=available
            )
        except ToolRejectedError as exc:
            return None, {"code": exc.code, "message": str(exc)}
        # RM05: an identifier must come from a user turn or a tool result,
        # never from the model alone (section 5).
        provenance = identifiers.provenance_text(self.messages, results)
        unsupported = identifiers.unsupported_identifiers(
            admitted.arguments, admitted.tool.declaration.input_schema, provenance
        )
        if unsupported:
            return None, {
                "code": "unsupported_identifier",
                "message": (
                    f"{', '.join(unsupported)} does not come from the request or tool results"
                ),
            }
        if admitted.tool.side_effect is SideEffect.MUTATING:
            return None, {
                "code": CONFIRMATION_REQUIRED,
                "message": (
                    f"tool {call.tool_name!r} is mutating and needs a confirmation, "
                    "which this run cannot request yet"
                ),
            }
        return admitted, None

    def _propose(
        self, call: ToolCall, skill: Skill | None, rejection: dict[str, str] | None
    ) -> str:
        proposal_id = f"proposal_{uuid.uuid4().hex[:16]}"
        arguments = self.o.redactor.redact(dict(call.arguments))
        self.emit(
            EventType.TOOL_PROPOSED,
            self.actor,
            {
                "proposal_id": proposal_id,
                "call_id": call.call_id,
                "tool_name": call.tool_name,
                "arguments": arguments,
                "skill_id": None if skill is None else skill.name,
                "skill_version": None if skill is None else skill.version,
                "run_state_version": self.record.version,
                "model": self.active_model.model_id,
                "admissible": rejection is None,
                "rejection": rejection,
            },
        )
        if self.authority.holder == LOCAL_ACTOR:
            # Until it produces an operation, it is only a proposal (RM13).
            self.local_proposals[proposal_id] = {
                "kind": "tool_call",
                "label": "proposal",
                "proposal_id": proposal_id,
                "tool_name": call.tool_name,
                "arguments": arguments,
                "rejection_code": None if rejection is None else rejection["code"],
            }
        return proposal_id

    async def _dispatch(self, call: ValidatedCall, proposal_id: str, actor: str) -> ToolResult:
        """Execute (or replay) the call, with the one retry section 11 allows.

        Only the actor that holds dispatch authority may dispatch (RM14).
        """
        self.authority.require(actor)
        execution = await self._execute(call, proposal_id, attempt=1)
        if self.o.executor.may_retry(call, execution.result, attempt=1):
            execution = await self._execute(call, proposal_id, attempt=2)
        return execution.result

    async def _execute(self, call: ValidatedCall, proposal_id: str, *, attempt: int) -> Execution:
        common = {"proposal_id": proposal_id, "tool_name": call.tool_name, "attempt": attempt}
        model_id = self.active_model.model_id

        def before_run(operation_id: str) -> None:
            # Only a real execution is charged (RM03); a replay never is.
            self.meter.charge_tool_call()
            self.emit(
                EventType.TOOL_STARTED,
                GATEWAY_ACTOR,
                {**common, "operation_id": operation_id, "model": model_id},
            )

        def on_finished(execution: Execution) -> None:
            self.secrets |= execution.secrets
            # The proposal has a recorded outcome now, so a handoff lists
            # it as a completed operation, never also as a proposal (RM13).
            self.local_proposals.pop(proposal_id, None)
            result = execution.result
            self.emit(
                EventType.TOOL_FINISHED,
                GATEWAY_ACTOR,
                {
                    **common,
                    "operation_id": result.operation_id,
                    "model": model_id,
                    "status": result.status.value,
                    "replayed": execution.replayed,
                    "result": result.to_json_object(),
                },
            )

        try:
            execution = await self.o.executor.execute(
                call,
                proposal_id=proposal_id,
                model=model_id,
                attempt=attempt,
                before_run=before_run,
                time_limit=self.meter.remaining_seconds,
                on_finished=on_finished,
            )
        except OperationInFlightError as exc:
            # RM14: an operation in flight blocks continuation that could repeat it.
            raise Escalation("tool_failure", str(exc), operation_id=exc.operation_id) from exc
        # A tool bounded by the remaining budget ends the run on budget.
        # Timer resolution can leave the trace clock just before its deadline.
        if execution.time_limit_exhausted:
            raise self.meter.time_exhausted()
        self.meter.check_time()
        return execution

    async def _final(
        self,
        conversation: Sequence[Message],
        predicate: Predicate,
        results: Sequence[ToolResult],
    ) -> str:
        """The final generation with tools disabled (section 7.3)."""
        assert self.language is not None
        language_line = ANSWER_LANGUAGE[self.language]
        _, generation = await self._generate(
            "final",
            (
                *conversation,
                Message(role=Role.SYSTEM, content=f"{FINAL_INSTRUCTION}\n{language_line}"),
            ),
            (),
        )
        if generation.calls:
            raise Escalation("invalid_output", "the final generation proposed a tool call")
        answer = generation.text
        if not answer.strip() or not predicate(answer, results):
            self._keep_draft(answer)
            raise Escalation(
                "invalid_output", "the final answer does not meet the completion criteria"
            )
        return self._publish(generation, LOCAL_ACTOR)

    def _publish(self, generation: _Generation, role: str) -> str:
        """Publish a validated answer as answer.delta events, once (RM14, RM20).

        role is the dispatch role of the model, local or external; the
        events are labeled with the locality its adapter declares.
        """
        self.authority.claim_final_answer(role)
        model = self.active_model
        answer = generation.text
        published = self.scrub(answer)
        # A secret may span pieces; then the scrubbed answer is one delta.
        pieces = generation.pieces if published == answer else [published]
        drafts: list[EventDraft] = [
            (EventType.ANSWER_DELTA, model.actor, {"text": piece, "model": model.model_id})
            for piece in pieces
            if piece
        ]
        self.o.store.append_events(self.run_id, drafts)
        return published


def _correction_exchange(text: str) -> list[Message]:
    """A rejected answer and the gateway's correction, for the next generation."""
    return [
        Message(role=Role.ASSISTANT, content=text or "(no answer)"),
        Message(role=Role.USER, content=CORRECTION),
    ]


def _tool_exchange(call: ToolCall, content: Mapping[str, Any]) -> list[Message]:
    """The proposed call and its outcome, as data for the next generation (RM12)."""
    return [
        Message(role=Role.ASSISTANT, tool_calls=(call,)),
        Message(
            role=Role.TOOL,
            tool_call_id=call.call_id,
            content=json.dumps(dict(content), ensure_ascii=False),
        ),
    ]


__all__ = [
    "ConfiguredModel",
    "Escalation",
    "LocalOrchestrator",
    "ResumeInputError",
    "RunOutcome",
]
