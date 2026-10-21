"""The client-owned alias relaymind-client-tools (RM08, RM19, AC10).

Issue #24, spec 0.16 section 7.2. The alias returns tool_calls to the
caller, who executes them and sends matching tool messages back. The
gateway never executes a client-declared function and offers none of its
own tools. Deterministic: the real orchestrator runs over model doubles.
"""

import json

import httpx
import openai
import pytest

from api_helpers import (
    RUN_ID_HEADER,
    RunningServer,
    call,
    chat,
    healthy_gateway,
    make_app,
    sse_events,
    token_of,
)
from gateway_helpers import build_gateway, replace_handler, text_turn, tool_turn
from helpers import make_capabilities, of_type
from relaymind.adapters import Completed, ToolCall
from relaymind.api.chat import ClientToolsChat
from relaymind.api.chat_request import CLIENT_TOOLS
from relaymind.orchestrator.budgets import LocalBudgets

WEATHER = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Weather for a city",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
            "additionalProperties": False,
        },
    },
}
TIME = {
    "type": "function",
    "function": {"name": "get_time", "parameters": {"type": "object", "properties": {}}},
}
# The same name as a tool the gateway has installed (spec section 9).
HEALTH = {
    "type": "function",
    "function": {
        "name": "get_service_health",
        "parameters": {"type": "object", "properties": {"service": {"type": "string"}}},
    },
}
PROMPT = "What is the weather in Oslo?"
ANSWER = "It is 4 degrees in Oslo."


def weather_call(call_id="call_w1", city="Oslo"):
    return tool_turn("get_weather", call_id=call_id, city=city)


def client_app(tmp_path, turns, **options):
    gateway = build_gateway(tmp_path, turns, **options)
    return gateway, make_app(gateway, client_tools=ClientToolsChat(gateway.orchestrator))


def request(app, **fields):
    fields.setdefault("model", CLIENT_TOOLS)
    fields.setdefault("tools", [WEATHER])
    return chat(app, prompt=PROMPT, **fields)


def events_of(gateway, response):
    return gateway.store.events(response.headers[RUN_ID_HEADER])


def message_of(response):
    return response.json()["choices"][0]["message"]


def error_of(response):
    return response.json()["error"]


def follow_up(call_id, content='{"temp_c": 4}', city="Oslo"):
    """The second request of a round trip: the assistant call and the caller's result."""
    return [
        {"role": "user", "content": PROMPT},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": json.dumps({"city": city})},
                }
            ],
        },
        {"role": "tool", "tool_call_id": call_id, "content": content},
    ]


def post(app, body, **kwargs):
    return call(app, "POST", "/v1/chat/completions", json_body=body, **kwargs)


# -- the round trip (AC10) ---------------------------------------------------------------


