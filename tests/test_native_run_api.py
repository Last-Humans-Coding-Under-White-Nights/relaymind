"""The native run API: runs, SSE events, resume, cancel, health (RM21, AC11, #25).

Spec 0.17 section 7.4. The app runs the real orchestrator over model
doubles; the client calls it in process inside one event loop, because
a native run continues in the background after its 202. One test uses a
real loopback socket to prove that an SSE stream stays open across a
wait. Idempotency keys are issue #26 and in-flight writes are issue
#29; neither is covered here.
"""

import asyncio
import threading

import httpx
import pytest

from api_helpers import RunningServer, healthy_gateway, make_app, token_of
from gateway_helpers import (
    STAGING_OK,
    build_gateway,
    executed_tools,
    health_tail,
    route_turn,
    text_turn,
    tool_turn,
)
from helpers import of_type
from native_helpers import (
    GatedAdapter,
    error_of,
    parse_sse,
    run_to_rest,
    session,
    settled,
    start_run,
)
from relaymind.adapters import AdapterHealth
from relaymind.api.app import ChatSettings
from relaymind.api.native import NativeSettings, event_stream
from relaymind.orchestrator.local_loop import ConfiguredModel
from relaymind.orchestrator.route_decision import ExecutionMode
from relaymind.orchestrator.run_state import TERMINAL_STATES
from relaymind.tools import CallerContext
from relaymind.tools.sandbox import MUTATE_PERMISSION, READ_PERMISSION

ASK_SERVICE_PROMPT = "Is the service healthy?"


def gated_gateway(tmp_path):
    """A health run whose local model waits for adapter.gate, so the run stays in flight."""
    adapter = GatedAdapter(turns=[route_turn("check-service-health"), *health_tail()])
    gateway = build_gateway(
        tmp_path,
        None,
        local_model=ConfiguredModel(adapter=adapter, model_id="gated"),
        external_turns=[],
    )
    return gateway, adapter


def clarifying_gateway(tmp_path, **options):
    """A run that asks which service, then answers once it is named."""
    turns = [route_turn("check-service-health"), *health_tail()]
    return build_gateway(tmp_path, turns, external_turns=[], **options)


# -- POST /relay/v1/runs ---------------------------------------------------------


def test_creating_a_run_returns_202_with_its_reference_and_the_run_completes(tmp_path):
    """RM21: 202 with run_id, state_version, and events_url; the work continues after it."""
    gateway = healthy_gateway(tmp_path)
    app = make_app(gateway)

    async def go():
        async with session(app) as client:
            response = await start_run(client)
            assert response.status_code == 202
            body = response.json()
            run_id = body["run_id"]
            assert body["events_url"] == f"/relay/v1/runs/{run_id}/events"
            assert isinstance(body["state_version"], int)
            assert body["state"] in ("routing", "local_running", "completed")
            view = await settled(client, run_id)
            return run_id, view

    run_id, view = asyncio.run(go())
    assert view["state"] == "completed"
    assert view["output"] == STAGING_OK
    assert gateway.store.get_run(run_id).state == "completed"
    # A local run makes no external call (RM01), and the view says so.
    assert view["metrics"]["external_calls"] == 0
    assert gateway.external.requests == []


def test_a_native_run_records_no_model_alias_and_its_modes(tmp_path):
    """RM21, section 8: model_alias is null for a native run; both modes are recorded."""
    gateway = healthy_gateway(tmp_path)
    app = make_app(gateway)

    async def go():
        async with session(app) as client:
            _, view = await run_to_rest(client, execution_mode="local_only")
            return view

    view = asyncio.run(go())
    accepted = of_type(gateway.store.events(view["run_id"]), "run.accepted")[0].payload
    assert accepted["model_alias"] is None
    assert (accepted["requested_execution_mode"], accepted["effective_execution_mode"]) == (
        "local_only",
        "local_only",
    )
    assert (view["requested_execution_mode"], view["effective_execution_mode"]) == (
        "local_only",
        "local_only",
    )


