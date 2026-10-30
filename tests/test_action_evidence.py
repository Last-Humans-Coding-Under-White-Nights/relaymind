"""RM37/AC28: one published ActionEvidenceV1 shape, with no new wire fields."""

import asyncio
import copy
from importlib import resources

import pytest
from jsonschema import FormatChecker
from jsonschema.exceptions import ValidationError

from recommendation_helpers import BODY, NOW, SCOPE, accepted, claim
from relaymind.schema_util import StrictValidator, load_packaged_schema
from relaymind.store import RunStore
from relaymind.store.recommendation_event import validate_event, validate_view
from relaymind.store.recommendations import RecommendationStore
from test_asset_user_actions import action_client


def action_validator():
    path = resources.files("relaymind.store").joinpath("schemas", "action_evidence.v1.json")
    assert path.is_file(), "the published ActionEvidenceV1 schema must be packaged"
    schema = load_packaged_schema("relaymind.store", "action_evidence.v1.json")
    return schema, StrictValidator(schema, format_checker=FormatChecker())


def action_fixture():
    return dict(
        adapter_id="fixture",
        scope=SCOPE,
        recommendation_id="00000000-0000-0000-0000-000000000001",
        task_id="a" * 64,
        choice=dict(kind="none"),
        catalog_revision=1,
        host_snapshot_revision=None,
        recorded_at=NOW.isoformat(),
        state_version=2,
    )


def test_published_action_evidence_contract_is_one_closed_packaged_object():
    schema, validator = action_validator()
    assert schema["$id"] == "https://relaymind.local/schemas/action_evidence.v1.json"
    assert set(schema["required"]) == set(action_fixture())
    assert "schema_version" not in schema["properties"]
    assert "source_version" not in schema["properties"]
    for choice in (
        dict(kind="none"),
        dict(kind="cancel"),
        dict(kind="asset", asset_id="fixture:known", content_hash="b" * 64),
    ):
        validator.validate(action_fixture() | dict(choice=choice))


@pytest.mark.parametrize(
    "change",
    [
        dict(schema_version=1),
        dict(source_version="invented"),
        dict(host_snapshot_revision="caller"),
        dict(state_version=True),
        dict(catalog_revision=-1),
        dict(task_id=None),
        dict(scope=dict(user_id="u", workspace_id="w", actor="user")),
        dict(choice=dict(kind="unknown")),
        dict(choice=dict(kind="none", actor="user")),
        dict(choice=dict(kind="asset", asset_id="known", content_hash="bad")),
        None,
    ],
)
def test_standalone_action_evidence_rejects_unpublished_or_invalid_fields(change):
    _, validator = action_validator()
    action = action_fixture()
    if change is None:
        del action["adapter_id"]
    else:
        action.update(change)
    with pytest.raises(ValidationError):
        validator.validate(action)


@pytest.mark.parametrize("kind", ["asset", "none", "cancel"])
def test_actual_action_view_and_event_share_the_offline_contract(tmp_path, kind):
    async def go():
        _, validator = action_validator()
        async with action_client(tmp_path) as (client, app, service, runs, catalog, registry):
            parent = (await client.post("/relay/v1/recommendations", json=BODY)).json()
            rid = parent["recommendation_id"]
            choice = dict(kind=kind)
            if kind == "asset":
                candidate = parent["result"]["final_candidates"][0]
                choice.update(
                    asset_id=candidate["asset_id"], content_hash=candidate["content_hash"]
                )
            response = await client.post(
                f"/relay/v1/recommendations/{rid}/choice",
                json=dict(schema_version=1, expected_version=1, choice=choice),
                headers={"Idempotency-Key": "published-action"},
            )
            assert response.status_code == 200, response.text
            view = response.json()
            events = (await client.get(f"/relay/v1/recommendations/{rid}/events")).json()["events"]
            event = events[-1]
            assert event["payload"]["action"] == view["selected_action"]
            validator.validate(view["selected_action"])
            validate_view(view)
            validate_event(event)
            # The standalone and both transitive consumers reject the same extra field.
            changed_view, changed_event = copy.deepcopy(view), copy.deepcopy(event)
            changed_view["selected_action"]["source_version"] = "not-a-wire-field"
            changed_event["payload"]["action"]["source_version"] = "not-a-wire-field"
            with pytest.raises(ValueError):
                validate_view(changed_view)
            with pytest.raises(ValueError):
                validate_event(changed_event)

    asyncio.run(go())


def test_gateway_cancellation_keeps_its_distinct_null_action():
    with RunStore() as runs:
        store = RecommendationStore(runs)
        rid = store.begin(accepted(), claim()).view["recommendation_id"]
        store.finish(SCOPE, rid, 0, "cancelled", now=NOW, reason="shutdown")
        event = store.events(SCOPE, rid, after_seq=0, limit=100, now=NOW)["events"][-1]
        assert event["payload"]["origin"] == "gateway" and event["payload"]["action"] is None
        validate_event(event)
