"""POST /v1/chat/completions and GET /v1/models over HTTP (RM19, RM20, AC09, AC16).

Issue #22, spec 0.14 section 7.1. The app runs the real orchestrator
over model doubles; the client calls it in process. These tests cover
the response shape (stable request ID, virtual alias, X-RelayMind-Run-ID),
the stream framing, the execution mode header and its policy, the error
statuses the endpoint can reach, and the application token. The
parameter table itself is in tests/test_chat_request.py.
"""

import json

import pytest

from api_helpers import RUN_ID_HEADER, TOKEN, call, chat, healthy_gateway, make_app, sse_events
from gateway_helpers import (
    STAGING_OK,
    build_gateway,
    clarify_turn,
    route_turn,
    text_turn,
    tool_turn,
)
from helpers import of_type
from relaymind.adapters import ToolCall
from relaymind.api.app import ChatSettings
from relaymind.api.chat import ChatResult
from relaymind.api.chat_request import CLIENT_TOOLS
from relaymind.orchestrator.local_loop import RunOutcome
from relaymind.orchestrator.route_decision import ExecutionMode
from relaymind.orchestrator.run_state import RunState
from relaymind.tools import CallerContext
from relaymind.tools.sandbox import MUTATE_PERMISSION, READ_PERMISSION


def error_of(response):
    body = response.json()
    assert list(body) == ["error"]
    return body["error"]


# -- GET /v1/models ---------------------------------------------------------------


def test_models_lists_the_served_alias(tmp_path):
    app = make_app(healthy_gateway(tmp_path))
    response = call(app, "GET", "/v1/models")
    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "list"
    assert [m["id"] for m in body["data"]] == ["relaymind"]
    assert all(m["object"] == "model" and m["owned_by"] == "relaymind" for m in body["data"])
    assert all(isinstance(m["created"], int) for m in body["data"])


class ClientBackend:
    """A client-owned backend double: records what the API layer hands it."""

    def __init__(self, result):
        self.result = result
        self.seen = []

    async def complete(self, request, mode):
        self.seen.append((request, mode))
        return self.result


def tool_result():
    return ChatResult(
        run_id="run_client",
        content=None,
        finish_reason="tool_calls",
        tool_calls=(
            ToolCall(call_id="call_1", tool_name="get_weather", arguments={"city": "Oslo"}),
        ),
    )


def test_models_lists_the_client_tools_alias_only_when_it_is_served(tmp_path):
    app = make_app(healthy_gateway(tmp_path), client_tools=ClientBackend(tool_result()))
    ids = [m["id"] for m in call(app, "GET", "/v1/models").json()["data"]]
    assert ids == ["relaymind", CLIENT_TOOLS]


# -- the application token ---------------------------------------------------------


@pytest.mark.parametrize(
    "auth", ["Bearer wrong-token", "Basic " + TOKEN, TOKEN, "Bearer ", "Bearer"]
)
@pytest.mark.parametrize(
    ("method", "path"), [("GET", "/v1/models"), ("POST", "/v1/chat/completions")]
)
def test_a_wrong_token_is_401_and_creates_no_run(tmp_path, method, path, auth):
    gateway = healthy_gateway(tmp_path)
    app = make_app(gateway)
    body = {"model": "relaymind", "messages": [{"role": "user", "content": "hi"}]}
    response = call(app, method, path, json_body=body if method == "POST" else None, auth=auth)
    assert response.status_code == 401
    assert error_of(response) == {
        "code": "unauthenticated",
        "message": "a valid application token is required",
        "run_id": None,
    }
    assert "wrong-token" not in response.text
    assert response.headers["www-authenticate"] == "Bearer"
    assert gateway.adapter.requests == []


@pytest.mark.parametrize(
    ("method", "path"), [("GET", "/v1/models"), ("POST", "/v1/chat/completions")]
)
def test_a_missing_token_is_401(tmp_path, method, path):
    app = make_app(healthy_gateway(tmp_path))
    response = call(app, method, path, json_body={}, auth=False)
    assert response.status_code == 401
    assert error_of(response)["code"] == "unauthenticated"