@pytest.mark.parametrize(
    ("body", "code", "status"),
    [
        ("not json", "invalid_request", 400),
        ([], "invalid_request", 400),
        ({}, "invalid_request", 400),
        ({"messages": []}, "invalid_request", 400),
        ({"messages": [{"role": "assistant", "content": "hi"}]}, "invalid_request", 400),
        (
            {"messages": [{"role": "user", "content": "hi"}], "model": "relaymind"},
            "unsupported_parameter",
            400,
        ),
        (
            {"messages": [{"role": "user", "content": "hi"}], "stream": True},
            "unsupported_parameter",
            400,
        ),
        (
            {"messages": [{"role": "user", "content": "hi"}], "execution_mode": "turbo"},
            "invalid_execution_mode",
            400,
        ),
        (
            {"messages": [{"role": "user", "content": "hi"}], "execution_mode": 7},
            "invalid_execution_mode",
            400,
        ),
    ],
)
def test_an_invalid_creation_is_rejected_before_a_run_exists(tmp_path, body, code, status):
    """RM21, AC16: a rejected request has no run and its error carries run_id null."""
    gateway = healthy_gateway(tmp_path)
    app = make_app(gateway)

    async def go():
        async with session(app) as client:
            if body == "not json":
                return await client.post("/relay/v1/runs", content=b"{nope")
            return await client.post("/relay/v1/runs", json=body)

    response = asyncio.run(go())
    assert response.status_code == status
    error = error_of(response)
    assert (error["code"], error["run_id"]) == (code, None)
    assert gateway.store.unfinished_runs(()) == []
    assert gateway.adapter.requests == []


def test_every_native_endpoint_needs_the_application_token(tmp_path):
    """RM19, RM21: 401 without a valid token, before anything else."""
    app = make_app(healthy_gateway(tmp_path))
    calls = [
        ("POST", "/relay/v1/runs"),
        ("GET", "/relay/v1/runs/run_x"),
        ("GET", "/relay/v1/runs/run_x/events"),
        ("POST", "/relay/v1/runs/run_x/resume"),
        ("POST", "/relay/v1/runs/run_x/cancel"),
        ("GET", "/healthz"),
        ("GET", "/readyz"),
    ]

    async def go():
        async with session(app, auth=False) as client:
            return [await client.request(method, path, json={}) for method, path in calls]

    for response in asyncio.run(go()):
        assert response.status_code == 401
        assert error_of(response)["code"] == "unauthenticated"
        assert response.headers["www-authenticate"] == "Bearer"


def test_idempotency_keys_are_refused_until_they_are_implemented(tmp_path):
    """Spec 0.17 section 7.4: never imply deduplication that does not exist (#26)."""
    gateway = healthy_gateway(tmp_path)
    app = make_app(gateway)

    async def go():
        async with session(app) as client:
            return await client.post(
                "/relay/v1/runs",
                json={"messages": [{"role": "user", "content": "hi"}]},
                headers={"Idempotency-Key": "k1"},
            )

    response = asyncio.run(go())
    assert (response.status_code, error_of(response)["code"]) == (400, "unsupported_parameter")
    assert gateway.adapter.requests == []


def test_a_second_run_while_one_is_active_is_429_and_creates_nothing(tmp_path):
    """RM21, section 11: one active run; reject with 429, never queue."""
    gateway, adapter = gated_gateway(tmp_path)
    app = make_app(gateway)

    async def go():
        async with session(app) as client:
            first = await start_run(client)
            await adapter.started.wait()
            second = await start_run(client)
            adapter.gate.set()
            await settled(client, first.json()["run_id"])
            return second

    second = asyncio.run(go())
    assert (second.status_code, error_of(second)["code"]) == (429, "overloaded")
    # Only the first run exists; the rejected one left no record.
    assert len(gateway.store.unfinished_runs(())) == 1


# -- operator policy over execution modes ----------------------------------------


def test_a_mode_the_operator_forbids_is_403_and_creates_no_run(tmp_path):
    """RM21: execution mode is constrained by operator policy; never downgraded."""
    gateway = healthy_gateway(tmp_path)
    app = make_app(
        gateway, settings=ChatSettings(allowed_modes=frozenset({ExecutionMode.LOCAL_ONLY}))
    )

    async def go():
        async with session(app) as client:
            forbidden = await start_run(client, execution_mode="external_only")
            absent = await start_run(client)
            allowed = await start_run(client, execution_mode="local_only")
            return forbidden, absent, allowed

    forbidden, absent, allowed = asyncio.run(go())
    assert (forbidden.status_code, error_of(forbidden)["code"]) == (403, "execution_mode_forbidden")
    # An absent mode is auto, and auto is not permitted here either.
    assert (absent.status_code, error_of(absent)["code"]) == (403, "execution_mode_forbidden")
    assert allowed.status_code == 202


def test_local_only_never_calls_externally_and_reports_needs_external(tmp_path):
    """RM21, RM01: local_only that cannot complete locally fails as needs_external."""
    gateway = build_gateway(
        tmp_path,
        [route_turn("investigate-performance")],
        external_turns=[text_turn("An external answer.")],
    )
    app = make_app(gateway)

    async def go():
        async with session(app) as client:
            return await run_to_rest(
                client, "Why is staging-api slow?", execution_mode="local_only"
            )

    _, view = asyncio.run(go())
    assert view["state"] == "failed"
    assert (view["error_code"], view["output"]) == ("needs_external", None)
    assert view["reason_code"] is not None
    assert gateway.external.requests == []
    assert view["metrics"]["external_calls"] == 0


