"""Independent atomic recommendation lifecycle and receipts (RM36/AC27).

Automatic retention deletes the whole private ownership tree. #195 may register
synchronous dependent-row/cache hooks within that transaction; explicit retained
erasure will require its protected ErasureDigester, never a plain-hash fallback.
"""

import copy
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

from relaymind.canonical import canonical_digest, canonical_json
from relaymind.schema_util import freeze_json
from relaymind.store.recommendation_event import validate_event, validate_view
from relaymind.store.run_store import RunStore, utc_iso

MESSAGES = {
    "unauthenticated": "a valid application token is required",
    "invalid_request": "the recommendation request is invalid",
    "recommendation_not_found": "the recommendation was not found",
    "stale_version": "the recommendation version changed",
    "invalid_state": "the recommendation state does not permit this operation",
    "idempotency_conflict": "the idempotency key was used with a different request",
    "recommendation_in_progress": "the recommendation is still computing",
    "overloaded": "the recommendation service is busy",
    "recommendation_unavailable": "the recommendation service is unavailable",
    "deadline_exceeded": "the recommendation deadline was exceeded",
    "internal_error": "an internal error occurred",
}
STATUSES = {
    "unauthenticated": 401,
    "invalid_request": 400,
    "recommendation_not_found": 404,
    "stale_version": 409,
    "invalid_state": 409,
    "idempotency_conflict": 409,
    "recommendation_in_progress": 409,
    "overloaded": 429,
    "recommendation_unavailable": 503,
    "deadline_exceeded": 504,
    "internal_error": 500,
}


class RecommendationError(Exception):
    def __init__(self, code: str, recommendation_id: str | None = None):
        super().__init__(code)
        self.code, self.recommendation_id = code, recommendation_id

    @property
    def status(self) -> int:
        return STATUSES[self.code]

    def inner(self) -> dict:
        return dict(
            code=self.code,
            message=MESSAGES[self.code],
            run_id=None,
            recommendation_id=self.recommendation_id,
        )


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


def recommendation_budgets(
    total_ms: int = 500, primary_ms: int = 150, fallback: bool = True
) -> dict:
    return dict(
        total_ms=total_ms,
        primary_ms=primary_ms,
        fallback_enabled=fallback,
        candidate_limit=10,
        display_limit=3,
        max_catalog_records=1000,
        max_catalog_bytes=32 * 1024 * 1024,
        max_result_bytes=1024 * 1024,
        max_create_bytes=512 * 1024,
        max_task_bytes=8192,
        max_conversation_entries=32,
        max_conversation_bytes=32768,
        choice_seconds=600,
        retention_days=30,
    )


