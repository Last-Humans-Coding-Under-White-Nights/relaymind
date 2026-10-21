"""The demo UI over HTTP: shell, mode labels, trace fragments, controls (RM23, #28).

Spec 0.20 section 9 (Demo UI) and section 7.4. The app runs the real
orchestrator over model doubles and a disposable sandbox. The page's
script is not run here; these tests check the contract it relies on:
the static shell, the rendered trace with its data attributes, and that
the native endpoints complete a round trip from exactly those values.
"""

import asyncio
import re

import pytest

from api_helpers import make_app
from gateway_helpers import (
    STAGING_OK,
    clarify_turn,
    health_tail,
    replace_handler,
    route_turn,
    text_turn,
)
from native_helpers import gated_gateway, session, settled, start_run
from relaymind.ui import DataMode, UiSettings
from ui_helpers import (
    DELETE_ANSWER,
    DELETE_PROMPT,
    HEALTH_PROMPT,
    Controls,
    database_ids,
    delete_gateway,
    health_gateway,
    timed_gateway,
)

MODE_TEXT = {
    DataMode.LIVE: "LIVE",
    DataMode.DOUBLE: "DOUBLE",
    DataMode.REPLAY: "REPLAY",
}


def ui_app(gateway, mode="double", **ui):
    return make_app(gateway, ui=UiSettings(mode=mode, **ui))


async def create(client, prompt=HEALTH_PROMPT, **fields):
    response = await start_run(client, prompt, **fields)
    assert response.status_code == 202, response.text
    return response.json()["run_id"]


async def fragment(client, run_id):
    response = await client.get(f"/ui/runs/{run_id}")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/html")
    return response.text


# -- settings ----------------------------------------------------------------------------


def test_the_ui_needs_a_declared_data_mode():
    """RM23: there is no default mode, so the UI is never served unlabeled."""
    with pytest.raises(TypeError):
        UiSettings()
    with pytest.raises(ValueError, match="mode"):
        UiSettings(mode="demo")
    assert UiSettings(mode="replay").mode is DataMode.REPLAY


def test_an_app_without_ui_settings_serves_no_ui(tmp_path):
    """RM23: the UI is opt in, so the default app adds no surface."""

    async def go():
        async with session(make_app(health_gateway(tmp_path))) as client:
            return [(await client.get(path)).status_code for path in ("/ui", "/ui/app.js")]

    assert asyncio.run(go()) == [404, 404]


# -- the static shell ---------------------------------------------------------------------


@pytest.mark.parametrize("mode", list(DataMode))
def test_the_shell_always_shows_the_data_mode(tmp_path, mode):
    """RM23: live, replay, and double are separate modes with a persistent visible label."""
    app = ui_app(health_gateway(tmp_path), mode=mode.value)

    async def go():
        async with session(app, auth=False) as client:
            return await client.get("/ui")

    response = asyncio.run(go())
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    page = response.text
    assert f'data-ui-mode="{mode.value}"' in page
    assert MODE_TEXT[mode] in page
    # The label is outside the area that the page replaces with traces.
    label_at = page.index(f'data-ui-mode="{mode.value}"')
    assert label_at < page.index('id="traces"')
    others = [text for other, text in MODE_TEXT.items() if other is not mode]
    assert not any(f">{text}" in page for text in others)


def test_the_shell_holds_no_run_data_and_no_inline_script(tmp_path):
    """RM12, section 9: static files need no token, so they hold nothing of a run."""
    gateway = health_gateway(tmp_path)
    app = ui_app(gateway)

    async def go():
        async with session(app) as client:
            await create(client)
        async with session(app, auth=False) as client:
            return [await client.get(path) for path in ("/ui", "/ui/app.js", "/ui/app.css")]

    page, script, style = asyncio.run(go())
    assert (page.status_code, script.status_code, style.status_code) == (200, 200, 200)
    assert script.headers["content-type"].startswith("text/javascript")
    assert style.headers["content-type"].startswith("text/css")
    for response in (page, script, style):
        assert not re.search(r"run_[0-9a-f]{8}", response.text)
    assert re.search(r"<script(?![^>]*\ssrc=)", page.text) is None
    assert 'src="/ui/app.js"' in page.text
    assert "default-src 'none'" in page.headers["content-security-policy"]
    assert "script-src 'self'" in page.headers["content-security-policy"]


