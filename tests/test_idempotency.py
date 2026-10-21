"""Idempotency keys for execution-creating requests (RM17, RM21, AC11, #26).

Spec 0.18 section 7.4. Two POSTs create work: POST /relay/v1/runs and
POST /v1/chat/completions. A key is stored with the run it creates, so
a retry under the same key and payload never creates new work, a
changed payload is 409, and a chat duplicate is answered from the
stored run. Deterministic: the real orchestrator runs over model
doubles, and "no new work" is proved by the doubles seeing no request
and the store holding one run.
"""

import asyncio
from datetime import timedelta

import httpx
import pytest

from api_helpers import RUN_ID_HEADER, call, chat, healthy_gateway, make_app, sse_events, token_of
from gateway_helpers import STAGING_OK, build_gateway, health_tail, route_turn, text_turn, tool_turn
from helpers import FakeClock
from native_helpers import error_of, gated_gateway, session, settled, start_run
from relaymind.api.chat import ClientToolsChat
from relaymind.api.chat_request import CLIENT_TOOLS
from relaymind.redaction import Redactor
from relaymind.store import IdempotencyClaim, IdempotencyKeyTakenError, RunStore

KEY = {"Idempotency-Key": "key-1"}
REPLAYED = "idempotent-replayed"
WEATHER = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
    },
}


def run_ids(store) -> list[str]:
    return [row["run_id"] for row in store._conn.execute("SELECT run_id FROM runs")]


def run_count(gateway) -> int:
    return len(run_ids(gateway.store))


def accepted(store, caller="alice", key="k", request_hash="h1"):
    return store.create_run("accepted", idempotency=IdempotencyClaim(caller, key, request_hash))


# -- the store ----------------------------------------------------------------------


def test_a_key_is_stored_with_its_run_and_found_by_caller_and_key():
    """RM17, section 5: the key is part of the run's creation."""
    store = RunStore()
    run = accepted(store)
    record = store.find_idempotency_key("alice", "k")
    assert (record.run_id, record.request_hash) == (run.run_id, "h1")
    assert store.find_idempotency_key("bob", "k") is None
    assert store.find_idempotency_key("alice", "other") is None


def test_a_taken_key_creates_no_second_run():
    """RM17: the unique key and the run are one transaction; the loser leaves nothing."""
    store = RunStore()
    first = accepted(store)
    with pytest.raises(IdempotencyKeyTakenError):
        accepted(store, request_hash="h2")
    assert run_ids(store) == [first.run_id]
    # Another caller may use the same text.
    assert accepted(store, caller="bob").run_id != first.run_id


def test_a_key_leaves_with_its_run_at_retention_and_on_deletion():
    """Section 5, 7.4: keys stay valid for the seven-day retention window, then go."""
    clock = FakeClock()
    store = RunStore(clock=clock)
    old = accepted(store, key="old")
    deleted = accepted(store, key="deleted")
    clock.advance(timedelta(days=7).total_seconds())
    fresh = accepted(store, key="fresh")
    assert store.purge_expired() == sorted([old.run_id, deleted.run_id])
    assert store.find_idempotency_key("alice", "old") is None
    assert store.find_idempotency_key("alice", "fresh").run_id == fresh.run_id
    store.delete_run(fresh.run_id)
    assert store.find_idempotency_key("alice", "fresh") is None


# -- the key header -------------------------------------------------------------------


@pytest.mark.parametrize("value", ["", "x" * 256, "café".encode(), "has\ttab"])
def test_a_malformed_key_is_invalid_request_on_both_endpoints(tmp_path, value):
    """Spec 0.18 section 7.1: 1 to 255 printable ASCII characters."""
    gateway = healthy_gateway(tmp_path)
    app = make_app(gateway)
    headers = {"Idempotency-Key": value}
    body = {"messages": [{"role": "user", "content": "hi"}]}
    native = call(app, "POST", "/relay/v1/runs", json_body=body, headers=headers)
    assert (native.status_code, error_of(native)["code"]) == (400, "invalid_request")
    response = chat(app, headers=headers)
    assert (response.status_code, error_of(response)["code"]) == (400, "invalid_request")
    assert gateway.adapter.requests == [] and run_count(gateway) == 0


