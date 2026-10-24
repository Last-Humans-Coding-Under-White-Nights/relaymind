"""The chat backends and the compatible response shapes (RM19, RM20, spec 0.14 section 7.1).

A ``ChatBackend`` runs one validated ``ChatRequest`` and returns a
``ChatResult``. ``ManagedChat`` is the backend of the ``relaymind``
alias: it hands the conversation to the orchestrator, which owns
routing, tools, budgets, and the final answer (section 7.2), and
turns where the run ended into a chat result or an ``ApiError``. The
client-owned alias has its own, ``ClientToolsChat``: it returns the
caller's own function calls and executes nothing (RM08). The app only
needs the same two-line protocol.

The functions at the end build the JSON bodies. The stream is framing
plus answer content only (RM20): a role chunk, one content chunk for
each ``answer.delta`` of the run, a finish chunk, and ``[DONE]``. No
progress text is added to it.

A backend may also return a ``LiveAnswer``, text forwarded while the
provider generates it. The stream starts only with the first answer
text, so a provider that fails before it is a normal error response.
If that provider fails after the first chunk, the stream sends one
fixed error event and closes the connection with no finish chunk and
no ``[DONE]``. A request without stream collects the whole live
answer first (spec 0.15 section 7.3).
"""

import asyncio
import json
import logging
import time
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from relaymind.adapters import ToolCall, ToolSchema
from relaymind.api.chat_request import CLIENT_TOOLS, MANAGED, ChatRequest
from relaymind.api.errors import ApiError, error_from_exception, error_from_outcome
from relaymind.orchestrator.client_loop import ToolOffer, accept_client_request, run_client_request
from relaymind.orchestrator.local_loop import AcceptedRun, LocalOrchestrator, RunOutcome
from relaymind.orchestrator.route_decision import ExecutionMode
from relaymind.orchestrator.run_state import RunState
from relaymind.store import EventType, RunStore

__all__ = [
    "AcceptedChat",
    "AcceptingBackend",
    "ChatBackend",
    "ChatResult",
    "ClientToolsChat",
    "LiveAnswer",
    "ManagedChat",
    "StreamAborted",
    "answer_pieces",
    "collect_live",
    "completion_body",
    "early_stream",
    "open_stream",
    "stream_chunks",
]

STOP = "stop"
TOOL_CALLS = "tool_calls"
STREAM_FAILED_CODE = "external_failed"
STREAM_FAILED_MESSAGE = "the answer provider failed; no answer was completed"

logger = logging.getLogger(__name__)


class StreamAborted(Exception):
    """Raised after the error event of a failed stream, to close the connection.

    The status line is gone by then, so the only honest signal left is
    to end the response without a finish chunk and without [DONE]
    (RM20, section 11).
    """


class StreamFailure(Exception):
    """A recorded stream-only terminal error, with no invented HTTP status."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.code = "predicate_unmet"
        self.message = (
            "the answer did not meet the skill's completion criteria; no answer was completed"
        )

    def body(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message, "run_id": self.run_id}}


@dataclass(frozen=True)
class LiveAnswer:
    """Answer text that a backend forwards while the provider still generates it.

    pieces yields the answer text in the order received. fail records
    the run as failed (run.failed); the gateway calls it once, when the
    provider fails (RM20, spec 0.15 section 7.3). When the client goes
    away first, the gateway closes pieces (aclose), which is the
    cancellation request of a chat disconnect (section 7.4); fail is
    not called then. Managed streams read persisted verified deltas and retain
    their recorded terminal error instead of invoking this generic callback.
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

    def __post_init__(self) -> None:
        # A stream has one terminal path; a live answer must never be dropped silently.
        if self.live is not None and self.tool_calls:
            raise ValueError("a chat result has either tool_calls or a live answer, not both")

    @property
    def stream_pieces(self) -> tuple[str, ...]:
        """The content chunks of a stream: the pieces, or else the whole content."""
        return self.pieces or ((self.content,) if self.content else ())


class ChatBackend(Protocol):
    """Runs a validated request under an effective execution mode.

    A backend that can also accept a run before it works on it has an
    ``accept`` method returning an ``AcceptedChat`` (early response
    headers, RM32); a backend without one is always answered when its
    run has finished.
    """

    async def complete(self, request: ChatRequest, mode: ExecutionMode) -> ChatResult: ...


@runtime_checkable
class AcceptingBackend(Protocol):
    """A backend that can accept a run before it works on it (RM32)."""

    def accept(self, request: ChatRequest, mode: ExecutionMode) -> "AcceptedChat": ...