def test_the_page_script_keeps_the_token_in_memory_only(tmp_path):
    """Section 9: the token is never in browser storage or a URL."""
    app = ui_app(health_gateway(tmp_path))

    async def go():
        async with session(app, auth=False) as client:
            return (await client.get("/ui/app.js")).text

    script = asyncio.run(go())
    for forbidden in ("localStorage", "sessionStorage", "document.cookie", "indexedDB"):
        assert forbidden not in script
    assert "innerHTML" in script  # server-escaped fragments only
    assert "Authorization" in script


def test_the_shell_names_the_sandbox_when_one_is_configured(tmp_path):
    """RM23: a clearly labeled sandbox data service."""
    gateway = health_gateway(tmp_path)
    app = ui_app(gateway, fixtures=gateway.sandbox)

    async def go():
        async with session(app, auth=False) as client:
            return (await client.get("/ui")).text

    page = asyncio.run(go())
    assert gateway.sandbox.label in page
    assert "Synthetic values, not a production resource" in page


# -- the trace fragment -------------------------------------------------------------------


def test_a_trace_fragment_needs_the_token_and_a_known_run(tmp_path):
    """Section 9, section 11: the trace is behind the token; an unknown run is 404."""
    app = ui_app(health_gateway(tmp_path))

    async def go():
        async with session(app, auth=False) as client:
            anonymous = await client.get("/ui/runs/run_x")
        async with session(app) as client:
            unknown = await client.get("/ui/runs/run_x")
        return anonymous, unknown

    anonymous, unknown = asyncio.run(go())
    assert anonymous.status_code == 401
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "run_not_found"


def test_a_trace_fragment_renders_every_listed_field(tmp_path):
    """RM23, section 9: prompt, mode, model, skill, tool, result, answer, and timing."""
    gateway = health_gateway(tmp_path)
    app = ui_app(gateway)

    async def go():
        async with session(app) as client:
            run_id = await create(client, execution_mode="local_only")
            await settled(client, run_id)
            return run_id, await fragment(client, run_id)

    run_id, page = asyncio.run(go())
    assert f'data-run-id="{run_id}"' in page
    assert 'data-ui-mode="double"' in page
    for text in (
        HEALTH_PROMPT,
        "local_only",
        "double-qwen3-0.6b",
        "check-service-health",
        "get_service_health",
        "&quot;service&quot;: &quot;staging-api&quot;",
        "succeeded",
        "healthy",
        STAGING_OK,
        "First model action",
        "250 ms",
        "Active time",
        "Waiting time",
    ):
        assert text in page, text


def test_latency_is_labeled_as_model_action_latency_only(tmp_path):
    """RM25, section 12.1: never presented as completed-answer latency."""
    gateway = health_gateway(tmp_path)
    app = ui_app(gateway)

    async def go():
        async with session(app) as client:
            run_id = await create(client)
            await settled(client, run_id)
            return await fragment(client, run_id)

    page = asyncio.run(go())
    assert "model action latency" in page
    assert "not the time to the final answer" in page
    assert not re.search(r"(answer|completion|response) latency(?! is not)", page, re.I)
    # Active time sits in its own row, not under the latency label.
    assert "Latency" not in page


def test_tool_output_and_prompts_are_escaped_not_markup(tmp_path):
    """RM12, section 9: text in the evidence is data, never HTML."""
    gateway = health_gateway(tmp_path)
    app = ui_app(gateway)
    hostile = '<img src=x onerror="alert(1)"> Is staging-api healthy right now?'

    async def go():
        async with session(app) as client:
            run_id = await create(client, hostile)
            await settled(client, run_id)
            return await fragment(client, run_id)

    page = asyncio.run(go())
    assert "<img" not in page
    assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;" in page


