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

A backend may also return a ``LiveAnswer``, text forwarded while the
provider generates it. If that provider fails after the first chunk,
the stream sends one fixed error event and closes the connection with
no finish chunk and no ``[DONE]`` (spec 0.15 section 7.3).
"""

import json
import logging
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from relaymind.adapters import ToolCall
from relaymind.api.chat_request import MANAGED, ChatRequest
from relaymind.api.errors import ApiError, error_from_outcome
from relaymind.orchestrator.local_loop import LocalOrchestrator, RunOutcome
from relaymind.orchestrator.route_decision import ExecutionMode
from relaymind.orchestrator.run_state import RunState
from relaymind.store import EventType

__all__ = [
    "ChatBackend",
    "ChatResult",
    "LiveAnswer",
    "ManagedChat",
    "StreamAborted",
    "completion_body",
    "stream_chunks",
]

STOP = "stop"
TOOL_CALLS = "tool_calls"
STREAM_FAILED_CODE = "external_failed"
STREAM_FAILED_MESSAGE = "the answer stream failed and was closed; no answer was completed"

logger = logging.getLogger(__name__)


class StreamAborted(Exception):
    """Raised after the error event of a failed stream, to close the connection.

    The status line is gone by then, so the only honest signal left is
    to end the response without a finish chunk and without [DONE]
    (RM20, section 11).
    """


@dataclass(frozen=True)
class LiveAnswer:
    """Answer text that a backend forwards while the provider still generates it.

    pieces yields the answer text in the order received. fail records
    the run as failed (run.failed); the stream calls it once, when the
    provider fails after the first chunk was sent (RM20, spec 0.15
    section 7.3). The managed alias has no live answer: its run has
    finished before the first byte.
    """

    pieces: AsyncIterator[str]
    fail: Callable[[], None]


@dataclass(frozen=True)
class ChatResult:
    """What one chat request produced.

    content is the assistant text (None when the reply is tool calls).
    pieces are the answer deltas in the order the run published them,
    when they are known; their join is content. live, when set, is
    answer text still being generated; it replaces content and pieces
    in a stream. tool_calls are
    client-owned calls the caller executes (RM08); the gateway never
    runs them.
    """

    run_id: str
    content: str | None
    finish_reason: str = STOP
    tool_calls: tuple[ToolCall, ...] = ()
    pieces: tuple[str, ...] = ()
    live: LiveAnswer | None = None

    @property
    def stream_pieces(self) -> tuple[str, ...]:
        """The content chunks of a stream: the pieces, or else the whole content."""
        return self.pieces or ((self.content,) if self.content else ())


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
        return self._result(outcome, stream=request.stream)

    def _result(self, outcome: RunOutcome, *, stream: bool) -> ChatResult:
        run_id = outcome.run_id
        match outcome.state:
            case RunState.COMPLETED if outcome.answer is not None:
                pieces = self._pieces(outcome) if stream else ()
                return ChatResult(run_id, outcome.answer, pieces=pieces)
            case RunState.WAITING_INPUT if outcome.clarification is not None:
                # The question is the reply; the run waits for native resume or a new request.
                return ChatResult(run_id, outcome.clarification)
            case RunState.WAITING_CONFIRMATION if outcome.confirmation is not None:
                notice = _confirmation_notice(run_id, outcome.confirmation.tool_name)
                return ChatResult(run_id, notice)
            case RunState.CANCELLED:
                raise ApiError("run_terminal", f"run {run_id} was cancelled", run_id=run_id)
            case RunState.FAILED:
                error = error_from_outcome(outcome)
                if error is not None:
                    raise error
                # A run-state failure (section 11) is not a server error; the
                # chat caller, which has no run state view, learns that it ended.
                raise ApiError(
                    "run_terminal",
                    f"run {run_id} ended failed with {outcome.error_code}",
                    run_id=run_id,
                    reason_code=outcome.reason_code,
                )
        # A state this endpoint cannot report must not look like an answer.
        raise ApiError("internal_error", "an internal error occurred", run_id=run_id)

    def _pieces(self, outcome: RunOutcome) -> tuple[str, ...]:
        """The run's answer.delta texts, in order, when they are the answer; else ()."""
        events = self.orchestrator.store.events(outcome.run_id, event_type=EventType.ANSWER_DELTA)
        pieces = tuple(e.payload["text"] for e in events)
        return pieces if "".join(pieces) == outcome.answer else ()


def _confirmation_notice(run_id: str, tool_name: str) -> str:
    """Tell a chat caller that a confirmation is pending (section 7.4).

    The approval ID and the token are not in it: a chat client cannot
    approve with a free-form yes.
    """
    return (
        f"Confirmation is pending for {tool_name}. "
        "Nothing has changed yet. Approve or deny it with the native run API "
        f"(run {run_id}) or the demo UI; a chat reply cannot approve it."
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


async def stream_chunks(alias: str, result: ChatResult, created: int) -> AsyncIterator[str]:
    """The SSE lines of a result: framing and answer content only (RM20).

    A finished result is replayed. A live result is forwarded piece by
    piece; if its provider fails, the run is recorded as failed, one
    error event is sent, and StreamAborted closes the connection with
    no finish chunk and no [DONE] (spec 0.15 section 7.3).
    """

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
        calls = [
            {"index": index, **_tool_call_json(call)}
            for index, call in enumerate(result.tool_calls)
        ]
        yield chunk({"tool_calls": calls})
    else:
        yield chunk({"role": "assistant", "content": ""})
        if result.live is not None:
            try:
                async for piece in result.live.pieces:
                    if piece:
                        yield chunk({"content": piece})
            except Exception as exc:
                yield _failure_event(result.run_id, result.live, exc)
                raise StreamAborted from None
        else:
            for piece in result.stream_pieces:
                yield chunk({"content": piece})
    yield chunk({}, result.finish_reason)
    yield "data: [DONE]\n\n"


def _failure_event(run_id: str, live: LiveAnswer, exc: Exception) -> str:
    """Record the failed run and build the one error event of an aborted stream.

    The event text is fixed: provider text never reaches the client
    (RM12). The operator log names the failure type only.
    """
    logger.error("answer stream of %s failed: %s", run_id, type(exc).__name__)
    try:
        live.fail()
    except Exception as recorder:
        logger.error("could not record run.failed for %s: %s", run_id, type(recorder).__name__)
    error = {"code": STREAM_FAILED_CODE, "message": STREAM_FAILED_MESSAGE, "run_id": run_id}
    return f"data: {json.dumps({'error': error})}\n\n"
