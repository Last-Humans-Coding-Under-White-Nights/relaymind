"""RM36/AC27: independent atomic recommendation lifecycle and evidence."""

import copy
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from relaymind.store import RunStore

NOW = datetime(2026, 10, 29, tzinfo=UTC)
SCOPE = {"user_id": "u", "workspace_id": "w"}


def accepted(**changes):
    from relaymind.store.recommendations import recommendation_budgets

    value = dict(
        recommendation_id=str(uuid.uuid4()),
        task_id="a" * 64,
        task_text="greet",
        conversation=[],
        scope=SCOPE,
        budgets=recommendation_budgets(),
        configured_primary={"name": "lexical", "version": "1"},
        config_digest="b" * 64,
        catalog_revision=1,
        host_snapshot_revision=None,
        requested_kinds=["skill"],
        selection_scope="discovery",
        language_tags=[],
        code_switched=False,
        catalog=[],
        created_at=NOW,
    )
    return value | changes


def claim(key="key", digest="c" * 64, scope=None):
    from relaymind.store.recommendations import RecommendationClaim

    return RecommendationClaim(
        scope or SCOPE, "application", "POST", "/relay/v1/recommendations", None, key, digest
    )


def empty_result():
    return dict(
        schema_version=1,
        decision="empty_catalog",
        catalog_revision=1,
        host_snapshot_revision=None,
        attempts=[],
        primary_attempt=None,
        base_candidates=[],
        base_decision="empty_catalog",
        final_candidates=[],
        final_decision="empty_catalog",
        fallback_reason=None,
        adaptation=None,
        admission={
            "outcome": "accepted",
            "rechecked_catalog_revision": 1,
            "checked_at": NOW.isoformat(),
        },
        observed_ms=1,
        embeddings=[],
        host_run_ids=[],
    )


def test_atomic_lifecycle_replay_expiry_and_retention():
    from relaymind.store.recommendations import RecommendationStore

    with RunStore() as runs:
        store = RecommendationStore(runs)
        raw = accepted()
        got = store.begin(raw, claim())
        rid = got.view["recommendation_id"]
        raw["task_text"] = "changed"
        assert got.view["state"] == "computing" and got.view["state_version"] == 0
        assert (
            store.events(SCOPE, rid, after_seq=0, limit=100, now=NOW)["events"][0]["payload"][
                "task_text"
            ]
            == "greet"
        )
        result = empty_result()
        ready = store.finish(SCOPE, rid, 0, "ready", result=result, now=NOW, active_ms=1)
        result["decision"] = "changed"
        assert ready.view["result"]["decision"] == "empty_catalog"
        assert ready.view["state_version"] == 1
        replay = store.lookup_claim(claim(), now=NOW).response
        assert replay.status == 200 and replay.body == ready.view
        expired = store.get(SCOPE, rid, now=NOW + timedelta(seconds=600))
        assert expired.view["state"] == "expired" and expired.view["state_version"] == 2
        assert (
            store.lookup_claim(claim(), now=NOW + timedelta(seconds=601)).response.body["state"]
            == "ready"
        )
        page = store.events(SCOPE, rid, after_seq=0, limit=2, now=NOW + timedelta(seconds=601))
        assert [e["seq"] for e in page["events"]] == [1, 2] and page["has_more"]
        assert page["next_after_seq"] == 2
        assert (
            store.events(SCOPE, rid, after_seq=10**100, limit=1, now=NOW)["next_after_seq"]
            == 10**100
        )
        with pytest.raises(Exception, match="invalid_state"):
            store.finish(SCOPE, rid, 2, "ready", result=empty_result(), now=NOW)
        assert store.sweep(now=NOW + timedelta(days=30))["purged"] == 1
        assert store.lookup_claim(claim(), now=NOW + timedelta(days=30)) is None
        with runs.sql_transaction() as db:
            assert db.execute("SELECT count(*) FROM recommendation_events").fetchone()[0] == 0
            assert db.execute("SELECT count(*) FROM recommendation_claims").fetchone()[0] == 0


