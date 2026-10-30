"""RM37/AC28: explicit feedback erasure, replay and retention ownership."""

import asyncio
from datetime import datetime, timedelta

import httpx
import pytest

from recommendation_helpers import BODY, setup
from test_asset_user_actions import ACTION_TOKEN, enable_fixture_action


def privacy_store(tmp_path, *, before_owner=None):
    """Real SQLite facades, deterministic registered fixture, no host authority."""
    import hashlib

    from relaymind.recommendations.registrations import (
        ActionRegistrations,
        Registration,
        RegistrationSnapshot,
    )
    from relaymind.store import RunStore
    from relaymind.store.asset_catalog import AssetCatalogStore
    from relaymind.store.asset_feedback import AssetFeedbackStore
    from relaymind.store.recommendations import RecommendationStore

    runs = RunStore(tmp_path / "privacy-db")
    recommendations = RecommendationStore(runs)
    if before_owner is not None:
        before_owner.append(RecommendationStore(runs))
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
            tuple(
                Registration(
                    "fixture",
                    "u",
                    workspace,
                    hashlib.sha256((ACTION_TOKEN + workspace).encode()).hexdigest(),
                    True,
                    "fixture-host",
                    "fixture-session",
                    "fixture-contract",
                )
                for workspace in ("a", "b")
            )
        )
    )
    feedback = AssetFeedbackStore(recommendations, AssetCatalogStore(runs), registrations)
    return runs, recommendations, registrations, feedback


def insert_feedback(recommendations, registrations, feedback, *, workspace, key, now, budgets=None):
    from recommendation_helpers import accepted, claim, empty_result
    from relaymind.api.user_actions import _claim

    scope = {"user_id": "u", "workspace_id": workspace}
    parent = accepted(scope=scope, created_at=now, **({"budgets": budgets} if budgets else {}))
    rid = parent["recommendation_id"]
    recommendations.begin(parent, claim(key, scope=scope))
    recommendations.finish(scope, rid, 0, "ready", result=empty_result(), now=now)
    action = registrations.authenticate(ACTION_TOKEN + workspace, scope)
    request = dict(
        schema_version=1,
        event_id=key,
        expected_feedback_revision=feedback_revision(feedback, scope),
        feedback={"kind": "usefulness", "value": "useful"},
    )
    receipt = _claim(action, "POST", "/relay/v1/recommendations/{id}/feedback", rid, key, request)
    response = feedback.create(action, rid, request, receipt, now=now)
    return rid, response.body["feedback_id"]


def feedback_revision(feedback, scope):
    with feedback.store.sql_transaction() as db:
        return feedback._scope_revision(db, scope)


@pytest.mark.parametrize("new_facade", [False, True], ids=["same-facade", "shared-run-store"])
def test_second_feedback_owner_is_rejected_before_any_binding_or_hook_change(tmp_path, new_facade):
    """RM37/AC28: lazy expiry must keep the original private cleanup owner."""
    import pytest

    from recommendation_helpers import NOW
    from relaymind.recommendations.errors import RecommendationError
    from relaymind.store.asset_feedback import AssetFeedbackStore
    from relaymind.store.recommendations import RecommendationStore

    runs, recommendations, registrations, owner = privacy_store(tmp_path)
    calls = []
    owner.invalidate_derived = lambda scope: calls.append(dict(scope))
    rid, _ = insert_feedback(
        recommendations, registrations, owner, workspace="a", key="one-owner", now=NOW
    )
    scope = dict(user_id="u", workspace_id="a")
    assert calls == [scope]
    calls.clear()
    original_callback = recommendations._maintain_feedback
    original_hooks = tuple(recommendations._purge_hooks)
    target = RecommendationStore(runs) if new_facade else recommendations
    target_callback = target._maintain_feedback
    target_hooks = tuple(target._purge_hooks)
    with pytest.raises(ValueError, match="feedback owner"):
        AssetFeedbackStore(target, owner.catalog, registrations)
    assert target._maintain_feedback == target_callback
    assert tuple(target._purge_hooks) == target_hooks
    assert recommendations._maintain_feedback == original_callback
    assert tuple(recommendations._purge_hooks) == original_hooks
    assert original_callback.__self__ is owner
    late = NOW + timedelta(days=30)
    with pytest.raises(RecommendationError, match="recommendation_not_found"):
        recommendations.get(scope, rid, now=late)
    assert calls == [scope]
    with runs.sql_read() as db:
        assert db.execute("SELECT count(*) FROM feedback_records").fetchone()[0] == 0
        assert db.execute("SELECT feedback_revision FROM recommendation_scope").fetchone()[0] == 2
        assert db.execute("SELECT count(*) FROM recommendations").fetchone()[0] == 1
    recommendations.sweep(now=late)
    assert calls == [scope]
    runs.close()


def test_shared_run_store_facade_sweep_uses_the_existing_feedback_owner(tmp_path):
    """RM37/AC28: a facade cannot purge owned rows without their cleanup owner."""
    from recommendation_helpers import NOW
    from relaymind.store.recommendations import RecommendationStore

    runs, recommendations, registrations, owner = privacy_store(tmp_path)
    calls = []
    owner.invalidate_derived = lambda scope: calls.append(dict(scope))
    insert_feedback(
        recommendations, registrations, owner, workspace="a", key="facade-sweep", now=NOW
    )
    calls.clear()
    scope = dict(user_id="u", workspace_id="a")
    late = NOW + timedelta(days=30)
    assert RecommendationStore(runs).sweep(now=late)["purged"] == 1
    assert calls == [scope]
    with runs.sql_read() as db:
        assert db.execute("SELECT count(*) FROM feedback_records").fetchone()[0] == 0
        assert db.execute("SELECT feedback_revision FROM recommendation_scope").fetchone()[0] == 2
    recommendations.sweep(now=late)
    assert calls == [scope]
    runs.close()