def test_a_key_sent_twice_is_invalid_request(tmp_path):
    gateway = healthy_gateway(tmp_path)
    app = make_app(gateway)
    body = {"model": "relaymind", "messages": [{"role": "user", "content": "hi"}]}

    async def go():
        transport = httpx.ASGITransport(app=app)
        headers = httpx.Headers(
            [
                ("Authorization", f"Bearer {token_of(app)}"),
                ("Idempotency-Key", "a"),
                ("Idempotency-Key", "b"),
            ]
        )
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            return await client.post("/v1/chat/completions", json=body, headers=headers)

    response = asyncio.run(go())
    assert (response.status_code, error_of(response)["code"]) == (400, "invalid_request")
    assert run_count(gateway) == 0


# -- native creation ------------------------------------------------------------------------


def test_a_duplicate_native_creation_returns_the_existing_run_and_no_new_inference(tmp_path):
    """AC11, RM21: same key and payload; the existing run reference, no new work."""
    gateway = healthy_gateway(tmp_path)
    app = make_app(gateway)

    async def go():
        async with session(app) as client:
            first = await start_run(client, headers=KEY)
            view = await settled(client, first.json()["run_id"])
            calls_before = len(gateway.adapter.requests)
            again = await start_run(client, headers=KEY)
            return first, view, calls_before, again

    first, view, calls_before, again = asyncio.run(go())
    assert first.status_code == 202 and REPLAYED not in first.headers
    assert again.status_code == 202 and again.headers[REPLAYED] == "true"
    body = again.json()
    assert body["run_id"] == first.json()["run_id"]
    assert body["state"] == "completed" and body["state_version"] == view["state_version"]
    assert body["events_url"] == first.json()["events_url"]
    assert len(gateway.adapter.requests) == calls_before
    assert run_count(gateway) == 1


def test_a_duplicate_while_the_run_is_active_is_not_429(tmp_path):
    """Section 7.4: the duplicate is answered from the key before the slot is checked."""
    gateway, adapter = gated_gateway(tmp_path)
    app = make_app(gateway)

    async def go():
        async with session(app) as client:
            first = await start_run(client, headers=KEY)
            await adapter.started.wait()
            again = await start_run(client, headers=KEY)
            other = await start_run(client)
            adapter.gate.set()
            await settled(client, first.json()["run_id"])
            return first, again, other

    first, again, other = asyncio.run(go())
    assert again.status_code == 202 and again.json()["run_id"] == first.json()["run_id"]
    assert other.status_code == 429
    assert run_count(gateway) == 1


def test_a_changed_native_payload_under_the_same_key_is_409(tmp_path):
    """AC11, RM21: a changed payload is idempotency_conflict and creates no run."""
    gateway = healthy_gateway(tmp_path)
    app = make_app(gateway)

    async def go():
        async with session(app) as client:
            first = await start_run(client, headers=KEY)
            await settled(client, first.json()["run_id"])
            changed = await start_run(client, "Is staging-db healthy?", headers=KEY)
            mode = await start_run(client, execution_mode="local_only", headers=KEY)
            return changed, mode

    for response in asyncio.run(go()):
        assert response.status_code == 409
        error = error_of(response)
        assert (error["code"], error["run_id"]) == ("idempotency_conflict", None)
    assert run_count(gateway) == 1


def test_json_whitespace_and_key_order_do_not_change_the_payload(tmp_path):
    """Section 7.4: the hash is over the decoded body, not its bytes."""
    gateway = healthy_gateway(tmp_path)
    app = make_app(gateway)
    one = '{"messages": [{"role": "user", "content": "hi"}], "execution_mode": "auto"}'
    two = '{"execution_mode":"auto","messages":[{"content":"hi","role":"user"}]}'

    async def go():
        async with session(app) as client:
            first = await client.post("/relay/v1/runs", content=one, headers=KEY)
            second = await client.post("/relay/v1/runs", content=two, headers=KEY)
            await settled(client, first.json()["run_id"])
            return first, second

    first, second = asyncio.run(go())
    assert second.status_code == 202 and second.json()["run_id"] == first.json()["run_id"]


