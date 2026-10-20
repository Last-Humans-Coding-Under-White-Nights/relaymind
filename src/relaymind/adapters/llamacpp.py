"""llama.cpp server adapter (RM18, spec section 6).

Talks to a separate llama-server process over its OpenAI-compatible
HTTP API on loopback. Model-specific request shaping stays inside this
module; the shared chat-completions stream protocol lives in
openai_compat, and the rest of the gateway only sees normalized events.

Non-thinking mode for Qwen3 is configured through the pinned runtime's
chat-template settings (``chat_template_kwargs.enable_thinking``), not
through prompt strings, and is verified by the live smoke test.
"""

import asyncio
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from types import MappingProxyType

import httpx

from relaymind.adapters.base import (
    AdapterCapabilities,
    AdapterEvent,
    AdapterHealth,
    GenerationFailed,
    GenerationRequest,
    ModelAdapter,
)
from relaymind.adapters.openai_compat import (
    StreamAccumulator,
    encode_message,
    encode_tools,
    next_line_or_cancel,
    parse_sse_line,
)

# Spec section 6: non-thinking is the default; an operator may enable
# thinking mode through the same chat-template settings.
NON_THINKING: Mapping[str, object] = MappingProxyType({"enable_thinking": False})


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
        "messages": [encode_message(message) for message in request.messages],
    }
    if request.tool_schemas:
        body["tools"] = encode_tools(request.tool_schemas)
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
                    line = await next_line_or_cancel(lines, cancel_event)
                    if line is None:
                        # A cancelled generation ends without Completed;
                        # a naturally ended stream settles buffered
                        # state even when no finish chunk arrived.
                        if not cancel_event.is_set():
                            for event in accumulator.flush():
                                yield event
                        return
                    chunk = parse_sse_line(line)
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