def test_a_function_call_and_its_tool_result_round_trip_with_the_same_id(tmp_path):
    """AC10, RM08, RM19: valid call IDs and finish reasons; the caller executes."""
    gateway, app = client_app(tmp_path, [weather_call(), text_turn(ANSWER)])

    first = request(app)
    assert first.status_code == 200
    choice = first.json()["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["content"] is None
    assert choice["message"]["tool_calls"] == [
        {
            "id": "call_w1",
            "type": "function",
            "function": {"name": "get_weather", "arguments": json.dumps({"city": "Oslo"})},
        }
    ]

    second = post(
        app, {"model": CLIENT_TOOLS, "tools": [WEATHER], "messages": follow_up("call_w1")}
    )
    assert second.status_code == 200
    choice = second.json()["choices"][0]
    assert (choice["finish_reason"], choice["message"]["content"]) == ("stop", ANSWER)
    assert "tool_calls" not in choice["message"]

    # The model saw the caller's result as a tool message with the same ID, as data.
    seen = gateway.adapter.requests[1].messages
    assert seen[-1].role.value == "tool"
    assert (seen[-1].tool_call_id, seen[-1].content) == ("call_w1", '{"temp_c": 4}')
    assert seen[-2].tool_calls[0].call_id == "call_w1"
    # Two requests, two runs, both completed with no external call.
    assert first.headers[RUN_ID_HEADER] != second.headers[RUN_ID_HEADER]
    for response in (first, second):
        record = gateway.store.get_run(response.headers[RUN_ID_HEADER])
        assert record.state == "completed"
        (completed,) = of_type(events_of(gateway, response), "run.completed")
        assert completed.payload["external_calls"] == 0
    assert gateway.external.requests == []


def test_the_openai_sdk_completes_the_round_trip_over_a_socket(tmp_path):
    """AC10: a standard SDK executes the call itself and sends the result back."""
    gateway, app = client_app(tmp_path, [weather_call("call_sdk"), text_turn(ANSWER)])
    with RunningServer(app) as running:
        client = openai.OpenAI(
            api_key=token_of(app),
            base_url=running.base_url,
            http_client=httpx.Client(trust_env=False),
            max_retries=0,
        )
        messages = [{"role": "user", "content": PROMPT}]
        first = client.chat.completions.create(
            model=CLIENT_TOOLS, messages=messages, tools=[WEATHER]
        )
        (tool_call,) = first.choices[0].message.tool_calls
        assert first.choices[0].finish_reason == "tool_calls"
        assert tool_call.id == "call_sdk"
        city = json.loads(tool_call.function.arguments)["city"]
        messages += [
            first.choices[0].message,
            {"role": "tool", "tool_call_id": tool_call.id, "content": f"4 degrees in {city}"},
        ]
        second = client.chat.completions.create(
            model=CLIENT_TOOLS, messages=messages, tools=[WEATHER]
        )
    assert second.choices[0].message.content == ANSWER
    assert second.choices[0].finish_reason == "stop"


def test_tool_calls_and_text_stream_from_the_real_backend(tmp_path):
    """RM20, AC10: a streamed call is a tool-call delta with finish_reason tool_calls."""
    _, app = client_app(tmp_path, [weather_call(), text_turn("It is ", "4 degrees.")])
    chunks = sse_events(request(app, stream=True))
    assert chunks[-1] == "[DONE]"
    (delta,) = [
        c["choices"][0]["delta"]["tool_calls"]
        for c in chunks[:-1]
        if "tool_calls" in c["choices"][0]["delta"]
    ]
    assert delta[0]["id"] == "call_w1"
    assert chunks[-2]["choices"][0]["finish_reason"] == "tool_calls"

    chunks = sse_events(request(app, stream=True))
    text = [c["choices"][0]["delta"].get("content") for c in chunks[:-1]]
    assert "".join(t for t in text if t) == "It is 4 degrees."
    assert chunks[-2]["choices"][0]["finish_reason"] == "stop"


# -- the gateway never executes a client function (RM08) --------------------------------------


def test_a_client_function_named_like_a_gateway_tool_is_never_executed(tmp_path):
    """RM08, AC10: names in a client tools array register no handler."""
    gateway, app = client_app(tmp_path, [tool_turn("get_service_health", service="staging-api")])
    ran = []
    replace_handler(gateway, "get_service_health", lambda arguments: ran.append(arguments))

    response = request(app, tools=[HEALTH])

    assert message_of(response)["tool_calls"][0]["function"]["name"] == "get_service_health"
    assert ran == []
    events = events_of(gateway, response)
    assert not of_type(events, "tool.started")
    assert not of_type(events, "tool.finished")
    assert gateway.store.tool_operations(response.headers[RUN_ID_HEADER]) == []
    (proposed,) = of_type(events, "tool.proposed")
    assert proposed.payload["tool_name"] == "get_service_health"
    assert proposed.payload["proposal_id"] == message_of(response)["tool_calls"][0]["id"]
    assert proposed.payload["model"] == "double-qwen3-0.6b"


def test_the_model_is_offered_only_the_functions_the_caller_supplied(tmp_path):
    """RM08: gateway-installed business tools are disabled for this alias."""
    gateway, app = client_app(tmp_path, [weather_call()])
    request(app, tools=[WEATHER, TIME])
    (generation,) = gateway.adapter.requests
    assert [t.name for t in generation.tool_schemas] == ["get_weather", "get_time"]
    assert generation.tool_schemas[0].input_schema["required"] == ["city"]
    assert generation.tool_schemas[0].description == "Weather for a city"


def test_a_gateway_tool_the_caller_did_not_supply_is_not_exposed(tmp_path):
    """RM08, RM09: a proposal for a registered gateway tool is invalid output."""
    gateway, app = client_app(
        tmp_path,
        [tool_turn("delete_database", database_id="db-1")] * 2,
        external_turns=None,
    )
    response = request(app)
    assert response.status_code == 422
    assert error_of(response)["code"] == "needs_external"
    assert error_of(response)["reason_code"] == "invalid_output"
    assert not of_type(events_of(gateway, response), "tool.started")


def test_the_run_trace_records_a_client_owned_run(tmp_path):
    """RM22, RM01: accepted as the client alias, no skills, no routing generation."""
    gateway, app = client_app(tmp_path, [weather_call()], external_turns=[])
    response = request(app)
    events = events_of(gateway, response)
    (accepted,) = of_type(events, "run.accepted")
    assert accepted.payload["model_alias"] == CLIENT_TOOLS
    assert accepted.payload["skills_offered"] == []
    (route,) = of_type(events, "route.selected")
    decision = route.payload["decision"]
    assert (decision["route"], decision["skill_id"], decision["reason_code"]) == (
        "local",
        None,
        "simple_skill",
    )
    assert not of_type(events, "skill.loaded")
    assert [e.payload["purpose"] for e in of_type(events, "model.started")] == ["skill"]
    (completed,) = of_type(events, "run.completed")
    assert completed.payload["external_calls"] == 0
    assert completed.payload["final_answer_model"] == "double-qwen3-0.6b"
    assert gateway.external.requests == []


def test_secret_arguments_of_an_exposed_call_are_redacted_in_the_trace(tmp_path):
    """RM12: the caller gets the real arguments; the trace does not store a secret field."""
    login = {
        "type": "function",
        "function": {"name": "login", "parameters": {"type": "object"}},
    }
    gateway, app = client_app(tmp_path, [tool_turn("login", call_id="c1", password="hunter2")])
    response = request(app, tools=[login])
    assert json.loads(message_of(response)["tool_calls"][0]["function"]["arguments"]) == {
        "password": "hunter2"
    }
    assert "hunter2" not in repr(events_of(gateway, response))


# -- tool_choice (RM19) ---------------------------------------------------------------------


def test_tool_choice_none_offers_no_function_and_needs_text(tmp_path):
    """RM19: none keeps the model from calling a function."""
    gateway, app = client_app(tmp_path, [text_turn(ANSWER)])
    response = request(app, tool_choice="none")
    assert message_of(response)["content"] == ANSWER
    assert gateway.adapter.requests[0].tool_schemas == ()


def test_a_named_tool_choice_offers_only_that_function_and_returns_its_call(tmp_path):
    """RM19: the named function is the only one offered and the one called."""
    gateway, app = client_app(tmp_path, [tool_turn("get_time", call_id="c_t")])
    response = request(
        app,
        tools=[WEATHER, TIME],
        tool_choice={"type": "function", "function": {"name": "get_time"}},
    )
    assert message_of(response)["tool_calls"][0]["function"]["name"] == "get_time"
    assert [t.name for t in gateway.adapter.requests[0].tool_schemas] == ["get_time"]


def test_required_without_a_call_is_repaired_once(tmp_path):
    """RM19, RM03: text where a call is required gets one format-repair generation."""
    gateway, app = client_app(tmp_path, [text_turn("I will not call."), weather_call()])
    response = request(app, tool_choice="required")
    assert message_of(response)["tool_calls"][0]["id"] == "call_w1"
    purposes = [
        e.payload["purpose"] for e in of_type(events_of(gateway, response), "model.started")
    ]
    assert purposes == ["skill", "repair"]
    repair_messages = gateway.adapter.requests[1].messages
    assert repair_messages[-2].content == "I will not call."
    assert "[gateway]" in repair_messages[-1].content


def test_a_call_to_another_function_than_the_named_one_is_invalid(tmp_path):
    """RM19: a named tool_choice is not met by a call to a different function."""
    gateway, app = client_app(tmp_path, [tool_turn("get_time"), tool_turn("get_time")])
    response = request(
        app,
        tools=[WEATHER, TIME],
        tool_choice={"type": "function", "function": {"name": "get_weather"}},
    )
    assert response.status_code == 422
    assert error_of(response)["reason_code"] == "invalid_output"


def test_a_call_when_none_was_chosen_is_invalid(tmp_path):
    """RM19: with none the gateway never returns tool_calls."""
    _, app = client_app(tmp_path, [weather_call(), weather_call()])
    response = request(app, tool_choice="none")
    assert response.status_code == 422


@pytest.mark.parametrize(
    "arguments",
    [{"city": 5}, {}, {"city": "Oslo", "extra": 1}],
    ids=["wrong_type", "missing_required", "unknown_field"],
)
def test_arguments_that_break_the_supplied_schema_are_not_returned(tmp_path, arguments):
    """RM09: the full name and arguments are validated; invalid ones never reach the caller."""
    _, app = client_app(
        tmp_path, [tool_turn("get_weather", **arguments), tool_turn("get_weather", **arguments)]
    )
    response = request(app)
    assert response.status_code == 422
    assert "tool_calls" not in response.text


def test_a_valid_call_after_an_invalid_one_is_returned(tmp_path):
    """RM03: the repair generation can produce the valid call."""
    gateway, app = client_app(tmp_path, [tool_turn("get_weather", city=5), weather_call()])
    response = request(app)
    assert message_of(response)["tool_calls"][0]["function"]["arguments"] == json.dumps(
        {"city": "Oslo"}
    )


# -- one call per response, and call IDs (RM19) ----------------------------------------------


def test_only_the_first_call_of_a_generation_is_exposed(tmp_path):
    """RM19: parallel calls are not supported, so one call is exposed."""
    two = [
        ToolCall(call_id="c1", tool_name="get_weather", arguments={"city": "Oslo"}),
        ToolCall(call_id="c2", tool_name="get_weather", arguments={"city": "Rome"}),
        Completed(finish_reason="tool_calls"),
    ]
    gateway, app = client_app(tmp_path, [two])
    response = request(app)
    (exposed,) = message_of(response)["tool_calls"]
    assert exposed["id"] == "c1"
    (proposed,) = of_type(events_of(gateway, response), "tool.proposed")
    assert proposed.payload["proposal_id"] == "c1"
    (finished,) = [
        e
        for e in of_type(events_of(gateway, response), "model.finished")
        if e.payload["tool_calls"]
    ]
    assert finished.payload["tool_calls"] == 2


def test_a_call_id_used_earlier_in_the_conversation_is_replaced(tmp_path):
    """RM19: call IDs stay unique across the conversation."""
    gateway, app = client_app(tmp_path, [weather_call("call_w1", city="Rome")])
    response = post(
        app, {"model": CLIENT_TOOLS, "tools": [WEATHER], "messages": follow_up("call_w1")}
    )
    (exposed,) = message_of(response)["tool_calls"]
    assert exposed["id"] != "call_w1"
    assert exposed["id"].strip()
    # The next request can use it.
    messages = follow_up("call_w1") + [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [exposed],
        },
        {"role": "tool", "tool_call_id": exposed["id"], "content": "sunny"},
    ]
    gateway.adapter._turns.append(text_turn(ANSWER))
    again = post(app, {"model": CLIENT_TOOLS, "tools": [WEATHER], "messages": messages})
    assert again.status_code == 200


