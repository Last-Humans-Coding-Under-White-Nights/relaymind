"""RM37/AC28: private action channel and choice evidence."""

import asyncio
import hashlib
from contextlib import asynccontextmanager

import httpx
import pytest

from helpers import FakeClock
from recommendation_helpers import BODY, setup

ACTION_TOKEN = "test-action-credential-" + "a" * 40


@pytest.mark.parametrize("change", ["source_version", "identity"])
def test_rejection_binds_original_source_identity_but_correction_binds_current(
    tmp_path, monkeypatch, change
):
    """RM37/AC28 I1: equal bytes cannot silently rebind rejection to new metadata."""
    from dataclasses import replace

    async def go():
        async with action_client(tmp_path) as (client, app, service, runs, catalog, registry):
            parent = (await client.post("/relay/v1/recommendations", json=BODY)).json()
            candidate = parent["result"]["final_candidates"][0]
            snapshot = catalog.snapshot()
            changed = tuple(
                replace(
                    asset,
                    **(
                        {"source_version": "new-source-version"}
                        if change == "source_version"
                        else {
                            "identity": dict(asset.identity)
                            | {"package_version": "new-package-version"}
                        }
                    ),
                )
                for asset in snapshot.assets
            )
            monkeypatch.setattr(catalog, "snapshot", lambda: replace(snapshot, assets=changed))
            path = f"/relay/v1/recommendations/{parent['recommendation_id']}/feedback"
            body = dict(
                schema_version=1,
                event_id="reject-binding",
                expected_feedback_revision=0,
                feedback=dict(
                    kind="rejection",
                    asset_id=candidate["asset_id"],
                    content_hash=candidate["content_hash"],
                ),
            )
            rejected = await client.post(
                path, json=body, headers={"Idempotency-Key": "reject-binding"}
            )
            assert (
                rejected.status_code == 409 and rejected.json()["error"]["code"] == "asset_changed"
            ), rejected.text
            with runs.sql_transaction() as db:
                assert db.execute("SELECT count(*) FROM feedback_records").fetchone()[0] == 0
                assert (
                    db.execute("SELECT feedback_revision FROM recommendation_scope").fetchone()[0]
                    == 0
                )
            correction = body | {
                "event_id": "correct-current",
                "feedback": body["feedback"] | {"kind": "correction"},
            }
            corrected = await client.post(
                path, json=correction, headers={"Idempotency-Key": "correct-current"}
            )
            assert corrected.status_code == 200, corrected.text
            with runs.sql_transaction() as db:
                binding = __import__("json").loads(
                    db.execute("SELECT asset_json FROM feedback_bindings").fetchone()[0]
                )
                assert binding[change] == changed[0].to_json_object()[change]

    asyncio.run(go())


def test_rejection_accepts_unchanged_offer_after_unrelated_source_publication(tmp_path):
    """RM37/AC28 I1: original target stays rejectable across unrelated revisions."""
    from test_asset_sources import capture, skill

    async def go():
        async with action_client(tmp_path) as (client, app, service, runs, catalog, registry):
            parent = (await client.post("/relay/v1/recommendations", json=BODY)).json()
            candidate = parent["result"]["final_candidates"][0]
            skill(tmp_path / "unrelated", name="unrelated", description="different", body="other")
            catalog.publish(
                capture(tmp_path / "unrelated", source_id="unrelated"),
                expected_revision=catalog.revision(),
            )
            assert catalog.revision() > parent["catalog_revision"]
            response = await client.post(
                f"/relay/v1/recommendations/{parent['recommendation_id']}/feedback",
                json=dict(
                    schema_version=1,
                    event_id="unchanged-rejection",
                    expected_feedback_revision=0,
                    feedback=dict(
                        kind="rejection",
                        asset_id=candidate["asset_id"],
                        content_hash=candidate["content_hash"],
                    ),
                ),
                headers={"Idempotency-Key": "unchanged-rejection"},
            )
            assert response.status_code == 200, response.text

    asyncio.run(go())


@pytest.mark.parametrize(
    "target_case", ["rejection", "correction", "outside_correction", "unknown"]
)
def test_feedback_removed_source_targets_use_tombstone_evidence(tmp_path, target_case):
    """RM37/AC28 I2: known removal is asset_changed; arbitrary IDs stay invalid."""
    from test_asset_sources import capture, skill

    async def go():
        async with action_client(tmp_path) as (client, app, service, runs, catalog, registry):
            parent = (await client.post("/relay/v1/recommendations", json=BODY)).json()
            offered = parent["result"]["final_candidates"][0]
            source = tmp_path / "source"
            outside_source = tmp_path / "outside-source"
            skill(outside_source, name="outside", description="different", body="other")
            catalog.publish(
                capture(outside_source, source_id="outside"), expected_revision=catalog.revision()
            )
            outside = next(
                asset
                for asset in catalog.snapshot().assets
                if asset.asset_id != offered["asset_id"]
            )
            (source / "SKILL.md").unlink()
            (outside_source / "SKILL.md").unlink()
            catalog.publish(
                capture(source, previous=catalog.inventory("example")),
                expected_revision=catalog.revision(),
            )
            catalog.publish(
                capture(outside_source, source_id="outside", previous=catalog.inventory("outside")),
                expected_revision=catalog.revision(),
            )
            with runs.sql_transaction() as db:
                tombstones = {
                    row[0] for row in db.execute("SELECT asset_id FROM asset_catalog_tombstones")
                }
                assert {offered["asset_id"], outside.asset_id} <= tombstones
            target_id, content_hash = offered["asset_id"], offered["content_hash"]
            if target_case == "outside_correction":
                target_id, content_hash = outside.asset_id, outside.content_hash
                assert target_id not in {
                    c["asset_id"] for c in parent["result"]["final_candidates"]
                }
            elif target_case == "unknown":
                target_id, content_hash = "example:never-known", "b" * 64
            response = await client.post(
                f"/relay/v1/recommendations/{parent['recommendation_id']}/feedback",
                json=dict(
                    schema_version=1,
                    event_id="removed-target",
                    expected_feedback_revision=0,
                    feedback=dict(
                        kind="rejection" if target_case == "rejection" else "correction",
                        asset_id=target_id,
                        content_hash=content_hash,
                    ),
                ),
                headers={"Idempotency-Key": "removed-target"},
            )
            expected = (
                (400, "invalid_request") if target_case == "unknown" else (409, "asset_changed")
            )
            assert (response.status_code, response.json()["error"]["code"]) == expected, (
                response.text
            )
            with runs.sql_transaction() as db:
                assert db.execute("SELECT count(*) FROM feedback_records").fetchone()[0] == 0
                assert (
                    db.execute("SELECT feedback_revision FROM recommendation_scope").fetchone()[0]
                    == 0
                )

    asyncio.run(go())


