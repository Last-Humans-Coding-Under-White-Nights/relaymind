"""llama.cpp adapter translation layer: requests out, events in.

Covers RM18 (normalized events; model-specific templates and parsing
stay inside the adapter) and RM09 (a tool call is emitted only after
complete argument parsing; partial streamed JSON never executes).
Deterministic, no server. Issue #3.
"""

from helpers import HEALTH_TOOL, delta_chunk
from helpers import make_request as make_plain_request
from relaymind.adapters import (
    Completed,
    GenerationFailed,
    GenerationOptions,
    TextDelta,
    ToolCall,
    Usage,
)
from relaymind.adapters.llamacpp import StreamAccumulator, build_chat_request


def make_request(**overrides):
    base = dict(
        tool_schemas=(HEALTH_TOOL,),
        options=GenerationOptions(max_output_tokens=128, temperature=0.2),
    )
    base.update(overrides)
    return make_plain_request(**base)


def test_build_chat_request_maps_messages_tools_and_options():
    body = build_chat_request(make_request(), chat_template_kwargs={"enable_thinking": False})
    assert body["stream"] is True
    assert body["messages"] == [{"role": "user", "content": "is staging-api healthy?"}]
    assert body["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "get_service_health",
                "description": "Return current health for a service.",
                "parameters": HEALTH_TOOL.input_schema,
            },
        }
    ]
    assert body["max_tokens"] == 128
    assert body["temperature"] == 0.2
    # Non-thinking mode goes through the chat template settings (spec section 6).
    assert body["chat_template_kwargs"] == {"enable_thinking": False}


def test_build_chat_request_omits_unset_options_and_empty_tools():
    body = build_chat_request(
        make_request(tool_schemas=(), options=GenerationOptions()),
        chat_template_kwargs=None,
    )
    assert "tools" not in body
    assert "max_tokens" not in body
    assert "temperature" not in body
    assert "chat_template_kwargs" not in body


def test_text_deltas_stream_through():
    acc = StreamAccumulator()
    events = acc.feed(delta_chunk({"content": "staging-api "}))
    events += acc.feed(delta_chunk({"content": "is healthy"}))
    events += acc.feed(delta_chunk({}, finish_reason="stop"))
    assert events == [
        TextDelta(text="staging-api "),
        TextDelta(text="is healthy"),
        Completed(finish_reason="stop"),
    ]


def test_tool_call_is_emitted_only_after_complete_argument_parsing():
    acc = StreamAccumulator()
    first = acc.feed(
        delta_chunk(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_7",
                        "function": {"name": "get_service_health", "arguments": '{"serv'},
                    }
                ]
            }
        )
    )
    # Partial JSON: nothing may be emitted yet (RM09).
    assert first == []
    second = acc.feed(
        delta_chunk(
            {"tool_calls": [{"index": 0, "function": {"arguments": 'ice": "staging-api"}'}}]}
        )
    )
    assert second == []
    final = acc.feed(delta_chunk({}, finish_reason="tool_calls"))
    assert final == [
        ToolCall(
            call_id="call_7",
            tool_name="get_service_health",
            arguments={"service": "staging-api"},
        ),
        Completed(finish_reason="tool_calls"),
    ]


def test_unparseable_tool_arguments_fail_instead_of_executing():
    acc = StreamAccumulator()
    acc.feed(
        delta_chunk(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_8",
                        "function": {"name": "get_service_health", "arguments": '{"broken'},
                    }
                ]
            }
        )
    )
    events = acc.feed(delta_chunk({}, finish_reason="tool_calls"))
    assert len(events) == 1
    failure = events[0]
    assert isinstance(failure, GenerationFailed)
    assert failure.code == "invalid_tool_arguments"


def test_usage_chunk_maps_to_usage_event():
    acc = StreamAccumulator()
    events = acc.feed({"choices": [], "usage": {"prompt_tokens": 21, "completion_tokens": 4}})
    assert events == [Usage(input_tokens=21, output_tokens=4)]


def test_thinking_mode_can_be_enabled_through_chat_template_kwargs():
    # Spec 0.2, section 6: an operator MAY enable thinking mode through
    # the same chat-template settings; non-thinking stays the default.
    body = build_chat_request(make_request(), chat_template_kwargs={"enable_thinking": True})
    assert body["chat_template_kwargs"] == {"enable_thinking": True}


def test_reasoning_content_never_becomes_answer_text():
    # Spec 0.2, section 6: reasoning output MUST NOT appear as answer
    # content in either mode. llama-server streams it as
    # reasoning_content; only content becomes TextDelta.
    acc = StreamAccumulator()
    events = acc.feed(delta_chunk({"reasoning_content": "thinking about services..."}))
    events += acc.feed(delta_chunk({"content": "staging-api is healthy"}))
    events += acc.feed(delta_chunk({}, finish_reason="stop"))
    assert events == [
        TextDelta(text="staging-api is healthy"),
        Completed(finish_reason="stop"),
    ]
