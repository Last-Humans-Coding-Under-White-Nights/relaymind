"""Chat request validation: the section 7.1 parameter table (RM19, AC16).

Spec 0.14 section 7.1, issue #22. Every row of the compatibility table
has an accept test and a reject test, and an unsupported field gets the
named unsupported_parameter error, never a silent drop. These tests
check the pure parser, ``relaymind.api.chat_request``, without a web
framework; ``tests/test_chat_endpoint.py`` checks the same rules over
HTTP.
"""

import pytest

from relaymind.adapters import GenerationOptions, Role
from relaymind.api.chat_request import MANAGED, parse_chat_request
from relaymind.api.errors import ApiError

CLIENT = "relaymind-client-tools"
SERVED = (MANAGED, CLIENT)
USER = {"role": "user", "content": "Is staging-api healthy?"}
WEATHER = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Weather for a city",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
    },
}


def body(**fields):
    return {"model": MANAGED, "messages": [USER], **fields}


def parse(payload, **options):
    options.setdefault("served", SERVED)
    return parse_chat_request(payload, **options)


def rejected(payload, code, mentions, **options):
    with pytest.raises(ApiError) as caught:
        parse(payload, **options)
    assert caught.value.code == code, caught.value.message
    assert mentions in caught.value.message
    assert caught.value.run_id is None
    return caught.value


def test_minimal_request_is_accepted():
    request = parse(body())
    assert request.alias == MANAGED
    assert request.stream is False
    assert [(m.role, m.content) for m in request.messages] == [
        (Role.USER, "Is staging-api healthy?")
    ]
    assert request.final_options == GenerationOptions()
    assert request.tools == () and request.tool_choice is None


@pytest.mark.parametrize("payload", [None, [], "text", 3])
def test_body_must_be_an_object(payload):
    rejected(payload, "invalid_request", "JSON object")


def test_model_is_required_and_must_be_served():
    rejected({"messages": [USER]}, "invalid_request", "model")
    rejected(body(model=3), "invalid_request", "model")
    rejected(body(model="gpt-4o"), "invalid_request", "gpt-4o")
    # The client-owned alias is refused when the gateway does not serve it.
    rejected(body(model=CLIENT), "invalid_request", CLIENT, served=(MANAGED,))


@pytest.mark.parametrize("stream", [True, False])
def test_stream_flag_is_a_boolean(stream):
    assert parse(body(stream=stream)).stream is stream


@pytest.mark.parametrize("stream", ["true", 1, {}])
def test_stream_flag_of_another_type_is_rejected(stream):
    rejected(body(stream=stream), "invalid_request", "stream")


def test_n_must_be_one():
    parse(body(n=1))
    rejected(body(n=2), "unsupported_parameter", "n")
    rejected(body(n=0), "unsupported_parameter", "n")
    rejected(body(n="1"), "invalid_request", "n")
    rejected(body(n=True), "invalid_request", "n")


def test_token_budget_becomes_the_final_response_budget():
    assert parse(body(max_completion_tokens=64)).final_options.max_output_tokens == 64
    # max_tokens is the legacy alias for the same budget.
    assert parse(body(max_tokens=32)).final_options.max_output_tokens == 32


@pytest.mark.parametrize("value", [0, -1, 1.5, "10", True])
@pytest.mark.parametrize("name", ["max_completion_tokens", "max_tokens"])
def test_token_budget_must_be_a_positive_integer(name, value):
    rejected(body(**{name: value}), "invalid_request", name)


def test_both_token_budgets_together_are_rejected():
    rejected(body(max_tokens=5, max_completion_tokens=5), "invalid_request", "max_tokens")


def test_sampling_values_are_applied_to_the_final_response():
    options = parse(body(temperature=0.2, top_p=0.5)).final_options
    assert (options.temperature, options.top_p) == (0.2, 0.5)
    assert parse(body(temperature=0)).final_options.temperature == 0
    assert parse(body(temperature=2)).final_options.temperature == 2


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("temperature", -0.1),
        ("temperature", 2.1),
        ("temperature", "hot"),
        ("temperature", True),
        ("top_p", 0),
        ("top_p", 1.1),
        ("top_p", "1"),
    ],
)
def test_sampling_values_outside_their_bounds_are_rejected(field, value):
    rejected(body(**{field: value}), "invalid_request", field)