def test_a_failed_operation_is_inspectable_in_the_fragment(tmp_path):
    """RM23: a user can inspect failed operations."""
    gateway = health_gateway(tmp_path, external_turns=None)

    def broken(arguments):
        raise RuntimeError("sandbox unreachable")

    replace_handler(gateway, "get_service_health", broken)
    app = ui_app(gateway)

    async def go():
        async with session(app) as client:
            run_id = await create(client)
            await settled(client, run_id)
            return await fragment(client, run_id)

    page = asyncio.run(go())
    assert "Failed operations" in page
    assert "tool_failure" in page
    assert "needs_external" in page or "failed" in page


def test_reading_a_trace_changes_nothing_and_does_not_take_the_token(tmp_path):
    """RM11, RM21: the fragment is a read; the token is still returned once by the native view."""
    gateway = delete_gateway(tmp_path)
    app = ui_app(gateway)

    async def go():
        async with session(app) as client:
            run_id = await create(client, DELETE_PROMPT)
            # The first native read that sees the approval is the one that gets the token.
            native = await settled(client, run_id)
            before = len(gateway.store.events(run_id))
            first = await fragment(client, run_id)
            second = await fragment(client, run_id)
            after = len(gateway.store.events(run_id))
            return before, after, first, second, native

    before, after, first, second, native = asyncio.run(go())
    assert before == after
    assert native["pending_approval"]["token"]
    assert native["pending_approval"]["token"] not in first + second


# -- controls and the native round trips ---------------------------------------------------


def test_a_confirmation_round_trips_through_the_native_resume_endpoint(tmp_path):
    """RM11, RM21, section 9: approve with the values the trace carries; one mutation."""
    gateway = delete_gateway(tmp_path, text_turn(DELETE_ANSWER), text_turn(DELETE_ANSWER))
    app = ui_app(gateway)

    async def go():
        async with session(app) as client:
            run_id = await create(client, DELETE_PROMPT)
            # What the page does: read the token once from the native view, then resume.
            view = await settled(client, run_id)
            page = await fragment(client, run_id)
            controls = Controls(page)
            assert sorted(controls.actions()) == ["approve", "cancel", "deny"]
            approve = controls.get("approve")
            assert view["pending_approval"]["approval_id"] == approve["data-approval-id"]
            response = await client.post(
                f"/relay/v1/runs/{approve['data-run-id']}/resume",
                json={
                    "expected_version": int(approve["data-expected-version"]),
                    "approval_id": approve["data-approval-id"],
                    "token": view["pending_approval"]["token"],
                    "approve": True,
                },
            )
            assert response.status_code == 202
            done = await settled(client, run_id, states={"completed"})
            return done, await fragment(client, run_id)

    done, page = asyncio.run(go())
    assert done["state"] == "completed"
    assert "demo-orders-db" not in database_ids(gateway)
    assert Controls(page).actions() == []
    assert "approved" in page


def test_a_denial_round_trips_and_mutates_nothing(tmp_path):
    """RM11, section 9: deny with the trace's values; the run is cancelled."""
    gateway = delete_gateway(tmp_path)
    app = ui_app(gateway)

    async def go():
        async with session(app) as client:
            run_id = await create(client, DELETE_PROMPT)
            view = await settled(client, run_id)
            deny = Controls(await fragment(client, run_id)).get("deny")
            await client.post(
                f"/relay/v1/runs/{run_id}/resume",
                json={
                    "expected_version": int(deny["data-expected-version"]),
                    "approval_id": deny["data-approval-id"],
                    "token": view["pending_approval"]["token"],
                    "approve": False,
                },
            )
            await settled(client, run_id, states={"cancelled"})
            return await settled(client, run_id), await fragment(client, run_id)

    done, page = asyncio.run(go())
    assert done["state"] == "cancelled"
    assert "demo-orders-db" in database_ids(gateway)
    assert "denied" in page


