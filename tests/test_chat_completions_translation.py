"""External Chat Completions adapter: request translation.

Covers RM18 (spec section 6): endpoint, model identifier, and
supported parameters are operator configuration, not hard-coded
product dependencies. Deterministic, no provider. Issue #4.
"""

from dataclasses import dataclass

import pytest

from helpers import HEALTH_TOOL, make_request
from relaymind.adapters import GenerationOptions, UnsupportedCapabilityError
from relaymind.adapters.chat_completions import ChatCompletionsConfig, build_chat_request


def test_build_chat_request_includes_configured_model():
    body = build_chat_request(
        make_request(
            tool_schemas=(HEALTH_TOOL,),
            options=GenerationOptions(max_output_tokens=128, temperature=0.2, top_p=0.9),
        ),
        model="provider-large-1",
        supported_options=("max_output_tokens", "temperature", "top_p"),
    )
    assert body["model"] == "provider-large-1"
    assert body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}
    assert body["messages"] == [{"role": "user", "content": "is staging-api healthy?"}]
    assert body["tools"][0]["function"]["name"] == "get_service_health"
    assert body["max_tokens"] == 128
    assert body["temperature"] == 0.2
    assert body["top_p"] == 0.9


def test_unsupported_option_is_rejected_not_dropped():
    # Spec section 6: configuration records the supported parameters;
    # silently dropping a requested option would misreport what the
    # provider actually ran.
    with pytest.raises(UnsupportedCapabilityError, match="temperature"):
        build_chat_request(
            make_request(options=GenerationOptions(temperature=0.2)),
            model="provider-large-1",
            supported_options=("max_output_tokens",),
        )


def test_future_options_are_never_silently_dropped():
    # If GenerationOptions grows a field this adapter cannot translate,
    # a set value must fail loudly instead of quietly not reaching the
    # provider (the trace must reflect what the provider actually ran).
    @dataclass(frozen=True)
    class ExtendedOptions(GenerationOptions):
        seed: int | None = None

    with pytest.raises(UnsupportedCapabilityError, match="seed"):
        build_chat_request(
            make_request(options=ExtendedOptions(seed=7)),
            model="provider-large-1",
            supported_options=("max_output_tokens", "temperature", "top_p", "seed"),
        )


def test_unset_options_do_not_trip_support_checks():
    body = build_chat_request(
        make_request(options=GenerationOptions()),
        model="provider-large-1",
        supported_options=(),
    )
    assert "max_tokens" not in body
    assert "temperature" not in body
    assert "top_p" not in body


# -- request options (#120; spec 0.31 section 6) ------------------------------------------------


def test_request_options_are_merged_into_the_body():
    body = build_chat_request(
        make_request(),
        model="provider-large-1",
        supported_options=(),
        request_options={"thinking": {"type": "disabled"}, "seed": 7},
    )
    assert body["thinking"] == {"type": "disabled"}
    assert body["seed"] == 7


@pytest.mark.parametrize("key", ["model", "messages", "tools", "stream", "stream_options"])
def test_request_options_never_override_the_fields_the_gateway_owns(key):
    """Spec 0.31 section 6: model, messages, tools, stream, and stream_options are set last."""
    request = make_request(tool_schemas=(HEALTH_TOOL,))
    plain = build_chat_request(request, model="provider-large-1", supported_options=())
    body = build_chat_request(
        request,
        model="provider-large-1",
        supported_options=(),
        request_options={key: "hijacked"},
    )
    assert body == plain


def test_request_options_do_not_replace_a_translated_sampling_option():
    body = build_chat_request(
        make_request(options=GenerationOptions(temperature=0.2)),
        model="m",
        supported_options=("temperature",),
        request_options={"temperature": 1.5},
    )
    assert body["temperature"] == 0.2


def test_config_freezes_its_request_options():
    options = {"thinking": {"type": "disabled"}}
    config = ChatCompletionsConfig(
        base_url="https://x.example/v1", model="m", context_limit=1024, request_options=options
    )
    options["thinking"]["type"] = "enabled"
    options["extra"] = 1
    assert dict(config.request_options) == {"thinking": {"type": "disabled"}}
    with pytest.raises(TypeError):
        config.request_options["extra"] = 2
    assert (
        ChatCompletionsConfig(
            base_url="https://x.example/v1", model="m", context_limit=1024
        ).request_options
        == {}
    )


@pytest.mark.parametrize(
    "options",
    [
        {"": 1},
        {"   ": 1},
        {"outer": {"": 1}},
        {"outer": {"   ": 1}},
        {"outer": {"inner": {"\t": 1}}},
        {"outer": [{"": 1}]},
    ],
)
def test_an_empty_or_blank_key_is_refused_at_any_depth(options):
    """Spec 0.31 section 14.1: a nested key is checked like a top-level key."""
    with pytest.raises(ValueError, match="request_options .*key that is not text"):
        ChatCompletionsConfig(
            base_url="https://x.example/v1", model="m", context_limit=1024, request_options=options
        )