# -- answer options ------------------------------------------------------------------------


def test_the_token_budget_does_not_cut_off_a_generation_that_may_call_a_function(tmp_path):
    """Spec 7.1: a generation offered tools keeps its own token budget."""
    gateway, app = client_app(tmp_path, [weather_call(), text_turn(ANSWER)])
    request(app, max_completion_tokens=50, temperature=0.5)
    assert gateway.adapter.requests[0].options.max_output_tokens is None
    assert gateway.adapter.requests[0].options.temperature == 0.5

    request(app, max_completion_tokens=50, tool_choice="none")
    assert gateway.adapter.requests[1].options.max_output_tokens == 50


# -- execution modes and fallback (RM04, section 7.2) -----------------------------------------


def test_external_fallback_happens_before_anything_is_exposed(tmp_path):
    """RM04, section 7.2: a failed local stage falls back; the response is the external one."""
    gateway, app = client_app(
        tmp_path,
        [text_turn("no"), text_turn("no again")],
        external_turns=[weather_call("call_ext")],
    )
    response = request(app, tool_choice="required")
    assert response.status_code == 200
    (exposed,) = message_of(response)["tool_calls"]
    assert exposed["id"] == "call_ext"
    events = events_of(gateway, response)
    (handoff,) = of_type(events, "handoff.started")
    assert handoff.payload["reason_code"] == "invalid_output"
    assert handoff.payload["allowed_tools"] == ["get_weather"]
    (completed,) = of_type(events, "run.completed")
    assert completed.payload["external_calls"] == 1
    assert completed.payload["final_answer_model"] == "double-external"
    # The external model got the original conversation and the supplied function only.
    (external_request,) = gateway.external.requests
    assert [t.name for t in external_request.tool_schemas] == ["get_weather"]
    assert external_request.messages[-1].content == PROMPT
    assert not of_type(events, "tool.started")


