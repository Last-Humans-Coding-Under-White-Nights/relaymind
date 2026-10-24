"""Normalized adapter generation events.

Covers RM18 (normalized outputs: text_delta, tool_call, usage,
completed, error) and RM09 (partial streamed argument JSON never
executes): a ToolCall can only be built from fully parsed arguments.
Issue #1.
"""

import dataclasses
import typing

import pytest

from relaymind.adapters import (
    AdapterEvent,
    Completed,
    FirstToken,
    GenerationFailed,
    TextDelta,
    ToolCall,
    Usage,
)


def test_tool_call_accepts_fully_parsed_arguments():
    call = ToolCall(
        call_id="call_1",
        tool_name="get_service_health",
        arguments={"service": "staging-api"},
    )
    assert call.arguments["service"] == "staging-api"


def test_tool_call_rejects_unparsed_argument_string():
    # A partial streamed JSON fragment must never become a ToolCall (RM09).
    with pytest.raises(TypeError):
        ToolCall(
            call_id="call_1",
            tool_name="get_service_health",
            arguments='{"service": "stag',
        )


def test_tool_call_rejects_none_arguments():
    with pytest.raises(TypeError):
        ToolCall(call_id="call_1", tool_name="get_service_health", arguments=None)


def test_events_are_immutable():
    delta = TextDelta(text="hello")
    with pytest.raises(dataclasses.FrozenInstanceError):
        delta.text = "changed"


def test_tool_call_arguments_cannot_be_mutated_after_emission():
    # A confirmation binds to canonical arguments (RM11); the emitted
    # mapping must not be changeable in place.
    source = {"service": "staging-api"}
    call = ToolCall(call_id="call_1", tool_name="get_service_health", arguments=source)
    with pytest.raises(TypeError):
        call.arguments["service"] = "payments-api"
    source["service"] = "payments-api"
    assert call.arguments["service"] == "staging-api"


def test_tool_call_requires_nonempty_identifiers():
    with pytest.raises(ValueError):
        ToolCall(call_id="", tool_name="get_service_health", arguments={})
    with pytest.raises(ValueError):
        ToolCall(call_id="call_1", tool_name="", arguments={})


def test_usage_records_cached_tokens_when_provided():
    usage = Usage(input_tokens=100, output_tokens=5, cached_tokens=80)
    assert usage.cached_tokens == 80
    with pytest.raises(ValueError):
        Usage(input_tokens=1, output_tokens=1, cached_tokens=-1)


def test_usage_counts_the_source_did_not_report_are_none_never_zero():
    """RM16, spec 0.31 section 6 (finding F9): an unreported count is absent, not 0."""
    usage = Usage(input_tokens=1, output_tokens=1)
    assert usage.cached_tokens is None
    assert usage.reasoning_tokens is None
    # A reported zero is a measurement and stays 0.
    assert Usage(input_tokens=1, output_tokens=1, cached_tokens=0).cached_tokens == 0


def test_usage_records_reasoning_tokens_when_provided():
    usage = Usage(input_tokens=10, output_tokens=40, reasoning_tokens=32)
    assert usage.reasoning_tokens == 32
    with pytest.raises(ValueError):
        Usage(input_tokens=1, output_tokens=1, reasoning_tokens=-1)


def test_first_token_is_a_marker_event_without_fields():
    """Spec 0.31 section 6: the marker carries no time; the consumer stamps it."""
    assert FirstToken() == FirstToken()
    assert FirstToken in typing.get_args(AdapterEvent)


def test_completed_carries_finish_reason():
    done = Completed(finish_reason="stop")
    assert done.finish_reason == "stop"


def test_usage_rejects_negative_token_counts():
    with pytest.raises(ValueError):
        Usage(input_tokens=-1, output_tokens=0)


@pytest.mark.parametrize(
    "field", ["input_tokens", "output_tokens", "cached_tokens", "reasoning_tokens"]
)
@pytest.mark.parametrize("value", [True, False, 1.0, 1.5, float("nan"), float("inf"), -1, "1"])
def test_usage_requires_exact_nonnegative_integer_counts(field, value):
    """RM16/RM18/RM25: a boolean, float or string is not a token measurement."""
    values = {"input_tokens": 1, "output_tokens": 1, field: value}
    with pytest.raises(ValueError, match="token counts"):
        Usage(**values)


@pytest.mark.parametrize("value", [None, 0, 1])
def test_usage_preserves_unknown_and_exact_integer_counts(value):
    """RM16/RM25: unreported counts and measured zero remain distinct."""
    usage = Usage(value, value, value, value)
    assert dataclasses.astuple(usage) == (value,) * 4


def test_generation_failed_carries_stable_code_and_message():
    err = GenerationFailed(code="provider_timeout", message="upstream timed out")
    assert err.code == "provider_timeout"
    assert err.message == "upstream timed out"


def test_tool_call_rejects_whitespace_identifiers():
    with pytest.raises(ValueError):
        ToolCall(call_id="   ", tool_name="get_service_health", arguments={})
    with pytest.raises(ValueError):
        ToolCall(call_id="call_1", tool_name=" \t", arguments={})