@pytest.mark.parametrize("timing", ["before", "after"])
@pytest.mark.parametrize("entrypoint", ["begin", "sweep", "recover"])
def test_all_shared_facade_parent_purges_use_one_lifetime_hook(tmp_path, timing, entrypoint):
    """RM37/AC28: every physical purge uses the one shared RunStore owner."""
    from recommendation_helpers import NOW, accepted, claim
    from relaymind.store.recommendations import RecommendationStore

    early = []
    runs, recommendations, registrations, owner = privacy_store(tmp_path, before_owner=early)
    facade = early[0] if timing == "before" else RecommendationStore(runs)
    calls, batches = [], []
    owner.invalidate_derived = lambda scope: calls.append(dict(scope))
    rid, _ = insert_feedback(
        recommendations, registrations, owner, workspace="a", key="purge-matrix", now=NOW
    )
    calls.clear()

    def observe(db, ids, *, now):
        batches.append(ids)

    recommendations.register_purge_hook(observe)
    scope = dict(user_id="u", workspace_id="a")
    late = NOW + timedelta(days=30)
    if entrypoint == "begin":
        facade.begin(accepted(scope=scope, created_at=late), claim("next-parent", scope=scope))
    else:
        getattr(facade, entrypoint)(now=late)
    assert calls == [scope]
    assert batches == ([(rid,), ()] if entrypoint == "recover" else [(rid,)])
    with runs.sql_read() as db:
        assert db.execute("SELECT count(*) FROM feedback_records").fetchone()[0] == 0
        assert db.execute("SELECT feedback_revision FROM recommendation_scope").fetchone()[0] == 2
    recommendations.sweep(now=late)
    assert calls == [scope]
    runs.close()


@pytest.mark.parametrize("timing", ["before", "after"])
@pytest.mark.parametrize("entrypoint", ["get", "events", "choice"])
def test_shared_facade_scoped_reads_and_choice_use_the_lifetime_owner(tmp_path, timing, entrypoint):
    """RM37/AC28: scope cleanup follows A even through an earlier or later facade."""
    from recommendation_helpers import NOW
    from relaymind.api.user_actions import _claim
    from relaymind.recommendations.errors import RecommendationError
    from relaymind.store.recommendations import RecommendationStore

    early = []
    runs, recommendations, registrations, owner = privacy_store(tmp_path, before_owner=early)
    facade = early[0] if timing == "before" else RecommendationStore(runs)
    calls = []
    owner.invalidate_derived = lambda scope: calls.append(dict(scope))
    rid, _ = insert_feedback(
        recommendations, registrations, owner, workspace="a", key="read-matrix", now=NOW
    )
    calls.clear()
    scope = dict(user_id="u", workspace_id="a")
    late = NOW + timedelta(days=30)
    with pytest.raises(RecommendationError, match="recommendation_not_found"):
        if entrypoint == "get":
            facade.get(scope, rid, now=late)
        elif entrypoint == "events":
            facade.events(scope, rid, after_seq=0, limit=100, now=late)
        else:
            action = registrations.authenticate(ACTION_TOKEN + "a", scope)
            body = dict(schema_version=1, expected_version=1, choice=dict(kind="none"))
            facade.choose(
                action,
                rid,
                body,
                _claim(
                    action,
                    "POST",
                    "/relay/v1/recommendations/{id}/choice",
                    rid,
                    "facade-choice",
                    body,
                ),
                now=late,
                registrations=registrations,
            )
    assert calls == [scope]
    with runs.sql_read() as db:
        assert db.execute("SELECT count(*) FROM feedback_records").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM recommendations").fetchone()[0] == 1
        assert db.execute("SELECT feedback_revision FROM recommendation_scope").fetchone()[0] == 2
    runs.close()


def test_shared_facade_claim_lookup_stays_logical_and_does_not_clean(tmp_path):
    """RM36/RM37 AC27/AC28: a historical claim lookup adds no cleanup write."""
    from recommendation_helpers import NOW, claim

    early = []
    runs, recommendations, registrations, owner = privacy_store(tmp_path, before_owner=early)
    calls, statements = [], []
    owner.invalidate_derived = lambda scope: calls.append(dict(scope))
    insert_feedback(
        recommendations, registrations, owner, workspace="a", key="claim-matrix", now=NOW
    )
    calls.clear()
    scope = dict(user_id="u", workspace_id="a")
    runs._conn.set_trace_callback(statements.append)
    assert (
        early[0].lookup_claim(claim("claim-matrix", scope=scope), now=NOW + timedelta(days=30))
        is None
    )
    runs._conn.set_trace_callback(None)
    assert calls == [] and "BEGIN IMMEDIATE" not in statements
    with runs.sql_read() as db:
        assert db.execute("SELECT count(*) FROM feedback_records").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM recommendations").fetchone()[0] == 1
    runs.close()