def test_correction_outside_original_pool_accepts_current_source_target(tmp_path):
    """RM37/AC28 I2: removal checks do not restrict correction to old offers."""
    from test_asset_sources import capture, skill

    async def go():
        async with action_client(tmp_path) as (client, app, service, runs, catalog, registry):
            parent = (await client.post("/relay/v1/recommendations", json=BODY)).json()
            skill(
                tmp_path / "outside-source", name="outside", description="different", body="other"
            )
            catalog.publish(
                capture(tmp_path / "outside-source", source_id="outside"),
                expected_revision=catalog.revision(),
            )
            # RM38/AC29: the new source is explicitly configured and published;
            # RM37/AC28 still admits correction outside the original pool.
            from dataclasses import replace

            from relaymind.assets.settings import SourceConfig

            service.provider.settings = replace(
                service.provider.settings,
                sources=(
                    *service.provider.settings.sources,
                    SourceConfig("outside", "local_directory", tmp_path / "outside-source"),
                ),
            )
            await service.refresh_catalog()
            outside = next(
                asset
                for asset in catalog.snapshot().assets
                if asset.asset_id
                not in {c["asset_id"] for c in parent["result"]["final_candidates"]}
            )
            response = await client.post(
                f"/relay/v1/recommendations/{parent['recommendation_id']}/feedback",
                json=dict(
                    schema_version=1,
                    event_id="current-outside",
                    expected_feedback_revision=0,
                    feedback=dict(
                        kind="correction",
                        asset_id=outside.asset_id,
                        content_hash=outside.content_hash,
                    ),
                ),
                headers={"Idempotency-Key": "current-outside"},
            )
            assert response.status_code == 200, response.text

    asyncio.run(go())


@pytest.mark.parametrize(
    "entrypoint",
    [
        "stale_feedback",
        "fresh_feedback",
        "stale_reset",
        "fresh_reset",
        "feedback_read",
        "stale_delete",
        "absent_delete",
        "eligible_read",
        "stale_choice",
    ],
)
def test_fresh_action_and_feedback_reads_commit_scoped_choice_expiry(tmp_path, entrypoint):
    """RM37/AC28 I3: lazy expiry survives fresh CAS/absence errors and stays scoped."""
    from helpers import FakeClock
    from recommendation_helpers import accepted, claim, empty_result

    async def go():
        clock = FakeClock()
        async with action_client(tmp_path, clock=clock) as (
            client,
            app,
            service,
            runs,
            catalog,
            registry,
        ):
            parent = (await client.post("/relay/v1/recommendations", json=BODY)).json()
            foreign_scope = dict(user_id="u", workspace_id="other")
            foreign = accepted(scope=foreign_scope, created_at=clock.utc_now())
            service.store.begin(foreign, claim("foreign-parent", scope=foreign_scope))
            service.store.finish(
                foreign_scope,
                foreign["recommendation_id"],
                0,
                "ready",
                result=empty_result(),
                now=clock.utc_now(),
            )
            clock.advance(600)
            path = f"/relay/v1/recommendations/{parent['recommendation_id']}"
            if entrypoint.endswith("feedback"):
                response = await client.post(
                    path + "/feedback",
                    json=dict(
                        schema_version=1,
                        event_id="lazy-expiry",
                        expected_feedback_revision=99 if entrypoint == "stale_feedback" else 0,
                        feedback=dict(kind="usefulness", value="useful"),
                    ),
                    headers={"Idempotency-Key": "lazy-feedback"},
                )
                assert response.status_code == (409 if entrypoint == "stale_feedback" else 200), (
                    response.text
                )
            elif entrypoint.endswith("reset"):
                response = await client.post(
                    "/relay/v1/feedback/reset",
                    json=dict(
                        schema_version=1,
                        expected_feedback_revision=99 if entrypoint == "stale_reset" else 0,
                    ),
                    headers={"Idempotency-Key": "lazy-reset"},
                )
                assert response.status_code == (409 if entrypoint == "stale_reset" else 200), (
                    response.text
                )
            elif entrypoint == "feedback_read":
                assert (await client.get("/relay/v1/feedback")).status_code == 200
            elif entrypoint.endswith("delete"):
                response = await client.delete(
                    "/relay/v1/feedback/00000000-0000-0000-0000-000000000001?expected_feedback_revision="
                    + ("99" if entrypoint == "stale_delete" else "0"),
                    headers={"Idempotency-Key": "lazy-delete"},
                )
                assert response.status_code == (409 if entrypoint == "stale_delete" else 404), (
                    response.text
                )
            elif entrypoint == "eligible_read":
                assert (
                    app.state.feedback_store.eligible_corrections(
                        service.scope, catalog.snapshot(), now=clock.utc_now()
                    )
                    == ()
                )
            else:
                response = await client.post(
                    path + "/choice",
                    json=dict(schema_version=1, expected_version=0, choice=dict(kind="none")),
                    headers={"Idempotency-Key": "lazy-choice"},
                )
                assert response.status_code == 409
            # Inspect direct storage: a later GET must not conceal missing expiry.
            with runs.sql_transaction() as db:
                assert tuple(
                    db.execute(
                        "SELECT state,state_version FROM recommendations WHERE recommendation_id=?",
                        (parent["recommendation_id"],),
                    ).fetchone()
                ) == ("expired", 2)
                assert tuple(
                    db.execute(
                        "SELECT state,state_version FROM recommendations WHERE recommendation_id=?",
                        (foreign["recommendation_id"],),
                    ).fetchone()
                ) == ("ready", 1)
                events = [
                    __import__("json").loads(row[0])
                    for row in db.execute(
                        "SELECT event_json FROM recommendation_events WHERE recommendation_id=?",
                        (parent["recommendation_id"],),
                    )
                ]
                assert sum(event["event_type"] == "expired" for event in events) == 1
                assert (
                    __import__("json").loads(
                        db.execute(
                            "SELECT body_json FROM recommendation_claims WHERE recommendation_id=?",
                            (parent["recommendation_id"],),
                        ).fetchone()[0]
                    )
                    == parent
                )

    asyncio.run(go())


