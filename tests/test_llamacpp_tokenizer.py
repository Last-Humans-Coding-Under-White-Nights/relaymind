"""Local tokenizer on the llama.cpp adapter (issue #47).

Spec section 4.1: the fast router input is capped at 2,048 tokens
measured by the local tokenizer, and longer inputs are never silently
truncated. Spec section 6: the capability record states tokenizer
availability. RM16: an adapter never claims a tokenizer it does not
have. RM03: budgets are measured, recorded values, so the count must
come from the pinned runtime, not an estimate.

The count applies the runtime's chat template (llama-server
/apply-template, with the adapter's chat-template settings and the
offered tools) and tokenizes the result (/tokenize), so it measures the
same prompt a generation would send. Deterministic over
httpx.MockTransport; the live check is in test_llamacpp_live_smoke.py.
"""

import asyncio
import json

import httpx
import pytest

from helpers import HEALTH_TOOL, LLAMACPP_CONFIG, make_llamacpp_adapter, run_closing
from relaymind.adapters import (
    GenerationRequest,
    Message,
    ModelAdapter,
    Role,
    UnsupportedCapabilityError,
)
from relaymind.adapters.base import TokenCountError
from relaymind.adapters.chat_completions import ChatCompletionsAdapter, ChatCompletionsConfig
from relaymind.adapters.llamacpp import build_chat_request

MESSAGES = (
    Message(role=Role.SYSTEM, content="You route requests."),
    Message(role=Role.USER, content="staging-api 现在健康吗？"),
)

TEMPLATED = "<|im_start|>system\nYou route requests.<|im_end|>\n<|im_start|>user\n..."