def test_a_clarification_round_trips_through_the_native_resume_endpoint(tmp_path):
    """RM05, RM21, section 9: the answer goes to resume with the version the trace carries."""
    gateway = timed_gateway(
        tmp_path,
        [clarify_turn(), route_turn("check-service-health"), *health_tail()],
        external_turns=[],
    )
    app = ui_app(gateway)

    async def go():
        async with session(app) as client:
            run_id = await create(client, "Is the service healthy?")
            await settled(client, run_id)
            page = await fragment(client, run_id)
            assert "Which service do you mean?" in page
            controls = Controls(page)
            assert sorted(controls.actions()) == ["answer", "cancel"]
            answer = controls.get("answer")
            response = await client.post(
                f"/relay/v1/runs/{answer['data-run-id']}/resume",
                json={
                    "expected_version": int(answer["data-expected-version"]),
                    "input": "staging-api",
                },
            )
            assert response.status_code == 202, response.text
            done = await settled(client, run_id, states={"completed"})
            return done, await fragment(client, run_id)

    done, page = asyncio.run(go())
    assert done["state"] == "completed"
    assert done["output"] == STAGING_OK
    assert "staging-api" in page and "Your answer" in page


def test_a_running_run_can_be_cancelled_from_the_trace(tmp_path):
    """RM14, RM17, section 9: the cancel control uses the native cancel endpoint."""
    gateway, adapter = gated_gateway(tmp_path)
    app = ui_app(gateway)

    async def go():
        async with session(app) as client:
            run_id = await create(client)
            await asyncio.wait_for(adapter.started.wait(), 5)
            page = await fragment(client, run_id)
            cancel = Controls(page).get("cancel")
            response = await client.post(
                f"/relay/v1/runs/{cancel['data-run-id']}/cancel",
                json={"expected_version": int(cancel["data-expected-version"])},
            )
            assert response.status_code == 200, response.text
            done = await settled(client, run_id, states={"cancelled"})
            return done, await fragment(client, run_id)

    done, page = asyncio.run(go())
    assert done["state"] == "cancelled"
    assert Controls(page).actions() == []
    assert "cancel_requested" in page


def test_a_finished_run_has_no_controls(tmp_path):
    """Section 9: a terminal run carries no control."""
    gateway = health_gateway(tmp_path)
    app = ui_app(gateway)

    async def go():
        async with session(app) as client:
            run_id = await create(client)
            await settled(client, run_id)
            return await fragment(client, run_id)

    assert Controls(asyncio.run(go())).actions() == []


# -- replay mode ----------------------------------------------------------------------------


def test_replay_mode_shows_a_recorded_run_and_offers_no_controls(tmp_path):
    """RM23: a recorded run is shown with its label; nothing can be resumed or cancelled."""
    gateway = delete_gateway(tmp_path)
    recorder = ui_app(gateway, mode="double")
    replay = make_app(gateway, ui=UiSettings(mode="replay"))

    async def go():
        async with session(recorder) as client:
            run_id = await create(client, DELETE_PROMPT)
            await settled(client, run_id)
        calls = len(gateway.adapter.requests)
        async with session(replay) as client:
            page = await fragment(client, run_id)
        return page, calls

    page, calls = asyncio.run(go())
    assert 'data-ui-mode="replay"' in page
    assert Controls(page).actions() == []
    assert "waiting_confirmation" in page
    assert len(gateway.adapter.requests) == calls  # no model was called to show it


def test_every_mode_label_is_in_its_own_fragment(tmp_path):
    """RM23: the label is part of what is rendered, so a pasted trace keeps it."""
    gateway = health_gateway(tmp_path)
    run_id = {}

    async def go(mode):
        app = ui_app(gateway, mode=mode)
        async with session(app) as client:
            if not run_id:
                run_id["id"] = await create(client)
                await settled(client, run_id["id"])
            return await fragment(client, run_id["id"])

    for mode in DataMode:
        page = asyncio.run(go(mode.value))
        assert f'data-ui-mode="{mode.value}"' in page
        assert MODE_TEXT[mode] in page