def test_shared_facade_cleanup_failure_rolls_back_the_whole_owner_batch(tmp_path):
    """RM37/AC28: a facade cannot commit parent deletion after owner cleanup fails."""
    from recommendation_helpers import NOW
    from relaymind.store.recommendations import RecommendationStore

    runs, recommendations, registrations, owner = privacy_store(tmp_path)
    insert_feedback(
        recommendations, registrations, owner, workspace="a", key="shared-rollback", now=NOW
    )

    def fail(scope):
        raise RuntimeError("owner cleanup failed")

    owner.invalidate_derived = fail
    with pytest.raises(RuntimeError, match="owner cleanup failed"):
        RecommendationStore(runs).sweep(now=NOW + timedelta(days=30))
    with runs.sql_read() as db:
        assert db.execute("SELECT count(*) FROM recommendations").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM feedback_records").fetchone()[0] == 1
        assert db.execute("SELECT feedback_revision FROM recommendation_scope").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM feedback_audits").fetchone()[0] == 0
    runs.close()


def test_logical_parent_retention_cleans_only_scoped_feedback_before_read(tmp_path):
    """RM36/RM37 AC27/AC28: lazy feedback privacy never physically deletes parents."""
    import pytest

    from recommendation_helpers import NOW, claim
    from relaymind.recommendations.errors import RecommendationError

    runs, recommendations, registrations, feedback = privacy_store(tmp_path)
    rid, fid = insert_feedback(
        recommendations, registrations, feedback, workspace="a", key="logical-a", now=NOW
    )
    other_rid, other_fid = insert_feedback(
        recommendations, registrations, feedback, workspace="b", key="logical-b", now=NOW
    )
    scope = dict(user_id="u", workspace_id="a")
    late = NOW + timedelta(days=30)
    with pytest.raises(RecommendationError, match="recommendation_not_found"):
        recommendations.get(scope, rid, now=late)
    assert recommendations.lookup_claim(claim("logical-a", scope=scope), now=late) is None
    with runs.sql_read() as db:
        assert db.execute("SELECT count(*) FROM recommendations").fetchone()[0] == 2
        assert (
            db.execute(
                "SELECT count(*) FROM feedback_records WHERE feedback_id=?", (fid,)
            ).fetchone()[0]
            == 0
        )
        assert (
            db.execute(
                "SELECT count(*) FROM feedback_records WHERE feedback_id=?", (other_fid,)
            ).fetchone()[0]
            == 1
        )
        assert [
            tuple(r)
            for r in db.execute(
                "SELECT workspace_id,feedback_revision FROM recommendation_scope "
                "ORDER BY workspace_id"
            )
        ] == [("a", 2), ("b", 1)]
        assert not any(
            __import__("json").loads(row[0])["event_type"] == "expired"
            for row in db.execute(
                "SELECT event_json FROM recommendation_events WHERE recommendation_id=?", (rid,)
            )
        )
    assert recommendations.sweep(now=late)["purged"] == 2
    assert not (tmp_path / "privacy-db.erasure-key").exists()
    runs.close()


def test_unchanged_parent_and_feedback_reads_use_read_snapshots_and_live_revision(tmp_path):
    """RM36/RM37 AC27/AC28: no due privacy/choice work means no write transaction."""
    from recommendation_helpers import NOW, claim

    runs, recommendations, registrations, feedback = privacy_store(tmp_path)
    rid, _ = insert_feedback(
        recommendations, registrations, feedback, workspace="a", key="read-only", now=NOW
    )
    scope = dict(user_id="u", workspace_id="a")
    action = registrations.authenticate(ACTION_TOKEN + "a", scope)
    current = feedback.catalog.snapshot()
    statements = []
    runs._conn.set_trace_callback(statements.append)
    view = recommendations.get(scope, rid, now=NOW).view
    assert view["feedback_revision"] == 1
    recommendations.events(scope, rid, after_seq=0, limit=100, now=NOW)
    assert (
        recommendations.lookup_claim(claim("read-only", scope=scope), now=NOW).response.body[
            "feedback_revision"
        ]
        == 0
    )
    assert feedback.list(action, now=NOW)["feedback_revision"] == 1
    assert feedback.eligible_corrections(scope, current, now=NOW) == ()
    runs._conn.set_trace_callback(None)
    assert any(sql == "BEGIN DEFERRED" for sql in statements)
    assert not any(sql == "BEGIN IMMEDIATE" for sql in statements)
    runs.close()


def test_feedback_and_receipts_keep_the_parent_accepted_short_retention(tmp_path):
    """RM03/RM36/RM37 AC27/AC28: recorded parent deadlines bound private children."""
    import pytest

    from recommendation_helpers import NOW
    from relaymind.recommendations.errors import RecommendationError
    from relaymind.recommendations.limits import recommendation_budgets
    from relaymind.store.run_store import utc_iso

    runs, recommendations, registrations, feedback = privacy_store(tmp_path)
    budgets = recommendation_budgets() | dict(choice_seconds=60, retention_days=2, display_limit=1)
    rid, _ = insert_feedback(
        recommendations,
        registrations,
        feedback,
        workspace="a",
        key="accepted-short",
        now=NOW,
        budgets=budgets,
    )
    scope = dict(user_id="u", workspace_id="a")
    view = recommendations.get(scope, rid, now=NOW).view
    assert view["choice_expires_at"] == utc_iso(NOW + timedelta(seconds=60))
    deadline = utc_iso(NOW + timedelta(days=2))
    assert view["retain_until"] == deadline
    with runs.sql_read() as db:
        assert db.execute("SELECT retain_until FROM feedback_records").fetchone()[0] == deadline
        assert db.execute("SELECT retain_until FROM action_receipts").fetchone()[0] == deadline
        assert db.execute("SELECT retain_until FROM feedback_event_ids").fetchone()[0] == deadline
    with pytest.raises(RecommendationError, match="recommendation_not_found"):
        recommendations.get(scope, rid, now=NOW + timedelta(days=2))
    with runs.sql_read() as db:
        assert db.execute("SELECT count(*) FROM recommendations").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM feedback_records").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM action_receipts").fetchone()[0] == 0
    assert recommendations.sweep(now=NOW + timedelta(days=2))["purged"] == 1
    runs.close()


