"""Normalized adapter generation events.

Covers RM18 (normalized outputs: text_delta, tool_call, usage,
completed, error) and RM09 (partial streamed argument JSON never
executes): a ToolCall can only be built from fully parsed arguments.
Issue #1.
"""

import dataclasses

import pytest

from relaymind.adapters import (
    Completed,
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


def test_completed_carries_finish_reason():
    done = Completed(finish_reason="stop")
    assert done.finish_reason == "stop"


def test_usage_rejects_negative_token_counts():
    with pytest.raises(ValueError):
        Usage(input_tokens=-1, output_tokens=0)


def test_generation_failed_carries_stable_code_and_message():
    err = GenerationFailed(code="provider_timeout", message="upstream timed out")
    assert err.code == "provider_timeout"
    assert err.message == "upstream timed out"