def test_a_valid_local_call_is_final_and_makes_no_external_call(tmp_path):
    """RM01, section 7.2: once the local call is accepted nothing falls back."""
    gateway, app = client_app(tmp_path, [weather_call()], external_turns=[])
    response = request(app)
    assert message_of(response)["tool_calls"][0]["id"] == "call_w1"
    assert gateway.external.requests == []


def test_local_only_returns_needs_external_when_the_local_model_cannot_call(tmp_path):
    """RM04, section 7.2: local_only never falls back."""
    gateway, app = client_app(
        tmp_path, [text_turn("no"), text_turn("no")], external_turns=[weather_call()]
    )
    response = request(
        app, tool_choice="required", headers={"X-RelayMind-Execution-Mode": "local_only"}
    )
    assert response.status_code == 422
    assert error_of(response)["code"] == "needs_external"
    assert error_of(response)["run_id"] == response.headers[RUN_ID_HEADER]
    assert gateway.external.requests == []
    assert gateway.store.get_run(response.headers[RUN_ID_HEADER]).error_code == "needs_external"


def test_external_only_skips_the_local_model(tmp_path):
    """Section 7.2: external_only sends the request to the external model directly."""
    gateway, app = client_app(tmp_path, [], external_turns=[weather_call("call_ext")])
    response = request(app, headers={"X-RelayMind-Execution-Mode": "external_only"})
    assert message_of(response)["tool_calls"][0]["id"] == "call_ext"
    assert gateway.adapter.requests == []
    events = events_of(gateway, response)
    (route,) = of_type(events, "route.selected")
    assert route.payload["decision"]["route"] == "external"
    assert route.payload["decision"]["reason_code"] == "execution_mode"
    (handoff,) = of_type(events, "handoff.started")
    assert handoff.payload["reason_code"] == "execution_mode"


