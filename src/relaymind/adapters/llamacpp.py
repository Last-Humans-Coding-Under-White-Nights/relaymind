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
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

import httpx

from relaymind.adapters.base import (
    AdapterCapabilities,
    AdapterEvent,
    AdapterHealth,
    GenerationOptions,
    GenerationRequest,
    Message,
    ModelAdapter,
    TokenCountError,
    ToolSchema,
)
from relaymind.adapters.local_http import is_loopback_url
from relaymind.adapters.openai_compat import (
    encode_prompt,
    stream_chat_events,
    streamed_chat_body,
)

# Spec section 6: non-thinking is the default; an operator may enable
# thinking mode through the same chat-template settings.
NON_THINKING: Mapping[str, object] = MappingProxyType({"enable_thinking": False})


def prompt_fields(
    messages: Sequence[Message],
    tool_schemas: Sequence[ToolSchema],
    chat_template_kwargs: Mapping[str, object] | None,
) -> dict[str, object]:
    """The fields that shape the templated prompt.

    Messages, offered tools, and chat-template settings. A generation
    and a token count both build them here, so the count measures the
    prompt a generation sends (spec section 4.1).
    """
    fields = encode_prompt(messages, tool_schemas)
    if chat_template_kwargs:
        fields["chat_template_kwargs"] = dict(chat_template_kwargs)
    return fields


def chat_body(fields: Mapping[str, object], options: GenerationOptions) -> dict[str, object]:
    """A streamed llama-server chat completion body around the prompt fields."""
    body = streamed_chat_body(fields)
    if options.max_output_tokens is not None:
        body["max_tokens"] = options.max_output_tokens
    if options.temperature is not None:
        body["temperature"] = options.temperature
    if options.top_p is not None:
        body["top_p"] = options.top_p
    return body


def build_chat_request(
    request: GenerationRequest,
    *,
    chat_template_kwargs: Mapping[str, object] | None,
) -> dict[str, object]:
    """Map a normalized request onto a llama-server chat completion body."""
    return chat_body(
        prompt_fields(request.messages, request.tool_schemas, chat_template_kwargs),
        request.options,
    )


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

    locality = "local"

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
            trust_env=not is_loopback_url(config.base_url),
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
            # count_tokens measures through the pinned runtime's own
            # template and tokenizer (spec section 4.1).
            tokenizer_available=True,
        )

    def _prompt_fields(
        self, messages: Sequence[Message], tool_schemas: Sequence[ToolSchema]
    ) -> dict[str, object]:
        """The fields that shape the templated prompt.

        generate and count_tokens both build the prompt here, so a
        subclass that reshapes it reshapes both and the count keeps
        measuring the prompt a generation sends (spec section 4.1).
        """
        return prompt_fields(messages, tool_schemas, self._config.chat_template_kwargs)

    def _chat_body(self, request: GenerationRequest) -> dict[str, object]:
        """The chat completion body generate sends; subclasses may extend it.

        Fields added here (sampling, response format, cache control) do
        not shape the prompt; prompt changes belong in _prompt_fields.
        """
        return chat_body(
            self._prompt_fields(request.messages, request.tool_schemas), request.options
        )

    async def generate(self, request: GenerationRequest) -> AsyncIterator[AdapterEvent]:
        body = self._chat_body(request)
        cancel_event = self._cancel_events.setdefault(request.generation_id, asyncio.Event())
        try:
            async for event in stream_chat_events(
                self._client,
                "/v1/chat/completions",
                body,
                cancel_event=cancel_event,
                provider_label="llama-server",
            ):
                yield event
        finally:
            self._cancel_events.pop(request.generation_id, None)

    async def count_tokens(
        self,
        messages: Sequence[Message],
        tool_schemas: Sequence[ToolSchema] = (),
    ) -> int:
        """Count input tokens with the pinned runtime (spec section 4.1).

        Applies the runtime's chat template with this adapter's
        chat-template settings and the offered tools (/apply-template),
        then tokenizes that prompt (/tokenize), so the count is the
        prompt a generation would send. A runtime failure raises
        TokenCountError; no estimate is substituted (RM16).
        """
        template_body = self._prompt_fields(messages, tool_schemas)
        prompt = (await self._post_json("/apply-template", template_body)).get("prompt")
        if not isinstance(prompt, str):
            raise TokenCountError("llama-server /apply-template returned no prompt")
        tokens = (
            await self._post_json(
                "/tokenize", {"content": prompt, "add_special": True, "parse_special": True}
            )
        ).get("tokens")
        if not isinstance(tokens, list):
            raise TokenCountError("llama-server /tokenize returned no token list")
        return len(tokens)

    async def _post_json(self, path: str, body: Mapping[str, object]) -> Mapping[str, object]:
        try:
            response = await self._client.post(path, json=body)
        except httpx.HTTPError as exc:
            raise TokenCountError(f"llama-server {path} unreachable: {exc}") from exc
        if response.status_code != 200:
            raise TokenCountError(f"llama-server {path} returned status {response.status_code}")
        try:
            data = response.json()
        except ValueError as exc:
            raise TokenCountError(f"llama-server {path} returned invalid JSON") from exc
        if not isinstance(data, Mapping):
            raise TokenCountError(f"llama-server {path} returned a non-object")
        return data

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