def test_external_only_bypasses_local_inference(tmp_path):
    """RM21, section 7.4: external_only runs no local generation."""
    gateway = build_gateway(
        tmp_path,
        [],
        external_turns=[
            tool_turn("get_service_health", service="staging-api"),
            text_turn(STAGING_OK),
        ],
    )
    app = make_app(gateway)

    async def go():
        async with session(app) as client:
            return await run_to_rest(
                client, "Is staging-api healthy right now?", execution_mode="external_only"
            )

    _, view = asyncio.run(go())
    assert view["state"] == "completed"
    assert view["output"] == STAGING_OK
    assert gateway.adapter.requests == []
    assert view["metrics"]["external_calls"] >= 1


def test_parallel_demo_fails_explicitly_when_its_workflow_is_unavailable(tmp_path):
    """RM21, RM15: parallel_demo without an enabled workflow ends as workflow_unavailable."""
    gateway = build_gateway(tmp_path, [], external_turns=[])
    app = make_app(gateway)

    async def go():
        async with session(app) as client:
            return await run_to_rest(
                client, "Run the health checklist.", execution_mode="parallel_demo"
            )

    _, view = asyncio.run(go())
    assert view["state"] == "failed"
    assert (view["error_code"], view["reason_code"]) == (
        "workflow_unavailable",
        "parallel_workflow",
    )
    assert gateway.adapter.requests == [] and gateway.external.requests == []


# -- GET /relay/v1/runs/{id} -----------------------------------------------------


def test_an_unknown_run_is_404_on_every_run_endpoint(tmp_path):
    """RM21, section 11: run_not_found for a run that does not exist."""
    app = make_app(healthy_gateway(tmp_path))

    async def go():
        async with session(app) as client:
            return [
                await client.get("/relay/v1/runs/run_nope"),
                await client.get("/relay/v1/runs/run_nope/events"),
                await client.post(
                    "/relay/v1/runs/run_nope/resume", json={"expected_version": 1, "input": "x"}
                ),
                await client.post("/relay/v1/runs/run_nope/cancel", json={"expected_version": 1}),
            ]

    for response in asyncio.run(go()):
        assert response.status_code == 404
        assert error_of(response) == {
            "code": "run_not_found",
            "message": error_of(response)["message"],
            "run_id": "run_nope",
        }


def test_a_waiting_run_shows_its_question_and_a_failed_run_its_codes(tmp_path):
    """RM21: the view carries the pending question, metrics, and the version."""
    gateway = build_gateway(tmp_path, [route_turn("check-service-health")], external_turns=[])
    app = make_app(gateway)

    async def go():
        async with session(app) as client:
            return await run_to_rest(client, ASK_SERVICE_PROMPT)

    _, view = asyncio.run(go())
    assert view["state"] == "waiting_input"
    assert view["state_version"] == gateway.store.get_run(view["run_id"]).version
    question = view["pending_question"]
    assert question["question"].startswith("Which service do you mean?")
    assert (question["missing"], question["skill_id"]) == ("service", "check-service-health")
    assert view["pending_approval"] is None and view["output"] is None
    assert view["metrics"]["active_ms"] >= 0 and view["metrics"]["waiting_ms"] >= 0


# -- GET /relay/v1/runs/{id}/events (AC11) ---------------------------------------


def test_events_stream_in_order_with_sequence_ids_and_end_at_the_terminal_event(tmp_path):
    """RM21, AC11: SSE with increasing sequence IDs, one message per stored event."""
    gateway = healthy_gateway(tmp_path)
    app = make_app(gateway)

    async def go():
        async with session(app) as client:
            accepted, _ = await run_to_rest(client)
            return accepted, await client.get(accepted["events_url"])

    accepted, response = asyncio.run(go())
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    messages = parse_sse(response.text)
    stored = gateway.store.events(accepted["run_id"])
    assert [m["id"] for m in messages] == [e.seq for e in stored]
    assert [m["id"] for m in messages] == sorted(set(m["id"] for m in messages))
    assert [m["event"] for m in messages] == [e.event_type.value for e in stored]
    assert [m["data"] for m in messages] == [e.to_json_object() for e in stored]
    assert messages[0]["event"] == "run.accepted"
    assert messages[-1]["event"] == "run.completed"