def test_retention_batches_scopes_and_standalone_expiry_without_a_key(tmp_path):
    """RM37/AC28: one revision per affected scope, including empty-parent batches."""
    from recommendation_helpers import NOW
    from relaymind.store.run_store import utc_iso

    runs, recommendations, registrations, feedback = privacy_store(tmp_path)
    pairs = [
        insert_feedback(
            recommendations, registrations, feedback, workspace=workspace, key=str(i), now=NOW
        )
        for i, workspace in enumerate(("a", "a", "a", "b"))
    ]
    due = NOW + timedelta(days=1)
    with runs.sql_transaction() as db:
        for rid, _ in (pairs[0], pairs[1], pairs[3]):
            db.execute(
                "UPDATE recommendations SET retain_until=? WHERE recommendation_id=?",
                (utc_iso(due), rid),
            )
        db.execute(
            "UPDATE feedback_records SET retain_until=? WHERE feedback_id=?",
            (utc_iso(due), pairs[2][1]),
        )
        db.execute("UPDATE action_receipts SET retain_until=?", (utc_iso(due),))
        db.execute("UPDATE feedback_event_ids SET retain_until=?", (utc_iso(due),))
    recommendations.sweep(now=due)
    recommendations.sweep(now=due)
    with runs.sql_transaction() as db:
        assert db.execute("SELECT count(*) FROM feedback_records").fetchone()[0] == 0
        assert [
            tuple(r)
            for r in db.execute(
                "SELECT workspace_id,feedback_revision FROM recommendation_scope "
                "ORDER BY workspace_id"
            )
        ] == [("a", 4), ("b", 2)]
        audits = [
            __import__("json").loads(r[0])
            for r in db.execute("SELECT audit_json FROM feedback_audits")
        ]
        assert sorted(a["removed_count"] for a in audits) == [1, 3]
        assert db.execute("SELECT count(*) FROM action_receipts").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM erasure_key_binding").fetchone()[0] == 0
    assert not (tmp_path / "privacy-db.erasure-key").exists()
    # The surviving parent gets independent earlier feedback expiry, with no
    # expired-parent IDs at all on this sweep.
    _, fid = insert_feedback(
        recommendations, registrations, feedback, workspace="a", key="later", now=due
    )
    with runs.sql_transaction() as db:
        db.execute(
            "UPDATE feedback_records SET retain_until=? WHERE feedback_id=?", (utc_iso(due), fid)
        )
        db.execute("UPDATE action_receipts SET retain_until=?", (utc_iso(due),))
        db.execute("UPDATE feedback_event_ids SET retain_until=?", (utc_iso(due),))
    recommendations.sweep(now=due)
    assert feedback_revision(feedback, {"user_id": "u", "workspace_id": "a"}) == 6
    runs.close()


def test_retention_hook_failure_rolls_back_parent_feedback_and_revision(tmp_path):
    """RM37/AC28: dependent erasure and parent purge have one commit boundary."""
    import pytest

    from recommendation_helpers import NOW

    runs, recommendations, registrations, feedback = privacy_store(tmp_path)
    insert_feedback(
        recommendations, registrations, feedback, workspace="a", key="rollback", now=NOW
    )

    def fail(db, ids, *, now):
        raise RuntimeError("injected retention failure")

    recommendations.register_purge_hook(fail)
    with pytest.raises(RuntimeError, match="injected"):
        recommendations.sweep(now=NOW + timedelta(days=30))
    with runs.sql_transaction() as db:
        assert db.execute("SELECT count(*) FROM recommendations").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM feedback_records").fetchone()[0] == 1
        assert db.execute("SELECT feedback_revision FROM recommendation_scope").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM feedback_audits").fetchone()[0] == 0
    runs.close()


