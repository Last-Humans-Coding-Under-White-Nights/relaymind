"""The comparison view: auto and external_only on reset fixtures (RM23, RM11, #28).

Spec 0.20 section 9 (Comparison). The same input runs in each mode, one
run after the other, and the fixtures are reset before each. All model
output comes from scripted doubles (spec section 13); the sandbox is a
disposable copy.
"""

import asyncio

import pytest

from api_helpers import make_app
from gateway_helpers import STAGING_OK, build_gateway, health_tail, route_turn, tool_turn
from native_helpers import GatedAdapter, session
from relaymind.api.app import ChatSettings
from relaymind.api.native import NativeSettings
from relaymind.orchestrator.local_loop import ConfiguredModel
from relaymind.orchestrator.route_decision import ExecutionMode
from relaymind.ui import UiSettings
from relaymind.ui.trace_view import build_trace_view
from ui_helpers import (
    DELETE_ARGS,
    DELETE_PROMPT,
    HEALTH_PROMPT,
    MUTATING_CALLER,
    SpyFixtures,
    database_ids,
    external_health_turns,
    spy_on_starts,
    timed_gateway,
)


def comparing_gateway(tmp_path):
    """The same task, finished by the local model (auto) and by the external one."""
    return timed_gateway(
        tmp_path,
        [route_turn("check-service-health"), *health_tail()],
        external_turns=external_health_turns(),
    )


def compare_app(gateway, *, fixtures=True, mode="double", **options):
    ui = UiSettings(
        mode=mode, fixtures=gateway.sandbox if fixtures is True else fixtures, **options
    )
    return make_app(gateway, ui=ui, native=NativeSettings(cancel_wait_seconds=0.5))


async def compare(client, prompt=HEALTH_PROMPT, **fields):
    return await client.post(
        "/ui/comparisons",
        json={"messages": [{"role": "user", "content": prompt}], **fields},
    )


def test_both_modes_run_the_same_input_and_each_has_its_own_trace(tmp_path):
    """RM23: auto then external_only; each arm is its own run with its own models."""
    gateway = comparing_gateway(tmp_path)
    app = compare_app(gateway)

    async def go():
        async with session(app) as client:
            return await compare(client)

    response = asyncio.run(go())
    assert response.status_code == 200, response.text
    arms = response.json()["arms"]
    assert [arm["execution_mode"] for arm in arms] == ["auto", "external_only"]
    assert [arm["state"] for arm in arms] == ["completed", "completed"]
    assert arms[0]["run_id"] != arms[1]["run_id"]
    auto, external = (build_trace_view(gateway.store, arm["run_id"]) for arm in arms)
    assert auto["prompt"] == external["prompt"] == [{"role": "user", "content": HEALTH_PROMPT}]
    assert auto["execution_mode"]["effective"] == "auto"
    assert external["execution_mode"]["effective"] == "external_only"
    assert {c["actor"] for c in auto["model_calls"]} == {"local"}
    assert {c["actor"] for c in external["model_calls"]} == {"external"}
    assert auto["final_answer"]["text"] == external["final_answer"]["text"] == STAGING_OK
    # The local arm ran no external generation (RM01), measured on the double.
    assert len(gateway.external.requests) == 2  # only the external_only arm called it


def test_the_fixtures_are_reset_before_each_run_and_a_run_ends_before_the_next(tmp_path):
    """RM23: reset, auto to rest, reset, external_only; never two runs at once."""
    gateway = comparing_gateway(tmp_path)
    log: list[str] = []
    spy_on_starts(gateway, log)
    app = compare_app(gateway, fixtures=SpyFixtures(gateway.sandbox, log))

    async def go():
        async with session(app) as client:
            return await compare(client)

    assert asyncio.run(go()).status_code == 200
    assert log == [
        "reset",
        "start:auto:after=None",
        "reset",
        "start:external_only:after=completed",
    ]


def test_a_change_one_run_left_behind_is_gone_for_the_next(tmp_path):
    """RM23, section 9: a real reset of the sandbox copy, with its label in the answer."""
    gateway = comparing_gateway(tmp_path)
    gateway.sandbox.delete_database("demo-orders-db", expected_revision=3)
    assert "demo-orders-db" not in database_ids(gateway)
    app = compare_app(gateway)

    async def go():
        async with session(app) as client:
            return await compare(client)

    arms = asyncio.run(go()).json()["arms"]
    assert "demo-orders-db" in database_ids(gateway)
    for arm in arms:
        assert arm["fixture"] == {
            "label": gateway.sandbox.label,
            "scenario": "baseline",
        }


