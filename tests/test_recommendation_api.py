"""RM36/AC27: closed JSON API, scoped exact replay and truthful readiness."""

import asyncio
import json

import pytest

from recommendation_helpers import BODY, client_for, setup


def test_create_poll_readiness_and_exact_defaults_replay(tmp_path):
    async def go():
        app, service, runs, _ = setup(tmp_path)
        await service.refresh_catalog()
        async with await client_for(app) as client:
            ready = await client.get("/relay/v1/recommendations/readiness")
            assert ready.status_code == 200 and ready.json()["state"] == "ready"
            response = await client.post("/relay/v1/recommendations", json=BODY)
            assert response.status_code == 200, response.text
            view = response.json()
            assert view["state"] == "ready" and view["result"]["primary_attempt"] == 0
            rid = view["recommendation_id"]
            replay = await client.post(
                "/relay/v1/recommendations",
                json=BODY
                | dict(
                    conversation=[],
                    language_tags=[],
                    code_switched=False,
                    host_snapshot_revision=None,
                ),
            )
            assert replay.content == response.content
            got = await client.get("/relay/v1/recommendations/" + rid)
            assert got.json() == view
            events = await client.get(f"/relay/v1/recommendations/{rid}/events?limit=2")
            assert events.json()["has_more"] and events.json()["next_after_seq"] == 2
            tail = await client.get(f"/relay/v1/recommendations/{rid}/events?after_seq=2")
            assert [e["event_type"] for e in tail.json()["events"]] == [
                "selector.finished",
                "ready",
            ]
            assert (
                await client.post("/relay/v1/recommendations", json=BODY | {"task_text": "other"})
            ).status_code == 409
            assert (
                await client.get(f"/relay/v1/recommendations/{rid}/events?after_seq={10**100}")
            ).json()["next_after_seq"] == 10**100
            for suffix in (
                "?limit=0",
                "?limit=1&limit=2",
                "?other=1",
                "?after_seq=-1",
                "?after_seq=1.0",
                # Past Python's integer-string limit int() raises; still a bad request.
                "?limit=" + "9" * 5000,
                "?after_seq=" + "9" * 5000,
            ):
                bad = await client.get(f"/relay/v1/recommendations/{rid}/events" + suffix)
                assert bad.status_code == 400
                assert bad.json()["error"]["code"] == "invalid_request"
            # RM37/AC28 adds private actions; the app bearer grants no authority.
            for path in (f"/{rid}/choice", f"/{rid}/feedback"):
                assert (
                    await client.post("/relay/v1/recommendations" + path, json={})
                ).status_code == 403
            for path in ("/registration", "/cancel"):
                assert (
                    await client.post("/relay/v1/recommendations" + path, json={})
                ).status_code in (404, 405)
        await service.stop()
        runs.close()

    asyncio.run(go())


@pytest.mark.parametrize(
    "change",
    [
        {"schema_version": True},
        {"schema_version": 1.0},
        {"actor": "user"},
        {"snapshot": {}},
        {"availability": "available"},
        {"host_snapshot_revision": "x"},
        {"code_switched": 1},
        {"requested_kinds": ["skill", "skill"]},
        {"language_tags": ["zh", "zh"]},
        {"conversation": [{"role": "tool", "text": "x"}]},
        {"task_text": "中" * 2731},
    ],
)
def test_strict_parse_precedes_unavailable(tmp_path, change):
    from relaymind.assets.settings import RecommendationSettings

    # The contract is 400, then 503, then 401, not timing: a generous budget
    # keeps a busy CI runner from turning the 503 into a 504.
    settings = RecommendationSettings(
        enabled=True, user_id="u", workspace_id="w", total_ms=10_000, primary_ms=150
    )

    async def go():
        app, service, runs, _ = setup(tmp_path, populate=False, settings=settings)
        async with await client_for(app) as c:
            response = await c.post("/relay/v1/recommendations", json=BODY | change)
            assert response.status_code == 400
            assert response.json()["error"]["run_id"] is None
            assert (await c.post("/relay/v1/recommendations", json=BODY)).status_code == 503
            assert (
                await c.post(
                    "/relay/v1/recommendations",
                    json=BODY,
                    headers={"Authorization": "Bearer wrong"},
                )
            ).status_code == 401
        runs.close()

    asyncio.run(go())


