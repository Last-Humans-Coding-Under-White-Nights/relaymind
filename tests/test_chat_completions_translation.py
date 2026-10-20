"""External Chat Completions adapter: request translation.

Covers RM18 (spec section 6): endpoint, model identifier, and
supported parameters are operator configuration, not hard-coded
product dependencies. Deterministic, no provider. Issue #4.
"""

from dataclasses import dataclass

import pytest

from helpers import HEALTH_TOOL, make_request
from relaymind.adapters import GenerationOptions, UnsupportedCapabilityError
from relaymind.adapters.chat_completions import build_chat_request


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
