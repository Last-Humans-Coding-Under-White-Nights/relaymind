"""llama.cpp adapter translation layer: requests out, events in.

Covers RM18 (normalized events; model-specific templates and parsing
stay inside the adapter) and RM09 (a tool call is emitted only after
complete argument parsing; partial streamed JSON never executes).
Deterministic, no server. Issue #3.
"""

from relaymind.adapters import (
    Completed,
    GenerationFailed,
    GenerationOptions,
    GenerationRequest,
    Message,
    Role,
    TextDelta,
    ToolCall,
    ToolSchema,
    Usage,
)
from relaymind.adapters.llamacpp import StreamAccumulator, build_chat_request

HEALTH_TOOL = ToolSchema(
    name="get_service_health",
    description="Return current health for a service.",
    input_schema={
        "type": "object",
        "properties": {"service": {"enum": ["staging-api", "payments-api"]}},
        "required": ["service"],
        "additionalProperties": False,
    },
)


def make_request(**overrides):
    base = dict(
        generation_id="gen_1",
        messages=(Message(role=Role.USER, content="is staging-api healthy?"),),
        tool_schemas=(HEALTH_TOOL,),
        options=GenerationOptions(max_output_tokens=128, temperature=0.2),
    )
    base.update(overrides)
    return GenerationRequest(**base)


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


def delta_chunk(delta, finish_reason=None):
    return {"choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]}


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