@dataclass(frozen=True)
class AcceptedChat:
    """A chat run that is accepted and not yet driven (RM32, spec 0.32 section 7.3).

    The run holds the execution slot and has its run ID. ``start``
    drives it in a task the response owns, so a disconnect maps to the
    run through ``LocalOrchestrator.request_cancel``; the task yields the
    ChatResult or raises the ApiError the request would have got.
    """

    run: AcceptedRun
    to_result: Callable[[RunOutcome], ChatResult]
    progress: Callable[[asyncio.Task[ChatResult]], ChatResult] | None = None

    @property
    def run_id(self) -> str:
        return self.run.run_id

    def start(self) -> "asyncio.Task[ChatResult]":
        return self.run.start(self.to_result)


class _RunChat:
    """Turns where a run ended into a chat result or an ``ApiError``."""

    def __init__(self, orchestrator: LocalOrchestrator) -> None:
        self.orchestrator = orchestrator

    def _accepted(self, run: AcceptedRun, request: ChatRequest) -> AcceptedChat:
        return AcceptedChat(run, lambda outcome: self._result(outcome, stream=request.stream))

    def _result(self, outcome: RunOutcome, *, stream: bool) -> ChatResult:
        run_id = outcome.run_id
        match outcome.state:
            case RunState.COMPLETED if outcome.tool_calls:
                # Client-owned calls: the caller executes them, never the gateway (RM08).
                return ChatResult(run_id, None, TOOL_CALLS, tool_calls=outcome.tool_calls)
            case RunState.COMPLETED if outcome.answer is not None:
                store = self.orchestrator.store
                pieces = answer_pieces(store, run_id, outcome.answer) if stream else ()
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
                if stream and outcome.error_code == "predicate_unmet":
                    raise StreamFailure(run_id)
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


def answer_pieces(store: RunStore, run_id: str, answer: str) -> tuple[str, ...]:
    """The run's answer.delta texts, in order, when they are the answer; else ()."""
    events = store.events(run_id, event_type=EventType.ANSWER_DELTA)
    pieces = tuple(e.payload["text"] for e in events)
    return pieces if "".join(pieces) == answer else ()


class ManagedChat(_RunChat):
    """The ``relaymind`` alias: managed skills and tools through the orchestrator."""

    async def complete(self, request: ChatRequest, mode: ExecutionMode) -> ChatResult:
        if request.stream and self.orchestrator.performance.answer_stream == "live":
            accepted = self.accept(request, mode)
            task = accepted.start()
            assert accepted.progress is not None
            return accepted.progress(task)
        outcome = await self.orchestrator.run(
            request.messages,
            execution_mode=mode.value,
            model_alias=MANAGED,
            final_options=request.final_options,
            idempotency=request.idempotency,
            answer_stream_effective="buffered",
        )
        return self._result(outcome, stream=request.stream)

    def accept(self, request: ChatRequest, mode: ExecutionMode) -> AcceptedChat:
        effective = self.orchestrator.performance.answer_stream if request.stream else "buffered"
        run = self.orchestrator.accept(
            request.messages,
            execution_mode=mode.value,
            model_alias=MANAGED,
            final_options=request.final_options,
            idempotency=request.idempotency,
            answer_stream_effective=effective,
        )
        return AcceptedChat(
            run,
            lambda outcome: self._result(outcome, stream=request.stream),
            (lambda task: self._progress(run.run_id, task)) if effective == "live" else None,
        )

    def _progress(self, run_id: str, task: asyncio.Task[ChatResult]) -> ChatResult:
        async def pieces() -> AsyncIterator[str]:
            cursor = 0
            delivered = False
            try:
                while True:
                    # Snapshot termination BEFORE reading: a final append can never
                    # slip between the last drain and a terminal check (RM17/RM32).
                    ended = task.done()
                    events = self.orchestrator.store.events(
                        run_id, after_seq=cursor, event_type=EventType.ANSWER_DELTA
                    )
                    for event in events:
                        cursor = event.seq
                        delivered = True
                        yield event.payload["text"]
                    if ended:
                        if task.cancelled():
                            raise ApiError("run_terminal", "the run was cancelled", run_id=run_id)
                        try:
                            result = task.result()
                        except (ApiError, StreamFailure):
                            raise
                        except Exception as exc:
                            raise error_from_exception(exc, run_id=run_id) from None
                        if not delivered:
                            for piece in result.stream_pieces:
                                yield piece
                        return
                    # Store-backed cursor: bounded memory and no native consumer needed.
                    await asyncio.wait({task}, timeout=0.01)
            finally:
                if not task.done() and not task.cancelling():
                    self.orchestrator.request_cancel(task)

                # The response owns the configured native cancellation wait. Closing
                # this cursor must not introduce a second, hard-coded wait or cancel.
                def retrieve(ended: asyncio.Task[ChatResult]) -> None:
                    if not ended.cancelled():
                        ended.exception()

                if task.done():
                    retrieve(task)
                else:
                    task.add_done_callback(retrieve)

        return ChatResult(run_id, None, live=LiveAnswer(pieces(), lambda: None))