@pytest.mark.parametrize("operation", ["feedback", "reset"])
def test_action_replay_preserves_historical_receipt_without_lazy_expiry_events(tmp_path, operation):
    """RM37/AC28 I3: replay precedes fresh maintenance and never becomes a new action."""
    from helpers import FakeClock

    async def go():
        clock = FakeClock()
        async with action_client(tmp_path, clock=clock) as (
            client,
            app,
            service,
            runs,
            catalog,
            registry,
        ):
            parent = (await client.post("/relay/v1/recommendations", json=BODY)).json()
            path = (
                "/relay/v1/feedback/reset"
                if operation == "reset"
                else f"/relay/v1/recommendations/{parent['recommendation_id']}/feedback"
            )
            body = dict(schema_version=1, expected_feedback_revision=0)
            if operation == "feedback":
                body.update(event_id="historical", feedback=dict(kind="usefulness", value="useful"))
            original = await client.post(path, json=body, headers={"Idempotency-Key": "historical"})
            assert original.status_code == 200, original.text
            clock.advance(600)
            replay = await client.post(path, json=body, headers={"Idempotency-Key": "historical"})
            assert replay.status_code == original.status_code
            assert replay.json() == original.json()
            if operation == "feedback":
                assert (
                    await client.post(
                        path, json=body, headers={"Idempotency-Key": "historical-alias"}
                    )
                ).json() == original.json()
            with runs.sql_transaction() as db:
                assert tuple(
                    db.execute("SELECT state,state_version FROM recommendations").fetchone()
                ) == ("ready", 1)
                assert not any(
                    __import__("json").loads(row[0])["event_type"] == "expired"
                    for row in db.execute("SELECT event_json FROM recommendation_events")
                )

    asyncio.run(go())


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/relay/v1/feedback"),
        ("POST", "/relay/v1/feedback/reset"),
        ("DELETE", "/relay/v1/feedback/00000000-0000-0000-0000-000000000001"),
        ("POST", "/relay/v1/recommendations/00000000-0000-0000-0000-000000000001/choice"),
        ("POST", "/relay/v1/recommendations/00000000-0000-0000-0000-000000000001/feedback"),
    ],
)
def test_disabled_service_rejects_supplied_action_token_with_bearer_first(tmp_path, method, path):
    """RM37/AC28 M1: the default disabled service has no action authority."""
    from fastapi import FastAPI

    from relaymind.api.recommendations import install_recommendation_routes
    from relaymind.recommendations.registrations import ActionRegistrations
    from relaymind.store import RunStore

    async def go():
        with RunStore(tmp_path / "disabled-db") as runs:
            app = FastAPI()
            app.state.application_token = "a" * 40
            app.state.action_registrations = ActionRegistrations(runs)
            install_recommendation_routes(app, None, configured_scope=None)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
            ) as client:
                headers = {
                    "Authorization": "Bearer " + app.state.application_token,
                    "X-RelayMind-User-Action-Token": "unregistered",
                }
                response = await client.request(method, path, headers=headers)
                assert (response.status_code, response.json()["error"]["code"]) == (
                    403,
                    "user_action_required",
                ), response.text
                response = await client.request(
                    method, path, headers=headers | {"Authorization": "Bearer obsolete"}
                )
                assert (response.status_code, response.json()["error"]["code"]) == (
                    401,
                    "unauthenticated",
                )
                assert response.headers.get("WWW-Authenticate") == "Bearer"

    asyncio.run(go())


@pytest.mark.parametrize("parameter", ["limit", "expected_feedback_revision"])
@pytest.mark.parametrize("digits", ["9" * 5000, "0" * 4999 + "1"], ids=["large", "leading-zero"])
def test_oversized_decimal_action_queries_are_invalid_request(tmp_path, parameter, digits):
    """RM37/AC28 M2: numeric query parsing is bounded before integer conversion."""

    async def go():
        async with action_client(tmp_path) as (client, app, service, runs, catalog, registry):
            if parameter == "limit":
                response = await client.get("/relay/v1/feedback", params={parameter: digits})
            else:
                response = await client.delete(
                    "/relay/v1/feedback/00000000-0000-0000-0000-000000000001",
                    params={parameter: digits},
                    headers={"Idempotency-Key": "oversized-decimal"},
                )
            assert (response.status_code, response.json()["error"]["code"]) == (
                400,
                "invalid_request",
            ), response.text
            # Ordinary bounded leading-zero values retain their integer meaning.
            if parameter == "limit":
                assert (await client.get("/relay/v1/feedback?limit=0001")).status_code == 200
            else:
                bounded = await client.delete(
                    "/relay/v1/feedback/00000000-0000-0000-0000-000000000001?expected_feedback_revision=0000",
                    headers={"Idempotency-Key": "bounded-decimal"},
                )
                assert bounded.status_code == 404, bounded.text

    asyncio.run(go())