def test_an_option_the_adapters_do_not_accept_is_unsupported_unless_default():
    supported = {"max_output_tokens"}
    rejected(
        body(temperature=0.3), "unsupported_parameter", "temperature", supported_options=supported
    )
    rejected(body(top_p=0.3), "unsupported_parameter", "top_p", supported_options=supported)
    rejected(body(max_tokens=9), "unsupported_parameter", "max_tokens", supported_options=set())
    # The API default is accepted and not applied.
    request = parse(body(temperature=1, top_p=1), supported_options=supported)
    assert request.final_options == GenerationOptions()


def test_response_format_is_text_only():
    parse(body(response_format={"type": "text"}))
    rejected(
        body(response_format={"type": "json_object"}), "unsupported_parameter", "response_format"
    )
    rejected(
        body(response_format={"type": "json_schema", "json_schema": {}}),
        "unsupported_parameter",
        "response_format",
    )
    rejected(body(response_format="text"), "invalid_request", "response_format")


@pytest.mark.parametrize(
    "field",
    [
        "stop",
        "seed",
        "logit_bias",
        "logprobs",
        "top_logprobs",
        "presence_penalty",
        "frequency_penalty",
        "stream_options",
        "metadata",
        "store",
        "modalities",
        "audio",
        "prediction",
        "reasoning_effort",
        "service_tier",
        "web_search_options",
        "functions",
        "function_call",
        "execution_mode",
        "something_new",
    ],
)
def test_other_fields_are_named_unsupported_parameters(field):
    value = {"x": 1} if field in {"metadata", "execution_mode"} else True
    error = rejected(body(**{field: value}), "unsupported_parameter", field)
    assert error.status == 400


def test_a_null_field_counts_as_omitted_and_defaults_are_accepted():
    parse(body(stop=None, seed=None, temperature=None, n=None, stream=None, tools=None))
    parse(body(presence_penalty=0, frequency_penalty=0.0, logprobs=False, store=False))
    parse(body(user="end-user-1"))
    rejected(body(presence_penalty=0.5), "unsupported_parameter", "presence_penalty")
    rejected(body(logprobs=True), "unsupported_parameter", "logprobs")
    rejected(body(user=5), "invalid_request", "user")


def test_message_content_may_be_text_parts():
    request = parse(
        body(
            messages=[
                {
                    "role": "user",
                    "content": [{"type": "text", "text": "Is "}, {"type": "text", "text": "ok?"}],
                }
            ]
        )
    )
    assert request.messages[0].content == "Is ok?"


@pytest.mark.parametrize(
    "content",
    [
        [{"type": "image_url", "image_url": {"url": "http://x/y.png"}}],
        [{"type": "input_audio", "input_audio": {}}],
    ],
)
def test_non_text_content_parts_are_unsupported(content):
    rejected(
        body(messages=[{"role": "user", "content": content}]),
        "unsupported_parameter",
        "messages[0].content[0].type",
    )


def test_message_fields_outside_the_contract_are_unsupported():
    rejected(
        body(messages=[{"role": "user", "content": "hi", "name": "bob"}]),
        "unsupported_parameter",
        "messages[0].name",
    )
    rejected(
        body(messages=[{"role": "user", "content": "hi", "audio": {}}]),
        "unsupported_parameter",
        "messages[0].audio",
    )


@pytest.mark.parametrize(
    "messages",
    [
        None,
        [],
        {},
        ["hi"],
        [{"content": "hi"}],
        [{"role": "robot", "content": "hi"}],
        [{"role": "user"}],
        [{"role": "user", "content": 5}],
        [{"role": "user", "content": "   "}],
        [{"role": "system", "content": ""}, USER],
    ],
)
def test_malformed_messages_are_invalid_requests(messages):
    rejected(body(messages=messages), "invalid_request", "messages")


