"""RM09, RM19, RM20, AC09/AC10: malformed SDK responses are gateway findings."""

import copy
import json

import httpx
import pytest

from gateway_helpers import health_tail, route_turn, text_turn, tool_turn
from test_live_acceptance_script import (
    harness as harness,
)
from test_live_acceptance_script import (
    owners_of_failures,
    run_scenario,
    serve_doubles,
)


class RewriteChat(httpx.AsyncBaseTransport):
    """Corrupt the HTTP boundary while keeping the actual SDK and gateway traces."""

    def __init__(self, transport, rewrite, *, error=None):
        self.transport = transport
        self.rewrite = rewrite
        self.error = error
        self.requests = []

    async def handle_async_request(self, request):
        assert request.url.path == "/v1/chat/completions"
        body = json.loads(request.content)
        self.requests.append(body)
        turn = len(self.requests)
        if self.error is not None:
            status, code, error_turn = self.error
            if error_turn is None or error_turn == turn:
                return httpx.Response(
                    status,
                    headers={"X-RelayMind-Run-ID": f"failed_{turn}"},
                    json={"error": {"code": code, "message": "Failed", "run_id": f"failed_{turn}"}},
                )
        response = await self.transport.handle_async_request(request)
        data = await response.aread()
        if body.get("stream"):
            blocks = []
            for block in data.decode().split("\n\n"):
                if block.startswith("data: ") and block != "data: [DONE]":
                    chunk = json.loads(block[6:])
                    self.rewrite(chunk, turn)
                    block = "data: " + json.dumps(chunk)
                blocks.append(block)
            data = "\n\n".join(blocks).encode()
        else:
            parsed = json.loads(data)
            self.rewrite(parsed, turn)
            data = json.dumps(parsed).encode()
        headers = dict(response.headers)
        headers.pop("content-length", None)
        return httpx.Response(response.status_code, headers=headers, content=data)


def sdk_case(harness, tmp_path, monkeypatch, scenario, rewrite, *, error=None):
    if scenario == "ac09_sdk":
        turns = [route_turn("check-service-health"), *health_tail()] * 2
    else:
        turns = [
            tool_turn("get_service_status", call_id="call_status", service="staging-api"),
            text_turn("staging-api is healthy (checked at 2026-10-20T02:58:00Z)."),
        ]
    api = serve_doubles(harness, tmp_path, turns)
    transport = RewriteChat(api.transport, rewrite, error=error)
    api.transport = transport
    clients = []
    original = api.sdk

    def sdk(mode=None):
        client = original(mode)
        clients.append(client)
        return client

    monkeypatch.setattr(api, "sdk", sdk)
    result = run_scenario(harness, scenario, api)
    assert clients and all(client.is_closed() for client in clients)
    return result, transport.requests


@pytest.mark.parametrize("scenario", ["ac09_sdk", "ac10_client_tools"])
@pytest.mark.parametrize("turn", [1, 2])
@pytest.mark.parametrize("count", [0, 2])
def test_invalid_choice_count_is_a_gateway_finding(
    harness, tmp_path, monkeypatch, scenario, turn, count
):
    """RM19/RM20, AC09/AC10: one choice per response/chunk, with no unpack crash."""

    def rewrite(body, current):
        if current == turn:
            body["choices"] = body["choices"] * count

    result, requests = sdk_case(harness, tmp_path, monkeypatch, scenario, rewrite)
    assert result.status == "failed", result.to_json_object()
    assert {owner for owner, _ in owners_of_failures(result)} == {"gateway"}
    if scenario == "ac10_client_tools":
        assert len(requests) == turn


@pytest.mark.parametrize(
    "arguments",
    [
        "{",
        "null",
        "[]",
        '"service"',
        '{"service":"unknown"}',
        '{"service":"staging-api","extra":true}',
    ],
)
def test_invalid_exposed_arguments_are_not_sent_back_to_the_gateway(
    harness, tmp_path, monkeypatch, arguments
):
    """RM09, AC10: malformed/schema-invalid calls fail the gateway check and never execute."""

    def rewrite(body, turn):
        if turn == 1:
            body["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = arguments

    result, requests = sdk_case(harness, tmp_path, monkeypatch, "ac10_client_tools", rewrite)
    assert result.status == "failed", result.to_json_object()
    assert {owner for owner, _ in owners_of_failures(result)} == {"gateway"}
    assert len(requests) == 1
    assert "second" not in result.observations


@pytest.mark.parametrize("fault", ["blank_id", "unknown_name", "multiple_calls", "wrong_finish"])
def test_invalid_exposed_calls_are_gateway_findings(harness, tmp_path, monkeypatch, fault):
    """RM09/RM19, AC10: the caller never echoes an invalid exposed tool call."""

    def rewrite(body, turn):
        if turn != 1:
            return
        choice = body["choices"][0]
        call = choice["message"]["tool_calls"][0]
        if fault == "blank_id":
            call["id"] = " "
        elif fault == "unknown_name":
            call["function"]["name"] = "delete_demo_database"
        elif fault == "multiple_calls":
            choice["message"]["tool_calls"].append(copy.deepcopy(call))
        else:
            choice["finish_reason"] = "stop"

    result, requests = sdk_case(harness, tmp_path, monkeypatch, "ac10_client_tools", rewrite)
    assert result.status == "failed", result.to_json_object()
    assert {owner for owner, _ in owners_of_failures(result)} == {"gateway"}
    assert len(requests) == 1


@pytest.mark.parametrize("scenario", ["ac09_sdk", "ac10_client_tools"])
@pytest.mark.parametrize(
    "status,code,owner",
    [
        (422, "needs_external", "model"),
        (503, "external_failed", "model"),
        (500, "internal_error", "gateway"),
        (401, "unauthenticated", "gateway"),
        (500, "needs_external", "gateway"),
        (422, "unknown", "gateway"),
    ],
)
def test_http_error_owner_depends_on_status_and_code(
    harness, tmp_path, monkeypatch, scenario, status, code, owner
):
    """Section 11, RM19, AC09/AC10: only correctly mapped model failures are model findings."""
    result, _ = sdk_case(
        harness,
        tmp_path,
        monkeypatch,
        scenario,
        lambda *_: None,
        error=(status, code, None),
    )
    assert result.status == "failed", result.to_json_object()
    assert {kind for kind, _ in owners_of_failures(result)} == {owner}
    assert result.run_ids


@pytest.mark.parametrize(
    "status,code,owner", [(422, "needs_external", "model"), (500, "internal_error", "gateway")]
)
def test_second_client_turn_http_failure_is_recorded(
    harness, tmp_path, monkeypatch, status, code, owner
):
    """RM19, AC10: errors after the caller's valid result remain findings and close the SDK."""
    result, requests = sdk_case(
        harness,
        tmp_path,
        monkeypatch,
        "ac10_client_tools",
        lambda *_: None,
        error=(status, code, 2),
    )
    assert result.status == "failed", result.to_json_object()
    assert {kind for kind, _ in owners_of_failures(result)} == {owner}
    assert len(requests) == 2