def test_without_an_external_model_a_local_stop_is_needs_external(tmp_path):
    """RM04: no external model configured."""
    _, app = client_app(tmp_path, [text_turn("x"), text_turn("x")])
    response = request(app, tool_choice="required")
    assert response.status_code == 422
    assert error_of(response)["reason_code"] == "invalid_output"


def test_an_exhausted_local_generation_budget_stops_with_local_budget(tmp_path):
    """RM03, RM04: the repair generation needs room in the local limits."""
    _, app = client_app(
        tmp_path,
        [text_turn("x")],
        budgets=LocalBudgets(local_generations=1, repair_generations=1),
    )
    response = request(app, tool_choice="required")
    assert response.status_code == 422
    assert error_of(response)["reason_code"] == "local_budget"


def test_an_external_failure_after_fallback_is_external_failed(tmp_path):
    """RM04, section 11: the external stage has no further escalation."""
    _, app = client_app(
        tmp_path,
        [text_turn("x"), text_turn("x")],
        external_turns=[text_turn("still text"), text_turn("still text")],
    )
    response = request(app, tool_choice="required")
    assert response.status_code == 503
    assert error_of(response)["code"] == "external_failed"
    assert error_of(response)["reason_code"] == "invalid_output"


def test_the_local_model_without_tool_calling_cannot_serve_offered_functions(tmp_path):
    """RM18, RM04: a missing capability is a stop, not a silent text answer."""
    _, app = client_app(tmp_path, [], capabilities=make_capabilities(tool_calling=False))
    response = request(app)
    assert response.status_code == 422
    assert error_of(response)["reason_code"] == "capability_missing"