@pytest.mark.parametrize(
    "raw",
    [
        b'{"schema_version":1,"schema_version":1}',
        b'{"x":NaN}',
        b'{"x":1e999}',
        b"\xff",
        b"[]",
        b"{}{}",
    ],
)
def test_strict_raw_json(tmp_path, raw):
    async def go():
        app, _, runs, _ = setup(tmp_path, populate=False)
        async with await client_for(app) as c:
            assert (await c.post("/relay/v1/recommendations", content=raw)).status_code == 400
        runs.close()

    asyncio.run(go())


def test_bound_key_and_unavailable_before_every_key_lookup(tmp_path):
    """RM36/AC27: byte/key admission is independent of host scheduling time."""
    from helpers import FakeClock

    async def go():
        app, service, runs, catalog = setup(tmp_path, clock=FakeClock())
        await service.refresh_catalog()
        async with await client_for(app) as c:
            raw = json.dumps(BODY).encode()
            first = await c.post(
                "/relay/v1/recommendations", content=raw + b" " * (512 * 1024 - len(raw))
            )
            assert first.status_code == 200, first.text
            assert (
                await c.post(
                    "/relay/v1/recommendations", content=raw + b" " * (512 * 1024 - len(raw) + 1)
                )
            ).status_code == 400
            for headers in (
                {"Idempotency-Key": ""},
                [("Idempotency-Key", "one"), ("Idempotency-Key", "two")],
            ):
                assert (
                    await c.post("/relay/v1/recommendations", json=BODY, headers=headers)
                ).status_code == 400
            service.provider.cache.invalidate()
            for body in (
                BODY,
                BODY | {"task_text": "changed"},
                BODY | {"selection_scope": "runtime"},
            ):
                assert (await c.post("/relay/v1/recommendations", json=body)).status_code == 503
            with runs.sql_transaction() as db:
                assert db.execute("SELECT count(*) FROM recommendations").fetchone()[0] == 1
            await service.refresh_catalog()
            replay = await c.post("/relay/v1/recommendations", json=BODY)
            assert replay.status_code == 200 and replay.content == first.content
        runs.close()

    asyncio.run(go())


def test_disabled_routes_use_recommendation_error_envelope(tmp_path, monkeypatch):
    from api_helpers import call, healthy_gateway, make_app
    from helpers import FakeClock
    from relaymind.api import recommendations
    from relaymind.selection.asset_chain import SelectionBudget

    monkeypatch.setattr(
        recommendations, "SelectionBudget", lambda: SelectionBudget(clock=FakeClock())
    )
    app = make_app(healthy_gateway(tmp_path))
    response = call(app, "GET", "/relay/v1/recommendations/readiness")
    assert response.status_code == 200 and response.json()["reason"] == "disabled"
    assert (
        call(
            app,
            "POST",
            "/relay/v1/recommendations",
            json_body=BODY,
            headers={"Idempotency-Key": "key"},
        ).status_code
        == 503
    )
    error = call(app, "GET", "/relay/v1/recommendations/readiness", auth=False)
    assert error.status_code == 401 and error.json()["error"]["recommendation_id"] is None