def tokenizer_runtime(seen: list, *, tokens: int = 5):
    """A llama-server double for /apply-template and /tokenize."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append((request.method, request.url.path, body))
        if request.url.path == "/apply-template":
            return httpx.Response(200, json={"prompt": TEMPLATED})
        if request.url.path == "/tokenize":
            return httpx.Response(200, json={"tokens": list(range(tokens))})
        return httpx.Response(404)

    return handler


def test_capabilities_report_the_tokenizer():
    adapter = make_llamacpp_adapter(lambda request: httpx.Response(200))
    caps = run_closing(adapter, lambda a: a.capabilities())
    assert caps.tokenizer_available is True


def test_count_is_the_token_length_of_the_templated_prompt():
    seen: list = []
    adapter = make_llamacpp_adapter(tokenizer_runtime(seen, tokens=28))

    count = run_closing(adapter, lambda a: a.count_tokens(MESSAGES))

    assert count == 28
    assert [(method, path) for method, path, _ in seen] == [
        ("POST", "/apply-template"),
        ("POST", "/tokenize"),
    ]
    # The templated prompt is what gets tokenized, with special tokens
    # parsed, as the runtime does for a chat completion.
    assert seen[1][2]["content"] == TEMPLATED
    assert seen[1][2]["parse_special"] is True
    # No tools offered, so none are templated.
    assert "tools" not in seen[0][2]


def test_the_template_request_mirrors_a_generation():
    # Same messages, same chat-template settings (non-thinking adds an
    # empty think block to the prompt), and the offered tools: the count
    # measures what a generation would send.
    seen: list = []
    adapter = make_llamacpp_adapter(tokenizer_runtime(seen))

    run_closing(adapter, lambda a: a.count_tokens(MESSAGES, tool_schemas=(HEALTH_TOOL,)))

    template_body = seen[0][2]
    assert template_body["messages"] == [
        {"role": "system", "content": "You route requests."},
        {"role": "user", "content": "staging-api 现在健康吗？"},
    ]
    assert template_body["chat_template_kwargs"] == {"enable_thinking": False}
    assert [tool["function"]["name"] for tool in template_body["tools"]] == ["get_service_health"]
    # Counting never generates.
    assert "stream" not in template_body
    # The template request carries exactly the prompt fields of the
    # chat body generate sends, so the two cannot drift apart.
    generation = build_chat_request(
        GenerationRequest(generation_id="g", messages=MESSAGES, tool_schemas=(HEALTH_TOOL,)),
        chat_template_kwargs=LLAMACPP_CONFIG.chat_template_kwargs,
    )
    assert template_body == {key: generation[key] for key in template_body}
    assert set(template_body) == {"messages", "tools", "chat_template_kwargs"}


@pytest.mark.parametrize(
    "broken",
    [
        {"/apply-template": httpx.Response(500, text="boom")},
        {"/apply-template": httpx.Response(200, json={"text": "no prompt"})},
        {"/tokenize": httpx.Response(503, text="loading model")},
        {"/tokenize": httpx.Response(200, json={"tokens": "many"})},
        {"/tokenize": httpx.Response(200, content=b"not json")},
    ],
    ids=["template-500", "template-shape", "tokenize-503", "tokenize-shape", "tokenize-json"],
)
def test_runtime_failures_raise_instead_of_guessing(broken):
    # RM16: an unmeasured input never gets an estimated count; the router
    # must see the failure and take the documented path.
    healthy = tokenizer_runtime([])

    def handler(request: httpx.Request) -> httpx.Response:
        return broken.get(request.url.path) or healthy(request)

    adapter = make_llamacpp_adapter(handler)
    with pytest.raises(TokenCountError):
        run_closing(adapter, lambda a: a.count_tokens(MESSAGES))


def test_an_unreachable_runtime_raises_a_token_count_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    adapter = make_llamacpp_adapter(handler)
    with pytest.raises(TokenCountError, match="connection refused"):
        run_closing(adapter, lambda a: a.count_tokens(MESSAGES))


def test_the_external_adapter_has_no_tokenizer():
    # RM16: the external adapter keeps tokenizer_available=False and
    # refuses to count rather than estimate; no request is sent.
    requests: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200)

    adapter = ChatCompletionsAdapter(
        ChatCompletionsConfig(base_url="https://x.example/v1", model="m", context_limit=1024),
        transport=httpx.MockTransport(handler),
    )
    caps = run_closing(adapter, lambda a: a.capabilities())
    assert caps.tokenizer_available is False
    with pytest.raises(UnsupportedCapabilityError, match="tokenizer"):
        run_closing(adapter, lambda a: a.count_tokens(MESSAGES))
    assert requests == []


def test_count_tokens_is_optional_on_the_adapter_contract():
    # Adapters without a tokenizer need not implement counting; the
    # contract's default refuses explicitly.
    class NoTokenizer(ModelAdapter):
        async def capabilities(self):
            raise NotImplementedError

        def generate(self, request):
            raise NotImplementedError

        async def cancel(self, generation_id):
            return None

        async def health(self):
            raise NotImplementedError

    with pytest.raises(UnsupportedCapabilityError, match="tokenizer"):
        asyncio.run(NoTokenizer().count_tokens(MESSAGES))


def test_a_prompt_shaping_override_reaches_both_the_count_and_the_generation():
    # One overridable method builds the prompt fields for generate and
    # for count_tokens, so a subclass cannot shape one without the other.
    from relaymind.adapters.llamacpp import LlamaCppAdapter

    class Prefixed(LlamaCppAdapter):
        def _prompt_fields(self, messages, tool_schemas):
            fields = super()._prompt_fields(messages, tool_schemas)
            fields["chat_template_kwargs"] = {**fields["chat_template_kwargs"], "probe": True}
            return fields

    seen: list = []
    adapter = Prefixed(LLAMACPP_CONFIG, transport=httpx.MockTransport(tokenizer_runtime(seen)))
    run_closing(adapter, lambda a: a.count_tokens(MESSAGES))
    assert seen[0][2]["chat_template_kwargs"] == {"enable_thinking": False, "probe": True}

    body = Prefixed(LLAMACPP_CONFIG)._chat_body(
        GenerationRequest(generation_id="g", messages=MESSAGES)
    )
    assert body["chat_template_kwargs"] == {"enable_thinking": False, "probe": True}