@asynccontextmanager
async def action_client(tmp_path, **options):
    # A controlled clock: the recommendation budget is measured on it, so a
    # slow or loaded runner cannot turn the parent recommendation into a
    # deadline failure (seen on Windows CI with the wall clock).
    options.setdefault("clock", FakeClock())
    app, service, runs, catalog = setup(tmp_path, **options)
    registry = enable_fixture_action(app, service, runs)
    await service.refresh_catalog()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://127.0.0.1",
        headers={
            "Authorization": "Bearer " + app.state.application_token,
            "X-RelayMind-User-Action-Token": ACTION_TOKEN,
            "Idempotency-Key": "parent",
        },
    ) as client:
        try:
            yield client, app, service, runs, catalog, registry
        finally:
            await service.stop()
            runs.close()


@pytest.mark.parametrize(
    "case,code",
    [
        ("bearer", "unauthenticated"),
        ("token", "user_action_required"),
        ("duplicate_token", "user_action_required"),
        ("actor", "invalid_request"),
        ("query_token", "invalid_request"),
        ("version_bool", "invalid_request"),
        ("unknown_choice", "invalid_request"),
        ("duplicate_json", "invalid_request"),
    ],
)
def test_action_precedence_and_untrusted_content(tmp_path, case, code):
    """RM37/AC28: body/source assertions cannot supply action authority."""

    async def go():
        async with action_client(tmp_path) as (client, app, service, runs, catalog, registry):
            parent = (await client.post("/relay/v1/recommendations", json=BODY)).json()
            path = f"/relay/v1/recommendations/{parent['recommendation_id']}/choice"
            body = dict(schema_version=1, expected_version=1, choice={"kind": "none"})
            headers = {"Idempotency-Key": "action"}
            if case == "bearer":
                headers["Authorization"] = "Bearer obsolete"
                body["actor"] = "user"
            elif case == "token":
                headers["X-RelayMind-User-Action-Token"] = "forged"
                body["actor"] = "user"
            elif case == "duplicate_token":
                headers = [
                    ("Idempotency-Key", "action"),
                    ("X-RelayMind-User-Action-Token", ACTION_TOKEN),
                    ("X-RelayMind-User-Action-Token", ACTION_TOKEN),
                ]
            elif case == "actor":
                body["actor"] = "user"
            elif case == "query_token":
                path += "?token=" + ACTION_TOKEN
            elif case == "version_bool":
                body["expected_version"] = True
            elif case == "unknown_choice":
                body["choice"]["event_id"] = "fake-authority"
            if case == "duplicate_json":
                response = await client.post(
                    path, content=b'{"schema_version":1,"schema_version":1}', headers=headers
                )
            else:
                response = await client.post(path, json=body, headers=headers)
            assert response.json()["error"]["code"] == code, response.text
            assert ACTION_TOKEN not in response.text
            assert (
                service.store.get(
                    service.scope, parent["recommendation_id"], now=service.clock.utc_now()
                ).view["state"]
                == "ready"
            )

    asyncio.run(go())


def test_rotation_preserves_caller_but_revoked_replays_fail(tmp_path):
    """RM37/AC28: auth is current even for immutable historical receipts."""
    from relaymind.recommendations.registrations import Registration, RegistrationSnapshot

    async def go():
        async with action_client(tmp_path) as (client, app, service, runs, catalog, registry):
            parent = (await client.post("/relay/v1/recommendations", json=BODY)).json()
            path = f"/relay/v1/recommendations/{parent['recommendation_id']}/choice"
            body = dict(schema_version=1, expected_version=1, choice={"kind": "none"})
            original = await client.post(path, json=body, headers={"Idempotency-Key": "rotation"})
            new_token = "new-credential-" + "z" * 40
            registry.replace(
                RegistrationSnapshot(
                    (
                        Registration(
                            "fixture",
                            "u",
                            "w",
                            hashlib.sha256(new_token.encode()).hexdigest(),
                            True,
                            "fixture-host",
                            "fixture-session",
                            "fixture-contract",
                        ),
                    )
                )
            )
            assert (
                await client.post(path, json=body, headers={"Idempotency-Key": "rotation"})
            ).json()["error"]["code"] == "user_action_required"
            assert (
                await client.post(
                    path,
                    json=body,
                    headers={
                        "Idempotency-Key": "rotation",
                        "X-RelayMind-User-Action-Token": new_token,
                    },
                )
            ).json() == original.json()
            # A previously authenticated action is not authority after revocation.
            action = registry.authenticate(new_token, service.scope)
            registry.replace(RegistrationSnapshot(()))
            from relaymind.api.user_actions import _claim

            with pytest.raises(Exception, match="user_action_required"):
                service.store.choose(
                    action,
                    parent["recommendation_id"],
                    body,
                    _claim(
                        action,
                        "POST",
                        "/relay/v1/recommendations/{id}/choice",
                        parent["recommendation_id"],
                        "rotation",
                        body,
                    ),
                    now=service.clock.utc_now(),
                    registrations=registry,
                )

    asyncio.run(go())


def test_post_ready_choice_accepts_unrelated_revision_and_rejects_exact_change(tmp_path):
    """RM37/AC28: choice rechecks its exact target, separately from compute pin."""
    from test_asset_sources import capture, skill

    async def go():
        async with action_client(tmp_path) as (client, app, service, runs, catalog, registry):
            first = (await client.post("/relay/v1/recommendations", json=BODY)).json()
            target = first["result"]["final_candidates"][0]
            source = tmp_path / "source"
            skill(source / "extra", name="extra", description="different", body="other")
            catalog.publish(capture(source), expected_revision=catalog.revision())
            chosen = await client.post(
                f"/relay/v1/recommendations/{first['recommendation_id']}/choice",
                json=dict(
                    schema_version=1,
                    expected_version=1,
                    choice=dict(
                        kind="asset",
                        asset_id=target["asset_id"],
                        content_hash=target["content_hash"],
                    ),
                ),
                headers={"Idempotency-Key": "asset"},
            )
            assert chosen.status_code == 200, chosen.text
            await service.refresh_catalog()
            second = (
                await client.post(
                    "/relay/v1/recommendations",
                    json=BODY,
                    headers={"Idempotency-Key": "second-parent"},
                )
            ).json()
            original = second["result"]["final_candidates"][0]
            skill(source, body="changed asset text")
            catalog.publish(capture(source), expected_revision=catalog.revision())
            changed = await client.post(
                f"/relay/v1/recommendations/{second['recommendation_id']}/choice",
                json=dict(
                    schema_version=1,
                    expected_version=1,
                    choice=dict(
                        kind="asset",
                        asset_id=original["asset_id"],
                        content_hash=original["content_hash"],
                    ),
                ),
                headers={"Idempotency-Key": "changed"},
            )
            assert changed.json()["error"]["code"] == "asset_changed", changed.text

    asyncio.run(go())