class RecommendationStore:
    def __init__(self, store: RunStore) -> None:
        self.store = store
        self._purge_hooks: list[Callable] = []

    def register_purge_hook(self, hook: Callable) -> None:
        """Synchronous transactional ownership extension for #195 private state."""
        self._purge_hooks.append(hook)

    def _purge(self, db, now):
        ids = tuple(
            r[0]
            for r in db.execute(
                "SELECT recommendation_id FROM recommendations WHERE retain_until<=?",
                (utc_iso(now),),
            )
        )
        for rid in ids:
            for hook in self._purge_hooks:
                hook(db, rid)
            db.execute(
                "UPDATE recommendation_slot SET recommendation_id=NULL WHERE recommendation_id=?",
                (rid,),
            )
            db.execute("DELETE FROM recommendations WHERE recommendation_id=?", (rid,))
        return len(ids)

    def _row(self, db, scope, rid):
        row = db.execute(
            "SELECT * FROM recommendations WHERE recommendation_id=? AND user_id=? AND "
            "workspace_id=?",
            (rid, scope["user_id"], scope["workspace_id"]),
        ).fetchone()
        if row is None:
            raise RecommendationError("recommendation_not_found")
        return row

    def _lookup(self, db, claim):
        row = db.execute(
            "SELECT * FROM recommendation_claims WHERE namespace=? AND key_digest=?",
            (claim.namespace, claim.key_digest),
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
        with self.store.sql_transaction() as db:
            self._purge(db, now)
            return self._lookup(db, claim)

    def _event(self, db, rid, kind, payload, now, elapsed, *, actor="gateway"):
        seq = db.execute(
            "SELECT COALESCE(MAX(seq),0)+1 FROM recommendation_events WHERE recommendation_id=?",
            (rid,),
        ).fetchone()[0]
        event = validate_event(
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
            )
        )
        db.execute(
            "INSERT INTO recommendation_events VALUES(?,?,?)", (rid, seq, canonical_json(event))
        )
        return event

    def _write(self, db, view, active_ms):
        view = validate_view(view)
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
            retain_until=utc_iso(now + timedelta(days=30)),
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
            row = self._row(db, scope, recommendation_id)
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
        now = now or self.store.clock.utc_now()
        if transition not in ("ready", "failed", "cancelled"):
            raise RecommendationError("invalid_state", recommendation_id)
        with self.store.sql_transaction() as db:
            row = self._row(db, scope, recommendation_id)
            self._computing(db, row, expected_version)
            view = json.loads(row["view_json"])
            view.update(state=transition, state_version=expected_version + 1)
            if transition == "ready":
                from relaymind.selection.asset_contract import parse_recommendation_result

                result = parse_recommendation_result(result).to_json_object()
                if result["catalog_revision"] != view["catalog_revision"]:
                    raise RecommendationError("invalid_state", recommendation_id)
                offered = {r["asset_id"]: r for r in json.loads(row["accepted_json"])["catalog"]}
                view.update(
                    result=result,
                    decision=result["decision"],
                    ready_at=utc_iso(now),
                    choice_expires_at=utc_iso(now + timedelta(seconds=600)),
                    displayed_candidates=[
                        dict(candidate=c, asset=offered[c["asset_id"]])
                        for c in result["final_candidates"][:3]
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
            obj = self._write(db, view, active_ms)
            self._event(db, recommendation_id, transition, payload, now, active_ms)
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

    def _expire(self, db, row, now):
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
        return StoredRecommendation(validate_view(view))

    def get(self, scope, recommendation_id, *, now):
        with self.store.sql_transaction() as db:
            self._purge(db, now)
            try:
                row = self._row(db, scope, recommendation_id)
            except RecommendationError:
                result = None
            else:
                result = self._expire(db, row, now)
        if result is None:
            raise RecommendationError("recommendation_not_found")
        return result

    def events(self, scope, recommendation_id, *, after_seq, limit, now):
        if (
            type(after_seq) is not int
            or after_seq < 0
            or type(limit) is not int
            or not 1 <= limit <= 1000
        ):
            raise RecommendationError("invalid_request")
        self.get(scope, recommendation_id, now=now)
        with self.store.sql_transaction() as db:
            self._row(db, scope, recommendation_id)
            rows = db.execute(
                "SELECT event_json FROM recommendation_events WHERE recommendation_id=? AND "
                "seq>? ORDER BY seq LIMIT ?",
                (recommendation_id, min(after_seq, 2**63 - 1), limit + 1),
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

    def sweep(self, *, now):
        with self.store.sql_transaction() as db:
            purged = self._purge(db, now)
            rows = db.execute("SELECT * FROM recommendations WHERE state='ready'").fetchall()
            expired = sum(self._expire(db, r, now).view["state"] == "expired" for r in rows)
            return dict(purged=purged, expired=expired)

    def recover(self, *, now):
        with self.store.sql_transaction() as db:
            self._purge(db, now)
            rows = db.execute("SELECT * FROM recommendations WHERE state='computing'").fetchall()
            for row in rows:
                accepted = json.loads(row["accepted_json"])
                events = db.execute(
                    "SELECT event_json FROM recommendation_events WHERE recommendation_id=? "
                    "ORDER BY seq",
                    (row["recommendation_id"],),
                ).fetchall()
                attempts = [
                    json.loads(e[0])["payload"]["attempt_record"]
                    for e in events
                    if json.loads(e[0])["event_type"] == "selector.finished"
                ]
                self.finish(
                    accepted["scope"],
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
