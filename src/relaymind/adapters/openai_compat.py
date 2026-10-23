"""Shared OpenAI-compatible chat-completions wire protocol.

The local llama.cpp adapter and the external Chat Completions adapter
speak the same streamed chat-completions protocol. Message encoding,
SSE parsing, and stream accumulation live here; endpoint-specific
request shaping and lifecycle stay in each adapter.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field

import httpx

from relaymind.adapters.base import (
    AdapterEvent,
    Completed,
    FirstToken,
    GenerationFailed,
    GenerationRequest,
    Message,
    Role,
    TextDelta,
    ToolCall,
    ToolSchema,
    Usage,
)


def encode_message(message: Message) -> dict[str, object]:
    """Encode one normalized message in OpenAI chat format.

    Tool turns carry their references: a tool message names the call it
    answers, an assistant message carries the calls it proposed. The
    developer role maps to system: standard chat templates drop roles
    they do not know.
    """
    role = "system" if message.role is Role.DEVELOPER else message.role.value
    encoded: dict[str, object] = {"role": role, "content": message.content}
    if message.tool_call_id is not None:
        encoded["tool_call_id"] = message.tool_call_id
    if message.tool_calls:
        encoded["tool_calls"] = [
            {
                "id": call.call_id,
                "type": "function",
                "function": {
                    "name": call.tool_name,
                    "arguments": json.dumps(dict(call.arguments)),
                },
            }
            for call in message.tool_calls
        ]
    return encoded


def encode_tools(tool_schemas: Sequence[ToolSchema]) -> list[dict[str, object]]:
    """Encode tool schemas as OpenAI function tools."""
    return [
        {
            "type": "function",
            "function": {
                "name": schema.name,
                "description": schema.description,
                # input_schema is a MappingProxyType; the wire body
                # must be plain-JSON serializable.
                "parameters": dict(schema.input_schema),
            },
        }
        for schema in tool_schemas
    ]


def encode_prompt(
    messages: Sequence[Message], tool_schemas: Sequence[ToolSchema]
) -> dict[str, object]:
    """The prompt fields of a chat body: messages, and tools when offered."""
    prompt: dict[str, object] = {"messages": [encode_message(message) for message in messages]}
    if tool_schemas:
        prompt["tools"] = encode_tools(tool_schemas)
    return prompt


def streamed_chat_body(prompt: Mapping[str, object]) -> dict[str, object]:
    """A streamed chat-completion body around the given prompt fields."""
    return {
        **prompt,
        # Last, so no prompt field can switch streaming off. Streaming
        # responses carry no usage unless explicitly asked.
        "stream": True,
        "stream_options": {"include_usage": True},
    }


def base_chat_body(request: GenerationRequest) -> dict[str, object]:
    """The chat-completion body every compatible endpoint shares."""
    return streamed_chat_body(encode_prompt(request.messages, request.tool_schemas))


def _count(value: object) -> int | None:
    """A reported token count, or None when the source sent none (RM16).

    Only a non-negative integer is a count. A boolean, a float, text, or
    a negative number is not a measurement, so it is treated as absent
    instead of being coerced into one.
    """
    if type(value) is int and value >= 0:
        return value
    return None


def _detail(container: object, key: str, name: str) -> int | None:
    """The count usage[key][name], or None when either level is missing."""
    details = container.get(key) if isinstance(container, Mapping) else None
    return _count(details.get(name)) if isinstance(details, Mapping) else None


@dataclass
class _PartialToolCall:
    call_id: str = ""
    tool_name: str = ""
    arguments_json: str = ""


@dataclass
class StreamAccumulator:
    """Turn streamed chat-completion chunks into normalized events.

    Tool-call argument fragments are buffered; a ToolCall event exists
    only after the finish chunk arrives and the full argument JSON
    parses (RM09: partial streamed argument JSON must never execute).
    Completed is the terminal event: it is deferred to flush() at the
    end of the stream, because with include_usage the usage chunk
    arrives after the finish chunk and must still be emitted first.

    Counts (spec 0.31 section 6). The cached prompt tokens come from
    usage.prompt_tokens_details.cached_tokens, and the reasoning tokens
    from usage.completion_tokens_details.reasoning_tokens. llama-server
    also reports the reused prompt tokens as timings.cache_n, on the
    final chunk or on the usage chunk. When both are present the usage
    object wins, because it is the provider's own accounting and the
    standard field; timings.cache_n is the fallback for a runtime that
    sends no details. A count that is missing or not a non-negative
    integer stays None (absent), never 0.

    The first token (RM25). take_first_token() is True once, after the
    first chunk with non-empty answer text, reasoning text, or a
    tool-call fragment. The pump turns it into the FirstToken marker.
    """

    _tool_calls: dict[int, _PartialToolCall] = field(default_factory=dict)
    # The finish reason seen on the wire; Completed waits for flush().
    _finish_reason: str | None = None
    # True once a terminal event went out (failure or flush); nothing
    # may be emitted after it.
    _terminal: bool = False
    # llama-server's timings.cache_n, kept for the usage chunk that may follow.
    _cache_n: int | None = None
    _first_token_seen: bool = False
    _first_token_pending: bool = False

    def take_first_token(self) -> bool:
        """True exactly once, after the first generated fragment arrived."""
        pending, self._first_token_pending = self._first_token_pending, False
        return pending

    def _note_token(self) -> None:
        if not self._first_token_seen:
            self._first_token_seen = True
            self._first_token_pending = True

    def feed(self, chunk: Mapping[str, object]) -> list[AdapterEvent]:
        if self._terminal:
            return []
        error = chunk.get("error")
        if error is not None:
            # Providers stream runtime errors (context exhausted, busy
            # slots) as an error chunk without choices or usage.
            self._terminal = True
            self._tool_calls.clear()
            message = error.get("message") if isinstance(error, Mapping) else None
            return [
                GenerationFailed(
                    code="provider_error", message=str(message or error), received=True
                )
            ]
        events: list[AdapterEvent] = []
        timings = chunk.get("timings")
        if isinstance(timings, Mapping) and (cache_n := _count(timings.get("cache_n"))) is not None:
            self._cache_n = cache_n
        usage = chunk.get("usage")
        if isinstance(usage, Mapping):
            cached = _detail(usage, "prompt_tokens_details", "cached_tokens")
            events.append(
                Usage(
                    input_tokens=int(usage.get("prompt_tokens") or 0),
                    output_tokens=int(usage.get("completion_tokens") or 0),
                    cached_tokens=self._cache_n if cached is None else cached,
                    reasoning_tokens=_detail(
                        usage, "completion_tokens_details", "reasoning_tokens"
                    ),
                )
            )
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            if self._finish_reason is None:
                content = delta.get("content")
                if content:
                    self._note_token()
                    events.append(TextDelta(text=content))
                # llama-server names reasoning text reasoning_content; some
                # providers name it reasoning. It is never answer text.
                if delta.get("reasoning_content") or delta.get("reasoning"):
                    self._note_token()
                for fragment in delta.get("tool_calls") or []:
                    self._note_token()
                    self._absorb_tool_fragment(fragment)
                finish_reason = choice.get("finish_reason")
                if finish_reason:
                    self._finish_reason = str(finish_reason)
                    events.extend(self._settle_tool_calls())
            # Content or finish reasons after the finish chunk are
            # protocol junk and stay out of the answer.
        return events

    def flush(self) -> list[AdapterEvent]:
        """Settle the end of the stream ([DONE] or EOF).

        Emits any tool calls still buffered (a stream may end without a
        finish chunk) and then the terminal Completed, exactly once.
        """
        if self._terminal:
            return []
        self._terminal = True
        events = self._settle_tool_calls()
        if any(isinstance(event, GenerationFailed) for event in events):
            return events
        events.append(Completed(finish_reason=self._finish_reason or "end_of_stream"))
        return events

    def _absorb_tool_fragment(self, fragment: Mapping[str, object]) -> None:
        partial = self._tool_calls.setdefault(int(fragment.get("index") or 0), _PartialToolCall())
        # A blank id counts as omitted; the call gets a synthesized one.
        if str(fragment.get("id") or "").strip():
            partial.call_id = str(fragment["id"])
        function = fragment.get("function") or {}
        if function.get("name"):
            # Names can be streamed in fragments, like arguments.
            partial.tool_name += str(function["name"])
        partial.arguments_json += str(function.get("arguments") or "")

    def _settle_tool_calls(self) -> list[AdapterEvent]:
        try:
            events: list[AdapterEvent] = []
            for index in sorted(self._tool_calls):
                partial = self._tool_calls[index]
                try:
                    # A tool without parameters may stream no argument
                    # fragments at all; that is an empty object, not an
                    # error.
                    raw_arguments = partial.arguments_json.strip()
                    arguments = json.loads(raw_arguments) if raw_arguments else {}
                    if not isinstance(arguments, dict):
                        raise ValueError("arguments are not a JSON object")
                    events.append(
                        ToolCall(
                            # A provider may omit the id; synthesize one
                            # rather than emit an invalid call.
                            call_id=partial.call_id or f"call_{index}",
                            tool_name=partial.tool_name,
                            arguments=arguments,
                        )
                    )
                except ValueError as exc:
                    self._terminal = True
                    return [
                        GenerationFailed(
                            code="invalid_tool_arguments",
                            message=(
                                f"tool call {partial.call_id or index} for "
                                f"{partial.tool_name or 'unknown tool'}: {exc}"
                            ),
                            received=True,
                        )
                    ]
            return events
        finally:
            self._tool_calls.clear()


def parse_sse_line(line: str) -> Mapping[str, object] | None:
    """Decode one server-sent-events line into a chunk, if it is one."""
    line = line.strip()
    if not line.startswith("data:"):
        return None
    payload = line[len("data:") :].strip()
    if payload == "[DONE]":
        return None
    try:
        chunk = json.loads(payload)
    except ValueError:
        return None
    return chunk if isinstance(chunk, Mapping) else None


async def stream_chat_events(
    client: httpx.AsyncClient,
    path: str,
    body: Mapping[str, object],
    *,
    cancel_event: asyncio.Event,
    headers: Mapping[str, str] | None = None,
    provider_label: str = "provider",
    scrub: Callable[[str], str] | None = None,
) -> AsyncIterator[AdapterEvent]:
    """Drive one streamed chat completion into normalized events.

    The shared pump for every OpenAI-compatible endpoint, so its
    invariants live in exactly one place: a cancelled generation ends
    without Completed; [DONE] or EOF settles the stream through
    flush(), with Completed as the terminal event; timeouts and
    transport errors map to the stable provider_timeout and
    provider_unreachable codes. scrub, when given, runs over error
    response bodies before they are truncated, so a secret crossing the
    truncation boundary can never leak as an unmatchable fragment.
    """
    accumulator = StreamAccumulator()
    # One long-lived wait on the cancel event per generation; each
    # streamed line then costs a single anext future.
    cancel_wait = asyncio.ensure_future(cancel_event.wait())
    try:
        async with client.stream("POST", path, json=dict(body), headers=headers) as response:
            if response.status_code != 200:
                detail = (await response.aread()).decode(errors="replace")
                if scrub is not None:
                    detail = scrub(detail)
                yield GenerationFailed(
                    code="provider_error",
                    message=f"{provider_label} returned {response.status_code}: {detail[:200]}",
                    received=True,
                )
                return
            lines = aiter(response.aiter_lines())
            while True:
                line = await _next_line_or_cancel(lines, cancel_wait, cancel_event)
                if line is None:
                    # A cancelled generation ends without Completed.
                    if cancel_event.is_set():
                        return
                    break  # EOF without [DONE]: settle what arrived.
                if line.strip() == "data: [DONE]":
                    # The protocol's termination marker; never wait for
                    # EOF on a kept-open connection.
                    break
                chunk = parse_sse_line(line)
                if chunk is None:
                    continue
                events = accumulator.feed(chunk)
                if accumulator.take_first_token():
                    # Before the chunk's own events, so a consumer dates the
                    # token when it arrived, not when the call is complete.
                    yield FirstToken()
                for event in events:
                    yield event
            for event in accumulator.flush():
                yield event
    except httpx.TimeoutException as exc:
        yield GenerationFailed(code="provider_timeout", message=str(exc))
    except httpx.HTTPError as exc:
        yield GenerationFailed(code="provider_unreachable", message=str(exc))
    finally:
        cancel_wait.cancel()
        await asyncio.gather(cancel_wait, return_exceptions=True)


async def _next_line_or_cancel(
    lines,
    cancel_wait: "asyncio.Future[bool]",
    cancel_event: asyncio.Event,
) -> str | None:
    """The next stream line, or None on end of stream or cancellation."""
    next_line = asyncio.ensure_future(anext(lines))
    try:
        await asyncio.wait({next_line, cancel_wait}, return_when=asyncio.FIRST_COMPLETED)
        if cancel_event.is_set():
            return None
        try:
            return next_line.result()
        except StopAsyncIteration:
            return None
    finally:
        if not next_line.done():
            next_line.cancel()
            await asyncio.gather(next_line, return_exceptions=True)
