"""llama.cpp server adapter (RM18, spec section 6).

Talks to a separate llama-server process over its OpenAI-compatible
HTTP API on loopback. Model-specific request shaping and stream
parsing stay inside this module; the rest of the gateway only sees
normalized events.

Non-thinking mode for Qwen3 is configured through the pinned runtime's
chat-template settings (``chat_template_kwargs.enable_thinking``), not
through prompt strings, and is verified by the live smoke test.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

import httpx

from relaymind.adapters.base import (
    AdapterCapabilities,
    AdapterEvent,
    AdapterHealth,
    Completed,
    GenerationFailed,
    GenerationRequest,
    Message,
    ModelAdapter,
    Role,
    TextDelta,
    ToolCall,
    Usage,
)

# Spec section 6: non-thinking is the default; an operator may enable
# thinking mode through the same chat-template settings.
NON_THINKING: Mapping[str, object] = MappingProxyType({"enable_thinking": False})


def _encode_message(message: Message) -> dict[str, object]:
    """Encode one normalized message in OpenAI chat format.

    Tool turns carry their references: a tool message names the call it
    answers, an assistant message carries the calls it proposed. The
    developer role maps to system: standard llama.cpp / Qwen chat
    templates drop roles they do not know.
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


def build_chat_request(
    request: GenerationRequest,
    *,
    chat_template_kwargs: Mapping[str, object] | None,
) -> dict[str, object]:
    """Map a normalized request onto a llama-server chat completion body."""
    body: dict[str, object] = {
        "stream": True,
        # Streaming responses carry no usage unless explicitly asked.
        "stream_options": {"include_usage": True},
        "messages": [_encode_message(message) for message in request.messages],
    }
    if request.tool_schemas:
        body["tools"] = [
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
            for schema in request.tool_schemas
        ]
    options = request.options
    if options.max_output_tokens is not None:
        body["max_tokens"] = options.max_output_tokens
    if options.temperature is not None:
        body["temperature"] = options.temperature
    if options.top_p is not None:
        body["top_p"] = options.top_p
    if chat_template_kwargs:
        body["chat_template_kwargs"] = dict(chat_template_kwargs)
    return body


@dataclass
class _PartialToolCall:
    call_id: str = ""
    tool_name: str = ""
    arguments_json: str = ""


@dataclass
class StreamAccumulator:
    """Turn streamed chat-completion chunks into normalized events.

    Tool-call argument fragments are buffered; a ToolCall event exists
    only after the stream finishes and the full argument JSON parses
    (RM09: partial streamed argument JSON must never execute).
    """

    _tool_calls: dict[int, _PartialToolCall] = field(default_factory=dict)
    # True once the stream outcome is settled (finish, failure, or
    # provider error chunk); the end-of-stream flush then adds nothing.
    _closed: bool = False

    def feed(self, chunk: Mapping[str, object]) -> list[AdapterEvent]:
        error = chunk.get("error")
        if error is not None:
            # llama-server streams runtime errors (context exhausted,
            # busy slots) as an error chunk without choices or usage.
            self._closed = True
            self._tool_calls.clear()
            message = error.get("message") if isinstance(error, Mapping) else None
            return [GenerationFailed(code="provider_error", message=str(message or error))]
        events: list[AdapterEvent] = []
        usage = chunk.get("usage")
        if isinstance(usage, Mapping):
            events.append(
                Usage(
                    input_tokens=int(usage.get("prompt_tokens") or 0),
                    output_tokens=int(usage.get("completion_tokens") or 0),
                )
            )
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            content = delta.get("content")
            if content:
                events.append(TextDelta(text=content))
            for fragment in delta.get("tool_calls") or []:
                self._absorb_tool_fragment(fragment)
            finish_reason = choice.get("finish_reason")
            if finish_reason:
                events.extend(self._finish(finish_reason))
        return events

    def flush(self) -> list[AdapterEvent]:
        """Settle a stream that ended without an explicit finish chunk.

        [DONE] or EOF can arrive before any finish_reason; buffered tool
        calls and the Completed event must still come out.
        """
        if self._closed:
            return []
        return self._finish("end_of_stream")

    def _absorb_tool_fragment(self, fragment: Mapping[str, object]) -> None:
        partial = self._tool_calls.setdefault(int(fragment.get("index") or 0), _PartialToolCall())
        if fragment.get("id"):
            partial.call_id = str(fragment["id"])
        function = fragment.get("function") or {}
        if function.get("name"):
            # Names can be streamed in fragments, like arguments.
            partial.tool_name += str(function["name"])
        partial.arguments_json += str(function.get("arguments") or "")

    def _finish(self, finish_reason: str) -> list[AdapterEvent]:
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
                    return [
                        GenerationFailed(
                            code="invalid_tool_arguments",
                            message=(
                                f"tool call {partial.call_id or index} for "
                                f"{partial.tool_name or 'unknown tool'}: {exc}"
                            ),
                        )
                    ]
            events.append(Completed(finish_reason=finish_reason))
            return events
        finally:
            self._tool_calls.clear()
            self._closed = True