def test_a_refused_request_stores_no_key(tmp_path):
    """Section 7.4: a request refused before a run exists can be sent again under its key."""
    gateway = healthy_gateway(tmp_path)
    app = make_app(gateway)

    async def go():
        async with session(app) as client:
            refused = await client.post("/relay/v1/runs", json={"messages": []}, headers=KEY)
            retried = await start_run(client, headers=KEY)
            await settled(client, retried.json()["run_id"])
            return refused, retried

    refused, retried = asyncio.run(go())
    assert refused.status_code == 400 and retried.status_code == 202
    assert REPLAYED not in retried.headers


def test_a_key_belongs_to_one_endpoint_payload(tmp_path):
    """Section 7.4: the endpoint is part of the request hash."""
    gateway = healthy_gateway(tmp_path)
    app = make_app(gateway)
    first = chat(app, headers=KEY)
    assert first.status_code == 200

    async def go():
        async with session(app) as client:
            return await start_run(client, headers=KEY)

    native = asyncio.run(go())
    assert (native.status_code, error_of(native)["code"]) == (409, "idempotency_conflict")
    assert run_count(gateway) == 1


# -- chat ---------------------------------------------------------------------------------------


def test_a_chat_duplicate_returns_the_stored_completed_response(tmp_path):
    """AC11, RM17: same body, run, and id; the model double sees nothing new."""
    gateway = healthy_gateway(tmp_path)
    app = make_app(gateway)
    first = chat(app, headers=KEY)
    calls_before = len(gateway.adapter.requests)
    again = chat(app, headers=KEY)
    assert first.status_code == again.status_code == 200
    # created is the time the run ended, so replays of one response are equal.
    third = chat(app, headers=KEY)
    assert again.json() == third.json()
    assert {**again.json(), "created": 0} == {**first.json(), "created": 0}
    assert again.json()["choices"][0]["message"]["content"] == STAGING_OK
    assert again.headers[RUN_ID_HEADER] == first.headers[RUN_ID_HEADER]
    assert again.headers[REPLAYED] == "true" and REPLAYED not in first.headers
    assert len(gateway.adapter.requests) == calls_before
    assert run_count(gateway) == 1


def test_a_streaming_chat_duplicate_replays_compatible_chunks(tmp_path):
    """AC11, RM20: the stored answer.delta texts, in order, with the same framing."""
    half = len(STAGING_OK) // 2
    turns = [
        route_turn("check-service-health"),
        tool_turn("get_service_health", service="staging-api"),
    ]
    turns += [text_turn(STAGING_OK[:half], STAGING_OK[half:]), text_turn(STAGING_OK)]
    gateway = build_gateway(tmp_path, turns, external_turns=[])
    app = make_app(gateway)
    first = chat(app, stream=True, headers=KEY)
    calls_before = len(gateway.adapter.requests)
    again = chat(app, stream=True, headers=KEY)
    assert again.status_code == 200 and again.headers[RUN_ID_HEADER] == first.headers[RUN_ID_HEADER]
    events = sse_events(again)

    def framing(events):
        return [e if e == "[DONE]" else {**e, "created": 0} for e in events]

    # created is the time the run ended, so replays of one response are equal.
    assert events == sse_events(chat(app, stream=True, headers=KEY))
    assert framing(events) == framing(sse_events(first))
    assert events[-1] == "[DONE]"
    assert events[0]["choices"][0]["delta"] == {"role": "assistant", "content": ""}
    assert events[-2]["choices"][0]["finish_reason"] == "stop"
    pieces = [e["choices"][0]["delta"].get("content") for e in events[1:-2]]
    assert pieces and all(pieces)
    assert len(gateway.adapter.requests) == calls_before and run_count(gateway) == 1


def test_a_changed_chat_payload_under_the_same_key_is_409(tmp_path):
    """AC11: a different prompt, stream flag, or mode is a changed payload."""
    gateway = healthy_gateway(tmp_path)
    app = make_app(gateway)
    assert chat(app, headers=KEY).status_code == 200
    for changed in (
        chat(app, prompt="Is staging-db healthy?", headers=KEY),
        chat(app, stream=True, headers=KEY),
        chat(app, headers={**KEY, "X-RelayMind-Execution-Mode": "local_only"}),
    ):
        assert changed.status_code == 409
        error = error_of(changed)
        assert (error["code"], error["run_id"]) == ("idempotency_conflict", None)
    assert run_count(gateway) == 1