class ClientToolsChat(_RunChat):
    """The ``relaymind-client-tools`` alias: functions the caller executes (RM08, AC10).

    The orchestrator offers the model only the supplied functions and
    returns its call to the caller. The gateway executes nothing
    (spec 0.16 section 7.2).
    """

    def _offer(self, request: ChatRequest) -> ToolOffer:
        choice = request.tool_choice
        return ToolOffer(
            tuple(ToolSchema(t.name, t.description or "", t.parameters) for t in request.tools),
            choice="auto" if choice is None else choice.mode,
            function=None if choice is None else choice.name,
        )

    def accept(self, request: ChatRequest, mode: ExecutionMode) -> AcceptedChat:
        """Take the slot and write run.accepted; the work starts with ``AcceptedChat.start``."""
        run = accept_client_request(
            self.orchestrator,
            request.messages,
            self._offer(request),
            execution_mode=mode.value,
            model_alias=CLIENT_TOOLS,
            final_options=request.final_options,
            idempotency=request.idempotency,
        )
        return self._accepted(run, request)

    async def complete(self, request: ChatRequest, mode: ExecutionMode) -> ChatResult:
        offer = self._offer(request)
        outcome = await run_client_request(
            self.orchestrator,
            request.messages,
            offer,
            execution_mode=mode.value,
            model_alias=CLIENT_TOOLS,
            final_options=request.final_options,
            idempotency=request.idempotency,
        )
        return self._result(outcome, stream=request.stream)


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


def _arguments_text(arguments: Mapping[str, Any]) -> str:
    """function.arguments with sorted keys, as the store keeps them (spec 0.18 section 7.4).

    The store writes tool.proposed with sorted keys, so a replay of the
    stored call (``relaymind.api.idempotency``) sends the same text.
    """
    return json.dumps(dict(arguments), sort_keys=True)


