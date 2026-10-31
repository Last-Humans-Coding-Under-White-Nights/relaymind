"""Independent atomic recommendation lifecycle and receipts (RM36/AC27).

Automatic retention deletes the whole private ownership tree. #195 may register
synchronous dependent-row/cache hooks within that transaction; explicit retained
erasure will require its protected ErasureDigester, never a plain-hash fallback.

Reads apply retention logically (a row past retain_until is not found) and take
no write lock; the sweeper owns the physical purge. A read that observes an
elapsed choice window records the expiry once, in a write transaction.
"""

import copy
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

from relaymind.canonical import canonical_digest, canonical_json
from relaymind.recommendations.admission import (
    current_asset,
    offered_record,
    same_identity,
)
from relaymind.recommendations.errors import MESSAGES, STATUSES, RecommendationError
from relaymind.recommendations.limits import MAX_EVENTS_PAGE, MAX_SEQUENCE, recommendation_budgets
from relaymind.schema_util import freeze_json
from relaymind.selection.asset_contract import parse_recommendation_result
from relaymind.store.recommendation_event import validate_event, validate_view
from relaymind.store.run_store import RunStore, append_event, utc_iso

__all__ = [
    "MESSAGES",
    "STATUSES",
    "RecommendationClaim",
    "RecommendationError",
    "RecommendationStore",
    "StoredClaim",
    "StoredRecommendation",
    "StoredResponse",
    "recommendation_budgets",
]

# The columns every lifecycle read needs; accepted_json (the full offer) is read
# only by ready/action admission, not ordinary lifecycle reads.
_VIEW_COLUMNS = "recommendation_id,user_id,workspace_id,state,state_version,view_json,active_ms"


@dataclass(frozen=True)
class StoredResponse:
    status: int
    body: Mapping

    def __post_init__(self):
        object.__setattr__(self, "body", freeze_json(self.body))
        canonical_json(self.body)


@dataclass(frozen=True)
class StoredRecommendation:
    view: Mapping


@dataclass(frozen=True)
class StoredClaim:
    recommendation_id: str
    response: StoredResponse | None


@dataclass(frozen=True)
class RecommendationClaim:
    scope: Mapping
    caller: str
    method: str
    endpoint: str
    object_id: str | None
    key: str
    request_digest: str

    @property
    def namespace(self) -> str:
        return canonical_digest(
            dict(
                scope=dict(self.scope),
                caller=self.caller,
                method=self.method,
                endpoint=self.endpoint,
                object_id=self.object_id,
            )
        )

    @property
    def key_digest(self) -> str:
        return canonical_digest(self.key)


# Ready recommendations of one scope whose choice window has lapsed.
_SCOPE_EXPIRY_DUE = (
    "user_id=? AND workspace_id=? AND retain_until>? AND state='ready' AND "
    "json_extract(view_json,'$.choice_expires_at')<=?"
)


def _expiry_due(row, now) -> bool:
    return row["state"] == "ready" and json.loads(row["view_json"])["choice_expires_at"] <= utc_iso(
        now
    )