def test_the_token_is_checked_before_the_body(tmp_path):
    app = make_app(healthy_gateway(tmp_path))
    response = call(app, "POST", "/v1/chat/completions", content=b"not json", auth=False)
    assert response.status_code == 401


# -- a completed run: response shape ------------------------------------------------


def test_a_completed_run_answers_with_one_assistant_choice(tmp_path):
    gateway = healthy_gateway(tmp_path)
    response = chat(make_app(gateway))
    assert response.status_code == 200
    body = response.json()
    run_id = response.headers[RUN_ID_HEADER]
    assert body["id"] == f"chatcmpl-{run_id}"
    assert body["object"] == "chat.completion"
    assert body["model"] == "relaymind"
    assert isinstance(body["created"], int)
    assert body["choices"] == [
        {
            "index": 0,
            "message": {"role": "assistant", "content": STAGING_OK},
            "finish_reason": "stop",
        }
    ]
    # No unified usage across tokenizers (section 7.3), no fingerprint.
    assert "usage" not in body and "system_fingerprint" not in body
    # The request ID is the run that served it, and the trace proves locality (RM01).
    record = gateway.store.get_run(run_id)
    assert record.state == "completed"
    (accepted,) = of_type(gateway.store.events(run_id), "run.accepted")
    assert accepted.payload["model_alias"] == "relaymind"
    assert gateway.external.requests == []


def test_the_system_and_user_messages_reach_the_orchestrator(tmp_path):
    gateway = healthy_gateway(tmp_path)
    response = chat(
        make_app(gateway),
        messages=[
            {"role": "system", "content": "Be brief."},
            {"role": "user", "content": "Is staging-api healthy right now?"},
        ],
    )
    assert response.status_code == 200
    routed = " ".join(m.content or "" for m in gateway.adapter.requests[0].messages)
    assert "Be brief." in routed and "Is staging-api healthy right now?" in routed


def test_the_token_budget_applies_to_the_final_generation(tmp_path):
    gateway = healthy_gateway(tmp_path)
    response = chat(make_app(gateway), max_completion_tokens=77, temperature=0.3)
    assert response.status_code == 200
    final = gateway.adapter.requests[-1]
    assert final.options.max_output_tokens == 77
    assert final.options.temperature == 0.3


# -- streaming ------------------------------------------------------------------------


def test_a_stream_has_role_content_finish_and_done(tmp_path):
    gateway = healthy_gateway(tmp_path)
    response = chat(make_app(gateway), stream=True)
    assert response.status_code == 200
    events = sse_events(response)
    run_id = response.headers[RUN_ID_HEADER]
    assert events[-1] == "[DONE]"
    chunks = events[:-1]
    assert {c["id"] for c in chunks} == {f"chatcmpl-{run_id}"}
    assert {c["object"] for c in chunks} == {"chat.completion.chunk"}
    assert {c["model"] for c in chunks} == {"relaymind"}
    assert chunks[0]["choices"][0]["delta"] == {"role": "assistant", "content": ""}
    assert chunks[0]["choices"][0]["finish_reason"] is None
    assert chunks[-1]["choices"][0]["delta"] == {}
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    # Only answer content between the framing chunks (RM20), in the order of answer.delta.
    middle = chunks[1:-1]
    assert all(set(c["choices"][0]["delta"]) == {"content"} for c in middle)
    assert all(c["choices"][0]["finish_reason"] is None for c in middle)
    pieces = [c["choices"][0]["delta"]["content"] for c in middle]
    published = [e.payload["text"] for e in of_type(gateway.store.events(run_id), "answer.delta")]
    assert pieces == published
    assert "".join(pieces) == STAGING_OK