def _tool_call_json(call: ToolCall) -> dict[str, Any]:
    return {
        "id": call.call_id,
        "type": "function",
        "function": {"name": call.tool_name, "arguments": _arguments_text(call.arguments)},
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


async def collect_live(result: ChatResult) -> ChatResult:
    """The whole answer of a live result, for a request without stream.

    A provider failure is a normal error response, external_failed
    with the run ID; no partial text is returned as an answer (RM20,
    spec 0.15 section 7.3). A result that is not live is returned as is.
    """
    live = result.live
    if live is None:
        return result
    try:
        text = "".join([piece async for piece in live.pieces])
    except Exception as exc:
        raise _failed(result.run_id, live, exc) from None
    finally:
        await _close(live.pieces)
    return ChatResult(result.run_id, text, finish_reason=result.finish_reason)


async def open_stream(alias: str, result: ChatResult, created: int) -> AsyncIterator[str]:
    """The SSE lines of a result, started before the status line is sent.

    The first line is produced here. For a live result that means the
    first answer text has arrived, so a provider that fails before it
    raises ApiError external_failed, a normal error response with its
    status (section 11). Closing the returned stream closes the
    provider stream.
    """
    lines = stream_chunks(alias, result, created)
    first = await anext(lines)
    return _resumed(first, lines)


async def _resumed(first: str, lines: AsyncGenerator[str]) -> AsyncIterator[str]:
    try:
        yield first
        async for line in lines:
            yield line
    finally:
        await lines.aclose()


async def stream_chunks(alias: str, result: ChatResult, created: int) -> AsyncGenerator[str]:
    """The SSE lines of a result: framing and answer content only (RM20).

    A finished result is replayed. A live result is forwarded piece by
    piece, and the role chunk waits for its first non-empty piece: a
    provider failure before it raises ApiError external_failed before
    any line is produced. If the provider fails later, the run is
    recorded as failed, one error event is sent, and StreamAborted
    closes the connection with no finish chunk and no [DONE] (spec 0.15
    section 7.3). However the stream ends, the provider stream is
    closed.
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

    live = result.live
    if result.tool_calls:
        yield chunk({"role": "assistant", "content": None})
        calls = [
            {"index": index, **_tool_call_json(call)}
            for index, call in enumerate(result.tool_calls)
        ]
        yield chunk({"tool_calls": calls})
    elif live is not None:
        try:
            try:
                first = await _first_text(live.pieces)
            except Exception as exc:
                raise _failed(result.run_id, live, exc) from None
            yield chunk({"role": "assistant", "content": ""})
            if first is not None:
                yield chunk({"content": first})
                try:
                    async for piece in live.pieces:
                        if piece:
                            yield chunk({"content": piece})
                except Exception as exc:
                    error = _failed(result.run_id, live, exc)
                    yield f"data: {json.dumps(error.body())}\n\n"
                    raise StreamAborted from None
        finally:
            await _close(live.pieces)
    else:
        yield chunk({"role": "assistant", "content": ""})
        for piece in result.stream_pieces:
            yield chunk({"content": piece})
    yield chunk({}, result.finish_reason)
    yield "data: [DONE]\n\n"


KEEP_ALIVE = ": keep-alive\n\n"


async def early_stream(
    alias: str,
    run_id: str,
    task: "asyncio.Task[ChatResult]",
    *,
    heartbeat_seconds: float,
    on_internal_error: Callable[[Exception], None],
) -> AsyncGenerator[str]:
    """The SSE lines of a streamed request whose headers went out at acceptance (RM32).

    Spec 0.32 section 7.3. It sends an SSE comment at once and then every
    heartbeat_seconds until the run's task is done: a comment is not a
    chunk, so no content reaches the client before the answer (RM20). A
    run that produced a result then sends exactly the chunks of a
    buffered response. A run that ended without an answer, or whose task
    failed, sends the one mid-stream error event of section 7.3 (its
    code, a fixed message, the run ID, and the reason code when it has
    one) and StreamAborted closes the connection with no finish chunk
    and no [DONE]; the status line cannot change any more. The caller
    owns the task and cancels it when the response ends first.
    """
    yield KEEP_ALIVE
    while not task.done():
        await asyncio.wait({task}, timeout=heartbeat_seconds)
        if not task.done():
            yield KEEP_ALIVE
    try:
        if task.cancelled():
            raise ApiError("run_terminal", f"run {run_id} was cancelled", run_id=run_id)
        result = task.result()
    except Exception as exc:
        error = exc if isinstance(exc, StreamFailure) else error_from_exception(exc, run_id=run_id)
        if error.run_id is None:
            error.run_id = run_id
        if error.code == "internal_error":
            on_internal_error(exc)
        yield f"data: {json.dumps(error.body())}\n\n"
        raise StreamAborted from None
    async for line in stream_chunks(alias, result, int(time.time())):
        yield line


async def _first_text(pieces: AsyncIterator[str]) -> str | None:
    """The first non-empty piece, or None when the provider ends without text."""
    async for piece in pieces:
        if piece:
            return piece
    return None


async def _close(pieces: AsyncIterator[str]) -> None:
    """Close the provider stream; for an unfinished one this requests cancellation."""
    aclose = getattr(pieces, "aclose", None)
    if aclose is not None:
        await aclose()


def _failed(run_id: str, live: LiveAnswer, exc: Exception) -> ApiError | StreamFailure:
    """Record the failed run and return its fixed error (RM20, section 11).

    The message is fixed: provider text never reaches the client
    (RM12). The operator log names the failure type only.
    """
    if isinstance(exc, ApiError | StreamFailure):
        return exc
    logger.error("answer stream of %s failed: %s", run_id, type(exc).__name__)
    try:
        live.fail()
    except Exception as recorder:
        logger.error("could not record run.failed for %s: %s", run_id, type(recorder).__name__)
    return ApiError(STREAM_FAILED_CODE, STREAM_FAILED_MESSAGE, run_id=run_id)


async def live_with_heartbeats(
    lines: AsyncIterator[str], heartbeat_seconds: float
) -> AsyncGenerator[str]:
    """Comments while waiting for a verified chunk; one iterator owns the cursor."""
    pending = None
    try:
        yield KEEP_ALIVE
        while True:
            pending = asyncio.create_task(anext(lines))
            while not pending.done():
                await asyncio.wait({pending}, timeout=heartbeat_seconds)
                if not pending.done():
                    yield KEEP_ALIVE
            try:
                line = pending.result()
            except StopAsyncIteration:
                return
            except (ApiError, StreamFailure) as error:
                yield f"data: {json.dumps(error.body())}\n\n"
                raise StreamAborted from None
            yield line
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        await _close(lines)