def test_erasure_key_loss_or_replacement_refuses_replay_in_the_same_process(tmp_path):
    """RM37/AC28: cached key bytes cannot bypass protected-file continuity."""
    import pytest

    from recommendation_helpers import NOW
    from relaymind.api.user_actions import _claim
    from relaymind.recommendations.registrations import PrivateFileError

    runs, recommendations, registrations, feedback = privacy_store(tmp_path)
    rid, fid = insert_feedback(
        recommendations, registrations, feedback, workspace="a", key="key-loss", now=NOW
    )
    action = registrations.authenticate(ACTION_TOKEN + "a", {"user_id": "u", "workspace_id": "a"})
    request = dict(schema_version=1, expected_feedback_revision=1)
    claim = _claim(action, "POST", "/relay/v1/feedback/reset", None, "reset-key-loss", request)
    feedback.reset(action, 1, claim, now=NOW)
    path = tmp_path / "privacy-db.erasure-key"
    original = path.read_bytes()
    path.unlink()
    with pytest.raises(PrivateFileError):
        feedback.reset(action, 1, claim, now=NOW)
    assert not path.exists()
    path.write_bytes(b"r" * 32)
    path.chmod(0o600)
    with pytest.raises(PrivateFileError):
        feedback.reset(action, 1, claim, now=NOW)
    # Operator restore crosses a process/store lifetime. New bytes under a new
    # inode never authorize continuing the old provider's physical identity.
    path.write_bytes(original)
    runs.close()
    from relaymind.recommendations.registrations import ActionRegistrations
    from relaymind.store import RunStore
    from relaymind.store.asset_catalog import AssetCatalogStore
    from relaymind.store.asset_feedback import AssetFeedbackStore
    from relaymind.store.recommendations import RecommendationStore

    with RunStore(tmp_path / "privacy-db") as reopened:
        restored_registrations = ActionRegistrations(
            reopened, qualification=registrations.qualification
        )
        restored = AssetFeedbackStore(
            RecommendationStore(reopened), AssetCatalogStore(reopened), restored_registrations
        )
        restored_action = restored_registrations.authenticate(ACTION_TOKEN + "a", action.scope)
        assert restored.reset(restored_action, 1, claim, now=NOW).body["removed_count"] == 1
        with reopened.sql_read() as db:
            assert (
                db.execute("SELECT feedback_revision FROM recommendation_scope").fetchone()[0] == 2
            )


def test_eligible_corrections_have_exact_catalog_provenance(tmp_path):
    """RM37/AC28: changed source/schema identity cannot feed later adaptation."""
    from relaymind.recommendations.erasure import HmacErasureDigester

    app, service, runs, catalog = setup(tmp_path)
    enable_fixture_action(app, service, runs)
    feedback = app.state.feedback_store
    feedback.erasure = HmacErasureDigester(b"x" * 32)
    action = app.state.action_registrations.authenticate(ACTION_TOKEN, service.scope)
    from recommendation_helpers import NOW, accepted, claim, empty_result
    from relaymind.api.user_actions import _claim

    original = catalog.snapshot().assets[0]
    parent = accepted(catalog=[original.to_json_object()])
    rid = parent["recommendation_id"]
    service.store.begin(parent, claim())
    service.store.finish(service.scope, rid, 0, "ready", result=empty_result(), now=NOW)
    body = dict(
        schema_version=1,
        event_id="correction",
        expected_feedback_revision=0,
        feedback=dict(
            kind="correction", asset_id=original.asset_id, content_hash=original.content_hash
        ),
    )
    response = feedback.create(
        action,
        rid,
        body,
        _claim(action, "POST", "/relay/v1/recommendations/{id}/feedback", rid, "correction", body),
        now=NOW,
    )
    assert response.status == 200
    assert len(feedback.eligible_corrections(service.scope, catalog.snapshot(), now=NOW)) == 1
    from dataclasses import replace

    changed = replace(catalog.snapshot(), assets=(replace(original, source_version="changed"),))
    assert feedback.eligible_corrections(service.scope, changed, now=NOW) == ()
    assert (
        feedback.eligible_corrections(
            {"user_id": "foreign", "workspace_id": "w"}, catalog.snapshot(), now=NOW
        )
        == ()
    )
    runs.close()


def test_invalid_feedback_cursor_is_a_closed_bounded_value(tmp_path):
    """RM37/AC28: malformed cursor never becomes a SQL overflow or alias."""
    import base64

    import pytest

    from recommendation_helpers import NOW

    runs, recommendations, registrations, feedback = privacy_store(tmp_path)
    action = registrations.authenticate(ACTION_TOKEN + "a", {"user_id": "u", "workspace_id": "a"})
    for cursor in (
        "!MQ",
        "MQ==",
        base64.urlsafe_b64encode(str(2**70).encode()).decode().rstrip("="),
    ):
        with pytest.raises(Exception, match="invalid_request"):
            feedback.list(action, after_id=cursor, now=NOW)
    runs.close()


def test_feedback_page_sequence_survives_reset_and_uuid_disorder(tmp_path, monkeypatch):
    """RM37/AC28: cursor is insertion order and sequence is never reused."""
    import uuid

    from recommendation_helpers import NOW
    from relaymind.api.user_actions import _claim

    runs, recommendations, registrations, feedback = privacy_store(tmp_path)
    # Generated UUID lexical order is deliberately unrelated to insertion order.
    values = iter(
        [
            uuid.UUID(int=99),
            uuid.UUID(int=90),
            uuid.UUID(int=80),
            uuid.UUID(int=70),
            uuid.UUID(int=60),
            uuid.UUID(int=50),
        ]
    )
    from types import SimpleNamespace

    monkeypatch.setattr(
        "relaymind.store.asset_feedback.uuid", SimpleNamespace(uuid4=lambda: next(values))
    )
    first = insert_feedback(
        recommendations, registrations, feedback, workspace="a", key="page1", now=NOW
    )
    second = insert_feedback(
        recommendations, registrations, feedback, workspace="a", key="page2", now=NOW
    )
    action = registrations.authenticate(ACTION_TOKEN + "a", {"user_id": "u", "workspace_id": "a"})
    page = feedback.list(action, limit=1, now=NOW)
    assert page["records"][0]["feedback_id"] == first[1]
    cursor = page["next_after_id"]
    assert feedback.list(action, after_id=cursor, now=NOW)["records"][0]["feedback_id"] == second[1]
    body = dict(schema_version=1, expected_feedback_revision=2)
    feedback.reset(
        action,
        2,
        _claim(action, "POST", "/relay/v1/feedback/reset", None, "paging-reset", body),
        now=NOW,
    )
    later = insert_feedback(
        recommendations, registrations, feedback, workspace="a", key="page3", now=NOW
    )
    assert feedback.list(action, after_id=cursor, now=NOW)["records"][0]["feedback_id"] == later[1]
    runs.close()