def test_a_request_over_the_local_context_limit_stops_before_generation(tmp_path):
    """RM13: the request is measured; an oversized one is not sent to the local model."""
    gateway, app = client_app(
        tmp_path, [weather_call()], capabilities=make_capabilities(context_limit=10)
    )
    response = request(app)
    assert response.status_code == 422
    assert error_of(response)["reason_code"] == "context_limit"
    assert gateway.adapter.requests == []


def test_a_text_answer_is_published_as_answer_deltas(tmp_path):
    """RM20: the answer of a client-owned run is append-only answer.delta events."""
    gateway, app = client_app(tmp_path, [text_turn("It is ", "4 degrees.")])
    response = request(app, tool_choice="none")
    assert message_of(response)["content"] == "It is 4 degrees."
    deltas = of_type(events_of(gateway, response), "answer.delta")
    assert [d.payload["text"] for d in deltas] == ["It is ", "4 degrees."]


# -- ownership mixing (RM08, RM19) -------------------------------------------------------------


@pytest.mark.parametrize(
    "mutate",
    [
        lambda body: body.update(tools=[WEATHER]),
        lambda body: body.update(tool_choice="none"),
        lambda body: body.update(messages=follow_up("c1")),
    ],
    ids=["tools", "tool_choice", "tool_result"],
)
def test_the_managed_alias_rejects_client_tool_ownership(tmp_path, mutate):
    """RM08, section 7.2: ambiguous ownership is 400 with no run."""
    gateway, app = client_app(tmp_path, [])
    body = {"model": "relaymind", "messages": [{"role": "user", "content": PROMPT}]}
    mutate(body)
    response = post(app, body)
    assert response.status_code == 400
    assert error_of(response)["code"] == "invalid_request"
    assert error_of(response)["run_id"] is None
    assert gateway.adapter.requests == []


@pytest.mark.parametrize(
    "messages",
    [
        [
            {"role": "user", "content": PROMPT},
            {"role": "tool", "tool_call_id": "nope", "content": "x"},
        ],
        follow_up("call_w1")[:2] + [{"role": "tool", "tool_call_id": "other", "content": "x"}],
    ],
    ids=["no_call_at_all", "id_of_another_call"],
)
def test_a_tool_result_without_a_matching_call_is_rejected(tmp_path, messages):
    """RM08, RM19: the gateway never accepts a result for a call it cannot match."""
    gateway, app = client_app(tmp_path, [])
    response = post(app, {"model": CLIENT_TOOLS, "tools": [WEATHER], "messages": messages})
    assert response.status_code == 400
    assert error_of(response)["code"] == "invalid_request"
    assert error_of(response)["run_id"] is None
    assert gateway.adapter.requests == []


def test_parallel_tool_calls_true_is_rejected_on_both_aliases_and_false_is_accepted(tmp_path):
    """RM19: false or omitted only."""
    gateway, app = client_app(tmp_path, [weather_call()])
    for alias in ("relaymind", CLIENT_TOOLS):
        response = request(app, model=alias, tools=None, parallel_tool_calls=True)
        assert response.status_code == 400
        assert error_of(response)["code"] == "unsupported_parameter"
    assert request(app, parallel_tool_calls=False).status_code == 200


def test_a_function_with_an_invalid_parameters_schema_is_rejected(tmp_path):
    """RM19: a schema the gateway cannot validate arguments against is invalid_request."""
    bad = {
        "type": "function",
        "function": {"name": "f", "parameters": {"type": "nonsense"}},
    }
    gateway, app = client_app(tmp_path, [])
    response = request(app, tools=[bad])
    assert response.status_code == 400
    assert error_of(response)["code"] == "invalid_request"
    assert "tools[0].function.parameters" in error_of(response)["message"]
    assert gateway.adapter.requests == []


def test_the_alias_is_not_served_without_the_client_backend(tmp_path):
    """RM19: the gateway lists only aliases it can run."""
    gateway = healthy_gateway(tmp_path)
    response = request(make_app(gateway))
    assert response.status_code == 400