def test_a_run_that_waits_is_cancelled_and_nothing_is_mutated(tmp_path):
    """RM11, section 9: a comparison collects no input; a waiting arm ends cancelled."""
    gateway = timed_gateway(
        tmp_path,
        [
            route_turn("delete-demo-database"),
            tool_turn("list_demo_databases", call_id="call_list"),
            tool_turn("delete_demo_database", call_id="call_delete", **DELETE_ARGS),
        ],
        caller=MUTATING_CALLER,
        external_turns=[
            tool_turn("list_demo_databases", call_id="e_list"),
            tool_turn("delete_demo_database", call_id="e_delete", **DELETE_ARGS),
        ],
    )
    app = compare_app(gateway)

    async def go():
        async with session(app) as client:
            return await compare(client, DELETE_PROMPT)

    response = asyncio.run(go())
    assert response.status_code == 200, response.text
    arms = response.json()["arms"]
    assert [arm["state"] for arm in arms] == ["cancelled", "cancelled"]
    assert "demo-orders-db" in database_ids(gateway)
    for arm in arms:
        view = build_trace_view(gateway.store, arm["run_id"])
        assert view["cancel_reason"] == "cancel_requested"
        assert view["confirmation"]["status"] == "voided"
        assert view["confirmation"]["target"] == "demo-orders-db"
    assert gateway.store.active_run_id() is None


def test_a_run_that_does_not_come_to_rest_is_asked_to_cancel(tmp_path):
    """RM03, RM14: the arm limit is bounded; the run is reported in the state it has."""
    adapter = GatedAdapter(turns=[route_turn("check-service-health"), *health_tail()])
    gateway = build_gateway(
        tmp_path,
        None,
        local_model=ConfiguredModel(adapter=adapter, model_id="gated"),
        external_turns=external_health_turns(),
    )
    app = compare_app(gateway, arm_wait_seconds=0.05)

    async def go():
        async with session(app) as client:
            return await compare(client)

    response = asyncio.run(go())
    assert response.status_code == 200, response.text
    arms = response.json()["arms"]
    assert [arm["state"] for arm in arms] == ["cancelled", "completed"]
    assert adapter.cancelled  # the provider was asked to stop the generation


# -- refusals ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"fixtures": None}, "fixture"),
        ({"mode": "replay"}, "replay"),
    ],
)
def test_a_comparison_that_cannot_reset_or_run_is_refused_before_any_run(
    tmp_path, options, message
):
    """Section 9: no reset service, or replay mode, means 409 and no run."""
    gateway = comparing_gateway(tmp_path)
    app = compare_app(gateway, **options)

    async def go():
        async with session(app) as client:
            return await compare(client)

    response = asyncio.run(go())
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "invalid_state"
    assert message in response.json()["error"]["message"]
    assert gateway.adapter.requests == [] and gateway.external.requests == []


def test_both_modes_must_be_permitted_by_the_operator(tmp_path):
    """Section 7.4: the same execution mode policy as every other surface."""
    gateway = comparing_gateway(tmp_path)
    app = make_app(
        gateway,
        ui=UiSettings(mode="double", fixtures=gateway.sandbox),
        settings=ChatSettings(allowed_modes=frozenset({ExecutionMode.AUTO})),
    )

    async def go():
        async with session(app) as client:
            return await compare(client)

    response = asyncio.run(go())
    assert (response.status_code, response.json()["error"]["code"]) == (
        403,
        "execution_mode_forbidden",
    )
    assert gateway.adapter.requests == []


def test_a_comparison_checks_the_token_and_the_body(tmp_path):
    """Sections 7.1, 11: 401 without the token; 400 for a body that is not a native run body."""
    gateway = comparing_gateway(tmp_path)
    app = compare_app(gateway)

    async def go():
        async with session(app, auth=False) as client:
            anonymous = await compare(client)
        async with session(app) as client:
            extra = await compare(client, execution_mode="auto")
            empty = await client.post("/ui/comparisons", json={})
            broken = await client.post("/ui/comparisons", content=b"{")
            listed = await client.post("/ui/comparisons", json=[])
        return anonymous, extra, empty, broken, listed

    anonymous, extra, empty, broken, listed = asyncio.run(go())
    assert anonymous.status_code == 401
    assert extra.status_code == 400 and extra.json()["error"]["code"] == "unsupported_parameter"
    assert empty.status_code == 400 and broken.status_code == 400 and listed.status_code == 400
    assert gateway.adapter.requests == []


def test_a_comparison_while_another_run_is_active_is_429_and_starts_nothing(tmp_path):
    """Section 11: one run is active at a time; the comparison does not queue."""
    gateway = comparing_gateway(tmp_path)
    other = gateway.orchestrator.machine.create()
    gateway.orchestrator.machine.transition(other.run_id, other.version, "routing")
    app = compare_app(gateway)

    async def go():
        async with session(app) as client:
            return await compare(client)

    response = asyncio.run(go())
    assert (response.status_code, response.json()["error"]["code"]) == (429, "overloaded")
    assert gateway.adapter.requests == [] and gateway.external.requests == []
