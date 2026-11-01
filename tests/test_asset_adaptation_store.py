"""RM37/RM42 AC28/AC33: coherent bounded history and synchronous invalidation."""

import asyncio
from datetime import timedelta

from recommendation_helpers import BODY
from test_asset_user_actions import action_client


async def correct(client, parent, revision, key):
    target = parent["result"]["final_candidates"][0]
    response = await client.post(
        f"/relay/v1/recommendations/{parent['recommendation_id']}/feedback",
        json=dict(
            schema_version=1,
            event_id=key,
            expected_feedback_revision=revision,
            feedback=dict(
                kind="correction", asset_id=target["asset_id"], content_hash=target["content_hash"]
            ),
        ),
        headers={"Idempotency-Key": key},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_snapshot_is_latest_twenty_eligible_under_one_recorded_revision(tmp_path):
    from helpers import FakeClock

    async def run():
        clock = FakeClock()
        async with action_client(tmp_path, clock=clock) as (client, app, service, runs, catalog, _):
            parent = (await client.post("/relay/v1/recommendations", json=BODY)).json()
            ids = []
            for i in range(22):
                clock.advance(1)
                value = await correct(client, parent, i, f"correct-{i}")
                ids.append(value["feedback_id"])
            snapshot = app.state.feedback_store.correction_snapshot(
                service.scope, catalog.snapshot(), now=clock.utc_now()
            )
            assert snapshot.feedback_revision == 22
            assert snapshot.catalog_revision == catalog.revision()
            assert [r["feedback_id"] for r in snapshot.records] == ids[-20:]
            other = app.state.feedback_store.correction_snapshot(
                {"user_id": "foreign", "workspace_id": "w"}, catalog.snapshot(), now=clock.utc_now()
            )
            assert other.records == () and other.feedback_revision == 0

    asyncio.run(run())


def test_scope_reset_invalidates_all_registered_private_owners_before_receipt(tmp_path):
    from helpers import FakeClock

    async def run():
        # A frozen clock keeps the real selection inside the budget under load.
        async with action_client(tmp_path, clock=FakeClock()) as (
            client,
            app,
            service,
            runs,
            catalog,
            _,
        ):
            parent = (await client.post("/relay/v1/recommendations", json=BODY)).json()
            await correct(client, parent, 0, "one")
            invalidated = []
            app.state.feedback_store.register_invalidator(
                lambda scope: invalidated.append(dict(scope))
            )
            response = await client.post(
                "/relay/v1/feedback/reset",
                json={"schema_version": 1, "expected_feedback_revision": 1},
                headers={"Idempotency-Key": "reset"},
            )
            assert response.status_code == 200, response.text
            assert invalidated == [dict(service.scope)]
            snapshot = app.state.feedback_store.correction_snapshot(
                service.scope, catalog.snapshot(), now=service.clock.utc_now()
            )
            assert snapshot.feedback_revision == 2 and snapshot.records == ()

    asyncio.run(run())


def test_expiry_and_changed_binding_are_filtered_before_history_truncation(tmp_path):
    from dataclasses import replace

    from helpers import FakeClock

    async def run():
        clock = FakeClock()
        async with action_client(tmp_path, clock=clock) as (client, app, service, runs, catalog, _):
            parent = (await client.post("/relay/v1/recommendations", json=BODY)).json()
            await correct(client, parent, 0, "one")
            owner = app.state.feedback_store
            snapshot = catalog.snapshot()
            changed = replace(
                snapshot,
                assets=tuple(replace(r, source_version="changed") for r in snapshot.assets),
            )
            assert (
                owner.correction_snapshot(service.scope, changed, now=clock.utc_now()).records == ()
            )
            clock.advance(timedelta(days=30).total_seconds())
            result = owner.correction_snapshot(service.scope, snapshot, now=clock.utc_now())
            assert result.records == () and result.feedback_revision == 2

    asyncio.run(run())