def test_last_event_id_replays_later_events_without_rerunning_work(tmp_path):
    """AC11: a reconnect replays retained later events; no model call, tool, or write."""
    gateway = healthy_gateway(tmp_path)
    app = make_app(gateway)

    async def go():
        async with session(app) as client:
            accepted, _ = await run_to_rest(client)
            full = parse_sse((await client.get(accepted["events_url"])).text)
            calls_before = (
                len(gateway.adapter.requests),
                len(gateway.store.tool_operations(accepted["run_id"])),
                len(gateway.store.events(accepted["run_id"])),
            )
            cut = full[3]["id"]
            replay = await client.get(accepted["events_url"], headers={"Last-Event-ID": str(cut)})
            beyond = await client.get(
                accepted["events_url"], headers={"Last-Event-ID": str(full[-1]["id"])}
            )
            calls_after = (
                len(gateway.adapter.requests),
                len(gateway.store.tool_operations(accepted["run_id"])),
                len(gateway.store.events(accepted["run_id"])),
            )
            return full, cut, replay, beyond, calls_before, calls_after

    full, cut, replay, beyond, before, after = asyncio.run(go())
    assert [m["id"] for m in parse_sse(replay.text)] == [m["id"] for m in full if m["id"] > cut]
    assert parse_sse(beyond.text) == []
    assert before == after
    assert gateway.external.requests == []


@pytest.mark.parametrize("value", ["abc", "-1", "1.5", ""])
def test_a_malformed_last_event_id_is_400(tmp_path, value):
    """RM21: the cursor is validated, not guessed."""
    gateway = healthy_gateway(tmp_path)
    app = make_app(gateway)

    async def go():
        async with session(app) as client:
            accepted, _ = await run_to_rest(client)
            return await client.get(accepted["events_url"], headers={"Last-Event-ID": value})

    response = asyncio.run(go())
    assert (response.status_code, error_of(response)["code"]) == (400, "invalid_request")


def test_a_client_that_leaves_the_event_stream_does_not_stop_the_run(tmp_path):
    """RM21, section 7.4: an SSE disconnect leaves the run available for reconnection."""
    gateway, adapter = gated_gateway(tmp_path)
    app = make_app(gateway)

    async def go():
        async with session(app) as client:
            accepted = (await start_run(client)).json()
            await adapter.started.wait()
            # Open the stream, read until it would wait, then go away.
            stream = client.stream("GET", accepted["events_url"])
            reader = asyncio.ensure_future(stream.__aenter__())
            await asyncio.sleep(0.05)
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
            adapter.gate.set()
            view = await settled(client, accepted["run_id"])
            replay = await client.get(accepted["events_url"])
            return view, replay

    view, replay = asyncio.run(go())
    assert view["state"] == "completed"
    assert parse_sse(replay.text)[-1]["event"] == "run.completed"


def test_an_event_stream_stays_open_across_a_wait_and_ends_when_the_run_does(tmp_path):
    """RM21, AC11: live tail over a real socket; one stream sees the wait and the resume."""
    gateway = clarifying_gateway(tmp_path)
    app = make_app(gateway)
    auth = {"Authorization": f"Bearer {token_of(app)}"}
    seen: list[dict] = []
    first_wait = threading.Event()
    failure: list[BaseException] = []

    with RunningServer(app) as server:
        base = f"http://{server.address[0]}:{server.address[1]}"
        with httpx.Client(base_url=base, headers=auth, timeout=10) as client:
            accepted = client.post(
                "/relay/v1/runs",
                json={"messages": [{"role": "user", "content": ASK_SERVICE_PROMPT}]},
            ).json()

            def read():
                try:
                    with client.stream("GET", accepted["events_url"]) as stream:
                        block = []
                        for line in stream.iter_lines():
                            if line:
                                block.append(line)
                                continue
                            if block and not block[0].startswith(":"):
                                seen.append(parse_sse("\n".join(block) + "\n\n")[0])
                                if seen[-1]["event"] == "clarification.required":
                                    first_wait.set()
                            block = []
                except BaseException as exc:  # reported by the test thread
                    failure.append(exc)
                    first_wait.set()

            reader = threading.Thread(target=read, daemon=True)
            reader.start()
            assert first_wait.wait(10), "the stream never showed the clarification"
            assert not failure, failure
            view = client.get(f"/relay/v1/runs/{accepted['run_id']}").json()
            assert view["state"] == "waiting_input"
            resumed = client.post(
                f"/relay/v1/runs/{accepted['run_id']}/resume",
                json={"expected_version": view["state_version"], "input": "staging-api"},
            )
            assert resumed.status_code == 202, resumed.text
            reader.join(15)
            assert not reader.is_alive(), "the stream did not end with the run"
    assert not failure, failure
    names = [m["event"] for m in seen]
    assert names.index("clarification.required") < names.index("clarification.answered")
    assert names[-1] == "run.completed"
    assert [m["id"] for m in seen] == sorted(set(m["id"] for m in seen))


