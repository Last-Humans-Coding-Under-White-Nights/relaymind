"""The client-owned run: functions the caller executes (RM08, RM19, AC10, spec 0.16 section 7.2).

A request of the ``relaymind-client-tools`` alias is its own run. The
model is offered the functions the caller supplied, narrowed by
tool_choice, and nothing else: the gateway installs no tool for this
alias and executes none. A generation is admissible when it is non-blank
text, or when its first tool call names an offered function and its
arguments are valid against that function's parameters schema (the
same strict check as RM09). Anything else is invalid output: one
format-repair generation, then an escalation (RM04).

The reply is buffered and complete before the run ends, so an external
fallback can only happen while nothing has been exposed, and one reply
never mixes local and external output (spec section 7.2). A reply with
tool calls completes the run: the caller executes the call and sends
the result in its next request, which is another run. The trace has
tool.proposed for the exposed call and never tool.started or
tool.finished.

There is no routing generation, because the offered skill catalog is
empty (section 8). The gateway records its own RouteDecision: local, or
external in external_only.
"""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from relaymind.adapters import (
    GenerationOptions,
    Message,
    ModelAdapter,
    Role,
    TokenCountError,
    ToolCall,
    ToolSchema,
    UnsupportedCapabilityError,
)
from relaymind.orchestrator.budgets import BudgetExceededError
from relaymind.orchestrator.handoff import RequestSize, measure_request
from relaymind.orchestrator.local_loop import (
    EXTERNAL_FAILED,
    EXTERNAL_OPTIONS,
    GATEWAY_ACTOR,
    LOCAL_OPTIONS,
    ContextLimitExceeded,
    Escalation,
    ExternalFailure,
    LocalOrchestrator,
    RunOutcome,
    _Generation,
    _Run,
    _tool_exchange,
)
from relaymind.orchestrator.route_decision import ExecutionMode
from relaymind.orchestrator.run_state import RunState
from relaymind.schema_util import first_schema_error
from relaymind.store.trace_event import EventType
from relaymind.tools.registry import parameters_validator

__all__ = ["ToolOffer", "run_client_request"]

CHOICES = ("auto", "none", "required", "function")
# One repair generation locally (RM03); the external stage gets one retry too.
EXTERNAL_ATTEMPTS = 2
CLIENT_MECHANISM = "client_owned"


@dataclass(frozen=True)
class ToolOffer:
    """The functions a caller supplied and how the model may use them (RM19).

    choice is auto, none, required, or function; function names the one
    function that must be called. none offers no function and a named
    choice offers only that one. The schemas come from a validated
    ChatRequest: their names are unique, a named function is one of
    them, and their parameters passed check_parameters_schema.
    """

    schemas: tuple[ToolSchema, ...] = ()
    choice: str = "auto"
    function: str | None = None
    offered: tuple[ToolSchema, ...] = field(init=False)
    _validators: Mapping[str, Any] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.choice not in CHOICES:
            raise ValueError(f"tool choice must be one of: {', '.join(CHOICES)}")
        if (self.choice == "function") != (self.function is not None):
            raise ValueError("a function name goes with the function choice, and only with it")
        offered = {
            "none": (),
            "function": tuple(s for s in self.schemas if s.name == self.function),
        }.get(self.choice, tuple(self.schemas))
        object.__setattr__(self, "schemas", tuple(self.schemas))
        object.__setattr__(self, "offered", offered)
        validators = {s.name: parameters_validator(dict(s.input_schema)) for s in offered}
        object.__setattr__(self, "_validators", validators)

    @property
    def requires_call(self) -> bool:
        return self.choice in ("required", "function")

    def problem(self, call: ToolCall) -> str | None:
        """Why a proposed call may not be returned, or None when it may (RM09)."""
        validator = self._validators.get(call.tool_name)
        if validator is None:
            return f"{call.tool_name!r} is not a function you may call now"
        try:
            error = first_schema_error(validator, dict(call.arguments))
        except Exception:
            return f"the arguments of {call.tool_name!r} could not be checked"
        if error is not None:
            where = "/".join(str(part) for part in error.absolute_path)
            return f"the arguments of {call.tool_name!r} are invalid at {where or 'the top'}"
        return None


@dataclass(frozen=True)
class _Reply:
    """An admitted generation: its text, or the one call to return."""

    generation: _Generation
    calls: tuple[ToolCall, ...] = ()


async def run_client_request(
    orchestrator: LocalOrchestrator,
    messages: Sequence[Message],
    offer: ToolOffer,
    *,
    execution_mode: str | None = None,
    model_alias: str | None = None,
    final_options: GenerationOptions | None = None,
) -> RunOutcome:
    """Run one client-owned request to its reply (spec 0.16 section 7.2).

    Raises SlotUnavailableError (429) when another run holds the
    execution slot; no run is created then. The run never waits: it
    ends completed with an answer or with the calls the caller executes,
    or failed.
    """
    run = _ClientRun(
        orchestrator, tuple(messages), offer, execution_mode, model_alias, final_options
    )
    return await run.execute_client()


