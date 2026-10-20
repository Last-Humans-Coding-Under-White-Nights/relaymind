"""llama.cpp adapter translation layer: requests out, events in.

Covers RM18 (normalized events; model-specific templates and parsing
stay inside the adapter) and RM09 (a tool call is emitted only after
complete argument parsing; partial streamed JSON never executes).
Deterministic, no server. Issue #3.
"""

import json

from helpers import HEALTH_TOOL, delta_chunk
from helpers import make_request as make_plain_request
from relaymind.adapters import (
    Completed,
    GenerationFailed,
    GenerationOptions,
    Message,
    Role,
    TextDelta,
    ToolCall,
    Usage,
)
from relaymind.adapters.llamacpp import build_chat_request
from relaymind.adapters.openai_compat import StreamAccumulator


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
    events += acc.flush()
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
    # The tool call settles at the finish chunk (RM09); Completed is
    # terminal and only comes out when the stream itself ends.
    assert final == [
        ToolCall(
            call_id="call_7",
            tool_name="get_service_health",
            arguments={"service": "staging-api"},
        ),
    ]
    assert acc.flush() == [Completed(finish_reason="tool_calls")]


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
    events += acc.flush()
    assert events == [
        TextDelta(text="staging-api is healthy"),
        Completed(finish_reason="stop"),
    ]


def test_tool_turn_messages_map_to_openai_format():
    # Finding: tool_call_id and tool_calls were dropped, breaking
    # multi-turn tool conversations (RM13: evidence goes back to the model).
    call = ToolCall(call_id="call_1", tool_name="get_service_health", arguments={"service": "s"})
    body = build_chat_request(
        make_request(
            messages=(
                Message(role=Role.USER, content="is s healthy?"),
                Message(role=Role.ASSISTANT, tool_calls=(call,)),
                Message(role=Role.TOOL, content='{"status": "ok"}', tool_call_id="call_1"),
            )
        ),
        chat_template_kwargs=None,
    )
    assert body["messages"][1] == {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "get_service_health",
                    "arguments": '{"service": "s"}',
                },
            }
        ],
    }
    assert body["messages"][2] == {
        "role": "tool",
        "content": '{"status": "ok"}',
        "tool_call_id": "call_1",
    }


def test_missing_stream_call_id_is_synthesized_not_crashing():
    acc = StreamAccumulator()
    acc.feed(
        delta_chunk(
            {
                "tool_calls": [
                    {"index": 0, "function": {"name": "get_service_health", "arguments": "{}"}}
                ]
            }
        )
    )
    events = acc.feed(delta_chunk({}, finish_reason="tool_calls"))
    calls = [event for event in events if isinstance(event, ToolCall)]
    assert calls and calls[0].call_id  # synthesized, never blank


def test_missing_tool_name_fails_as_event_not_exception():
    acc = StreamAccumulator()
    acc.feed(delta_chunk({"tool_calls": [{"index": 0, "function": {"arguments": "{}"}}]}))
    events = acc.feed(delta_chunk({}, finish_reason="tool_calls"))
    assert len(events) == 1
    assert isinstance(events[0], GenerationFailed)


def test_failure_is_terminal_and_clears_accumulated_state():
    acc = StreamAccumulator()
    acc.feed(
        delta_chunk(
            {
                "tool_calls": [
                    {"index": 0, "id": "c1", "function": {"name": "t", "arguments": "{bad"}}
                ]
            }
        )
    )
    first = acc.feed(delta_chunk({}, finish_reason="tool_calls"))
    assert isinstance(first[0], GenerationFailed)
    # A failure ends the stream: later chunks and the end-of-stream
    # flush must add nothing after the terminal event.
    assert acc.feed(delta_chunk({"content": "late"}, finish_reason="stop")) == []
    assert acc.flush() == []


def test_developer_role_maps_to_system():
    # Finding: standard llama.cpp / Qwen chat templates only know
    # system, user, and assistant; a verbatim "developer" role is
    # silently dropped during templating.
    body = build_chat_request(
        make_request(
            messages=(
                Message(role=Role.DEVELOPER, content="always use tools"),
                Message(role=Role.USER, content="is staging-api healthy?"),
            )
        ),
        chat_template_kwargs=None,
    )
    assert body["messages"][0] == {"role": "system", "content": "always use tools"}


def test_stream_options_request_usage_chunks():
    # Finding: streaming responses carry no usage unless
    # stream_options.include_usage is requested; without it the
    # advertised usage_reporting capability is never honored live.
    body = build_chat_request(make_request(), chat_template_kwargs=None)
    assert body["stream_options"] == {"include_usage": True}


def test_none_usage_counts_default_to_zero():
    # Finding: "prompt_tokens": None made int(None) raise TypeError.
    acc = StreamAccumulator()
    events = acc.feed({"choices": [], "usage": {"prompt_tokens": None, "completion_tokens": None}})
    assert events == [Usage(input_tokens=0, output_tokens=0)]


def test_none_chunk_fields_are_tolerated():
    # Finding: explicit null delta / tool_calls / function / index
    # values (seen in terminal chunks of some compatible servers)
    # raised AttributeError or TypeError.
    acc = StreamAccumulator()
    assert acc.feed({"choices": [{"index": 0, "delta": None, "finish_reason": None}]}) == []
    assert acc.feed(delta_chunk({"content": None, "tool_calls": None})) == []
    acc.feed(
        delta_chunk(
            {
                "tool_calls": [
                    {"index": None, "id": "call_9", "function": None},
                    {"index": None, "function": {"name": "t", "arguments": "{}"}},
                ]
            }
        )
    )
    events = acc.feed(delta_chunk({}, finish_reason="tool_calls"))
    events += acc.flush()
    assert events == [
        ToolCall(call_id="call_9", tool_name="t", arguments={}),
        Completed(finish_reason="tool_calls"),
    ]