def test_private_file_lease_checks_same_opened_owner_and_path(tmp_path):
    """RM37/AC28: final and ancestor substitutions cannot bypass privacy."""
    import pytest

    from relaymind.recommendations.private_files import (
        PrivateFileError,
        create_private_file_exclusive,
        open_private_file,
    )

    path = tmp_path / "private-key"
    assert create_private_file_exclusive(path, b"x" * 32)
    assert not create_private_file_exclusive(path, b"y" * 32)
    with open_private_file(path, maximum=32) as lease:
        assert lease.data == b"x" * 32
        assert "xxxxxxxx" not in repr(lease)
        path.chmod(0o644)
        with pytest.raises(PrivateFileError):
            lease.verify()
    path.chmod(0o600)
    alias = tmp_path / "alias"
    alias.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(PrivateFileError):
        with open_private_file(alias / "private-key", maximum=32):
            pass
    with open_private_file(path, maximum=32) as lease:
        path.rename(tmp_path / "old-key")
        assert create_private_file_exclusive(path, b"x" * 32)
        with pytest.raises(PrivateFileError):
            lease.verify()


def test_scope_invalidator_is_transactional_and_never_runs_on_replay(tmp_path):
    """RM37/AC28: synchronous derived cleanup may stay unavailable on rollback."""
    import pytest

    from recommendation_helpers import NOW
    from relaymind.api.user_actions import _claim

    runs, recommendations, registrations, feedback = privacy_store(tmp_path)
    calls = []
    feedback.invalidate_derived = lambda scope: calls.append(dict(scope))
    rid, fid = insert_feedback(
        recommendations, registrations, feedback, workspace="a", key="invalidator", now=NOW
    )
    assert calls == [{"user_id": "u", "workspace_id": "a"}]
    action = registrations.authenticate(ACTION_TOKEN + "a", calls[0])
    body = dict(schema_version=1, expected_feedback_revision=1)
    claim = _claim(action, "POST", "/relay/v1/feedback/reset", None, "invalidate-reset", body)

    def fail(scope):
        calls.append(dict(scope))
        raise RuntimeError("cleanup failed")

    feedback.invalidate_derived = fail
    with pytest.raises(RuntimeError, match="cleanup failed"):
        feedback.reset(action, 1, claim, now=NOW)
    with runs.sql_transaction() as db:
        assert db.execute("SELECT count(*) FROM feedback_records").fetchone()[0] == 1
        assert db.execute("SELECT feedback_revision FROM recommendation_scope").fetchone()[0] == 1
    feedback.invalidate_derived = lambda scope: calls.append(dict(scope))
    feedback.reset(action, 1, claim, now=NOW)
    count = len(calls)
    feedback.reset(action, 1, claim, now=NOW)
    recommendations.sweep(now=NOW)
    assert len(calls) == count
    runs.close()


def test_key_lease_rejects_same_bytes_replacement_and_missing_binding(tmp_path):
    """RM37/AC28: file identity is checked during process life; state needs binding."""
    import pytest

    from recommendation_helpers import NOW
    from relaymind.api.user_actions import _claim

    runs, recommendations, registrations, feedback = privacy_store(tmp_path)
    insert_feedback(
        recommendations, registrations, feedback, workspace="a", key="same-key", now=NOW
    )
    action = registrations.authenticate(ACTION_TOKEN + "a", {"user_id": "u", "workspace_id": "a"})
    body = dict(schema_version=1, expected_feedback_revision=1)
    claim = _claim(action, "POST", "/relay/v1/feedback/reset", None, "same-key-reset", body)
    feedback.reset(action, 1, claim, now=NOW)
    path = tmp_path / "privacy-db.erasure-key"
    original = path.read_bytes()
    path.rename(tmp_path / "retired-key")
    path.write_bytes(original)
    path.chmod(0o600)
    with pytest.raises(ValueError, match="key"):
        feedback.reset(action, 1, claim, now=NOW)
    with runs.sql_transaction() as db:
        db.execute("DELETE FROM erasure_key_binding")
    # Surviving keyed rows cannot be adopted after their binding disappears.
    with pytest.raises(ValueError, match="binding"):
        feedback.reset(action, 1, claim, now=NOW)
    runs.close()