class _ClientRun(_Run):
    def __init__(
        self,
        owner: LocalOrchestrator,
        messages: tuple[Message, ...],
        offer: ToolOffer,
        requested_mode: str | None,
        model_alias: str | None,
        final_options: GenerationOptions | None,
    ) -> None:
        super().__init__(owner, messages, requested_mode, model_alias, final_options)
        self.offer = offer
        # The offered catalog is empty for this alias (section 8).
        self.offered = owner.skills.offered_snapshot(())
        # Call IDs of the conversation; the reply must not reuse one.
        self._taken = {c.call_id for m in messages for c in m.tool_calls}

    async def execute_client(self) -> RunOutcome:
        self.open()
        return await self._protected(self.drive_client)

    # -- the run -----------------------------------------------------------------

    async def drive_client(self) -> RunOutcome:
        try:
            self._decide()
            if self.mode is ExecutionMode.EXTERNAL_ONLY:
                raise Escalation("execution_mode", "the execution mode sends the request external")
            self.move(RunState.LOCAL_RUNNING)
            reply = await self._local_stage()
        except Escalation as stop:
            return await self._escalate_client(stop)
        except BudgetExceededError as exceeded:
            return await self._escalate_client(
                Escalation(exceeded.reason_code, str(exceeded), budget=exceeded.budget)
            )
        return self._complete(reply)

    def _decide(self) -> None:
        """Record the gateway's own decision; skill_id is null for this alias (section 8)."""
        decision = self.o.router.client_decision(self.messages, self.mode)
        self.decision = decision
        self.language = decision.language
        mechanism = (
            "execution_mode" if self.mode is ExecutionMode.EXTERNAL_ONLY else CLIENT_MECHANISM
        )
        self.emit(
            EventType.ROUTE_SELECTED,
            GATEWAY_ACTOR,
            {"decision": decision.to_json_object(), "mechanism": mechanism},
        )

    # -- the local stage -------------------------------------------------------------

    async def _measure(
        self, adapter: ModelAdapter, conversation: Sequence[Message] | None = None
    ) -> RequestSize:
        """Measure a request against a model's context limit (RM13).

        conversation defaults to the request as sent. Offered functions
        need tool calling; raises UnsupportedCapabilityError or
        TokenCountError when the model cannot take the request.
        """
        schemas = self.offer.offered
        capabilities = await adapter.capabilities()
        if schemas:
            capabilities.require("tool_calling")
        messages = self.messages if conversation is None else conversation
        return await measure_request(adapter, capabilities, messages, schemas)

    async def _local_stage(self) -> _Reply:
        await self._check_local_size(self.messages)
        return await self._converse(("skill", "repair"), LOCAL_OPTIONS, self._check_local_size)

    async def _check_local_size(self, conversation: Sequence[Message]) -> None:
        """Escalate when the local model cannot take the conversation (RM04, RM13)."""
        try:
            size = await self._measure(self.o.model.adapter, conversation)
        except (UnsupportedCapabilityError, TokenCountError) as exc:
            raise self._local_unavailable(
                f"the local model cannot take the request: {exc}"
            ) from exc
        if not size.fits:
            raise Escalation(
                "context_limit",
                f"the request needs {size.size} ({size.method}); the local limit is {size.limit}",
            )

    async def _check_external_size(self, conversation: Sequence[Message]) -> None:
        """A retry grows the external request, so it is measured again (RM13)."""
        external = self.o.external
        assert external is not None
        try:
            size = await self._measure(external.adapter, conversation)
        except (UnsupportedCapabilityError, TokenCountError) as exc:
            # Section 11: an external stage that cannot finish is external_failed.
            raise ExternalFailure(f"the external request could not be measured: {exc}") from exc
        if not size.fits:
            raise ContextLimitExceeded(size)

    async def _converse(
        self,
        purposes: Sequence[str],
        defaults: GenerationOptions,
        check_size: Callable[[Sequence[Message]], Awaitable[None]],
    ) -> _Reply:
        """Generate until a reply is admissible, within the purposes given (RM03).

        Every generation is the response, a text or a call, so the
        caller's token budget applies to each (spec 7.1, 7.2). A repair
        needs room in the local limits; without it the stop is the
        unrepaired output (RM04). The grown conversation is measured
        before each further generation with check_size (RM13).
        """
        conversation = list(self.messages)
        schemas = self.offer.offered
        options = self._answer_options(defaults)
        problem = "no reply"
        for index, purpose in enumerate(purposes):
            if index:
                if purpose == "repair" and not self.meter.may_repair():
                    break
                await check_size(conversation)
            _, generation = await self._generate(purpose, conversation, schemas, options)
            reply, problem = self._admit_reply(generation)
            if reply is not None:
                return reply
            correction = f"{problem}. Reply again so that the request is met."
            if generation.calls:
                # The repair sees the call it proposed, with the reason as its result.
                conversation += _tool_exchange(generation.calls[0], {"error": correction})
                continue
            draft = generation.text.strip() or "(no usable reply)"
            conversation += [
                Message(role=Role.ASSISTANT, content=draft),
                Message(role=Role.USER, content=f"[gateway] {correction}"),
            ]
        raise Escalation("invalid_output", problem)

    def _admit_reply(self, generation: _Generation) -> tuple[_Reply | None, str]:
        """The admissible reply of a generation, or why there is none (RM09, RM19)."""
        offer = self.offer
        if generation.calls:
            call = generation.calls[0]
            if offer.choice == "none":
                return None, "a function call is not allowed when tool_choice is none"
            problem = offer.problem(call)
            if problem is not None:
                return None, problem
            return _Reply(generation, (self._unique(call),)), ""
        if offer.requires_call:
            return None, "a function call is required"
        if not generation.text.strip():
            return None, "the reply was blank"
        return _Reply(generation), ""

    def _unique(self, call: ToolCall) -> ToolCall:
        """The call with the model's ID, unless that ID was used already (RM19).

        A blank ID never gets here: ToolCall rejects one, and the adapter
        gives a call without a usable ID a synthesized one, which this
        check then keeps unique in the conversation (spec section 7.2).
        """
        if call.call_id not in self._taken:
            self._taken.add(call.call_id)
            return call
        number = 1
        while f"call_{self.run_id}_{number}" in self._taken:
            number += 1
        fresh = f"call_{self.run_id}_{number}"
        self._taken.add(fresh)
        return ToolCall(call_id=fresh, tool_name=call.tool_name, arguments=call.arguments)

    # -- the external stage -------------------------------------------------------------

    async def _escalate_client(self, stop: Escalation) -> RunOutcome:
        """Fall back to the external model before anything is exposed, or stop (RM04)."""
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
        # The gateway's decision is recorded before the local stage, so a
        # degraded handoff needs no further route.selected (section 11).
        try:
            size = await self._measure(external.adapter)
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
                    "skill": None,
                    "operation_ids": [],
                    "allowed_tools": [s.name for s in self.offer.offered],
                    "request": size.to_json_object(),
                    **(
                        {"degraded_mode": True, "local_failure": self.scrub(stop.detail)}
                        if stop.degraded
                        else {}
                    ),
                },
            ),
        )
        # RM14: from here only the external actor answers.
        self.authority.hand_off_to_external()
        self.meter.start_external()
        try:
            reply = await self._converse(
                ("external",) * EXTERNAL_ATTEMPTS, EXTERNAL_OPTIONS, self._check_external_size
            )
        except ContextLimitExceeded as exceeded:
            return self._fail_context(exceeded.size)
        except ExternalFailure as failure:
            return self._fail(
                EXTERNAL_FAILED,
                detail=failure.detail,
                reason_code=stop.reason_code,
                **failure.extra,
            )
        except Escalation as stopped:
            # A stop inside the external stage has no further escalation (RM14);
            # run.failed carries the reason it stopped on (section 11).
            return self._fail(
                EXTERNAL_FAILED,
                detail=stopped.detail,
                reason_code=stopped.reason_code,
                **stopped.extra,
            )
        except BudgetExceededError as exceeded:
            return self._fail(
                EXTERNAL_FAILED,
                detail=str(exceeded),
                reason_code=stop.reason_code,
                budget=exceeded.budget,
            )
        return self._complete(reply)

    # -- the end ------------------------------------------------------------------------

    def _complete(self, reply: _Reply) -> RunOutcome:
        """End the run with the reply: an answer, or the calls the caller executes."""
        model = self.active_model
        answer: str | None = None
        if reply.calls:
            for call in reply.calls:
                self.emit(
                    EventType.TOOL_PROPOSED,
                    model.actor,
                    {
                        "proposal_id": call.call_id,
                        "tool_name": call.tool_name,
                        "arguments": self.redact_arguments(call.arguments),
                        "model": model.model_id,
                        "owner": "client",
                    },
                )
        else:
            answer = self._publish(reply.generation, self.authority.holder)
        self.move(
            RunState.COMPLETED,
            event=(
                EventType.RUN_COMPLETED,
                GATEWAY_ACTOR,
                {
                    "final_answer_model": model.model_id,
                    "answer": answer,
                    "client_tool_calls": [
                        {"call_id": c.call_id, "tool_name": c.tool_name} for c in reply.calls
                    ],
                    **self._summary(),
                },
            ),
        )
        return RunOutcome(
            self.run_id,
            RunState.COMPLETED,
            answer=answer,
            decision=self.decision,
            tool_calls=reply.calls,
        )