def test_slot_scope_cas_and_rollback_across_connections(tmp_path):
    from relaymind.store.recommendations import RecommendationStore

    path = tmp_path / "db"
    with RunStore(path) as a, RunStore(path) as b:
        one, two = RecommendationStore(a), RecommendationStore(b)
        obj = one.begin(accepted(), claim())
        rid = obj.view["recommendation_id"]
        for key, digest, code in [
            ("key", "c" * 64, "recommendation_in_progress"),
            ("key", "d" * 64, "idempotency_conflict"),
            ("second", "c" * 64, "overloaded"),
        ]:
            with pytest.raises(Exception, match=code):
                two.begin(accepted(), claim(key, digest))
        with pytest.raises(Exception, match="recommendation_not_found"):
            two.get({"user_id": "other", "workspace_id": "w"}, rid, now=NOW)
        with pytest.raises(Exception, match="stale_version"):
            two.finish(SCOPE, rid, 5, "cancelled", now=NOW)
        with a.sql_transaction() as db:
            db.execute(
                "CREATE TRIGGER fail_receipt BEFORE UPDATE ON recommendation_claims BEGIN "
                "SELECT RAISE(ABORT,'injected'); END"
            )
        with pytest.raises(sqlite3.IntegrityError):
            one.finish(SCOPE, rid, 0, "ready", result=empty_result(), now=NOW)
        assert two.get(SCOPE, rid, now=NOW).view["state"] == "computing"
        assert len(two.events(SCOPE, rid, after_seq=0, limit=100, now=NOW)["events"]) == 1
        with a.sql_transaction() as db:
            db.execute("DROP TRIGGER fail_receipt")
        one.finish(SCOPE, rid, 0, "cancelled", now=NOW, reason="disconnect")
        assert two.begin(accepted(), claim("second")).view["state"] == "computing"
        assert a.active_run_id() is None


def test_closed_evidence_and_guard_rollback():
    from relaymind.store.recommendation_event import (
        validate_event,
        validate_job_event,
        validate_view,
    )
    from relaymind.store.recommendations import RecommendationStore

    with RunStore() as runs:
        store = RecommendationStore(runs)
        obj = store.begin(accepted(), claim())
        view = copy.deepcopy(obj.view)
        for key in ("extra", "run_id"):
            with pytest.raises(ValueError):
                validate_view(view | {key: None})
        event = store.events(SCOPE, view["recommendation_id"], after_seq=0, limit=1, now=NOW)[
            "events"
        ][0]
        for changed in (
            {"erased": True},
            {"payload": None},
            {"seq": True},
            {"schema_version": 1.0},
        ):
            with pytest.raises(ValueError):
                validate_event(event | changed)
        with pytest.raises(ValueError):
            validate_job_event({"schema_version": 1, "event_type": "made_up"})

        def guard():
            raise RuntimeError("guard")

        with pytest.raises(RuntimeError, match="guard"):
            store.finish(
                SCOPE,
                view["recommendation_id"],
                0,
                "ready",
                result=empty_result(),
                now=NOW,
                commit_guard=guard,
            )
        assert store.get(SCOPE, view["recommendation_id"], now=NOW).view["state"] == "computing"


def test_nested_result_references_reject_invalid_candidate_and_cost():
    """RM36/AC27: view and ready event resolve local result $defs offline."""
    from relaymind.store.recommendation_event import validate_event, validate_view
    from relaymind.store.recommendations import RecommendationStore
    from test_asset_selection import candidate

    with RunStore() as runs:
        store = RecommendationStore(runs)
        rid = store.begin(accepted(), claim()).view["recommendation_id"]
        store.finish(SCOPE, rid, 0, "ready", result=empty_result(), now=NOW)
        view = copy.deepcopy(store.get(SCOPE, rid, now=NOW).view)
        event = copy.deepcopy(store.events(SCOPE, rid, after_seq=1, limit=1, now=NOW)["events"][0])
        valid_result = copy.deepcopy(view["result"])
        valid_result.update(
            decision="choice_required",
            base_decision="choice_required",
            final_decision="choice_required",
            base_candidates=[candidate()],
            final_candidates=[candidate()],
            primary_attempt=0,
        )
        valid_result["attempts"] = [
            dict(
                attempt=1,
                selector=dict(name="lexical", version="1"),
                mechanism="lexical_overlap",
                outcome="no_match",
                reason=None,
                observed_ms=1,
                reported_ms=None,
                index_revision=None,
                cost_observations=[
                    dict(
                        provenance="observed",
                        value=dict(
                            input_tokens=None,
                            output_tokens=None,
                            cached_tokens=None,
                            reasoning_tokens=None,
                            calls=0,
                            cpu_ms=None,
                            peak_bytes=None,
                        ),
                    )
                ],
                result=None,
            )
        ]
        view["result"] = valid_result
        view["decision"] = "choice_required"
        event["payload"]["result"] = valid_result
        validate_view(view)
        validate_event(event)
        for mutate in (
            lambda result: result["final_candidates"][0].update(score=True),
            lambda result: result["attempts"][0]["cost_observations"][0]["value"].update(
                calls=True
            ),
        ):
            for base, validator in ((view, validate_view), (event, validate_event)):
                bad = copy.deepcopy(base)
                mutate(bad["result"] if validator is validate_view else bad["payload"]["result"])
                with pytest.raises(ValueError):
                    validator(bad)


