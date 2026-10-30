"""RM36/AC27: closed JSON API, scoped exact replay and truthful readiness."""

import asyncio
import json

import httpx
import pytest

from recommendation_helpers import BODY, setup


async def client_for(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://127.0.0.1",
        headers={
            "Authorization": "Bearer " + app.state.application_token,
            "Idempotency-Key": "key",
        },
    )


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
            ):
                assert (
                    await client.get(f"/relay/v1/recommendations/{rid}/events" + suffix)
                ).status_code == 400
            for path in (f"/{rid}/choice", f"/{rid}/feedback", "/registration", "/cancel"):
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
    async def go():
        app, service, runs, _ = setup(tmp_path, populate=False)
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
    async def go():
        app, service, runs, catalog = setup(tmp_path)
        await service.refresh_catalog()
        async with await client_for(app) as c:
            raw = json.dumps(BODY).encode()
            assert (
                await c.post(
                    "/relay/v1/recommendations", content=raw + b" " * (512 * 1024 - len(raw))
                )
            ).status_code == 200
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
            assert (await c.post("/relay/v1/recommendations", json=BODY)).status_code == 200
        runs.close()

    asyncio.run(go())


def test_disabled_routes_use_recommendation_error_envelope(tmp_path):
    from api_helpers import call, healthy_gateway, make_app

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