@dataclass(frozen=True)
class LlamaCppConfig:
    """Operator configuration for one llama-server endpoint."""

    base_url: str
    context_limit: int
    # Non-thinking by default (spec section 6); an operator overrides
    # this to enable thinking mode.
    chat_template_kwargs: Mapping[str, object] | None = NON_THINKING
    request_timeout_seconds: float = 30.0


class LlamaCppAdapter(ModelAdapter):
    """ModelAdapter over a llama-server OpenAI-compatible endpoint."""

    def __init__(
        self,
        config: LlamaCppConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._config = config
        self._client = httpx.AsyncClient(
            base_url=config.base_url,
            timeout=config.request_timeout_seconds,
            transport=transport,
        )
        # One awaited event per running generation; cancel() sets it to
        # interrupt the stream even while it is idle between chunks.
        self._cancel_events: dict[str, asyncio.Event] = {}

    async def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            text_streaming=True,
            tool_calling=True,
            structured_routing_output=True,
            context_limit=self._config.context_limit,
            reasoning_controls=("enable_thinking",),
            usage_reporting=True,
            # llama-server has /tokenize, but the adapter exposes no
            # tokenizer interface yet; advertising one would be
            # dishonest (RM16). Flip when that interface lands.
            tokenizer_available=False,
        )

    async def generate(self, request: GenerationRequest) -> AsyncIterator[AdapterEvent]:
        body = build_chat_request(request, chat_template_kwargs=self._config.chat_template_kwargs)
        accumulator = StreamAccumulator()
        cancel_event = self._cancel_events.setdefault(request.generation_id, asyncio.Event())
        try:
            async with self._client.stream("POST", "/v1/chat/completions", json=body) as response:
                if response.status_code != 200:
                    detail = (await response.aread()).decode(errors="replace")
                    yield GenerationFailed(
                        code="provider_error",
                        message=f"llama-server returned {response.status_code}: {detail[:200]}",
                    )
                    return
                lines = aiter(response.aiter_lines())
                while True:
                    line = await self._next_line_or_cancel(lines, cancel_event)
                    if line is None:
                        # A cancelled generation ends without Completed;
                        # a naturally ended stream settles buffered
                        # state even when no finish chunk arrived.
                        if not cancel_event.is_set():
                            for event in accumulator.flush():
                                yield event
                        return
                    chunk = _parse_sse_line(line)
                    if chunk is None:
                        continue
                    for event in accumulator.feed(chunk):
                        yield event
        except httpx.TimeoutException as exc:
            yield GenerationFailed(code="provider_timeout", message=str(exc))
        except httpx.HTTPError as exc:
            yield GenerationFailed(code="provider_unreachable", message=str(exc))
        finally:
            self._cancel_events.pop(request.generation_id, None)

    @staticmethod
    async def _next_line_or_cancel(lines, cancel_event: asyncio.Event) -> str | None:
        """The next stream line, or None on end of stream or cancellation."""
        next_line = asyncio.ensure_future(anext(lines))
        cancelled = asyncio.ensure_future(cancel_event.wait())
        try:
            done, _pending = await asyncio.wait(
                {next_line, cancelled}, return_when=asyncio.FIRST_COMPLETED
            )
            if cancel_event.is_set():
                return None
            try:
                return next_line.result()
            except StopAsyncIteration:
                return None
        finally:
            for task in (next_line, cancelled):
                task.cancel()
            await asyncio.gather(next_line, cancelled, return_exceptions=True)

    async def cancel(self, generation_id: str) -> None:
        event = self._cancel_events.get(generation_id)
        if event is not None:
            event.set()

    async def health(self) -> AdapterHealth:
        try:
            response = await self._client.get("/health")
        except httpx.HTTPError as exc:
            return AdapterHealth(available=False, warm=False, detail=str(exc))
        if response.status_code == 200:
            return AdapterHealth(available=True, warm=True, detail="ready")
        if response.status_code == 503:
            return AdapterHealth(available=True, warm=False, detail="loading model")
        return AdapterHealth(
            available=False, warm=False, detail=f"unexpected status {response.status_code}"
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "LlamaCppAdapter":
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        await self.aclose()


def _parse_sse_line(line: str) -> Mapping[str, object] | None:
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