def test_an_unfinished_chat_duplicate_is_409_with_its_run_id(tmp_path):
    """Section 7.4: the duplicate of a run still in flight names that run."""
    gateway, adapter = gated_gateway(tmp_path)
    app = make_app(gateway)
    body = {
        "model": "relaymind",
        "messages": [{"role": "user", "content": "Is staging-api healthy?"}],
    }

    async def go():
        async with session(app) as client:
            original = asyncio.ensure_future(
                client.post("/v1/chat/completions", json=body, headers=KEY)
            )
            await adapter.started.wait()
            duplicate = await client.post("/v1/chat/completions", json=body, headers=KEY)
            adapter.gate.set()
            return await original, duplicate

    original, duplicate = asyncio.run(go())
    assert original.status_code == 200
    assert duplicate.status_code == 409
    error = error_of(duplicate)
    assert error["code"] == "idempotency_conflict"
    assert error["run_id"] == original.headers[RUN_ID_HEADER]
    assert duplicate.headers[RUN_ID_HEADER] == error["run_id"]
    assert run_count(gateway) == 1


def test_a_chat_duplicate_of_a_failed_run_is_run_terminal_and_starts_nothing(tmp_path):
    """RM17: a retry after a failure is a new attempt under a new key."""
    gateway = build_gateway(tmp_path, [route_turn("investigate-performance")], external_turns=[])
    app = make_app(gateway)
    first = chat(
        app,
        prompt="Why is staging-api slow?",
        model="relaymind",
        headers={**KEY, "X-RelayMind-Execution-Mode": "local_only"},
    )
    assert first.status_code == 422
    calls_before = len(gateway.adapter.requests)
    again = chat(
        app,
        prompt="Why is staging-api slow?",
        headers={**KEY, "X-RelayMind-Execution-Mode": "local_only"},
    )
    assert again.status_code == 409
    error = error_of(again)
    assert (error["code"], error["run_id"]) == ("run_terminal", first.headers[RUN_ID_HEADER])
    assert len(gateway.adapter.requests) == calls_before and run_count(gateway) == 1


def test_a_client_owned_duplicate_replays_the_stored_tool_calls(tmp_path):
    """AC10, AC11: the calls the caller executes are replayed, and nothing is generated."""
    gateway = build_gateway(tmp_path, [tool_turn("get_weather", call_id="call_w1", city="Oslo")])
    app = make_app(gateway, client_tools=ClientToolsChat(gateway.orchestrator))
    fields = {"model": CLIENT_TOOLS, "tools": [WEATHER], "prompt": "Weather in Oslo?"}
    first = chat(app, headers=KEY, **fields)
    again = chat(app, headers=KEY, **fields)
    assert first.status_code == again.status_code == 200
    message = again.json()["choices"][0]
    assert message == first.json()["choices"][0]
    assert again.json()["id"] == first.json()["id"]
    assert message["finish_reason"] == "tool_calls"
    assert message["message"]["tool_calls"][0]["id"] == "call_w1"
    assert len(gateway.adapter.requests) == 1 and run_count(gateway) == 1


def test_redacted_client_tool_arguments_are_not_replayed_as_the_original(tmp_path):
    """RM12: stored arguments that were redacted differ from the first response; fail closed."""
    gateway = build_gateway(
        tmp_path,
        [tool_turn("get_weather", call_id="call_w1", city="Oslo", api_key="sk-1")],
        redactor=Redactor(secret_fields={"api_key"}),
    )
    app = make_app(gateway, client_tools=ClientToolsChat(gateway.orchestrator))
    fields = {"model": CLIENT_TOOLS, "tools": [WEATHER], "prompt": "Weather in Oslo?"}
    assert chat(app, headers=KEY, **fields).status_code == 200
    again = chat(app, headers=KEY, **fields)
    assert (again.status_code, error_of(again)["code"]) == (409, "idempotency_conflict")
    assert "sk-1" not in again.text