def test_conflicting_retry_does_not_rollback_unrelated_retention(tmp_path):
    """RM37/AC28: rejected fresh work preserves committed maintenance."""
    import pytest

    from recommendation_helpers import NOW
    from relaymind.api.user_actions import _claim
    from relaymind.recommendations.limits import recommendation_budgets

    runs, recommendations, registrations, feedback = privacy_store(tmp_path)
    insert_feedback(
        recommendations,
        registrations,
        feedback,
        workspace="a",
        key="expired",
        now=NOW,
        budgets=recommendation_budgets() | dict(retention_days=1),
    )
    rid, _ = insert_feedback(
        recommendations, registrations, feedback, workspace="a", key="live", now=NOW
    )
    action = registrations.authenticate(ACTION_TOKEN + "a", {"user_id": "u", "workspace_id": "a"})
    request = dict(
        schema_version=1,
        event_id="live",
        expected_feedback_revision=1,
        feedback={"kind": "usefulness", "value": "not_useful"},
    )
    with pytest.raises(Exception, match="idempotency_conflict"):
        feedback.create(
            action,
            rid,
            request,
            _claim(action, "POST", "/relay/v1/recommendations/{id}/feedback", rid, "live", request),
            now=NOW + timedelta(days=1),
        )
    with runs.sql_transaction() as db:
        # Physical parent purge belongs to sweep/begin/recovery in14b.
        assert db.execute("SELECT count(*) FROM recommendations").fetchone()[0] == 2
        assert db.execute("SELECT feedback_revision FROM recommendation_scope").fetchone()[0] == 3
    runs.close()


def test_cross_connection_choices_and_feedback_have_independent_cas(tmp_path):
    """RM37/AC28: concurrent lifecycle and scoped feedback each commit once."""
    from concurrent.futures import ThreadPoolExecutor

    from recommendation_helpers import NOW
    from relaymind.api.user_actions import _claim
    from relaymind.recommendations.registrations import ActionRegistrations
    from relaymind.store import RunStore
    from relaymind.store.asset_catalog import AssetCatalogStore
    from relaymind.store.asset_feedback import AssetFeedbackStore
    from relaymind.store.recommendations import RecommendationStore

    runs, recommendations, registrations, feedback = privacy_store(tmp_path)
    rid, _ = insert_feedback(
        recommendations, registrations, feedback, workspace="a", key="race-parent", now=NOW
    )

    def operation(kind):
        with RunStore(tmp_path / "privacy-db") as other:
            registry = ActionRegistrations(
                other,
                qualification=lambda *args: (
                    args == ("fixture", "fixture-host", "fixture-session", "fixture-contract")
                ),
            )
            action = registry.authenticate(
                ACTION_TOKEN + "a", {"user_id": "u", "workspace_id": "a"}
            )
            store = RecommendationStore(other)
            if kind == "choice":
                request = dict(schema_version=1, expected_version=1, choice={"kind": "none"})
                return store.choose(
                    action,
                    rid,
                    request,
                    _claim(
                        action,
                        "POST",
                        "/relay/v1/recommendations/{id}/choice",
                        rid,
                        "race-choice",
                        request,
                    ),
                    now=NOW,
                    registrations=registry,
                ).status
            owner = AssetFeedbackStore(store, AssetCatalogStore(other), registry)
            request = dict(
                schema_version=1,
                event_id="race-feedback",
                expected_feedback_revision=1,
                feedback={"kind": "usefulness", "value": "useful"},
            )
            return owner.create(
                action,
                rid,
                request,
                _claim(
                    action,
                    "POST",
                    "/relay/v1/recommendations/{id}/feedback",
                    rid,
                    "race-feedback",
                    request,
                ),
                now=NOW,
            ).status

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(operation, ("choice", "feedback"))) == [200, 200]
    with runs.sql_transaction() as db:
        assert tuple(db.execute("SELECT state,state_version FROM recommendations").fetchone()) == (
            "chosen",
            2,
        )
        assert db.execute("SELECT feedback_revision FROM recommendation_scope").fetchone()[0] == 2
    runs.close()


def test_delete_erases_feedback_and_alias_replays_but_preserves_parent(tmp_path):
    async def go():
        app, service, runs, _ = setup(tmp_path)
        enable_fixture_action(app, service, runs)
        await service.refresh_catalog()
        headers = {
            "Authorization": "Bearer " + app.state.application_token,
            "X-RelayMind-User-Action-Token": ACTION_TOKEN,
            "Idempotency-Key": "privacy-parent",
        }
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1", headers=headers
        ) as client:
            parent = (
                await client.post(
                    "/relay/v1/recommendations", json=BODY | {"task_text": "private-sentinel-195"}
                )
            ).json()
            rid = parent["recommendation_id"]
            path = f"/relay/v1/recommendations/{rid}/feedback"
            payload = {
                "schema_version": 1,
                "event_id": "event-erase-195",
                "expected_feedback_revision": 0,
                "feedback": {"kind": "usefulness", "value": "not_useful"},
            }
            first = await client.post(
                path, json=payload, headers={"Idempotency-Key": "create-erase"}
            )
            assert first.status_code == 200, first.text
            alias = await client.post(
                path, json=payload, headers={"Idempotency-Key": "alias-erase"}
            )
            assert alias.json() == first.json()
            fid = first.json()["feedback_id"]
            deleted = await client.delete(
                f"/relay/v1/feedback/{fid}?expected_feedback_revision=1",
                headers={"Idempotency-Key": "delete-erase"},
            )
            assert deleted.status_code == 200, deleted.text
            assert deleted.json() == {"schema_version": 1, "feedback_revision": 2, "deleted": True}
            assert (await client.get("/relay/v1/feedback")).json()["records"] == []
            assert (await client.get(f"/relay/v1/recommendations/{rid}")).json()["state"] == "ready"
            for key in ("create-erase", "alias-erase", "new-after-erase"):
                replay = await client.post(path, json=payload, headers={"Idempotency-Key": key})
                assert (
                    replay.status_code == 409 and replay.json()["error"]["code"] == "invalid_state"
                )
            repeat = await client.delete(
                f"/relay/v1/feedback/{fid}?expected_feedback_revision=1",
                headers={"Idempotency-Key": "delete-erase"},
            )
            assert repeat.json() == deleted.json()
            events = (await client.get(f"/relay/v1/recommendations/{rid}/events")).json()["events"]
            recorded = [e for e in events if e["event_type"] == "feedback.recorded"]
            assert len(recorded) == 1 and recorded[0]["erased"] and recorded[0]["payload"] is None
            with runs.sql_transaction() as db:
                assert db.execute("SELECT count(*) FROM feedback_records").fetchone()[0] == 0
                rows = db.execute("SELECT * FROM action_receipts").fetchall()
                assert fid not in str([tuple(r) for r in rows])
                erased = [r for r in rows if r["erased"]]
                assert len(erased) == 2 and all(r["erased_at"] is not None for r in erased)
                assert all(r["created_at"] <= r["erased_at"] < r["retain_until"] for r in erased)
        await service.stop()
        runs.close()

    asyncio.run(go())