def test_an_idle_stream_of_a_waiting_run_sends_a_keep_alive_and_can_be_closed(tmp_path):
    """RM21: the stream stays open while the run waits; closing it leaves the run waiting."""
    gateway = clarifying_gateway(tmp_path)
    app = make_app(gateway)

    async def go():
        async with session(app) as client:
            accepted, waiting = await run_to_rest(client, ASK_SERVICE_PROMPT)
            frames = []
            stream = event_stream(
                gateway.orchestrator,
                accepted["run_id"],
                0,
                NativeSettings(poll_seconds=0.001, heartbeat_seconds=0.0),
            )
            async for frame in stream:
                frames.append(frame)
                if frame.startswith(":"):
                    break
            await stream.aclose()
            return frames, await settled(client, accepted["run_id"])

    frames, after = asyncio.run(go())
    assert frames[-1] == ": keep-alive\n\n"
    assert [parse_sse(f)[0]["event"] for f in frames[:-1]][-1] == "clarification.required"
    assert after["state"] == "waiting_input"


# -- POST /relay/v1/runs/{id}/resume ---------------------------------------------


def test_a_resume_with_input_returns_202_and_the_run_continues(tmp_path):
    """RM21, RM05, RM17: resume with expected_version and input; the run completes."""
    gateway = clarifying_gateway(tmp_path)
    app = make_app(gateway)

    async def go():
        async with session(app) as client:
            _, waiting = await run_to_rest(client, ASK_SERVICE_PROMPT)
            run_id = waiting["run_id"]
            resumed = await client.post(
                f"/relay/v1/runs/{run_id}/resume",
                json={"expected_version": waiting["state_version"], "input": "staging-api"},
            )
            done = await settled(client, run_id)
            return waiting, resumed, done

    waiting, resumed, done = asyncio.run(go())
    assert resumed.status_code == 202
    body = resumed.json()
    assert body["run_id"] == waiting["run_id"]
    assert body["state_version"] > waiting["state_version"]
    assert body["events_url"] == f"/relay/v1/runs/{waiting['run_id']}/events"
    assert done["state"] == "completed" and done["output"] == STAGING_OK
    answered = of_type(gateway.store.events(waiting["run_id"]), "clarification.answered")
    assert [e.payload["input"] for e in answered] == ["staging-api"]


def test_a_stale_resume_is_409_and_a_blank_one_is_400_and_the_run_keeps_waiting(tmp_path):
    """RM17, section 7.4: stale version 409; blank input 400 invalid_resume_input."""
    gateway = clarifying_gateway(tmp_path)
    app = make_app(gateway)

    async def go():
        async with session(app) as client:
            _, waiting = await run_to_rest(client, ASK_SERVICE_PROMPT)
            url = f"/relay/v1/runs/{waiting['run_id']}/resume"
            version = waiting["state_version"]
            stale = await client.post(url, json={"expected_version": version - 1, "input": "x"})
            blank = await client.post(url, json={"expected_version": version, "input": "  "})
            missing = await client.post(url, json={"expected_version": version})
            approval = await client.post(
                url,
                json={
                    "expected_version": version,
                    "approval_id": "a",
                    "token": "t",
                    "approve": True,
                },
            )
            mixed = await client.post(
                url, json={"expected_version": version, "input": "x", "approve": True}
            )
            no_version = await client.post(url, json={"input": "staging-api"})
            return (
                waiting,
                [stale, blank, missing, approval, mixed, no_version],
                await settled(client, waiting["run_id"]),
            )

    waiting, responses, after = asyncio.run(go())
    stale, blank, missing, approval, mixed, no_version = responses
    assert (stale.status_code, error_of(stale)["code"]) == (409, "stale_version")
    for response in (blank, missing, approval, mixed):
        assert (response.status_code, error_of(response)["code"]) == (400, "invalid_resume_input")
    assert (no_version.status_code, error_of(no_version)["code"]) == (400, "invalid_request")
    assert after["state"] == "waiting_input"
    assert after["state_version"] == waiting["state_version"]


def test_a_resume_of_a_terminal_or_running_run_is_409(tmp_path):
    """RM17, section 11: terminal runs never restart; a running run cannot take a resume."""
    gateway, adapter = gated_gateway(tmp_path)
    app = make_app(gateway)

    async def go():
        async with session(app) as client:
            accepted = (await start_run(client)).json()
            await adapter.started.wait()
            running = await client.post(
                f"/relay/v1/runs/{accepted['run_id']}/resume",
                json={"expected_version": accepted["state_version"], "input": "x"},
            )
            adapter.gate.set()
            done = await settled(client, accepted["run_id"])
            terminal = await client.post(
                f"/relay/v1/runs/{accepted['run_id']}/resume",
                json={"expected_version": done["state_version"], "input": "x"},
            )
            return running, terminal

    running, terminal = asyncio.run(go())
    assert (running.status_code, error_of(running)["code"]) == (409, "invalid_state")
    assert (terminal.status_code, error_of(terminal)["code"]) == (409, "run_terminal")