def test_an_expired_key_starts_a_new_run(tmp_path):
    """Section 5, 7.4: after the seven-day window the key is unknown, with no purge run."""
    gateway = healthy_gateway(tmp_path)
    app = make_app(gateway)
    first = chat(app, headers=KEY)
    gateway.clock.advance(timedelta(days=7, seconds=1).total_seconds())
    gateway.adapter._turns.extend([route_turn("check-service-health"), *health_tail()])
    second = chat(app, headers=KEY)
    assert second.status_code == 200
    assert second.headers[RUN_ID_HEADER] != first.headers[RUN_ID_HEADER]


def test_a_key_taken_between_the_lookup_and_the_run_is_409(tmp_path):
    """RM17: if two requests race past the lookup, the unique key lets only one create work.

    Here every lookup misses, so the key that blocked the run cannot be
    read back; the request is then refused as a conflict.
    """
    gateway = healthy_gateway(tmp_path)
    app = make_app(gateway)
    assert chat(app, headers=KEY).status_code == 200
    real = gateway.store.find_idempotency_key
    gateway.store.find_idempotency_key = lambda caller, key: None  # the lookup loses the race
    try:
        raced = chat(app, headers=KEY)
    finally:
        gateway.store.find_idempotency_key = real
    assert (raced.status_code, error_of(raced)["code"]) == (409, "idempotency_conflict")
    assert run_count(gateway) == 1


# -- review round 1 ----------------------------------------------------------------------


def test_a_key_past_the_retention_window_is_unknown_without_a_purge():
    """Section 5, 7.4: a key is valid for seven days, whether or not a purge has run yet."""
    clock = FakeClock()
    store = RunStore(clock=clock)
    old = accepted(store)
    clock.advance(timedelta(days=7).total_seconds())
    assert store.find_idempotency_key("alice", "k") is None
    # The key is free again: a new run takes it in its creation transaction.
    fresh = accepted(store, request_hash="h2")
    assert fresh.run_id != old.run_id
    assert store.find_idempotency_key("alice", "k").run_id == fresh.run_id


def test_an_expired_native_key_starts_a_new_run(tmp_path):
    """Section 5, 7.4: after the seven-day window the native key is unknown too."""
    gateway = healthy_gateway(tmp_path)
    app = make_app(gateway)

    async def go():
        async with session(app) as client:
            first = await start_run(client, headers=KEY)
            await settled(client, first.json()["run_id"])
            gateway.clock.advance(timedelta(days=7, seconds=1).total_seconds())
            gateway.adapter._turns.extend([route_turn("check-service-health"), *health_tail()])
            second = await start_run(client, headers=KEY)
            await settled(client, second.json()["run_id"])
            return first, second

    first, second = asyncio.run(go())
    assert second.status_code == 202 and REPLAYED not in second.headers
    assert second.json()["run_id"] != first.json()["run_id"]


def test_a_native_duplicate_of_an_expired_wait_shows_it_ended(tmp_path):
    """Section 5, 7.4: a duplicate never shows a run waiting past its limit."""
    gateway = healthy_gateway(tmp_path)
    app = make_app(gateway)

    async def go():
        async with session(app) as client:
            first = await start_run(client, "Is the service healthy?", headers=KEY)
            waiting = await settled(client, first.json()["run_id"])
            gateway.clock.advance(gateway.orchestrator.budgets.waiting_seconds + 1)
            again = await start_run(client, "Is the service healthy?", headers=KEY)
            return waiting, again

    waiting, again = asyncio.run(go())
    assert waiting["state"] == "waiting_input"
    assert again.status_code == 202 and again.json()["run_id"] == waiting["run_id"]
    assert again.json()["state"] == "failed"
    record = gateway.store.get_run(waiting["run_id"])
    assert (record.state, record.error_code) == ("failed", "input_expired")
    assert again.json()["state_version"] == record.version


def test_a_chat_duplicate_of_an_expired_wait_is_run_terminal(tmp_path):
    """Section 5, 7.4: the expired wait ends first, so the duplicate sees a failed run."""
    gateway = healthy_gateway(tmp_path)
    app = make_app(gateway)
    first = chat(app, prompt="Is the service healthy?", headers=KEY)
    assert first.status_code == 200
    run_id = first.headers[RUN_ID_HEADER]
    assert gateway.store.get_run(run_id).state == "waiting_input"
    gateway.clock.advance(gateway.orchestrator.budgets.waiting_seconds + 1)
    again = chat(app, prompt="Is the service healthy?", headers=KEY)
    assert again.status_code == 409
    assert (error_of(again)["code"], error_of(again)["run_id"]) == ("run_terminal", run_id)