def test_feedback_fresh_error_commits_parent_choice_expiry(tmp_path):
    """RM37/AC28: fresh feedback never postpones the independent choice expiry."""
    from helpers import FakeClock

    async def go():
        clock = FakeClock()
        async with action_client(tmp_path, clock=clock) as (
            client,
            app,
            service,
            runs,
            catalog,
            registry,
        ):
            parent = (await client.post("/relay/v1/recommendations", json=BODY)).json()
            clock.advance(600)
            response = await client.post(
                f"/relay/v1/recommendations/{parent['recommendation_id']}/feedback",
                json=dict(
                    schema_version=1,
                    event_id="bad-feedback",
                    expected_feedback_revision=0,
                    feedback=dict(kind="rejection", asset_id="not-offered", content_hash="b" * 64),
                ),
                headers={"Idempotency-Key": "bad-feedback"},
            )
            assert response.status_code == 400, response.text
            with runs.sql_transaction() as db:
                row = db.execute("SELECT state,state_version FROM recommendations").fetchone()
                assert tuple(row) == ("expired", 2)

    asyncio.run(go())


def test_choice_rejects_changed_identity_even_with_same_original_hash(tmp_path, monkeypatch):
    """RM37/AC28: exact metadata/schema identity remains source-owned."""
    from dataclasses import replace

    async def go():
        async with action_client(tmp_path) as (client, app, service, runs, catalog, registry):
            parent = (await client.post("/relay/v1/recommendations", json=BODY)).json()
            target = parent["result"]["final_candidates"][0]
            snapshot = catalog.snapshot()
            changed = replace(
                snapshot,
                assets=tuple(
                    replace(asset, identity=dict(asset.identity) | {"package_version": "changed"})
                    for asset in snapshot.assets
                ),
            )
            monkeypatch.setattr(catalog, "snapshot", lambda: changed)
            response = await client.post(
                f"/relay/v1/recommendations/{parent['recommendation_id']}/choice",
                json=dict(
                    schema_version=1,
                    expected_version=1,
                    choice=dict(
                        kind="asset",
                        asset_id=target["asset_id"],
                        content_hash=target["content_hash"],
                    ),
                ),
                headers={"Idempotency-Key": "identity"},
            )
            assert response.json()["error"]["code"] == "asset_changed", response.text

    asyncio.run(go())


@pytest.mark.parametrize("embedded", [False, True])
def test_action_secrets_are_not_persisted_from_request_text(tmp_path, monkeypatch, embedded):
    """RM12/RM37/AC28: credential header never becomes model/task/event data."""

    from relaymind.selection.asset_lexical import LexicalAssetSelector

    seen = []
    select = LexicalAssetSelector.select

    async def capture(self, request, context):
        seen.append(request.to_json_object())
        return await select(self, request, context)

    monkeypatch.setattr(LexicalAssetSelector, "select", capture)

    async def go():
        async with action_client(tmp_path) as (client, app, service, runs, catalog, registry):
            bearer = app.state.application_token
            text = "hello " + ACTION_TOKEN
            if embedded:
                text = "hello prefix" + ACTION_TOKEN + "suffix prefix" + bearer + "suffix"
            body = BODY | {"task_text": text, "conversation": [{"role": "user", "text": text}]}
            parent = await client.post("/relay/v1/recommendations", json=body)
            assert parent.status_code == 200, parent.text
            assert seen
            assert ACTION_TOKEN not in str(seen)
            assert bearer not in str(seen)
            replay = await client.post("/relay/v1/recommendations", json=body)
            assert replay.content == parent.content
            assert ACTION_TOKEN not in replay.text and bearer not in replay.text
            with runs.sql_transaction() as db:
                assert ACTION_TOKEN not in str(
                    [tuple(row) for row in db.execute("SELECT accepted_json FROM recommendations")]
                )
                assert ACTION_TOKEN not in str(
                    [
                        tuple(row)
                        for row in db.execute("SELECT event_json FROM recommendation_events")
                    ]
                )
            feedback = await client.post(
                f"/relay/v1/recommendations/{parent.json()['recommendation_id']}/feedback",
                json=dict(
                    schema_version=1,
                    event_id=ACTION_TOKEN,
                    expected_feedback_revision=0,
                    feedback=dict(kind="usefulness", value="useful"),
                ),
                headers={"Idempotency-Key": "secret-event"},
            )
            assert feedback.status_code == 400

    asyncio.run(go())


def test_action_escaped_surrogate_is_invalid_request(tmp_path):
    """RM37/AC28: all accepted identifiers must have bounded UTF-8 bytes."""

    async def go():
        async with action_client(tmp_path) as (client, app, service, runs, catalog, registry):
            parent = (await client.post("/relay/v1/recommendations", json=BODY)).json()
            response = await client.post(
                f"/relay/v1/recommendations/{parent['recommendation_id']}/choice",
                content=b'{"schema_version":1,"expected_version":1,"choice":{"kind":"asset","asset_id":"\\ud800","content_hash":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}}',
                headers={"Idempotency-Key": "surrogate"},
            )
            assert response.status_code == 400, response.text

    asyncio.run(go())