def test_parent_retention_removes_feedback_once_and_empty_batch_is_noop(tmp_path):
    async def go():
        app, service, runs, _ = setup(tmp_path)
        enable_fixture_action(app, service, runs)
        await service.refresh_catalog()
        headers = {
            "Authorization": "Bearer " + app.state.application_token,
            "X-RelayMind-User-Action-Token": ACTION_TOKEN,
            "Idempotency-Key": "retention-parent",
        }
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1", headers=headers
        ) as client:
            parent = (await client.post("/relay/v1/recommendations", json=BODY)).json()
            rid = parent["recommendation_id"]
            created = await client.post(
                f"/relay/v1/recommendations/{rid}/feedback",
                json={
                    "schema_version": 1,
                    "event_id": "retention-event",
                    "expected_feedback_revision": 0,
                    "feedback": {"kind": "usefulness", "value": "useful"},
                },
                headers={"Idempotency-Key": "retention-feedback"},
            )
            assert created.status_code == 200
        expiry = datetime.fromisoformat(parent["retain_until"])
        service.store.sweep(now=expiry)
        service.store.sweep(now=expiry + timedelta(seconds=1))
        with runs.sql_transaction() as db:
            assert db.execute("SELECT count(*) FROM feedback_records").fetchone()[0] == 0
            assert (
                db.execute("SELECT feedback_revision FROM recommendation_scope").fetchone()[0] == 2
            )
            audits = db.execute("SELECT audit_json FROM feedback_audits").fetchall()
            assert len(audits) == 1 and '"operation":"retention"' in audits[0][0]
            assert db.execute("SELECT count(*) FROM action_receipts").fetchone()[0] == 0
        await service.stop()
        runs.close()

    asyncio.run(go())


def test_reset_erases_scoped_feedback_but_keeps_chosen_parent_and_choice_receipt(tmp_path):
    async def go():
        app, service, runs, _ = setup(tmp_path)
        enable_fixture_action(app, service, runs)
        await service.refresh_catalog()
        headers = {
            "Authorization": "Bearer " + app.state.application_token,
            "X-RelayMind-User-Action-Token": ACTION_TOKEN,
            "Idempotency-Key": "reset-parent",
        }
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1", headers=headers
        ) as client:
            parent = (await client.post("/relay/v1/recommendations", json=BODY)).json()
            rid = parent["recommendation_id"]
            chosen = await client.post(
                f"/relay/v1/recommendations/{rid}/choice",
                json={"schema_version": 1, "expected_version": 1, "choice": {"kind": "none"}},
                headers={"Idempotency-Key": "reset-choice"},
            )
            assert chosen.status_code == 200
            feedback_path = f"/relay/v1/recommendations/{rid}/feedback"
            payload = {
                "schema_version": 1,
                "event_id": "reset-event",
                "expected_feedback_revision": 0,
                "feedback": {"kind": "usefulness", "value": "useful"},
            }
            feedback = await client.post(
                feedback_path,
                json=payload,
                headers={"Idempotency-Key": "reset-feedback"},
            )
            assert feedback.status_code == 200
            reset = await client.post(
                "/relay/v1/feedback/reset",
                json={"schema_version": 1, "expected_feedback_revision": 1},
                headers={"Idempotency-Key": "reset-key"},
            )
            assert reset.status_code == 200, reset.text
            assert reset.json() == {"schema_version": 1, "feedback_revision": 2, "removed_count": 1}
            assert (await client.get("/relay/v1/feedback")).json()["records"] == []
            assert (await client.get(f"/relay/v1/recommendations/{rid}")).json()[
                "state"
            ] == "chosen"
            assert (
                await client.post(
                    f"/relay/v1/recommendations/{rid}/choice",
                    json={"schema_version": 1, "expected_version": 1, "choice": {"kind": "none"}},
                    headers={"Idempotency-Key": "reset-choice"},
                )
            ).json() == chosen.json()
            assert (
                await client.post(
                    feedback_path,
                    json=payload,
                    headers={"Idempotency-Key": "reset-feedback"},
                )
            ).json()["error"]["code"] == "invalid_state"
            assert (
                await client.post(
                    "/relay/v1/feedback/reset",
                    json={"schema_version": 1, "expected_feedback_revision": 1},
                    headers={"Idempotency-Key": "reset-key"},
                )
            ).json() == reset.json()
        await service.stop()
        runs.close()

    asyncio.run(go())