def test_readiness_v1_published_contract():
    """RM36/AC27: the published readiness schema closes fields and state evidence."""
    from relaymind.schema_util import load_packaged_schema
    from relaymind.store import recommendation_event

    schema = load_packaged_schema("relaymind.store", "recommendation_readiness.v1.json")
    assert schema["$id"] == "https://relaymind.local/schemas/recommendation_readiness.v1.json"
    valid = dict(
        schema_version=1,
        state="ready",
        reason="usable_catalog",
        catalog_revision=1,
        catalog_size=1,
        lexical_ready=True,
        semantic_coverage=dict(ready=0, total=1),
        pending_jobs=0,
        failed_jobs=0,
    )
    validate = recommendation_event.validate_readiness
    for state, reason in (
        ("ready", "usable_catalog"),
        ("empty", "empty_catalog"),
        ("unavailable", "disabled"),
        ("unavailable", "corrupt_catalog"),
        ("unavailable", "no_lexical_generation"),
    ):
        assert validate(valid | dict(state=state, reason=reason))["state"] == state
    assert validate(valid | {"catalog_revision": 0})["catalog_revision"] == 0
    for invalid in (
        valid | {"extra": None},
        valid | {"semantic_coverage": {"ready": 0, "total": 1, "extra": None}},
        valid | {"schema_version": True},
        valid | {"catalog_revision": -1},
        valid | {"catalog_size": True},
        valid | {"pending_jobs": -1},
        valid | {"failed_jobs": 1.0},
        valid | {"semantic_coverage": {"ready": 2, "total": 1}},
        valid | {"state": "empty"},
        valid | {"reason": "disabled"},
    ):
        with pytest.raises(ValueError):
            validate(invalid)


def test_readiness_endpoint_validates_own_body(tmp_path, monkeypatch):
    """RM36/AC27: malformed internal readiness cannot reach a client as healthy."""

    async def go():
        app, service, runs, _ = setup(tmp_path)
        await service.refresh_catalog()
        valid = service.readiness()
        monkeypatch.setattr(
            service,
            "readiness",
            lambda: valid | {"semantic_coverage": {"ready": 2, "total": 1}},
        )
        async with await client_for(app) as client:
            response = await client.get("/relay/v1/recommendations/readiness")
            assert response.status_code == 500
            assert response.json()["error"]["code"] == "internal_error"
        runs.close()

    asyncio.run(go())


@pytest.mark.parametrize(
    "state,reason",
    [
        ("ready", "usable_catalog"),
        ("empty", "empty_catalog"),
        ("unavailable", "no_lexical_generation"),
        ("unavailable", "disabled"),
    ],
)
def test_readiness_route_actual_states_match_published_contract(tmp_path, state, reason):
    """RM36/AC27: actual ready, empty and unavailable routes validate as ReadinessV1."""
    from relaymind.store.recommendation_event import validate_readiness

    async def go():
        if reason == "disabled":
            from api_helpers import healthy_gateway, make_app

            app = make_app(healthy_gateway(tmp_path))
            service = runs = None
        else:
            from relaymind.assets.sources import CaptureContext, capture_source

            app, service, runs, catalog = setup(tmp_path, populate=state == "ready")
            if state == "empty":
                source = service.provider.settings.sources[0]
                captured = capture_source(
                    source,
                    previous=catalog.inventory("example"),
                    context=CaptureContext(service.clock.utc_now()),
                )
                catalog.publish(captured, expected_revision=catalog.revision())
            await service.refresh_catalog()
        async with await client_for(app) as client:
            response = await client.get("/relay/v1/recommendations/readiness")
            assert response.status_code == 200
            body = response.json()
            assert (body["state"], body["reason"]) == (state, reason)
            assert validate_readiness(body) == body
        if service is not None:
            await service.stop()
            runs.close()

    asyncio.run(go())


def test_token_rotation_and_foreign_scope_are_server_owned(tmp_path):
    async def go():
        app, service, runs, _ = setup(tmp_path)
        await service.refresh_catalog()
        async with await client_for(app) as c:
            first = await c.post("/relay/v1/recommendations", json=BODY)
            app.state.application_token = "new" * 20
            assert (await c.post("/relay/v1/recommendations", json=BODY)).status_code == 401
            c.headers["Authorization"] = "Bearer " + app.state.application_token
            assert (await c.post("/relay/v1/recommendations", json=BODY)).content == first.content
            rid = first.json()["recommendation_id"]
            from fastapi import FastAPI

            from relaymind.api.recommendations import install_recommendation_routes

            foreign = FastAPI()
            foreign.state.application_token = app.state.application_token
            install_recommendation_routes(
                foreign, service, configured_scope={"user_id": "foreign", "workspace_id": "w"}
            )
            async with await client_for(foreign) as other:
                assert (await other.get("/relay/v1/recommendations/" + rid)).status_code == 404
        await service.stop()
        runs.close()

    asyncio.run(go())