def enable_fixture_action(app, service, runs):
    from relaymind.recommendations.registrations import (
        ActionRegistrations,
        Registration,
        RegistrationSnapshot,
    )

    registrations = ActionRegistrations(
        runs,
        qualification=lambda *args: (
            args
            == (
                "fixture",
                "fixture-host",
                "fixture-session",
                "fixture-contract",
            )
        ),
    )
    registrations.replace(
        RegistrationSnapshot(
            (
                Registration(
                    "fixture",
                    "u",
                    "w",
                    hashlib.sha256(ACTION_TOKEN.encode()).hexdigest(),
                    True,
                    "fixture-host",
                    "fixture-session",
                    "fixture-contract",
                ),
            )
        )
    )
    app.state.action_registrations = registrations
    from relaymind.store.asset_feedback import AssetFeedbackStore

    app.state.feedback_store = AssetFeedbackStore(
        service.store,
        service.provider.store,
        registrations,
        policy=service.provider.policy,
    )
    return registrations


def test_production_registration_gate_and_closed_private_file(tmp_path):
    """RM37/AC28: claims of qualified identity cannot enable a production channel."""
    import json

    import pytest

    from relaymind.recommendations.registrations import PrivateFileError, load_registrations

    path = tmp_path / "registrations.json"
    record = dict(
        schema_version=1,
        adapter_id="openclaw",
        user_id="u",
        workspace_id="w",
        credential_sha256=hashlib.sha256(ACTION_TOKEN.encode()).hexdigest(),
        enabled=True,
        host_identity="host",
        session_mapping="session",
        qualified_contract_revision="claimed",
    )
    from relaymind.recommendations.private_files import create_private_file_exclusive

    # Windows elevated tokens may default ordinary files to a group owner;
    # chmod does not establish TokenUser ownership or a restrictive DACL.
    assert create_private_file_exclusive(
        path, json.dumps(dict(schema_version=1, registrations=[record])).encode()
    )
    with pytest.raises(PrivateFileError):
        load_registrations(path)

    def qualification(*args):
        return args == ("openclaw", "host", "session", "claimed")

    assert (
        load_registrations(path, qualification=qualification).registrations[0].adapter_id
        == "openclaw"
    )
    record["schema_version"] = True
    path.write_text(json.dumps(dict(schema_version=1, registrations=[record])))
    with pytest.raises(PrivateFileError):
        load_registrations(path, qualification=qualification)


def test_private_registration_json_rejects_duplicate_fields_and_blank_refs(tmp_path):
    """RM37/AC28: operator records are closed and unambiguous before loading."""
    import json
    import os

    from relaymind.recommendations.private_files import create_private_file_exclusive
    from relaymind.recommendations.registrations import PrivateFileError, load_registrations

    path = tmp_path / "registration.json"
    assert create_private_file_exclusive(
        path, b'{"schema_version":1,"schema_version":1,"registrations":[]}'
    )
    with pytest.raises(PrivateFileError):
        load_registrations(path)
    raw = dict(
        schema_version=1,
        registrations=[
            dict(
                schema_version=1,
                adapter_id="fixture",
                user_id="u",
                workspace_id="w",
                credential_sha256="a" * 64,
                enabled=False,
                host_identity=" ",
                session_mapping="s",
                qualified_contract_revision="c",
            )
        ],
    )
    path.write_text(json.dumps(raw))
    with pytest.raises(PrivateFileError):
        load_registrations(path)
    path.write_text('{"schema_version":1,"registrations":[]}')
    if os.name == "nt":
        from test_private_files_windows import _set_native_acl

        _set_native_acl(path, "D:P(A;;FA;;;WD)")
    else:
        path.chmod(0o644)
    with pytest.raises(PrivateFileError):
        load_registrations(path)
    path.unlink()
    assert create_private_file_exclusive(path, b'{"schema_version":1,"registrations":[]}')
    link = tmp_path / "alias.json"
    link.symlink_to(path)
    with pytest.raises(PrivateFileError):
        load_registrations(link)


def test_default_registry_does_not_adopt_fixture_qualification_from_disk(tmp_path):
    """RM37/AC28: a persisted fixture registration does not qualify production."""
    from relaymind.recommendations.registrations import ActionRegistrations

    app, service, runs, _ = setup(tmp_path)
    enable_fixture_action(app, service, runs)
    with pytest.raises(Exception, match="user_action_required"):
        ActionRegistrations(runs).authenticate(ACTION_TOKEN, service.scope)
    runs.close()


def test_registry_replace_requires_explicit_qualified_contract_injection(tmp_path):
    """RM37/AC28: private programmatic loading obeys the same production gate."""
    import pytest

    from relaymind.recommendations.registrations import (
        ActionRegistrations,
        Registration,
        RegistrationSnapshot,
    )
    from relaymind.store import RunStore

    with RunStore(tmp_path / "registry") as runs:
        snapshot = RegistrationSnapshot(
            (Registration("openclaw", "u", "w", "a" * 64, True, "h", "s", "claim"),)
        )
        with pytest.raises(ValueError):
            ActionRegistrations(runs).replace(snapshot)


def test_launcher_resolves_private_path_and_loads_only_disabled_contracts(tmp_path):
    """RM37/AC28: no registration setting can bypass production qualification."""
    import json

    from launcher_helpers import write_config
    from relaymind.launcher import LaunchError, build_app, load_config
    from test_launcher import doubles

    path = tmp_path / "private.json"
    record = dict(
        schema_version=1,
        adapter_id="openclaw",
        user_id="u",
        workspace_id="w",
        credential_sha256="a" * 64,
        enabled=False,
        host_identity="host",
        session_mapping="session",
        qualified_contract_revision="claimed",
    )
    from relaymind.recommendations.private_files import create_private_file_exclusive

    # Windows elevated tokens may default ordinary files to a group owner;
    # chmod does not establish TokenUser ownership or a restrictive DACL.
    assert create_private_file_exclusive(
        path, json.dumps(dict(schema_version=1, registrations=[record])).encode()
    )
    config = load_config(
        write_config(tmp_path, "\n[user_actions]\nregistrations_path='private.json'\n")
    )
    assert config.user_actions.registrations_path == path
    launched = build_app(config, **doubles())
    assert launched.app.state.action_registrations is not None
    launched.orchestrator.store.close()
    record["enabled"] = True
    path.write_text(json.dumps(dict(schema_version=1, registrations=[record])))
    with pytest.raises(LaunchError, match="registration"):
        build_app(config, **doubles())