def test_an_expired_wait_is_shown_as_failed_and_a_late_resume_is_run_state(tmp_path):
    """Section 5, RM21: reading a run ends an expired wait; no stale 'waiting' is shown."""
    gateway = clarifying_gateway(tmp_path)
    app = make_app(gateway)

    async def go():
        async with session(app) as client:
            _, waiting = await run_to_rest(client, ASK_SERVICE_PROMPT)
            gateway.clock.advance(gateway.orchestrator.budgets.waiting_seconds + 1)
            return await client.get(f"/relay/v1/runs/{waiting['run_id']}")

    view = asyncio.run(go()).json()
    assert (view["state"], view["error_code"]) == ("failed", "input_expired")


# -- confirmation over the native API --------------------------------------------

CALLER = CallerContext(permissions=frozenset({READ_PERMISSION, MUTATE_PERMISSION}))
DELETE_PROMPT = "Delete the demo-orders-db sandbox database."
DELETE = {"database_id": "demo-orders-db", "expected_revision": 3}
DELETED = "Deleted demo-orders-db."


def deleting_gateway(tmp_path, *tail):
    turns = [
        route_turn("delete-demo-database"),
        tool_turn("list_demo_databases", call_id="call_list"),
        tool_turn("delete_demo_database", call_id="call_delete", **DELETE),
        *tail,
    ]
    return build_gateway(tmp_path, turns, caller=CALLER, external_turns=[])


def database_ids(gateway):
    return [db["database_id"] for db in gateway.sandbox.list_databases()]


async def confirming(client):
    """Start the delete run and read its pending approval, with the token, once."""
    _, waiting = await run_to_rest(client, DELETE_PROMPT)
    assert waiting["state"] == "waiting_confirmation"
    return waiting


def test_the_pending_approval_and_its_token_are_shown_once(tmp_path):
    """RM11, RM21: the token is revealed to the first read and never stored or traced."""
    gateway = deleting_gateway(tmp_path)
    app = make_app(gateway)

    async def go():
        async with session(app) as client:
            first = await confirming(client)
            second = (await client.get(f"/relay/v1/runs/{first['run_id']}")).json()
            return first, second

    first, second = asyncio.run(go())
    approval = first["pending_approval"]
    assert approval["tool_name"] == "delete_demo_database"
    assert approval["target"] == "demo-orders-db"
    assert approval["state_version"] == first["state_version"]
    assert isinstance(approval["token"], str) and len(approval["token"]) >= 32
    assert second["pending_approval"]["approval_id"] == approval["approval_id"]
    assert second["pending_approval"]["token"] is None
    token = approval["token"]
    run_id = first["run_id"]
    assert token not in repr([e.to_json_object() for e in gateway.store.events(run_id)])
    assert "demo-orders-db" in database_ids(gateway)


def test_an_approval_over_the_native_api_executes_the_held_call_once(tmp_path):
    """RM11, RM21: approve with approval_id, token, and expected_version; 202, then completed."""
    gateway = deleting_gateway(tmp_path, text_turn(DELETED), text_turn(DELETED))
    app = make_app(gateway)

    async def go():
        async with session(app) as client:
            waiting = await confirming(client)
            approval = waiting["pending_approval"]
            url = f"/relay/v1/runs/{waiting['run_id']}/resume"
            body = {
                "expected_version": waiting["state_version"],
                "approval_id": approval["approval_id"],
                "token": approval["token"],
                "approve": True,
            }
            resumed = await client.post(url, json=body)
            done = await settled(client, waiting["run_id"])
            replay = await client.post(url, json=body)
            return resumed, done, replay

    resumed, done, replay = asyncio.run(go())
    assert resumed.status_code == 202
    assert done["state"] == "completed"
    assert "demo-orders-db" not in database_ids(gateway)
    events = gateway.store.events(done["run_id"])
    assert executed_tools(events).count("delete_demo_database") == 1
    # A replay of a used token cannot execute again (the run is terminal).
    assert replay.status_code == 409