def test_instruction_roles_come_first_and_the_conversation_ends_with_the_user():
    system = {"role": "system", "content": "Be brief."}
    developer = {"role": "developer", "content": "Answer in English."}
    assistant = {"role": "assistant", "content": "Which service?"}
    request = parse(body(messages=[system, developer, USER, assistant, USER]))
    assert [m.role for m in request.messages] == [
        Role.SYSTEM,
        Role.DEVELOPER,
        Role.USER,
        Role.ASSISTANT,
        Role.USER,
    ]
    rejected(body(messages=[USER, system, USER]), "invalid_request", "messages[1]")
    rejected(body(messages=[system]), "invalid_request", "messages")
    rejected(body(messages=[assistant, USER]), "invalid_request", "messages[0]")
    rejected(body(messages=[USER, assistant]), "invalid_request", "messages[1]")


def test_an_assistant_message_needs_text_or_tool_calls():
    rejected(
        body(messages=[USER, {"role": "assistant", "content": ""}, USER]),
        "invalid_request",
        "messages[1]",
    )


# -- ownership of tools (section 7.2) ---------------------------------------


def call(call_id="call_1", name="get_weather", arguments='{"city": "Oslo"}'):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


def assistant_calls(*calls):
    return {"role": "assistant", "content": None, "tool_calls": list(calls)}


def tool_result(call_id="call_1", content="rain"):
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def test_the_managed_alias_refuses_client_tools():
    rejected(body(tools=[WEATHER]), "invalid_request", "tools")
    rejected(body(tool_choice="auto"), "invalid_request", "tool_choice")
    rejected(body(tool_choice="none"), "invalid_request", "tool_choice")
    rejected(
        body(messages=[USER, assistant_calls(call()), tool_result(), USER]),
        "invalid_request",
        "messages[1]",
    )
    rejected(body(messages=[USER, tool_result()]), "invalid_request", "messages[1]")
    # An empty tools array supplies nothing.
    parse(body(tools=[]))


def test_parallel_tool_calls_true_is_unsupported_on_both_aliases():
    for alias in (MANAGED, CLIENT):
        extra = {"tools": [WEATHER]} if alias == CLIENT else {}
        parse(body(model=alias, parallel_tool_calls=False, **extra))
        parse(body(model=alias, **extra))
        rejected(
            body(model=alias, parallel_tool_calls=True, **extra),
            "unsupported_parameter",
            "parallel_tool_calls",
        )
    rejected(
        body(model=CLIENT, tools=[WEATHER], parallel_tool_calls="no"),
        "invalid_request",
        "parallel_tool_calls",
    )


def client(**fields):
    return body(model=CLIENT, **fields)


def test_client_tools_are_normalized():
    request = parse(client(tools=[WEATHER]))
    (tool,) = request.tools
    assert tool.name == "get_weather"
    assert tool.description == "Weather for a city"
    assert tool.parameters["properties"]["city"] == {"type": "string"}
    assert request.tool_choice.mode == "auto"


@pytest.mark.parametrize(
    ("choice", "mode", "name"),
    [
        ("auto", "auto", None),
        ("none", "none", None),
        ("required", "required", None),
        ({"type": "function", "function": {"name": "get_weather"}}, "function", "get_weather"),
    ],
)
def test_tool_choice_forms_are_honored(choice, mode, name):
    request = parse(client(tools=[WEATHER], tool_choice=choice))
    assert (request.tool_choice.mode, request.tool_choice.name) == (mode, name)


@pytest.mark.parametrize(
    "choice",
    [
        "sometimes",
        3,
        {"type": "function", "function": {"name": "other_tool"}},
        {"type": "function"},
        {"type": "custom", "custom": {"name": "get_weather"}},
    ],
)
def test_other_tool_choices_are_invalid(choice):
    rejected(client(tools=[WEATHER], tool_choice=choice), "invalid_request", "tool_choice")


def test_a_tool_choice_that_needs_tools_needs_them():
    parse(client(tool_choice="none"))
    for choice in ("auto", "required"):
        rejected(client(tool_choice=choice), "invalid_request", "tool_choice")