def test_attempt_events_version_fence_and_atomic_begin_rollback():
    """RM36/AC27: events never advance lifecycle version or escape failed admission."""
    from relaymind.store.recommendations import RecommendationStore

    with RunStore() as runs:
        store = RecommendationStore(runs)
        with runs.sql_transaction() as db:
            db.execute(
                "CREATE TRIGGER reject_accepted BEFORE INSERT ON recommendation_events BEGIN "
                "SELECT RAISE(ABORT,'injected'); END"
            )
        with pytest.raises(sqlite3.IntegrityError):
            store.begin(accepted(), claim())
        assert store.lookup_claim(claim(), now=NOW) is None
        with runs.sql_transaction() as db:
            assert (
                db.execute("SELECT recommendation_id FROM recommendation_slot").fetchone()[0]
                is None
            )
            db.execute("DROP TRIGGER reject_accepted")
        obj = store.begin(accepted(), claim())
        rid = obj.view["recommendation_id"]
        started = dict(
            event_type="selector.started",
            payload=dict(
                attempt=1,
                selector={"name": "lexical", "version": "1"},
                mechanism="lexical_overlap",
                remaining_ms=100,
            ),
        )
        store.append_attempt_event(SCOPE, rid, 0, started, now=NOW, active_ms=2)
        assert store.get(SCOPE, rid, now=NOW).view["state_version"] == 0
        store.finish(SCOPE, rid, 0, "cancelled", now=NOW, reason="shutdown", active_ms=3)
        with pytest.raises(Exception, match="stale_version"):
            store.append_attempt_event(SCOPE, rid, 0, started, now=NOW)
        events = store.events(SCOPE, rid, after_seq=0, limit=100, now=NOW)["events"]
        assert [e["seq"] for e in events] == [1, 2, 3]
        assert [e["elapsed_ms"] for e in events] == [0, 2, 3]


def test_lazy_retention_read_purges_even_when_returning_not_found():
    """RM36/AC27: absence must not roll back the lazy privacy purge."""
    from relaymind.store.recommendations import RecommendationStore

    with RunStore() as runs:
        store = RecommendationStore(runs)
        rid = store.begin(accepted(), claim()).view["recommendation_id"]
        with pytest.raises(Exception, match="recommendation_not_found"):
            store.get(SCOPE, rid, now=NOW + timedelta(days=30))
        with runs.sql_transaction() as db:
            assert db.execute("SELECT count(*) FROM recommendations").fetchone()[0] == 0


def test_acceptance_final_budget_guard_rolls_back_object_key_and_slot():
    """RM36/AC27: admission serialization consumes the original deadline too."""
    from relaymind.store.recommendations import RecommendationStore

    with RunStore() as runs:
        store = RecommendationStore(runs)

        def guard():
            raise RuntimeError("admission deadline")

        with pytest.raises(RuntimeError, match="admission deadline"):
            store.begin(accepted(), claim(), commit_guard=guard, active_ms=5)
        assert store.lookup_claim(claim(), now=NOW) is None
        with runs.sql_transaction() as db:
            assert (
                db.execute("SELECT recommendation_id FROM recommendation_slot").fetchone()[0]
                is None
            )