def test_a_request_without_a_key_is_not_hashed(tmp_path):
    """Section 7.4: a request without the header behaves as before; nothing is hashed.

    The canonical form refuses NaN (RM10), which the JSON decoder accepts,
    so hashing a keyless body could newly refuse it.
    """
    gateway = build_gateway(tmp_path, [tool_turn("get_weather", call_id="call_w1", city="Oslo")])
    app = make_app(gateway, client_tools=ClientToolsChat(gateway.orchestrator))
    body = (
        '{"model": "relaymind-client-tools",'
        ' "messages": [{"role": "user", "content": "Weather in Oslo?"}],'
        ' "tools": [{"type": "function", "function": {"name": "get_weather", "parameters":'
        ' {"type": "object", "properties": {"city": {"type": "string"},'
        ' "days": {"type": "integer", "maximum": NaN}}}}}]}'
    )
    response = call(app, "POST", "/v1/chat/completions", content=body)
    assert response.status_code == 200, response.text


def test_replayed_tool_call_arguments_are_the_same_text(tmp_path):
    """AC10, AC11: function.arguments of a replay is the exact string first sent."""
    gateway = build_gateway(
        tmp_path, [tool_turn("get_weather", call_id="call_w1", units="metric", city="Oslo")]
    )
    app = make_app(gateway, client_tools=ClientToolsChat(gateway.orchestrator))
    fields = {"model": CLIENT_TOOLS, "tools": [WEATHER], "prompt": "Weather in Oslo?"}
    first = chat(app, headers=KEY, **fields)
    again = chat(app, headers=KEY, **fields)

    def arguments(response):
        return response.json()["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]

    assert again.headers[REPLAYED] == "true"
    assert arguments(again) == arguments(first)


def lose_the_lookup_once(store):
    """The first lookup misses, as if another request stored the key just after it."""
    real = store.find_idempotency_key
    missed = []

    def lookup(caller, key):
        if not missed:
            missed.append(key)
            return None
        return real(caller, key)

    store.find_idempotency_key = lookup


def test_a_same_payload_race_on_chat_is_answered_as_a_duplicate(tmp_path):
    """RM17, section 7.4: a key taken after the lookup is a duplicate, not a changed payload."""
    gateway = healthy_gateway(tmp_path)
    app = make_app(gateway)
    first = chat(app, headers=KEY)
    lose_the_lookup_once(gateway.store)
    raced = chat(app, headers=KEY)
    assert raced.status_code == 200 and raced.headers[REPLAYED] == "true"
    assert raced.headers[RUN_ID_HEADER] == first.headers[RUN_ID_HEADER]
    assert run_count(gateway) == 1


def test_a_same_payload_race_on_native_creation_returns_the_existing_run(tmp_path):
    """RM17, RM21, section 7.4: the raced creation answers with the run that took the key."""
    gateway = healthy_gateway(tmp_path)
    app = make_app(gateway)

    async def go():
        async with session(app) as client:
            first = await start_run(client, headers=KEY)
            await settled(client, first.json()["run_id"])
            lose_the_lookup_once(gateway.store)
            return first, await start_run(client, headers=KEY)

    first, raced = asyncio.run(go())
    assert raced.status_code == 202 and raced.headers[REPLAYED] == "true"
    assert raced.json()["run_id"] == first.json()["run_id"]
    assert run_count(gateway) == 1


def test_a_changed_payload_race_is_409_with_no_run_id(tmp_path):
    """RM17, section 7.4: a key taken after the lookup by another payload is a conflict."""
    gateway = healthy_gateway(tmp_path)
    app = make_app(gateway)
    assert chat(app, headers=KEY).status_code == 200
    lose_the_lookup_once(gateway.store)
    raced = chat(app, prompt="Is staging-db healthy?", headers=KEY)
    assert raced.status_code == 409
    assert (error_of(raced)["code"], error_of(raced)["run_id"]) == ("idempotency_conflict", None)
    assert run_count(gateway) == 1