class RecommendationStore:
    def __init__(self, store: RunStore) -> None:
        self.store = store

    @property
    def _purge_hooks(self) -> list[Callable]:
        return self.store._recommendation_purge_hooks

    @property
    def _maintain_feedback(self) -> Callable | None:
        with self.store._lock:
            owner = self.store._asset_feedback_owner
            return None if owner is None else owner.maintain_scope

    def register_purge_hook(self, hook: Callable) -> None:
        """Synchronous transactional ownership extension for #195 private state."""
        with self.store._lock:
            self._purge_hooks.append(hook)

    def _purge(self, db, now):
        ids = tuple(
            r[0]
            for r in db.execute(
                "SELECT recommendation_id FROM recommendations WHERE retain_until<=?",
                (utc_iso(now),),
            )
        )
        for hook in self._purge_hooks:
            hook(db, ids, now=now)
        for rid in ids:
            db.execute(
                "UPDATE recommendation_slot SET recommendation_id=NULL WHERE recommendation_id=?",
                (rid,),
            )
            db.execute("DELETE FROM recommendations WHERE recommendation_id=?", (rid,))
        return len(ids)

    def _row(self, db, scope, rid, *, now=None, columns=_VIEW_COLUMNS):
        retained = "" if now is None else " AND retain_until>?"
        row = db.execute(
            f"SELECT {columns} FROM recommendations WHERE recommendation_id=? AND user_id=? AND "
            f"workspace_id=?{retained}",
            (rid, scope["user_id"], scope["workspace_id"])
            + (() if now is None else (utc_iso(now),)),
        ).fetchone()
        if row is None:
            raise RecommendationError("recommendation_not_found")
        return row

    def _lookup(self, db, claim, now=None):
        retained = "" if now is None else " AND r.retain_until>?"
        row = db.execute(
            "SELECT c.request_digest,c.recommendation_id,c.status,c.body_json "
            "FROM recommendation_claims c JOIN recommendations r USING(recommendation_id) "
            f"WHERE c.namespace=? AND c.key_digest=?{retained}",
            (claim.namespace, claim.key_digest) + (() if now is None else (utc_iso(now),)),
        ).fetchone()
        if row is None:
            return None
        if row["request_digest"] != claim.request_digest:
            raise RecommendationError("idempotency_conflict", row["recommendation_id"])
        response = (
            None
            if row["status"] is None
            else StoredResponse(row["status"], json.loads(row["body_json"]))
        )
        return StoredClaim(row["recommendation_id"], response)

    def lookup_claim(self, claim: RecommendationClaim, *, now: datetime) -> StoredClaim | None:
        with self.store.sql_read() as db:
            return self._lookup(db, claim, now)

    def _event(self, db, rid, kind, payload, now, elapsed, *, actor="gateway", checked=False):
        return append_event(
            db,
            "recommendation_events",
            "recommendation_id",
            rid,
            lambda seq: validate_event(
                dict(
                    schema_version=1,
                    recommendation_id=rid,
                    seq=seq,
                    timestamp=utc_iso(now),
                    elapsed_ms=max(0, elapsed),
                    event_type=kind,
                    actor=actor,
                    erased=False,
                    payload=payload,
                ),
                result_checked=checked,
            ),
        )

    def _write(self, db, view, active_ms, *, checked=False):
        view = validate_view(view, result_checked=checked)
        db.execute(
            "UPDATE recommendations SET state=?,state_version=?,view_json=?,active_ms=? WHERE "
            "recommendation_id=?",
            (
                view["state"],
                view["state_version"],
                canonical_json(view),
                active_ms,
                view["recommendation_id"],
            ),
        )
        return StoredRecommendation(view)

    def begin(
        self,
        accepted: Mapping,
        claim: RecommendationClaim,
        *,
        commit_guard=lambda: None,
        active_ms=0,
    ) -> StoredRecommendation | StoredResponse:
        raw = copy.deepcopy(dict(accepted))
        now = raw.pop("created_at")
        rid, scope = raw.pop("recommendation_id"), raw["scope"]
        catalog = raw.pop("catalog")
        if scope != claim.scope:
            raise RecommendationError("invalid_request")
        view = dict(
            schema_version=1,
            recommendation_id=rid,
            task_id=raw["task_id"],
            state="computing",
            state_version=0,
            created_at=utc_iso(now),
            ready_at=None,
            choice_expires_at=None,
            retain_until=utc_iso(now + timedelta(days=raw["budgets"]["retention_days"])),
            catalog_revision=raw["catalog_revision"],
            host_snapshot_revision=None,
            decision=None,
            displayed_candidates=[],
            result=None,
            selected_action=None,
            feedback_revision=0,
            error=None,
        )
        validate_view(view)
        with self.store.sql_transaction() as db:
            self._purge(db, now)
            prior = self._lookup(db, claim)
            if prior is not None:
                if prior.response is None:
                    raise RecommendationError("recommendation_in_progress", prior.recommendation_id)
                return prior.response
            if (
                db.execute(
                    "SELECT recommendation_id FROM recommendation_slot WHERE singleton=1"
                ).fetchone()[0]
                is not None
            ):
                raise RecommendationError("overloaded")
            db.execute(
                "INSERT OR IGNORE INTO recommendation_scope(user_id,workspace_id) VALUES(?,?)",
                (scope["user_id"], scope["workspace_id"]),
            )
            view["feedback_revision"] = db.execute(
                "SELECT feedback_revision FROM recommendation_scope WHERE user_id=? AND "
                "workspace_id=?",
                (scope["user_id"], scope["workspace_id"]),
            ).fetchone()[0]
            db.execute(
                "INSERT INTO "
                "recommendations(recommendation_id,user_id,workspace_id,state,state_version,"
                "created_at,retain_until,view_json,accepted_json) "
                "VALUES(?,?,?,'computing',0,?,?,?,?)",
                (
                    rid,
                    scope["user_id"],
                    scope["workspace_id"],
                    view["created_at"],
                    view["retain_until"],
                    canonical_json(view),
                    canonical_json(raw | {"catalog": catalog}),
                ),
            )
            db.execute(
                "INSERT INTO recommendation_claims VALUES(?,?,?,?,NULL,NULL)",
                (claim.namespace, claim.key_digest, claim.request_digest, rid),
            )
            db.execute(
                "UPDATE recommendation_slot SET recommendation_id=? WHERE singleton=1", (rid,)
            )
            self._event(db, rid, "accepted", raw, now, active_ms)
            db.execute(
                "UPDATE recommendations SET active_ms=? WHERE recommendation_id=?", (active_ms, rid)
            )
            commit_guard()
        return StoredRecommendation(freeze_json(view))

    def append_attempt_event(
        self, scope, recommendation_id, expected_version, event, *, now=None, active_ms=0
    ):
        now = now or self.store.clock.utc_now()
        with self.store.sql_transaction() as db:
            row = self._row(
                db, scope, recommendation_id, columns="recommendation_id,state,state_version"
            )
            self._computing(db, row, expected_version)
            if event["event_type"] not in ("selector.started", "selector.finished"):
                raise RecommendationError("invalid_state", recommendation_id)
            self._event(
                db,
                recommendation_id,
                event["event_type"],
                event["payload"],
                now,
                active_ms,
                actor="selector",
            )
            db.execute(
                "UPDATE recommendations SET active_ms=? WHERE recommendation_id=?",
                (active_ms, recommendation_id),
            )

    def _computing(self, db, row, version):
        rid = row["recommendation_id"]
        if row["state_version"] != version:
            raise RecommendationError("stale_version", rid)
        if row["state"] != "computing":
            raise RecommendationError("invalid_state", rid)
        if (
            db.execute(
                "SELECT recommendation_id FROM recommendation_slot WHERE singleton=1"
            ).fetchone()[0]
            != rid
        ):
            raise RecommendationError("invalid_state", rid)

    def finish(
        self,
        scope,
        recommendation_id,
        expected_version,
        transition,
        receipt=None,
        *,
        result=None,
        now=None,
        active_ms=0,
        attempts=(),
        reason=None,
        code=None,
        commit_guard=lambda: None,
    ):
        """Commit one terminal transition.

        result is a parsed recommendation result (its to_json_object() is used
        as is) or a JSON mapping, which is parsed once here.
        """
        now = now or self.store.clock.utc_now()
        if transition not in ("ready", "failed", "cancelled"):
            raise RecommendationError("invalid_state", recommendation_id)
        if transition == "ready":
            result = (
                result.to_json_object()
                if hasattr(result, "to_json_object")
                else parse_recommendation_result(result).to_json_object()
            )
        with self.store.sql_transaction() as db:
            columns = _VIEW_COLUMNS + (",accepted_json" if transition == "ready" else "")
            row = self._row(db, scope, recommendation_id, columns=columns)
            self._computing(db, row, expected_version)
            view = json.loads(row["view_json"])
            view.update(state=transition, state_version=expected_version + 1)
            if transition == "ready":
                if result["catalog_revision"] != view["catalog_revision"]:
                    raise RecommendationError("invalid_state", recommendation_id)
                offer = json.loads(row["accepted_json"])
                budgets = offer["budgets"]
                offered = {r["asset_id"]: r for r in offer["catalog"]}
                view.update(
                    result=result,
                    decision=result["decision"],
                    ready_at=utc_iso(now),
                    choice_expires_at=utc_iso(now + timedelta(seconds=budgets["choice_seconds"])),
                    displayed_candidates=[
                        dict(candidate=c, asset=offered[c["asset_id"]])
                        for c in result["final_candidates"][: budgets["display_limit"]]
                    ],
                )
                payload = dict(
                    state_version=view["state_version"],
                    result=result,
                    ready_at=view["ready_at"],
                    choice_expires_at=view["choice_expires_at"],
                )
            elif transition == "failed":
                error = RecommendationError(code or "internal_error", recommendation_id)
                view["error"] = error.inner()
                payload = dict(
                    state_version=view["state_version"],
                    code=error.code,
                    reason=reason or "internal",
                    active_ms=active_ms,
                    attempts=list(attempts),
                )
                receipt = receipt or StoredResponse(error.status, {"error": error.inner()})
            else:
                payload = dict(
                    state_version=view["state_version"],
                    origin="gateway",
                    reason=reason or "shutdown",
                    action=None,
                    active_ms=active_ms,
                    attempts=list(attempts),
                )
            # The result was parsed once above; the view and event checks reuse that.
            obj = self._write(db, view, active_ms, checked=True)
            if transition == "ready" and result["adaptation"] is not None:
                self._event(
                    db,
                    recommendation_id,
                    "adaptation.applied",
                    dict(adaptation=result["adaptation"]),
                    now,
                    active_ms,
                )
            self._event(db, recommendation_id, transition, payload, now, active_ms, checked=True)
            response = receipt or StoredResponse(200, obj.view)
            encoded = canonical_json(response.body)
            # Final guard also fences serialization and all writes before COMMIT.
            commit_guard()
            db.execute(
                "UPDATE recommendation_claims SET status=?,body_json=? WHERE recommendation_id=?",
                (response.status, encoded, recommendation_id),
            )
            db.execute(
                "UPDATE recommendation_slot SET recommendation_id=NULL WHERE recommendation_id=?",
                (recommendation_id,),
            )
            commit_guard()
            return obj

    def cancel_if_computing(self, scope, recommendation_id, *, now, active_ms, attempts, reason):
        """Record a gateway cancel only while the object still computes; else no change."""
        try:
            return self.finish(
                scope,
                recommendation_id,
                0,
                "cancelled",
                now=now,
                active_ms=active_ms,
                attempts=attempts,
                reason=reason,
            )
        except RecommendationError as exc:
            if exc.code in ("invalid_state", "stale_version", "recommendation_not_found"):
                return None
            raise

    @staticmethod
    def feedback_revision(db, scope) -> int:
        """The scope's explicit-feedback revision; 0 before any feedback."""
        row = db.execute(
            "SELECT feedback_revision FROM recommendation_scope WHERE user_id=? AND workspace_id=?",
            (scope["user_id"], scope["workspace_id"]),
        ).fetchone()
        return 0 if row is None else row[0]

    def _expire(self, db, row, now):
        return StoredRecommendation(validate_view(self._expired_view(db, row, now)))

    def _expired_view(self, db, row, now) -> dict:
        """The row's view as a plain dict, recording a due expiry first."""
        view = json.loads(row["view_json"])
        if row["state"] == "ready" and view["choice_expires_at"] <= utc_iso(now):
            view.update(state="expired", state_version=view["state_version"] + 1)
            self._write(db, view, row["active_ms"])
            self._event(
                db,
                row["recommendation_id"],
                "expired",
                dict(
                    state_version=view["state_version"], choice_expires_at=view["choice_expires_at"]
                ),
                now,
                row["active_ms"],
            )
        return view

    def _expire_scope(self, db, scope, *, now):
        rows = db.execute(
            f"SELECT {_VIEW_COLUMNS} FROM recommendations WHERE {_SCOPE_EXPIRY_DUE}",
            (scope["user_id"], scope["workspace_id"], utc_iso(now), utc_iso(now)),
        ).fetchall()
        for row in rows:
            self._expire(db, row, now)

    def _observe_scope_expiry(self, scope, *, now):
        with self.store.sql_read() as db:
            due = db.execute(
                f"SELECT 1 FROM recommendations WHERE {_SCOPE_EXPIRY_DUE} LIMIT 1",
                (scope["user_id"], scope["workspace_id"], utc_iso(now), utc_iso(now)),
            ).fetchone()
        if due:
            with self.store.sql_transaction() as db:
                self._expire_scope(db, scope, now=now)

    def _action_maintenance(self, registrations, action, *, now):
        # Maintenance has its own committed boundary. A later rejected replay
        # or fresh precondition cannot undo expiry of unrelated private data.
        registrations.recheck(action)
        if self._maintain_feedback is not None:
            self._maintain_feedback(action.scope, now=now)

    def choose(
        self,
        action,
        recommendation_id,
        request,
        claim,
        *,
        now,
        registrations,
        catalog=None,
        policy=lambda record: True,
    ) -> StoredResponse:
        """RM37: one preference action, independent of feedback CAS and execution."""
        error = None
        result = None
        self._action_maintenance(registrations, action, now=now)
        with self.store.sql_transaction() as db:
            registrations.recheck(action)
            prior = self._lookup(db, claim, now)
            if prior is not None:
                if prior.response is None:
                    error = RecommendationError("recommendation_in_progress", recommendation_id)
                else:
                    result = prior.response
            else:
                try:
                    row = self._row(
                        db,
                        action.scope,
                        recommendation_id,
                        now=now,
                        columns=_VIEW_COLUMNS + ",accepted_json,retain_until",
                    )
                except RecommendationError as exc:
                    error = exc
                else:
                    view = self._expired_view(db, row, now)
                    expected = request["expected_version"]
                    # Section 11.1: expiry advances the version before fresh CAS;
                    # stale versions take precedence over lifecycle errors.
                    if expected != view["state_version"]:
                        error = RecommendationError("stale_version", recommendation_id)
                    elif view["state"] == "expired":
                        error = RecommendationError("recommendation_expired", recommendation_id)
                    elif view["state"] != "ready":
                        error = RecommendationError("invalid_state", recommendation_id)
                    else:
                        choice = request["choice"]
                        if choice["kind"] == "asset":
                            offered = {
                                item["asset_id"]: item
                                for item in view["result"]["final_candidates"]
                            }
                            target = offered.get(choice["asset_id"])
                            if target is None:
                                error = RecommendationError("invalid_request", recommendation_id)
                            elif target["content_hash"] != choice["content_hash"]:
                                error = RecommendationError("asset_changed", recommendation_id)
                            else:
                                original = offered_record(
                                    json.loads(row["accepted_json"]), choice["asset_id"]
                                )
                                # A catalog failure propagates as internal_error;
                                # only a missing or changed asset is asset_changed.
                                current = current_asset(catalog, choice["asset_id"])
                                if (
                                    original is None
                                    or current is None
                                    or not same_identity(original, current)
                                    or not policy(current)
                                ):
                                    error = RecommendationError("asset_changed", recommendation_id)
                        if error is None:
                            scope_rev = self.feedback_revision(db, action.scope)
                            version = view["state_version"] + 1
                            evidence = dict(
                                adapter_id=action.adapter_id,
                                scope=action.scope,
                                recommendation_id=recommendation_id,
                                task_id=view["task_id"],
                                choice=choice,
                                catalog_revision=view["catalog_revision"],
                                host_snapshot_revision=None,
                                recorded_at=utc_iso(now),
                                state_version=version,
                            )
                            kind = "cancelled" if choice["kind"] == "cancel" else "chosen"
                            view.update(
                                state=kind,
                                state_version=version,
                                selected_action=evidence,
                                feedback_revision=scope_rev,
                            )
                            self._write(db, view, row["active_ms"])
                            payload = (
                                dict(
                                    state_version=version,
                                    origin="direct_user",
                                    reason="user_cancel",
                                    action=evidence,
                                    active_ms=row["active_ms"],
                                    attempts=[],
                                )
                                if kind == "cancelled"
                                else dict(state_version=version, action=evidence)
                            )
                            self._event(
                                db,
                                recommendation_id,
                                kind,
                                payload,
                                now,
                                row["active_ms"],
                                actor="adapter",
                            )
                            result = StoredResponse(200, view)
                            db.execute(
                                "INSERT INTO recommendation_claims VALUES(?,?,?,?,?,?)",
                                (
                                    claim.namespace,
                                    claim.key_digest,
                                    claim.request_digest,
                                    recommendation_id,
                                    200,
                                    canonical_json(result.body),
                                ),
                            )
        if error is not None:
            raise error
        return result

    def _observe(self, scope, rid, now, read):
        """Run read(db, view) on the current view, recording a due expiry first.

        Only a read that finds an elapsed choice window takes the write lock.
        """
        if self._maintain_feedback is not None:
            self._maintain_feedback(scope, now=now)
        with self.store.sql_read() as db:
            row = self._row(db, scope, rid, now=now)
            if not _expiry_due(row, now):
                return read(db, StoredRecommendation(validate_view(json.loads(row["view_json"]))))
        with self.store.sql_transaction() as db:
            row = self._row(db, scope, rid, now=now)
            return read(db, self._expire(db, row, now))

    def get(self, scope, recommendation_id, *, now):
        def current_view(db, stored):
            revision = db.execute(
                "SELECT feedback_revision FROM recommendation_scope "
                "WHERE user_id=? AND workspace_id=?",
                (scope["user_id"], scope["workspace_id"]),
            ).fetchone()
            if revision is None:
                raise RecommendationError("internal_error")
            return StoredRecommendation(
                validate_view(dict(stored.view, feedback_revision=revision[0]))
            )

        return self._observe(scope, recommendation_id, now, current_view)

    def events(self, scope, recommendation_id, *, after_seq, limit, now):
        if (
            type(after_seq) is not int
            or after_seq < 0
            or type(limit) is not int
            or not 1 <= limit <= MAX_EVENTS_PAGE
        ):
            raise RecommendationError("invalid_request")

        def page(db, _stored):
            rows = db.execute(
                "SELECT event_json FROM recommendation_events WHERE recommendation_id=? AND "
                "seq>? ORDER BY seq LIMIT ?",
                (recommendation_id, min(after_seq, MAX_SEQUENCE), limit + 1),
            ).fetchall()
            events = [validate_event(json.loads(r[0]), allow_erased=True) for r in rows[:limit]]
            return freeze_json(
                dict(
                    schema_version=1,
                    recommendation_id=recommendation_id,
                    events=events,
                    next_after_seq=events[-1]["seq"] if events else after_seq,
                    has_more=len(rows) > limit,
                )
            )

        return self._observe(scope, recommendation_id, now, page)

    def sweep(self, *, now):
        with self.store.sql_transaction() as db:
            purged = self._purge(db, now)
            rows = db.execute(
                f"SELECT {_VIEW_COLUMNS} FROM recommendations WHERE state='ready' AND "
                "json_extract(view_json,'$.choice_expires_at')<=?",
                (utc_iso(now),),
            ).fetchall()
            expired = sum(self._expire(db, r, now).view["state"] == "expired" for r in rows)
            return dict(purged=purged, expired=expired)

    def recover(self, *, now):
        with self.store.sql_transaction() as db:
            self._purge(db, now)
            rows = db.execute(
                "SELECT recommendation_id,user_id,workspace_id,state_version,active_ms "
                "FROM recommendations WHERE state='computing'"
            ).fetchall()
            for row in rows:
                attempts = [
                    json.loads(e[0])["payload"]["attempt_record"]
                    for e in db.execute(
                        "SELECT event_json FROM recommendation_events WHERE recommendation_id=? "
                        "AND json_extract(event_json,'$.event_type')='selector.finished' "
                        "ORDER BY seq",
                        (row["recommendation_id"],),
                    )
                ]
                self.finish(
                    dict(user_id=row["user_id"], workspace_id=row["workspace_id"]),
                    row["recommendation_id"],
                    row["state_version"],
                    "failed",
                    now=now,
                    active_ms=row["active_ms"],
                    attempts=attempts,
                    reason="interrupted",
                    code="recommendation_unavailable",
                )
            return dict(recovered=len(rows), **self.sweep(now=now))