def test_a_stream_and_a_plain_response_give_the_same_answer(tmp_path):
    plain = chat(make_app(healthy_gateway(tmp_path / "a")))
    streamed = chat(make_app(healthy_gateway(tmp_path / "b")), stream=True)
    text = "".join(c["choices"][0]["delta"].get("content", "") for c in sse_events(streamed)[:-1])
    assert text == plain.json()["choices"][0]["message"]["content"]


# -- waiting runs ---------------------------------------------------------------------


def test_a_clarification_is_the_assistant_message(tmp_path):
    gateway = build_gateway(
        tmp_path, [clarify_turn("Which service do you mean?")], external_turns=[]
    )
    response = chat(make_app(gateway), prompt="Is it healthy?")
    assert response.status_code == 200
    choice = response.json()["choices"][0]
    assert choice["message"]["content"] == "Which service do you mean?"
    assert choice["finish_reason"] == "stop"
    run_id = response.headers[RUN_ID_HEADER]
    assert gateway.store.get_run(run_id).state == "waiting_input"


def test_a_pending_confirmation_is_explained_without_the_token(tmp_path):
    turns = [
        route_turn("delete-demo-database"),
        tool_turn("list_demo_databases", call_id="call_list"),
        tool_turn(
            "delete_demo_database",
            call_id="call_delete",
            database_id="demo-orders-db",
            expected_revision=3,
        ),
    ]
    gateway = build_gateway(
        tmp_path,
        turns,
        caller=CallerContext(permissions=frozenset({READ_PERMISSION, MUTATE_PERMISSION})),
    )
    outcomes = []
    original = gateway.orchestrator.run

    async def spy(*args, **kwargs):
        outcomes.append(await original(*args, **kwargs))
        return outcomes[-1]

    gateway.orchestrator.run = spy
    response = chat(make_app(gateway), prompt="Delete the demo-orders-db sandbox database.")
    assert response.status_code == 200
    (outcome,) = outcomes
    assert outcome.state is RunState.WAITING_CONFIRMATION
    run_id = response.headers[RUN_ID_HEADER]
    content = response.json()["choices"][0]["message"]["content"]
    assert "delete_demo_database" in content and run_id in content
    assert "confirmation" in content.lower()
    # The caller of a chat request cannot approve with a free-form yes (section 7.4).
    assert outcome.confirmation.token not in response.text
    assert outcome.confirmation.approval_id not in response.text
    assert gateway.sandbox.list_databases()


# -- execution mode header (spec 0.4, section 7.1) -----------------------------------


def mode_of(gateway, run_id):
    (accepted,) = of_type(gateway.store.events(run_id), "run.accepted")
    return (
        accepted.payload["requested_execution_mode"],
        accepted.payload["effective_execution_mode"],
    )


def test_an_absent_header_means_auto(tmp_path):
    gateway = healthy_gateway(tmp_path)
    response = chat(make_app(gateway))
    # Absent means auto (section 7.1): both the requested and effective mode are auto.
    assert mode_of(gateway, response.headers[RUN_ID_HEADER]) == ("auto", "auto")


@pytest.mark.parametrize("mode", ["auto", "local_only"])
def test_the_header_selects_the_execution_mode(tmp_path, mode):
    gateway = healthy_gateway(tmp_path)
    response = chat(make_app(gateway), headers={"X-RelayMind-Execution-Mode": mode})
    assert response.status_code == 200
    requested, effective = mode_of(gateway, response.headers[RUN_ID_HEADER])
    assert (requested, effective) == (mode, mode)


def test_external_only_bypasses_local_inference(tmp_path):
    # The scripted external model proposes the health tool, then answers.
    gateway = build_gateway(
        tmp_path,
        [],
        external_turns=[
            tool_turn("get_service_health", service="staging-api"),
            text_turn(STAGING_OK),
        ],
    )
    response = chat(make_app(gateway), headers={"X-RelayMind-Execution-Mode": "external_only"})
    assert response.status_code == 200, response.text
    assert gateway.adapter.requests == []
    assert response.json()["choices"][0]["message"]["content"] == STAGING_OK