def test_streamed_tool_name_fragments_are_concatenated():
    # Finding: a name split across deltas was overwritten, keeping
    # only the last fragment.
    acc = StreamAccumulator()
    acc.feed(delta_chunk({"tool_calls": [{"index": 0, "function": {"name": "get_"}}]}))
    acc.feed(
        delta_chunk(
            {
                "tool_calls": [
                    {"index": 0, "function": {"name": "service_health", "arguments": "{}"}}
                ]
            }
        )
    )
    events = acc.feed(delta_chunk({}, finish_reason="tool_calls"))
    calls = [event for event in events if isinstance(event, ToolCall)]
    assert calls and calls[0].tool_name == "get_service_health"


def test_empty_arguments_parse_as_empty_object():
    # Finding: a tool without arguments left arguments_json as "",
    # and json.loads("") failed a valid call.
    acc = StreamAccumulator()
    acc.feed(
        delta_chunk(
            {"tool_calls": [{"index": 0, "id": "call_5", "function": {"name": "list_services"}}]}
        )
    )
    events = acc.feed(delta_chunk({}, finish_reason="tool_calls"))
    events += acc.flush()
    assert events == [
        ToolCall(call_id="call_5", tool_name="list_services", arguments={}),
        Completed(finish_reason="tool_calls"),
    ]


def test_non_object_argument_json_fails_as_invalid_tool_arguments():
    # Valid JSON that is not an object must not become a tool call (RM09).
    for raw in ("[1, 2]", "42", '"hello"'):
        acc = StreamAccumulator()
        acc.feed(
            delta_chunk(
                {
                    "tool_calls": [
                        {"index": 0, "id": "call_6", "function": {"name": "t", "arguments": raw}}
                    ]
                }
            )
        )
        events = acc.feed(delta_chunk({}, finish_reason="tool_calls"))
        assert len(events) == 1, raw
        assert isinstance(events[0], GenerationFailed)
        assert events[0].code == "invalid_tool_arguments"
        # Built from received content: a transport event (RM25, 12.1).
        assert events[0].received is True


def test_streamed_error_chunk_becomes_generation_failed():
    # Finding: {"error": ...} chunks (context exhausted, server error)
    # were silently dropped because they carry no choices or usage.
    acc = StreamAccumulator()
    events = acc.feed({"error": {"message": "KV slot exhausted", "code": 500}})
    # The provider sent this failure: a transport event (RM25, 12.1).
    assert events == [
        GenerationFailed(code="provider_error", message="KV slot exhausted", received=True)
    ]
    # The stream is over; the end-of-stream flush must not add events.
    assert acc.flush() == []


def test_flush_emits_buffered_tool_calls_at_end_of_stream():
    # Finding: a stream ending at [DONE] / EOF without a finish_reason
    # chunk silently dropped accumulated tool calls and never completed.
    acc = StreamAccumulator()
    acc.feed(
        delta_chunk(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_7",
                        "function": {
                            "name": "get_service_health",
                            "arguments": '{"service": "staging-api"}',
                        },
                    }
                ]
            }
        )
    )
    events = acc.flush()
    assert events == [
        ToolCall(
            call_id="call_7",
            tool_name="get_service_health",
            arguments={"service": "staging-api"},
        ),
        Completed(finish_reason="end_of_stream"),
    ]


def test_flush_emits_the_terminal_completed_exactly_once():
    acc = StreamAccumulator()
    acc.feed(delta_chunk({"content": "hi"}))
    acc.feed(delta_chunk({}, finish_reason="stop"))
    assert acc.flush() == [Completed(finish_reason="stop")]
    assert acc.flush() == []


def test_usage_after_finish_is_emitted_before_completed():
    # OpenAI streaming with include_usage sends the usage chunk after
    # the finish_reason chunk; Completed stays the terminal event.
    acc = StreamAccumulator()
    events = acc.feed(delta_chunk({"content": "hi"}))
    events += acc.feed(delta_chunk({}, finish_reason="stop"))
    events += acc.feed({"choices": [], "usage": {"prompt_tokens": 21, "completion_tokens": 4}})
    events += acc.flush()
    assert events == [
        TextDelta(text="hi"),
        Usage(input_tokens=21, output_tokens=4),
        Completed(finish_reason="stop"),
    ]


def test_content_after_finish_is_ignored():
    acc = StreamAccumulator()
    acc.feed(delta_chunk({"content": "answer"}, finish_reason="stop"))
    assert acc.feed(delta_chunk({"content": "stray"})) == []
    assert acc.flush() == [Completed(finish_reason="stop")]


def test_duplicate_finish_reasons_yield_one_completed():
    acc = StreamAccumulator()
    acc.feed(delta_chunk({}, finish_reason="stop"))
    assert acc.feed(delta_chunk({}, finish_reason="length")) == []
    assert acc.flush() == [Completed(finish_reason="stop")]


def test_request_body_with_tools_is_json_serializable():
    # input_schema is a MappingProxyType on the frozen ToolSchema; the
    # wire body must still serialize (the live smoke caught this).
    body = build_chat_request(make_request(), chat_template_kwargs=None)
    json.dumps(body)