def test_a_wrong_token_is_403_and_a_denial_cancels_with_user_denied(tmp_path):
    """RM11, RM21: approval_invalid keeps the run waiting; approve=false cancels it."""
    gateway = deleting_gateway(tmp_path)
    app = make_app(gateway)

    async def go():
        async with session(app) as client:
            waiting = await confirming(client)
            approval = waiting["pending_approval"]
            url = f"/relay/v1/runs/{waiting['run_id']}/resume"
            version = waiting["state_version"]
            wrong = await client.post(
                url,
                json={
                    "expected_version": version,
                    "approval_id": approval["approval_id"],
                    "token": "not-the-token",
                    "approve": True,
                },
            )
            still = await settled(client, waiting["run_id"])
            wrong_shape = await client.post(url, json={"expected_version": version, "input": "yes"})
            denied = await client.post(
                url,
                json={
                    "expected_version": version,
                    "approval_id": approval["approval_id"],
                    "token": approval["token"],
                    "approve": False,
                },
            )
            return wrong, still, wrong_shape, denied, await settled(client, waiting["run_id"])

    wrong, still, wrong_shape, denied, final = asyncio.run(go())
    assert (wrong.status_code, error_of(wrong)["code"]) == (403, "approval_invalid")
    assert still["state"] == "waiting_confirmation"
    assert (wrong_shape.status_code, error_of(wrong_shape)["code"]) == (400, "invalid_resume_input")
    assert denied.status_code == 202
    assert final["state"] == "cancelled"
    (cancelled,) = of_type(gateway.store.events(final["run_id"]), "run.cancelled")
    assert cancelled.payload["reason"] == "user_denied"
    assert "demo-orders-db" in database_ids(gateway)


# -- POST /relay/v1/runs/{id}/cancel ---------------------------------------------


def test_cancelling_a_waiting_run_cancels_it_and_voids_its_approval(tmp_path):
    """RM17, RM21: cancel a waiting run with expected_version; nothing is mutated."""
    gateway = deleting_gateway(tmp_path)
    app = make_app(gateway)

    async def go():
        async with session(app) as client:
            waiting = await confirming(client)
            url = f"/relay/v1/runs/{waiting['run_id']}/cancel"
            stale = await client.post(url, json={"expected_version": waiting["state_version"] - 1})
            missing = await client.post(url, json={})
            cancelled = await client.post(url, json={"expected_version": waiting["state_version"]})
            again = await client.post(
                url, json={"expected_version": cancelled.json()["state_version"]}
            )
            return waiting, stale, missing, cancelled, again

    waiting, stale, missing, cancelled, again = asyncio.run(go())
    assert (stale.status_code, error_of(stale)["code"]) == (409, "stale_version")
    assert (missing.status_code, error_of(missing)["code"]) == (400, "invalid_request")
    assert cancelled.status_code == 200
    view = cancelled.json()
    assert (view["state"], view["pending_approval"]) == ("cancelled", None)
    assert (again.status_code, error_of(again)["code"]) == (409, "run_terminal")
    (event,) = of_type(gateway.store.events(waiting["run_id"]), "run.cancelled")
    assert event.payload["reason"] == "cancel_requested"
    assert event.payload["approval_id"] == waiting["pending_approval"]["approval_id"]
    assert gateway.store.get_pending_approval(waiting["run_id"]) is None
    assert "demo-orders-db" in database_ids(gateway)
    assert gateway.store.active_run_id() is None


def test_cancelling_a_waiting_clarification_records_run_cancelled(tmp_path):
    """RM17, RM22: run.cancelled carries active_ms and waiting_ms."""
    gateway = clarifying_gateway(tmp_path)
    app = make_app(gateway)

    async def go():
        async with session(app) as client:
            _, waiting = await run_to_rest(client, ASK_SERVICE_PROMPT)
            gateway.clock.advance(3)
            response = await client.post(
                f"/relay/v1/runs/{waiting['run_id']}/cancel",
                json={"expected_version": waiting["state_version"]},
            )
            return waiting, response

    waiting, response = asyncio.run(go())
    assert response.json()["state"] == "cancelled"
    (event,) = of_type(gateway.store.events(waiting["run_id"]), "run.cancelled")
    assert event.payload["waiting_ms"] >= 3000
    assert event.payload["reason"] == "cancel_requested"


def test_cancelling_a_running_run_stops_it_and_stops_dispatch(tmp_path):
    """RM21, RM17: a running run is cancelled; no tool runs after the request."""
    gateway, adapter = gated_gateway(tmp_path)
    app = make_app(gateway)

    async def go():
        async with session(app) as client:
            accepted = (await start_run(client)).json()
            await adapter.started.wait()
            view = (await client.get(f"/relay/v1/runs/{accepted['run_id']}")).json()
            stale = await client.post(
                f"/relay/v1/runs/{accepted['run_id']}/cancel",
                json={"expected_version": view["state_version"] - 1},
            )
            cancelled = await client.post(
                f"/relay/v1/runs/{accepted['run_id']}/cancel",
                json={"expected_version": view["state_version"]},
            )
            return accepted, stale, cancelled

    accepted, stale, cancelled = asyncio.run(go())
    assert (stale.status_code, error_of(stale)["code"]) == (409, "stale_version")
    assert cancelled.status_code == 200
    assert cancelled.json()["state"] == "cancelled"
    events = gateway.store.events(accepted["run_id"])
    (event,) = of_type(events, "run.cancelled")
    assert event.payload["reason"] == "cancel_requested"
    assert executed_tools(events) == []
    # Dispatch stopped: the one in-flight generation was the only model call.
    assert len(of_type(events, "model.started")) == 1
    assert gateway.store.active_run_id() is None


