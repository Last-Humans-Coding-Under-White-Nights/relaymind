"""The chat backends and the compatible response shapes (RM19, RM20, spec 0.14 section 7.1).

A ``ChatBackend`` runs one validated ``ChatRequest`` and returns a
``ChatResult``. ``ManagedChat`` is the backend of the ``relaymind``
alias: it hands the conversation to the orchestrator, which owns
routing, tools, budgets, and the final answer (section 7.2), and
turns where the run ended into a chat result or an ``ApiError``. The
client-owned alias has its own backend (issue #24); the app only needs
the same two-line protocol.

The functions at the end build the JSON bodies. The stream is framing
plus answer content only (RM20): a role chunk, one content chunk for
each ``answer.delta`` of the run, a finish chunk, and ``[DONE]``. No
progress text is added to it.
"""

import json
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from relaymind.adapters import ToolCall
from relaymind.api.chat_request import MANAGED, ChatRequest
from relaymind.api.errors import ApiError, error_from_outcome
from relaymind.orchestrator.local_loop import LocalOrchestrator, RunOutcome
from relaymind.orchestrator.route_decision import ExecutionMode
from relaymind.orchestrator.run_state import RunState

__all__ = [
    "ChatBackend",
    "ChatResult",
    "ManagedChat",
    "completion_body",
    "stream_chunks",
]

STOP = "stop"
TOOL_CALLS = "tool_calls"


@dataclass(frozen=True)
class ChatResult:
    """What one chat request produced.

    content is the assistant text (None when the reply is tool calls).
    pieces are the answer deltas in the order the run published them;
    their join is content. tool_calls are client-owned calls the caller
    executes (RM08); the gateway never runs them.
    """

    run_id: str
    content: str | None
    finish_reason: str = STOP
    tool_calls: tuple[ToolCall, ...] = ()
    pieces: tuple[str, ...] = ()


class ChatBackend(Protocol):
    """Runs a validated request under an effective execution mode."""

    async def complete(self, request: ChatRequest, mode: ExecutionMode) -> ChatResult: ...


class ManagedChat:
    """The ``relaymind`` alias: managed skills and tools through the orchestrator."""

    def __init__(self, orchestrator: LocalOrchestrator) -> None:
        self.orchestrator = orchestrator

    async def complete(self, request: ChatRequest, mode: ExecutionMode) -> ChatResult:
        outcome = await self.orchestrator.run(
            request.messages,
            execution_mode=mode.value,
            model_alias=MANAGED,
            final_options=request.final_options,
        )
        return self._result(outcome)

    def _result(self, outcome: RunOutcome) -> ChatResult:
        run_id = outcome.run_id
        match outcome.state:
            case RunState.COMPLETED if outcome.answer is not None:
                return ChatResult(run_id, outcome.answer, pieces=self._pieces(outcome))
            case RunState.WAITING_INPUT if outcome.clarification is not None:
                # The question is the reply; the run waits for native resume or a new request.
                return ChatResult(run_id, outcome.clarification)
            case RunState.WAITING_CONFIRMATION if outcome.confirmation is not None:
                return ChatResult(run_id, _confirmation_notice(outcome))
            case RunState.CANCELLED:
                raise ApiError("run_terminal", f"run {run_id} was cancelled", run_id=run_id)
            case RunState.FAILED:
                error = error_from_outcome(outcome)
                if error is not None:
                    raise error
        # A state this endpoint cannot report must not look like an answer.
        raise ApiError("internal_error", "an internal error occurred", run_id=run_id)

    def _pieces(self, outcome: RunOutcome) -> tuple[str, ...]:
        """The run's answer.delta texts, in order, when they are the answer."""
        events = self.orchestrator.store.events(outcome.run_id)
        pieces = tuple(e.payload["text"] for e in events if e.event_type == "answer.delta")
        return pieces if "".join(pieces) == outcome.answer else (outcome.answer or "",)


def _confirmation_notice(outcome: RunOutcome) -> str:
    """Tell a chat caller that a confirmation is pending (section 7.4).

    The approval ID and the token are not in it: a chat client cannot
    approve with a free-form yes.
    """
    assert outcome.confirmation is not None
    return (
        f"Confirmation is pending for {outcome.confirmation.tool_name}. "
        "Nothing has changed yet. Approve or deny it with the native run API "
        f"(run {outcome.run_id}) or the demo UI; a chat reply cannot approve it."
    )


# -- response bodies --------------------------------------------------------------


def request_id(result: ChatResult) -> str:
    return f"chatcmpl-{result.run_id}"


def _tool_call_json(call: ToolCall) -> dict[str, Any]:
    return {
        "id": call.call_id,
        "type": "function",
        "function": {"name": call.tool_name, "arguments": json.dumps(dict(call.arguments))},
    }


def completion_body(alias: str, result: ChatResult, created: int) -> dict[str, Any]:
    """The non-streaming chat.completion body: one assistant choice, no usage."""
    message: dict[str, Any] = {"role": "assistant", "content": result.content}
    if result.tool_calls:
        message["tool_calls"] = [_tool_call_json(c) for c in result.tool_calls]
    return {
        "id": request_id(result),
        "object": "chat.completion",
        "created": created,
        "model": alias,
        "choices": [{"index": 0, "message": message, "finish_reason": result.finish_reason}],
    }


def stream_chunks(alias: str, result: ChatResult, created: int) -> Iterator[str]:
    """The SSE lines of a finished result: framing and answer content only (RM20)."""

    def chunk(delta: Mapping[str, Any], finish: str | None = None) -> str:
        body = {
            "id": request_id(result),
            "object": "chat.completion.chunk",
            "created": created,
            "model": alias,
            "choices": [{"index": 0, "delta": dict(delta), "finish_reason": finish}],
        }
        return f"data: {json.dumps(body)}\n\n"

    if result.tool_calls:
        yield chunk({"role": "assistant", "content": None})
        calls: Sequence[dict[str, Any]] = [
            {"index": index, **_tool_call_json(call)}
            for index, call in enumerate(result.tool_calls)
        ]
        yield chunk({"tool_calls": list(calls)})
    else:
        yield chunk({"role": "assistant", "content": ""})
        for piece in result.pieces or ((result.content,) if result.content else ()):
            yield chunk({"content": piece})
    yield chunk({}, result.finish_reason)
    yield "data: [DONE]\n\n"
