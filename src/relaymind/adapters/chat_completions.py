"""External Chat Completions adapter (RM18, spec sections 3 and 6).

Talks to an operator-configured OpenAI-compatible Chat Completions
endpoint. Provider and model are configuration, not hard-coded product
dependencies. Credentials stay server-side in an environment variable
and never appear in traces or error messages.
"""

import asyncio
import os
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass

import httpx

from relaymind.adapters.base import (
    AdapterCapabilities,
    AdapterEvent,
    AdapterHealth,
    GenerationFailed,
    GenerationRequest,
    ModelAdapter,
    UnsupportedCapabilityError,
)
from relaymind.adapters.openai_compat import (
    StreamAccumulator,
    encode_message,
    encode_tools,
    next_line_or_cancel,
    parse_sse_line,
)

# GenerationOptions field -> chat-completions body key.
_OPTION_BODY_KEYS = {
    "max_output_tokens": "max_tokens",
    "temperature": "temperature",
    "top_p": "top_p",
}


def build_chat_request(
    request: GenerationRequest,
    *,
    model: str,
    supported_options: Sequence[str],
) -> dict[str, object]:
    """Map a normalized request onto an external chat completion body.

    Only operator-declared supported parameters are forwarded; a set
    option the provider does not support is an error, never a silent
    drop (the trace must reflect what the provider actually ran).
    """
    body: dict[str, object] = {
        "model": model,
        "stream": True,
        # Streaming responses carry no usage unless explicitly asked.
        "stream_options": {"include_usage": True},
        "messages": [encode_message(message) for message in request.messages],
    }
    if request.tool_schemas:
        body["tools"] = encode_tools(request.tool_schemas)
    for option_name, body_key in _OPTION_BODY_KEYS.items():
        value = getattr(request.options, option_name)
        if value is None:
            continue
        if option_name not in supported_options:
            raise UnsupportedCapabilityError(
                f"provider configuration does not support option: {option_name}"
            )
        body[body_key] = value
    return body


@dataclass(frozen=True)
class ChatCompletionsConfig:
    """Operator configuration for one external Chat Completions endpoint.

    The configuration holds the NAME of the environment variable with
    the credential, never the credential itself, so config dumps and
    traces stay secret-free.
    """

    base_url: str  # including the version prefix, e.g. https://host/v1
    model: str
    context_limit: int
    api_key_env: str = "RELAYMIND_EXTERNAL_API_KEY"
    supported_options: tuple[str, ...] = ("max_output_tokens", "temperature", "top_p")
    request_timeout_seconds: float = 60.0


class ChatCompletionsAdapter(ModelAdapter):
    """ModelAdapter over an external Chat Completions endpoint."""

    def __init__(
        self,
        config: ChatCompletionsConfig,
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

    def _api_key(self) -> str | None:
        """The credential, read fresh from the environment per call."""
        return os.environ.get(self._config.api_key_env) or None

    def _redact(self, text: str) -> str:
        """Strip the credential from text bound for events or traces.

        Providers and proxies sometimes echo the Authorization header
        in error bodies; the credential must never leave the adapter
        (issue #4, spec section 3).
        """
        api_key = self._api_key()
        if api_key:
            return text.replace(api_key, "[redacted]")
        return text

    async def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            text_streaming=True,
            tool_calling=True,
            structured_routing_output=True,
            context_limit=self._config.context_limit,
            # No qualified reasoning controls and no local tokenizer
            # for an external provider; claiming either would be
            # dishonest (RM16).
            reasoning_controls=(),
            usage_reporting=True,
            tokenizer_available=False,
        )

    async def generate(self, request: GenerationRequest) -> AsyncIterator[AdapterEvent]:
        api_key = self._api_key()
        if api_key is None:
            yield GenerationFailed(
                code="missing_credentials",
                message=f"environment variable {self._config.api_key_env} is not set",
            )
            return
        body = build_chat_request(
            request,
            model=self._config.model,
            supported_options=self._config.supported_options,
        )
        accumulator = StreamAccumulator()
        cancel_event = self._cancel_events.setdefault(request.generation_id, asyncio.Event())
        try:
            async with self._client.stream(
                "POST",
                "/chat/completions",
                json=body,
                headers={"Authorization": f"Bearer {api_key}"},
            ) as response:
                if response.status_code != 200:
                    detail = (await response.aread()).decode(errors="replace")
                    detail = f"provider returned {response.status_code}: {detail[:200]}"
                    yield GenerationFailed(
                        code="provider_error",
                        message=self._redact(detail),
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
                        # Streamed provider error messages can echo the
                        # Authorization header; scrub before emitting.
                        if isinstance(event, GenerationFailed):
                            event = GenerationFailed(
                                code=event.code, message=self._redact(event.message)
                            )
                        yield event
        except httpx.TimeoutException as exc:
            yield GenerationFailed(code="provider_timeout", message=self._redact(str(exc)))
        except httpx.HTTPError as exc:
            yield GenerationFailed(code="provider_unreachable", message=self._redact(str(exc)))
        finally:
            self._cancel_events.pop(request.generation_id, None)

    async def cancel(self, generation_id: str) -> None:
        event = self._cancel_events.get(generation_id)
        if event is not None:
            event.set()

    async def health(self) -> AdapterHealth:
        api_key = self._api_key()
        if api_key is None:
            return AdapterHealth(
                available=False,
                warm=False,
                detail=f"environment variable {self._config.api_key_env} is not set",
            )
        try:
            response = await self._client.get(
                "/models", headers={"Authorization": f"Bearer {api_key}"}
            )
        except httpx.HTTPError as exc:
            return AdapterHealth(available=False, warm=False, detail=self._redact(str(exc)))
        if response.status_code == 200:
            # An external provider has no local warm-up phase to report.
            return AdapterHealth(available=True, warm=True, detail="ready")
        if response.status_code in (401, 403):
            return AdapterHealth(available=False, warm=False, detail="authentication rejected")
        return AdapterHealth(
            available=False, warm=False, detail=f"unexpected status {response.status_code}"
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "ChatCompletionsAdapter":
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        await self.aclose()