def test_a_run_owned_by_a_chat_request_is_not_cancelled_by_the_native_endpoint(tmp_path):
    """Spec 0.17 section 7.4: a chat run is cancelled by closing its request."""
    gateway, adapter = gated_gateway(tmp_path)
    app = make_app(gateway)

    async def go():
        async with session(app) as client:
            chat = asyncio.ensure_future(
                client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "relaymind",
                        "messages": [{"role": "user", "content": "Is staging-api healthy?"}],
                    },
                )
            )
            await adapter.started.wait()
            (record,) = gateway.store.unfinished_runs(TERMINAL_STATES)
            response = await client.post(
                f"/relay/v1/runs/{record.run_id}/cancel",
                json={"expected_version": record.version},
            )
            adapter.gate.set()
            return response, await chat

    response, chat = asyncio.run(go())
    assert (response.status_code, error_of(response)["code"]) == (409, "invalid_state")
    assert chat.status_code == 200


# -- /healthz and /readyz --------------------------------------------------------


def test_healthz_reports_liveness(tmp_path):
    """RM21: process liveness."""
    app = make_app(healthy_gateway(tmp_path))

    async def go():
        async with session(app) as client:
            return await client.get("/healthz")

    response = asyncio.run(go())
    assert (response.status_code, response.json()) == (200, {"status": "ok"})


PERMITTED = ChatSettings(
    allowed_modes=frozenset(
        {ExecutionMode.AUTO, ExecutionMode.LOCAL_ONLY, ExecutionMode.EXTERNAL_ONLY}
    )
)


def readyz(app):
    async def go():
        async with session(app) as client:
            return await client.get("/readyz")

    return asyncio.run(go())


def test_readyz_reports_model_warm_state_explicitly(tmp_path):
    """RM21: readiness of each permitted mode, with explicit availability and warmth."""
    app = make_app(healthy_gateway(tmp_path), settings=PERMITTED)

    response = readyz(app)
    body = response.json()
    assert response.status_code == 200
    assert body["ready"] is True
    assert body["models"]["local"] == {"available": True, "warm": True, "detail": "scripted"}
    assert body["models"]["external"]["warm"] is True
    assert set(body["modes"]) == {"auto", "local_only", "external_only"}
    assert all(mode["ready"] for mode in body["modes"].values())


def test_a_cold_local_model_is_not_ready(tmp_path):
    """RM21, section 12: a model that is still loading is never reported as ready."""
    gateway = healthy_gateway(tmp_path)

    async def cold():
        return AdapterHealth(available=True, warm=False, detail="loading model")

    gateway.adapter.health = cold
    app = make_app(gateway, settings=PERMITTED)

    response = readyz(app)
    body = response.json()
    assert response.status_code == 503
    assert body["ready"] is False
    assert body["models"]["local"] == {"available": True, "warm": False, "detail": "loading model"}
    assert body["modes"]["auto"]["ready"] is False
    assert body["modes"]["local_only"]["ready"] is False
    assert body["modes"]["external_only"]["ready"] is True


def test_a_permitted_mode_without_its_model_is_not_ready(tmp_path):
    """RM21: external_only with no external model configured is reported, not hidden."""
    gateway = build_gateway(tmp_path, [])
    app = make_app(gateway, settings=PERMITTED)

    response = readyz(app)
    body = response.json()
    assert response.status_code == 503
    assert body["models"]["external"] is None
    assert body["modes"]["external_only"]["ready"] is False
    assert body["modes"]["auto"]["ready"] is True


def test_parallel_demo_is_not_ready_while_the_workflow_is_not_implemented(tmp_path):
    """RM21, RM15: readiness agrees with the run behavior (workflow_unavailable, #30)."""
    gateway = build_gateway(
        tmp_path,
        [],
        external_turns=[],
        enabled_workflows={"health-and-diagnostic-checklist"},
    )
    app = make_app(
        gateway, settings=ChatSettings(allowed_modes=frozenset({ExecutionMode.PARALLEL_DEMO}))
    )

    response = readyz(app)
    assert response.status_code == 503
    assert response.json()["modes"]["parallel_demo"]["ready"] is False