def test_choice_requires_a_separate_registered_action_even_with_current_bearer(tmp_path):
    async def go():
        app, service, runs, _ = setup(tmp_path, clock=FakeClock())
        await service.refresh_catalog()
        headers = {
            "Authorization": "Bearer " + app.state.application_token,
            "Idempotency-Key": "create-1",
        }
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1", headers=headers
        ) as client:
            created = await client.post("/relay/v1/recommendations", json=BODY)
            assert created.status_code == 200, created.text
            rid = created.json()["recommendation_id"]
            choice = await client.post(
                f"/relay/v1/recommendations/{rid}/choice",
                json={"schema_version": 1, "expected_version": 1, "choice": {"kind": "none"}},
                headers={"Idempotency-Key": "choose-1"},
            )
            assert choice.status_code == 403
            assert choice.json()["error"]["code"] == "user_action_required"
        await service.stop()
        runs.close()

    asyncio.run(go())


def test_registered_none_choice_is_one_lifecycle_action_and_no_feedback(tmp_path):
    async def go():
        app, service, runs, _ = setup(tmp_path, clock=FakeClock())
        enable_fixture_action(app, service, runs)
        await service.refresh_catalog()
        headers = {
            "Authorization": "Bearer " + app.state.application_token,
            "X-RelayMind-User-Action-Token": ACTION_TOKEN,
            "Idempotency-Key": "create-2",
        }
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1", headers=headers
        ) as client:
            created = await client.post("/relay/v1/recommendations", json=BODY)
            assert created.status_code == 200, created.text
            rid = created.json()["recommendation_id"]
            path = f"/relay/v1/recommendations/{rid}/choice"
            body = {"schema_version": 1, "expected_version": 1, "choice": {"kind": "none"}}
            chosen = await client.post(path, json=body, headers={"Idempotency-Key": "choose-2"})
            assert chosen.status_code == 200, chosen.text
            view = chosen.json()
            assert view["state"] == "chosen" and view["state_version"] == 2
            assert view["selected_action"]["adapter_id"] == "fixture"
            assert view["feedback_revision"] == 0
            assert (
                await client.post(path, json=body, headers={"Idempotency-Key": "choose-2"})
            ).json() == view
            events = (await client.get(f"/relay/v1/recommendations/{rid}/events")).json()["events"]
            assert [e["event_type"] for e in events].count("chosen") == 1
            with runs.sql_transaction() as db:
                assert db.execute("SELECT count(*) FROM feedback_records").fetchone()[0] == 0
        await service.stop()
        runs.close()

    asyncio.run(go())


def test_explicit_usefulness_feedback_has_independent_scope_revision(tmp_path):
    async def go():
        app, service, runs, _ = setup(tmp_path, clock=FakeClock())
        enable_fixture_action(app, service, runs)
        await service.refresh_catalog()
        headers = {
            "Authorization": "Bearer " + app.state.application_token,
            "X-RelayMind-User-Action-Token": ACTION_TOKEN,
            "Idempotency-Key": "create-feedback-parent",
        }
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1", headers=headers
        ) as client:
            parent = (await client.post("/relay/v1/recommendations", json=BODY)).json()
            rid = parent["recommendation_id"]
            payload = {
                "schema_version": 1,
                "event_id": "fixture-event-1",
                "expected_feedback_revision": 0,
                "feedback": {"kind": "usefulness", "value": "useful"},
            }
            response = await client.post(
                f"/relay/v1/recommendations/{rid}/feedback",
                json=payload,
                headers={"Idempotency-Key": "feedback-key-1"},
            )
            assert response.status_code == 200, response.text
            assert response.json()["feedback_revision"] == 1
            page = await client.get("/relay/v1/feedback")
            assert page.status_code == 200, page.text
            assert page.json()["feedback_revision"] == 1
            assert page.json()["records"][0]["feedback"]["kind"] == "usefulness"
            current = (await client.get(f"/relay/v1/recommendations/{rid}")).json()
            assert current["state_version"] == parent["state_version"]
            assert current["feedback_revision"] == 1
            assert (await client.post("/relay/v1/recommendations", json=BODY)).json() == parent
        await service.stop()
        runs.close()

    asyncio.run(go())


@pytest.mark.parametrize(
    "version_change,code",
    [
        (0, "stale_version"),
        (1, "recommendation_expired"),
        (-1, "stale_version"),
        (5, "stale_version"),
    ],
)
def test_choice_after_window_lapse_checks_version_before_expired_state(
    tmp_path, version_change, code
):
    """RM36/AC27, RM37/AC28, spec section 11.1: expire once, then check CAS.

    The old ready version is stale after expiry advances the lifecycle version.
    Only the current expired version reaches the recommendation_expired check.
    """
    from helpers import FakeClock

    async def go():
        clock = FakeClock()
        async with action_client(tmp_path, clock=clock) as (client, app, service, runs, *_):
            parent = (await client.post("/relay/v1/recommendations", json=BODY)).json()
            clock.advance(601)
            for _ in range(2):
                response = await client.post(
                    f"/relay/v1/recommendations/{parent['recommendation_id']}/choice",
                    json=dict(
                        schema_version=1,
                        expected_version=parent["state_version"] + version_change,
                        choice=dict(kind="none"),
                    ),
                    headers={"Idempotency-Key": "lapsed"},
                )
                assert response.status_code == 409, response.text
                assert response.json()["error"]["code"] == code, response.text
            # Inspect storage before a GET could perform a missing lazy expiry.
            with runs.sql_read() as db:
                assert tuple(
                    db.execute(
                        "SELECT state,state_version FROM recommendations WHERE recommendation_id=?",
                        (parent["recommendation_id"],),
                    ).fetchone()
                ) == ("expired", parent["state_version"] + 1)
                events = [
                    __import__("json").loads(row[0])
                    for row in db.execute(
                        "SELECT event_json FROM recommendation_events WHERE recommendation_id=?",
                        (parent["recommendation_id"],),
                    )
                ]
                assert sum(event["event_type"] == "expired" for event in events) == 1

    asyncio.run(go())