@pytest.mark.parametrize("value", ["fast", "AUTO", "", "local-only"])
def test_an_unknown_mode_is_400_and_creates_no_run(tmp_path, value):
    gateway = healthy_gateway(tmp_path)
    response = chat(make_app(gateway), headers={"X-RelayMind-Execution-Mode": value})
    assert response.status_code == 400
    error = error_of(response)
    assert error["code"] == "invalid_execution_mode" and error["run_id"] is None
    assert RUN_ID_HEADER not in response.headers
    assert gateway.adapter.requests == []


def test_a_mode_the_policy_forbids_is_403_never_downgraded(tmp_path):
    gateway = healthy_gateway(tmp_path)
    settings = ChatSettings(allowed_modes=frozenset({ExecutionMode.AUTO, ExecutionMode.LOCAL_ONLY}))
    app = make_app(gateway, settings=settings)
    response = chat(app, headers={"X-RelayMind-Execution-Mode": "external_only"})
    assert response.status_code == 403
    assert error_of(response)["code"] == "execution_mode_forbidden"
    assert gateway.adapter.requests == [] and gateway.external.requests == []
    # An allowed mode still works.
    assert chat(app, headers={"X-RelayMind-Execution-Mode": "local_only"}).status_code == 200


def test_parallel_demo_is_not_admissible_with_the_client_tools_alias(tmp_path):
    backend = ClientBackend(tool_result())
    app = make_app(healthy_gateway(tmp_path), client_tools=backend)
    response = chat(
        app,
        model=CLIENT_TOOLS,
        tools=[{"type": "function", "function": {"name": "get_weather"}}],
        headers={"X-RelayMind-Execution-Mode": "parallel_demo"},
    )
    assert response.status_code == 400
    assert error_of(response)["code"] == "invalid_execution_mode"
    assert backend.seen == []


def test_an_invalid_mode_value_is_checked_before_the_policy(tmp_path):
    settings = ChatSettings(allowed_modes=frozenset({ExecutionMode.AUTO}))
    app = make_app(healthy_gateway(tmp_path), settings=settings)
    response = chat(app, headers={"X-RelayMind-Execution-Mode": "bogus"})
    assert response.status_code == 400


# -- request validation over HTTP ------------------------------------------------------


def test_an_unsupported_parameter_is_a_named_400_with_no_run(tmp_path):
    gateway = healthy_gateway(tmp_path)
    response = chat(make_app(gateway), stop=["x"])
    assert response.status_code == 400
    error = error_of(response)
    assert error["code"] == "unsupported_parameter"
    assert "stop" in error["message"] and error["run_id"] is None
    assert RUN_ID_HEADER not in response.headers
    assert gateway.adapter.requests == []


@pytest.mark.parametrize("content", [b"not json", b"[]", b"", b'"text"'])
def test_a_body_that_is_not_a_json_object_is_invalid_request(tmp_path, content):
    app = make_app(healthy_gateway(tmp_path))
    response = call(app, "POST", "/v1/chat/completions", content=content)
    assert response.status_code == 400
    assert error_of(response)["code"] == "invalid_request"


def test_an_unserved_alias_is_invalid_request(tmp_path):
    response = chat(make_app(healthy_gateway(tmp_path)), model=CLIENT_TOOLS)
    assert response.status_code == 400
    assert error_of(response)["code"] == "invalid_request"


def test_an_idempotency_key_is_refused_until_idempotency_exists(tmp_path):
    gateway = healthy_gateway(tmp_path)
    response = chat(make_app(gateway), headers={"Idempotency-Key": "abc"})
    assert response.status_code == 400
    error = error_of(response)
    assert error["code"] == "unsupported_parameter" and "Idempotency-Key" in error["message"]
    assert gateway.adapter.requests == []


# -- errors a run can reach ---------------------------------------------------------------