@pytest.mark.parametrize(
    ("tools", "code", "mentions"),
    [
        ("tools", "invalid_request", "tools"),
        ([5], "invalid_request", "tools[0]"),
        ([{"type": "custom", "custom": {"name": "x"}}], "unsupported_parameter", "tools[0].type"),
        ([{"type": "function"}], "invalid_request", "tools[0].function"),
        (
            [{"type": "function", "function": {"name": "bad name!"}}],
            "invalid_request",
            "tools[0].function.name",
        ),
        (
            [{"type": "function", "function": {"name": "a" * 65}}],
            "invalid_request",
            "tools[0].function.name",
        ),
        (
            [{"type": "function", "function": {"name": "t", "parameters": []}}],
            "invalid_request",
            "tools[0].function.parameters",
        ),
        (
            [{"type": "function", "function": {"name": "t", "strict": True}}],
            "unsupported_parameter",
            "tools[0].function.strict",
        ),
        ([WEATHER, WEATHER], "invalid_request", "tools[1].function.name"),
    ],
)
def test_malformed_client_tools_are_rejected(tools, code, mentions):
    rejected(client(tools=tools), code, mentions)


def test_a_tool_result_follows_its_call_and_keeps_the_call_id():
    messages = [
        USER,
        assistant_calls(call("call_a"), call("call_b")),
        tool_result("call_b"),
        tool_result("call_a"),
    ]
    request = parse(client(tools=[WEATHER], messages=messages))
    assistant = request.messages[1]
    assert [c.call_id for c in assistant.tool_calls] == ["call_a", "call_b"]
    assert assistant.tool_calls[0].arguments == {"city": "Oslo"}
    assert [m.tool_call_id for m in request.messages[2:]] == ["call_b", "call_a"]


@pytest.mark.parametrize(
    ("messages", "mentions"),
    [
        # A tool result without a matching call.
        ([USER, tool_result("call_zzz")], "messages[1]"),
        ([USER, assistant_calls(call("call_1")), tool_result("call_zzz")], "messages[2]"),
        # The same call answered twice.
        ([USER, assistant_calls(call("call_1")), tool_result(), tool_result()], "messages[3]"),
        # A call left unanswered before the next message.
        ([USER, assistant_calls(call("a"), call("b")), tool_result("a"), USER], "messages[3]"),
        ([USER, assistant_calls(call("a")), USER], "messages[2]"),
        # The conversation ends with an assistant call nobody answered.
        ([USER, assistant_calls(call("a"))], "messages[1]"),
        # A call ID used twice.
        ([USER, assistant_calls(call("a"), call("a"))], "messages[1].tool_calls[1]"),
        (
            [
                USER,
                assistant_calls(call("a")),
                tool_result("a"),
                assistant_calls(call("a")),
                tool_result("a"),
            ],
            "messages[3].tool_calls[0]",
        ),
        # A call that is not a JSON object of arguments.
        (
            [USER, assistant_calls(call(arguments="{oops")), tool_result()],
            "messages[1].tool_calls[0]",
        ),
        (
            [USER, assistant_calls(call(arguments="[1]")), tool_result()],
            "messages[1].tool_calls[0]",
        ),
        (
            [USER, assistant_calls(call(arguments={"city": "x"})), tool_result()],
            "messages[1].tool_calls[0]",
        ),
        # A call of another type.
        (
            [USER, assistant_calls({**call(), "type": "custom"}), tool_result()],
            "messages[1].tool_calls[0]",
        ),
        # A tool message without a tool_call_id.
        ([USER, assistant_calls(call()), {"role": "tool", "content": "x"}], "messages[2]"),
    ],
)
def test_tool_call_references_are_validated(messages, mentions):
    rejected(client(tools=[WEATHER], messages=messages), "invalid_request", mentions)


def test_a_client_conversation_may_end_with_tool_results():
    request = parse(
        client(tools=[WEATHER], messages=[USER, assistant_calls(call()), tool_result()])
    )
    assert request.messages[-1].role is Role.TOOL