def test_choice_catalog_failure_is_internal_error_not_asset_changed(tmp_path, monkeypatch, caplog):
    """RM37/AC28, RM16: a catalog outage is never reported as a changed asset."""

    async def go():
        async with action_client(tmp_path) as (client, app, service, runs, catalog, registry):
            parent = (await client.post("/relay/v1/recommendations", json=BODY)).json()
            target = parent["result"]["final_candidates"][0]

            def broken():
                raise RuntimeError("catalog storage failed")

            monkeypatch.setattr(catalog, "snapshot", broken)
            response = await client.post(
                f"/relay/v1/recommendations/{parent['recommendation_id']}/choice",
                json=dict(
                    schema_version=1,
                    expected_version=parent["state_version"],
                    choice=dict(
                        kind="asset",
                        asset_id=target["asset_id"],
                        content_hash=target["content_hash"],
                    ),
                ),
                headers={"Idempotency-Key": "catalog-down"},
            )
            assert response.status_code == 500, response.text
            assert response.json()["error"]["code"] == "internal_error"

    asyncio.run(go())
    assert any(
        "RuntimeError" in record.getMessage() or record.exc_info for record in caplog.records
    )


@pytest.mark.parametrize("length", [256, 257])
def test_registered_action_credential_has_no_identifier_length_cap(tmp_path, monkeypatch, length):
    """RM37/AC28, section 14.5: the action credential has a minimum, not an ID cap."""
    import sys

    monkeypatch.setattr(sys.modules[__name__], "ACTION_TOKEN", "a" * length)

    async def go():
        async with action_client(tmp_path) as (client, *_):
            response = await client.get("/relay/v1/feedback")
            assert response.status_code == 200, response.text
            denied = await client.get(
                "/relay/v1/feedback", headers={"X-RelayMind-User-Action-Token": "b" * length}
            )
            assert denied.status_code == 403, denied.text

    asyncio.run(go())


def test_missing_feedback_store_reports_internal_error(tmp_path):
    """RM37/AC28, section 11.1: backend absence is not a lifecycle conflict."""

    async def go():
        async with action_client(tmp_path) as (client, app, *_):
            app.state.feedback_store = None
            response = await client.get("/relay/v1/feedback")
            assert response.status_code == 500, response.text
            assert response.json()["error"]["code"] == "internal_error"

    asyncio.run(go())


@pytest.mark.parametrize(
    "operation", ["authenticate", "choose", "create", "list", "delete", "reset"]
)
def test_action_storage_does_not_block_event_loop(tmp_path, monkeypatch, operation):
    """RM14/RM37/AC28: action I/O must leave the loop free to service cancellation."""
    from threading import Event

    async def go():
        async with action_client(tmp_path) as (client, app, service, runs, catalog, registry):
            parent = (await client.post("/relay/v1/recommendations", json=BODY)).json()
            base = f"/relay/v1/recommendations/{parent['recommendation_id']}"
            feedback_body = dict(
                schema_version=1,
                event_id="loop-progress",
                expected_feedback_revision=0,
                feedback=dict(kind="usefulness", value="useful"),
            )
            fid = None
            if operation == "delete":
                created = await client.post(
                    base + "/feedback",
                    json=feedback_body,
                    headers={"Idempotency-Key": "feedback"},
                )
                assert created.status_code == 200, created.text
                fid = created.json()["feedback_id"]
            owner = (
                registry
                if operation == "authenticate"
                else service.store
                if operation == "choose"
                else app.state.feedback_store
            )
            original = getattr(owner, operation)
            entered, release = Event(), Event()
            progressed = []

            def stalled(*args, **kwargs):
                entered.set()
                progressed.append(release.wait(2))
                return original(*args, **kwargs)

            monkeypatch.setattr(owner, operation, stalled)

            async def make_request():
                if operation in ("authenticate", "list"):
                    return await client.get("/relay/v1/feedback")
                if operation == "choose":
                    return await client.post(
                        base + "/choice",
                        json=dict(
                            schema_version=1,
                            expected_version=parent["state_version"],
                            choice=dict(kind="none"),
                        ),
                    )
                if operation == "create":
                    return await client.post(base + "/feedback", json=feedback_body)
                if operation == "delete":
                    return await client.delete(
                        f"/relay/v1/feedback/{fid}?expected_feedback_revision=1"
                    )
                return await client.post(
                    "/relay/v1/feedback/reset",
                    json=dict(
                        schema_version=1,
                        expected_feedback_revision=0,
                    ),
                )

            task = asyncio.create_task(make_request())
            try:
                assert await asyncio.to_thread(entered.wait, 5)
                release.set()
                response = await task
                assert response.status_code == 200, response.text
                assert progressed == [True], "storage blocked the event loop"
            finally:
                release.set()
                await task

    asyncio.run(go())


def test_choice_expiry_uses_time_after_worker_dispatch(tmp_path, monkeypatch):
    """RM36/RM37/AC28: waiting for a storage worker cannot extend the choice window."""
    from helpers import FakeClock
    from relaymind.api import user_actions

    async def go():
        clock = FakeClock()
        async with action_client(tmp_path, clock=clock) as (client, *_):
            parent = (await client.post("/relay/v1/recommendations", json=BODY)).json()
            dispatch = user_actions.run_in_threadpool

            async def delayed_dispatch(func, *args, **kwargs):
                clock.advance(601)
                return await dispatch(func, *args, **kwargs)

            monkeypatch.setattr(user_actions, "run_in_threadpool", delayed_dispatch)
            response = await client.post(
                f"/relay/v1/recommendations/{parent['recommendation_id']}/choice",
                json=dict(
                    schema_version=1,
                    expected_version=parent["state_version"],
                    choice=dict(kind="none"),
                ),
            )
            assert response.status_code == 409, response.text
            assert response.json()["error"]["code"] == "stale_version"
            view = (
                await client.get(f"/relay/v1/recommendations/{parent['recommendation_id']}")
            ).json()
            assert view["state"] == "expired"
            assert view["selected_action"] is None

    asyncio.run(go())