def test_needs_external_is_422_with_the_run_id_and_reason(tmp_path):
    gateway = build_gateway(tmp_path, [route_turn("investigate-performance")], external_turns=[])
    response = chat(
        make_app(gateway),
        prompt="Why is staging-api slow?",
        headers={"X-RelayMind-Execution-Mode": "local_only"},
    )
    assert response.status_code == 422
    error = error_of(response)
    assert error["code"] == "needs_external"
    assert error["reason_code"] == "complex_skill"
    assert error["run_id"] == response.headers[RUN_ID_HEADER]
    assert gateway.external.requests == []
    assert gateway.store.get_run(error["run_id"]).state == "failed"


def test_another_active_run_is_429(tmp_path):
    gateway = healthy_gateway(tmp_path)
    machine = gateway.orchestrator.machine
    other = machine.create()
    machine.transition(other.run_id, other.version, RunState.ROUTING)
    response = chat(make_app(gateway))
    assert response.status_code == 429
    error = error_of(response)
    assert error["code"] == "overloaded"
    assert gateway.adapter.requests == []


def test_an_unexpected_failure_is_a_fixed_500_without_detail(tmp_path):
    gateway = healthy_gateway(tmp_path)

    async def boom(*args, **kwargs):
        raise RuntimeError("secret-value-123 leaked in a stack trace")

    gateway.orchestrator.run = boom
    response = chat(make_app(gateway))
    assert response.status_code == 500
    error = error_of(response)
    assert error["code"] == "internal_error"
    assert "secret-value-123" not in response.text


def test_a_run_cancelled_during_the_request_is_409_run_terminal(tmp_path):
    gateway = healthy_gateway(tmp_path)

    async def cancelled(*args, **kwargs):
        return RunOutcome(run_id="run_x", state=RunState.CANCELLED)

    gateway.orchestrator.run = cancelled
    response = chat(make_app(gateway))
    assert response.status_code == 409
    assert error_of(response)["code"] == "run_terminal"
    assert response.headers[RUN_ID_HEADER] == "run_x"


# -- the client-owned seam (executed by issue #24) -----------------------------------------


TOOLS = [
    {
        "type": "function",
        "function": {"name": "get_weather", "parameters": {"type": "object", "properties": {}}},
    }
]


def test_a_client_tool_call_is_returned_to_the_caller(tmp_path):
    backend = ClientBackend(tool_result())
    gateway = healthy_gateway(tmp_path)
    app = make_app(gateway, client_tools=backend)
    response = chat(app, model=CLIENT_TOOLS, tools=TOOLS, prompt="Weather in Oslo?")
    assert response.status_code == 200
    assert response.headers[RUN_ID_HEADER] == "run_client"
    choice = response.json()["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["content"] is None
    assert choice["message"]["tool_calls"] == [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "get_weather", "arguments": json.dumps({"city": "Oslo"})},
        }
    ]
    assert response.json()["model"] == CLIENT_TOOLS
    (request, mode) = backend.seen[0]
    assert (request.alias, mode) == (CLIENT_TOOLS, ExecutionMode.AUTO)
    assert [t.name for t in request.tools] == ["get_weather"]
    # The managed orchestrator was never asked, and nothing was executed here.
    assert gateway.adapter.requests == []


def test_client_tool_calls_stream_as_deltas(tmp_path):
    app = make_app(healthy_gateway(tmp_path), client_tools=ClientBackend(tool_result()))
    response = chat(app, model=CLIENT_TOOLS, tools=TOOLS, stream=True)
    events = sse_events(response)
    chunks = events[:-1]
    assert events[-1] == "[DONE]"
    assert chunks[0]["choices"][0]["delta"] == {"role": "assistant", "content": None}
    (call_chunk,) = [c for c in chunks if "tool_calls" in c["choices"][0]["delta"]]
    assert call_chunk["choices"][0]["delta"]["tool_calls"] == [
        {
            "index": 0,
            "id": "call_1",
            "type": "function",
            "function": {"name": "get_weather", "arguments": json.dumps({"city": "Oslo"})},
        }
    ]
    assert chunks[-1]["choices"][0]["finish_reason"] == "tool_calls"
    assert all("content" not in c["choices"][0]["delta"] for c in chunks[1:])
