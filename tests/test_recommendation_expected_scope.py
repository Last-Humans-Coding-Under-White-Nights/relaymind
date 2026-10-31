"""RM37/RM39, AC28/AC30, spec 0.63 section 7.6: the caller's expected scope.

A native adapter binds one host person to one configured gateway scope. The
gateway serves exactly one scope, so a binding whose base URL or credential
reaches another scope must fail closed before anything is read or written,
instead of silently acting on someone else's recommendations and feedback.
"""

import asyncio
from contextlib import asynccontextmanager
from urllib.parse import quote

import httpx
import pytest

from recommendation_helpers import BODY, setup
from test_asset_user_actions import ACTION_TOKEN, enable_fixture_action

USER, WORKSPACE = "X-RelayMind-Expected-User", "X-RelayMind-Expected-Workspace"


@asynccontextmanager
async def client_for(tmp_path, **options):
    app, service, runs, catalog = setup(tmp_path, **options)
    enable_fixture_action(app, service, runs)
    await service.refresh_catalog()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://127.0.0.1",
        headers={
            "Authorization": "Bearer " + app.state.application_token,
            "X-RelayMind-User-Action-Token": ACTION_TOKEN,
        },
    ) as client:
        try:
            yield client, runs
        finally:
            await service.stop()
            runs.close()


def scope(user="u", workspace="w") -> dict:
    return {USER: quote(user, safe=""), WORKSPACE: quote(workspace, safe="")}


def counts(runs) -> tuple[int, int]:
    with runs.sql_transaction() as db:
        return (
            db.execute("SELECT count(*) FROM recommendations").fetchone()[0],
            db.execute("SELECT count(*) FROM feedback_records").fetchone()[0],
        )


def test_matching_expected_scope_is_served(tmp_path):
    async def go():
        async with client_for(tmp_path) as (client, runs):
            created = await client.post(
                "/relay/v1/recommendations",
                json=BODY,
                headers={"Idempotency-Key": "k1", **scope()},
            )
            assert created.status_code == 200, created.text
            rid = created.json()["recommendation_id"]
            read = await client.get(f"/relay/v1/recommendations/{rid}", headers=scope())
            assert read.status_code == 200
            listed = await client.get("/relay/v1/feedback", headers=scope())
            assert listed.status_code == 200, listed.text

    asyncio.run(go())


@pytest.mark.parametrize("other", [("v", "w"), ("u", "x"), ("ü", "w")])
def test_a_create_for_another_scope_is_refused_before_any_work(tmp_path, other):
    async def go():
        async with client_for(tmp_path) as (client, runs):
            refused = await client.post(
                "/relay/v1/recommendations",
                json=BODY,
                headers={"Idempotency-Key": "k1", **scope(*other)},
            )
            assert refused.status_code == 403, refused.text
            error = refused.json()["error"]
            assert error["code"] == "scope_mismatch" and error["recommendation_id"] is None
            assert counts(runs) == (0, 0)

    asyncio.run(go())


def test_actions_for_another_scope_are_refused_without_effect(tmp_path):
    async def go():
        async with client_for(tmp_path) as (client, runs):
            parent = (
                await client.post(
                    "/relay/v1/recommendations", json=BODY, headers={"Idempotency-Key": "p"}
                )
            ).json()
            rid = parent["recommendation_id"]
            candidate = parent["result"]["final_candidates"][0]
            feedback = dict(
                schema_version=1,
                event_id="e1",
                expected_feedback_revision=0,
                feedback=dict(
                    kind="correction",
                    asset_id=candidate["asset_id"],
                    content_hash=candidate["content_hash"],
                ),
            )
            other = scope("someone-else", "w")
            responses = [
                await client.post(
                    f"/relay/v1/recommendations/{rid}/feedback",
                    json=feedback,
                    headers={"Idempotency-Key": "f1", **other},
                ),
                await client.get("/relay/v1/feedback", headers=other),
                await client.get(f"/relay/v1/recommendations/{rid}", headers=other),
                await client.get(f"/relay/v1/recommendations/{rid}/events", headers=other),
            ]
            for response in responses:
                assert response.status_code == 403, response.text
                assert response.json()["error"]["code"] == "scope_mismatch"
            assert counts(runs) == (1, 0)

    asyncio.run(go())


@pytest.mark.parametrize(
    "headers",
    [
        {USER: "u"},  # one half only
        {WORKSPACE: "w"},
        [(USER, "u"), (USER, "u"), (WORKSPACE, "w")],  # duplicated
        {USER: "%FF", WORKSPACE: "w"},  # not UTF-8
        {USER: "u%2", WORKSPACE: "w"},  # bad escape
        {USER: "", WORKSPACE: "w"},  # empty identity
        {USER: "a b", WORKSPACE: "w"},  # not percent-encoded
        {USER: "user:name", WORKSPACE: "w"},  # raw general delimiter
        {USER: "user@example", WORKSPACE: "w"},
    ],
)
def test_malformed_expected_scope_is_an_invalid_request(tmp_path, headers):
    async def go():
        async with client_for(tmp_path) as (client, runs):
            pairs = list(headers.items() if isinstance(headers, dict) else headers)
            response = await client.post(
                "/relay/v1/recommendations",
                json=BODY,
                headers=[("Idempotency-Key", "k"), *pairs],
            )
            assert response.status_code == 400, response.text
            assert response.json()["error"]["code"] == "invalid_request"
            assert counts(runs) == (0, 0)

    asyncio.run(go())


def test_without_expected_scope_behavior_is_unchanged(tmp_path):
    async def go():
        async with client_for(tmp_path) as (client, runs):
            created = await client.post(
                "/relay/v1/recommendations", json=BODY, headers={"Idempotency-Key": "k"}
            )
            assert created.status_code == 200

    asyncio.run(go())


def test_bearer_is_checked_before_the_expected_scope(tmp_path):
    async def go():
        async with client_for(tmp_path) as (client, runs):
            response = await client.post(
                "/relay/v1/recommendations",
                json=BODY,
                headers={"Authorization": "Bearer wrong", "Idempotency-Key": "k", **scope("v")},
            )
            assert response.status_code == 401

    asyncio.run(go())


def test_escaped_general_delimiters_are_identity_data(tmp_path):
    async def go():
        async with client_for(tmp_path) as (client, runs):
            response = await client.post(
                "/relay/v1/recommendations",
                json=BODY,
                headers={"Idempotency-Key": "k", USER: "u%3Aname", WORKSPACE: "w%40x"},
            )
            assert response.status_code == 403
            assert response.json()["error"]["code"] == "scope_mismatch"

    asyncio.run(go())
