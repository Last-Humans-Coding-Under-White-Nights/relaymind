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

import httpx

from relaymind.adapters.base import (
    AdapterCapabilities,
    AdapterEvent,
    AdapterHealth,
    Completed,
    GenerationFailed,
    GenerationRequest,
    ModelAdapter,
    TextDelta,
    ToolCall,
    Usage,
)


def build_chat_request(
    request: GenerationRequest,
    *,
    chat_template_kwargs: Mapping[str, object] | None,
) -> dict[str, object]:
    """Map a normalized request onto a llama-server chat completion body."""
    body: dict[str, object] = {
        "stream": True,
        "messages": [
            {"role": message.role.value, "content": message.content} for message in request.messages
        ],
    }
    if request.tool_schemas:
        body["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": schema.name,
                    "description": schema.description,
                    "parameters": schema.input_schema,
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

    def feed(self, chunk: Mapping[str, object]) -> list[AdapterEvent]:
        events: list[AdapterEvent] = []
        usage = chunk.get("usage")
        if isinstance(usage, Mapping):
            events.append(
                Usage(
                    input_tokens=int(usage.get("prompt_tokens", 0)),
                    output_tokens=int(usage.get("completion_tokens", 0)),
                )
            )
        for choice in chunk.get("choices", []):
            delta = choice.get("delta", {})
            content = delta.get("content")
            if content:
                events.append(TextDelta(text=content))
            for fragment in delta.get("tool_calls", []):
                self._absorb_tool_fragment(fragment)
            finish_reason = choice.get("finish_reason")
            if finish_reason:
                events.extend(self._finish(finish_reason))
        return events

    def _absorb_tool_fragment(self, fragment: Mapping[str, object]) -> None:
        partial = self._tool_calls.setdefault(int(fragment.get("index", 0)), _PartialToolCall())
        if fragment.get("id"):
            partial.call_id = str(fragment["id"])
        function = fragment.get("function", {})
        if function.get("name"):
            partial.tool_name = str(function["name"])
        partial.arguments_json += str(function.get("arguments") or "")

    def _finish(self, finish_reason: str) -> list[AdapterEvent]:
        events: list[AdapterEvent] = []
        for index in sorted(self._tool_calls):
            partial = self._tool_calls[index]
            try:
                arguments = json.loads(partial.arguments_json)
            except ValueError:
                return [
                    GenerationFailed(
                        code="invalid_tool_arguments",
                        message=(
                            f"tool call {partial.call_id or index} for "
                            f"{partial.tool_name or 'unknown tool'} did not produce "
                            "parseable argument JSON"
                        ),
                    )
                ]
            if not isinstance(arguments, dict):
                return [
                    GenerationFailed(
                        code="invalid_tool_arguments",
                        message=f"tool call {partial.call_id or index} arguments are not an object",
                    )
                ]
            events.append(
                ToolCall(
                    call_id=partial.call_id,
                    tool_name=partial.tool_name,
                    arguments=arguments,
                )
            )
        self._tool_calls.clear()
        events.append(Completed(finish_reason=finish_reason))
        return events


@dataclass(frozen=True)
class LlamaCppConfig:
    """Operator configuration for one llama-server endpoint."""

    base_url: str
    context_limit: int
    chat_template_kwargs: Mapping[str, object] | None = None
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
        self._cancelled: dict[str, asyncio.Event] = {}

    async def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            text_streaming=True,
            tool_calling=True,
            structured_routing_output=True,
            context_limit=self._config.context_limit,
            reasoning_controls=("enable_thinking",),
            usage_reporting=True,
            tokenizer_available=True,
        )

    async def generate(self, request: GenerationRequest) -> AsyncIterator[AdapterEvent]:
        body = build_chat_request(request, chat_template_kwargs=self._config.chat_template_kwargs)
        cancelled = self._cancelled.setdefault(request.generation_id, asyncio.Event())
        accumulator = StreamAccumulator()
        try:
            async with self._client.stream("POST", "/v1/chat/completions", json=body) as response:
                if response.status_code != 200:
                    detail = (await response.aread()).decode(errors="replace")
                    yield GenerationFailed(
                        code="provider_error",
                        message=f"llama-server returned {response.status_code}: {detail[:200]}",
                    )
                    return
                async for line in response.aiter_lines():
                    if cancelled.is_set():
                        return
                    chunk = _parse_sse_line(line)
                    if chunk is None:
                        continue
                    for event in accumulator.feed(chunk):
                        yield event
        except httpx.HTTPError as exc:
            yield GenerationFailed(code="provider_unreachable", message=str(exc))
        finally:
            self._cancelled.pop(request.generation_id, None)

    async def cancel(self, generation_id: str) -> None:
        event = self._cancelled.get(generation_id)
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
