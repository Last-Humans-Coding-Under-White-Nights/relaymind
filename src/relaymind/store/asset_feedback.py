"""Scoped explicit feedback, evidence and privacy ownership (RM37/AC28)."""

import base64
import functools
import hmac
import json
import re
import uuid
from collections.abc import Mapping
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta

from jsonschema import FormatChecker

from relaymind.assets.catalog import validate_asset_record
from relaymind.canonical import canonical_json
from relaymind.recommendations.admission import (
    IDENTITY_FIELDS,
    current_asset,
    offered_record,
    same_identity,
)
from relaymind.recommendations.erasure import has_keyed_state, protected_erasure_digester
from relaymind.recommendations.errors import RecommendationError
from relaymind.recommendations.limits import (
    DEFAULT_EVENTS_PAGE,
    MAX_EVENTS_PAGE,
    MAX_SEQUENCE,
    RETENTION_DAYS,
)
from relaymind.recommendations.private_files import PrivateFileError
from relaymind.schema_util import StrictValidator, freeze_json, load_packaged_schema
from relaymind.store.recommendation_event import validate_event
from relaymind.store.recommendations import StoredResponse
from relaymind.store.run_store import utc_iso

_RECORD_VALIDATOR = StrictValidator(
    load_packaged_schema("relaymind.store", "feedback_record.v1.json"),
    format_checker=FormatChecker(),
)
_AUDIT_VALIDATOR = StrictValidator(
    load_packaged_schema("relaymind.store", "feedback_audit.v1.json"),
    format_checker=FormatChecker(),
)


def validate_feedback_record(value):
    _RECORD_VALIDATOR.validate(value)
    return freeze_json(value)


def validate_feedback_audit(value):
    _AUDIT_VALIDATOR.validate(value)
    return freeze_json(value)


@dataclass(frozen=True)
class CorrectionSnapshot:
    feedback_revision: int
    catalog_revision: int
    records: tuple


class AssetFeedbackStore:
    def __init__(
        self,
        recommendations,
        catalog,
        registrations,
        *,
        policy=lambda record: True,
        erasure=None,
        invalidate_derived=None,
    ):
        with recommendations.store._lock:
            if recommendations.store._asset_feedback_owner is not None:
                raise ValueError("a feedback owner is already attached")
            self.recommendations = recommendations
            self.catalog = catalog
            self.registrations = registrations
            self.policy = policy
            self.erasure = erasure
            self._key_operation = ContextVar("feedback-key-operation", default=None)
            self.invalidate_derived = invalidate_derived
            self._invalidators = []
            recommendations.register_purge_hook(self._retention_hook)
            recommendations.store._asset_feedback_owner = self

    @property
    def store(self):
        return self.recommendations.store

    @contextmanager
    def _transaction(self):
        with self.store.sql_transaction() as db, ExitStack() as leases:
            token = self._key_operation.set((leases, {}))
            try:
                yield db
            finally:
                self._key_operation.reset(token)

    def _scope_revision(self, db, scope):
        return self.recommendations.feedback_revision(db, scope)

    def _increment(self, db, scope):
        db.execute(
            "INSERT OR IGNORE INTO recommendation_scope(user_id,workspace_id) VALUES(?,?)",
            (scope["user_id"], scope["workspace_id"]),
        )
        if self.invalidate_derived is not None:
            self.invalidate_derived(dict(scope))
        for invalidate in tuple(self._invalidators):
            invalidate(dict(scope))
        db.execute(
            "UPDATE recommendation_scope SET feedback_revision=feedback_revision+1 "
            "WHERE user_id=? AND workspace_id=?",
            (scope["user_id"], scope["workspace_id"]),
        )
        return self._scope_revision(db, scope)

    def _lookup(self, db, claim):
        bound = db.execute(
            "SELECT 1 FROM erasure_key_binding WHERE singleton=1"
        ).fetchone() or has_keyed_state(db)
        digester = self._digester(db) if bound else None
        row = db.execute(
            "SELECT * FROM action_receipts WHERE namespace=? AND key_digest=?",
            (claim.namespace, claim.key_digest),
        ).fetchone()
        if row is None and digester is not None:
            row = db.execute(
                "SELECT * FROM action_receipts WHERE namespace=? AND key_digest=?",
                (
                    digester.digest(
                        purpose="lookup",
                        scope=claim.scope,
                        stable_caller=claim.caller,
                        value=claim.namespace,
                    ),
                    digester.digest(
                        purpose="key",
                        scope=claim.scope,
                        stable_caller=claim.caller,
                        value=claim.key_digest,
                    ),
                ),
            ).fetchone()
        if row is None:
            return None
        if row["erased"]:
            raise RecommendationError("invalid_state")
        if row["request_digest"] != claim.request_digest:
            raise RecommendationError("idempotency_conflict")
        return StoredResponse(row["status"], json.loads(row["body_json"]))

    def _receipt(self, db, claim, response, *, feedback_id, event_id, now, retain_until):
        db.execute(
            "INSERT INTO action_receipts(namespace,key_digest,request_digest,feedback_id,"
            "event_id,user_id,workspace_id,endpoint,caller,status,body_json,"
            "retain_until,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                claim.namespace,
                claim.key_digest,
                claim.request_digest,
                feedback_id,
                event_id,
                claim.scope["user_id"],
                claim.scope["workspace_id"],
                claim.endpoint,
                claim.caller,
                response.status,
                canonical_json(response.body),
                retain_until,
                utc_iso(now),
            ),
        )

    def _event_id(self, db, action, event_id):
        row = db.execute(
            "SELECT * FROM feedback_event_ids WHERE user_id=? AND workspace_id=? "
            "AND adapter_id=? AND event_id=?",
            (
                action.scope["user_id"],
                action.scope["workspace_id"],
                action.adapter_id,
                event_id,
            ),
        ).fetchone()
        if (
            row is None
            and db.execute("SELECT 1 FROM erasure_key_binding WHERE singleton=1").fetchone()
        ):
            digest = self._digester(db).digest(
                purpose="event_id",
                scope=action.scope,
                stable_caller=action.adapter_id,
                value=event_id,
            )
            row = db.execute(
                "SELECT * FROM feedback_event_ids WHERE user_id=? AND workspace_id=? "
                "AND adapter_id=? AND event_id=?",
                (
                    action.scope["user_id"],
                    action.scope["workspace_id"],
                    action.adapter_id,
                    digest,
                ),
            ).fetchone()
        return row

    def _digester(self, db):
        operation = self._key_operation.get()
        if operation is None:
            raise RuntimeError("keyed operation requires a transaction lease")
        leases, cache = operation
        if "digester" not in cache:
            cache["digester"] = (
                self.erasure
                if self.erasure is not None
                else leases.enter_context(protected_erasure_digester(self.store))
            )
        digester = cache["digester"]
        row = db.execute("SELECT binding FROM erasure_key_binding WHERE singleton=1").fetchone()
        if row is None:
            if has_keyed_state(db):
                raise PrivateFileError("missing erasure key binding")
            db.execute("INSERT INTO erasure_key_binding VALUES(1,?)", (digester.binding,))
        elif not hmac.compare_digest(row[0], digester.binding):
            raise PrivateFileError("erasure key binding mismatch")
        return digester

    def _audit(self, db, scope, actor, operation, revision, count, digest, now):
        audit = validate_feedback_audit(
            dict(
                schema_version=1,
                audit_id=str(uuid.uuid4()),
                scope=scope,
                adapter_id=actor,
                timestamp=utc_iso(now),
                operation=operation,
                feedback_revision=revision,
                removed_count=count,
                idempotency_digest=digest,
            )
        )
        db.execute(
            "INSERT INTO feedback_audits VALUES(?,?,?,?,?)",
            (
                audit["audit_id"],
                scope["user_id"],
                scope["workspace_id"],
                audit["timestamp"],
                canonical_json(audit),
            ),
        )

    @staticmethod
    def _erase_recorded_event(db, recommendation_id, feedback_id):
        """Erase the feedback.recorded event of one record, keeping its seq and envelope."""
        for row in db.execute(
            "SELECT seq,event_json FROM recommendation_events WHERE recommendation_id=? "
            "AND json_extract(event_json,'$.event_type')='feedback.recorded' "
            "AND json_extract(event_json,'$.payload.feedback_id')=?",
            (recommendation_id, feedback_id),
        ).fetchall():
            event = json.loads(row["event_json"])
            event.update(erased=True, payload=None)
            validate_event(event, allow_erased=True)
            db.execute(
                "UPDATE recommendation_events SET event_json=? WHERE recommendation_id=? AND seq=?",
                (canonical_json(event), recommendation_id, row["seq"]),
            )

    def _erase_feedback(self, db, row, *, digester, now):
        fid, rid = row["feedback_id"], row["recommendation_id"]
        scope = {"user_id": row["user_id"], "workspace_id": row["workspace_id"]}
        record = json.loads(row["record_json"])
        digest = functools.partial(digester.digest, scope=scope, stable_caller=record["adapter_id"])
        fid_digest = digest(purpose="object", value=fid)
        for receipt in db.execute(
            "SELECT * FROM action_receipts WHERE feedback_id=?", (fid,)
        ).fetchall():
            db.execute(
                "UPDATE action_receipts SET namespace=?,key_digest=?,request_digest=?,"
                "feedback_id=?,event_id=?,status=NULL,body_json=NULL,erased=1,erased_at=? "
                "WHERE namespace=? AND key_digest=?",
                (
                    digest(purpose="lookup", value=receipt["namespace"]),
                    digest(purpose="key", value=receipt["key_digest"]),
                    digest(purpose="request", value=receipt["request_digest"]),
                    fid_digest,
                    digest(purpose="event_id", value=receipt["event_id"]),
                    utc_iso(now),
                    receipt["namespace"],
                    receipt["key_digest"],
                ),
            )
        event = db.execute(
            "SELECT * FROM feedback_event_ids WHERE user_id=? AND workspace_id=? "
            "AND adapter_id=? AND feedback_id=?",
            (scope["user_id"], scope["workspace_id"], record["adapter_id"], fid),
        ).fetchone()
        if event is not None:
            db.execute(
                "UPDATE feedback_event_ids SET event_id=?,request_digest=?,feedback_id=?,"
                "response_json=NULL,erased=1,erased_at=? WHERE user_id=? AND workspace_id=? "
                "AND adapter_id=? AND event_id=?",
                (
                    digest(purpose="event_id", value=event["event_id"]),
                    digest(purpose="request", value=event["request_digest"]),
                    fid_digest,
                    utc_iso(now),
                    scope["user_id"],
                    scope["workspace_id"],
                    record["adapter_id"],
                    event["event_id"],
                ),
            )
        self._erase_recorded_event(db, rid, fid)
        db.execute("DELETE FROM feedback_records WHERE feedback_id=?", (fid,))
        return fid_digest

    def _scoped_mutation(self, action, claim, expected_revision, *, now, operation, erase):
        """Delete or reset: one CAS on the scope's feedback revision, audited and replayable.

        erase(db, digester) returns (removed_count, after) or None when the
        target does not exist; after(db, revision) runs once the revision moved.
        A fresh error is raised only after the transaction commits, so lazy
        expiry and maintenance stay committed (section 5.1).
        """
        error = None
        self.recommendations._action_maintenance(self.registrations, action, now=now)
        with self._transaction() as db:
            self.registrations.recheck(action)
            prior = self._lookup(db, claim)
            if prior is not None:
                return prior
            self.recommendations._expire_scope(db, action.scope, now=now)
            if self._scope_revision(db, action.scope) != expected_revision:
                error = RecommendationError("stale_version")
            else:
                digester = self._digester(db)
                erased = erase(db, digester)
                if erased is None:
                    error = RecommendationError("feedback_not_found")
                else:
                    count, after = erased
                    revision = self._increment(db, action.scope)
                    after(db, revision)
                    key_digest = digester.digest(
                        purpose="key",
                        scope=action.scope,
                        stable_caller=action.adapter_id,
                        value=claim.key_digest,
                    )
                    self._audit(
                        db,
                        action.scope,
                        action.adapter_id,
                        operation,
                        revision,
                        count,
                        key_digest,
                        now,
                    )
                    detail = (
                        dict(deleted=True) if operation == "delete" else dict(removed_count=count)
                    )
                    result = StoredResponse(
                        200, dict(schema_version=1, feedback_revision=revision, **detail)
                    )
                    self._receipt(
                        db,
                        claim,
                        result,
                        feedback_id=None,
                        event_id=None,
                        now=now,
                        retain_until=utc_iso(now + timedelta(days=RETENTION_DAYS)),
                    )
        if error is not None:
            raise error
        return result

    def delete(self, action, feedback_id, expected_feedback_revision, claim, *, now):
        def erase(db, digester):
            row = db.execute(
                "SELECT * FROM feedback_records WHERE feedback_id=? AND user_id=? "
                "AND workspace_id=?",
                (feedback_id, action.scope["user_id"], action.scope["workspace_id"]),
            ).fetchone()
            if row is None:
                return None
            fid_digest = self._erase_feedback(db, row, digester=digester, now=now)

            def after(db, revision):
                parent = self.recommendations._row(db, action.scope, row["recommendation_id"])
                self.recommendations._event(
                    db,
                    row["recommendation_id"],
                    "feedback.deleted",
                    dict(feedback_id_digest=fid_digest, feedback_revision=revision),
                    now,
                    parent["active_ms"],
                    actor="adapter",
                )

            return 1, after

        return self._scoped_mutation(
            action, claim, expected_feedback_revision, now=now, operation="delete", erase=erase
        )

    def reset(self, action, expected_feedback_revision, claim, *, now):
        def erase(db, digester):
            rows = db.execute(
                "SELECT * FROM feedback_records WHERE user_id=? AND workspace_id=? "
                "ORDER BY insertion_sequence",
                (action.scope["user_id"], action.scope["workspace_id"]),
            ).fetchall()
            for row in rows:
                self._erase_feedback(db, row, digester=digester, now=now)
            return len(rows), lambda db, revision: None

        return self._scoped_mutation(
            action, claim, expected_feedback_revision, now=now, operation="reset", erase=erase
        )

    def create(self, action, recommendation_id, request, claim, *, now):
        self.recommendations._action_maintenance(self.registrations, action, now=now)
        with self._transaction() as db:
            self.registrations.recheck(action)
            existing = self._lookup(db, claim)
            if existing is not None:
                return existing
            response, error = self._create_in(db, action, recommendation_id, request, claim, now)
        # A fresh error is raised after the commit, so lazy expiry stays recorded.
        if error is not None:
            raise error
        return response

    def _create_in(self, db, action, recommendation_id, request, claim, now):
        """One feedback create inside the open transaction: (response, error)."""
        event = self._event_id(db, action, request["event_id"])
        if event is not None:
            if event["erased"]:
                return None, RecommendationError("invalid_state")
            if event["request_digest"] != claim.request_digest:
                return None, RecommendationError("idempotency_conflict")
            response = StoredResponse(200, json.loads(event["response_json"]))
            self._receipt(
                db,
                claim,
                response,
                feedback_id=event["feedback_id"],
                event_id=request["event_id"],
                now=now,
                retain_until=event["retain_until"],
            )
            return response, None
        self.recommendations._expire_scope(db, action.scope, now=now)
        if self._scope_revision(db, action.scope) != request["expected_feedback_revision"]:
            return None, RecommendationError("stale_version")
        try:
            parent = self.recommendations._row(
                db,
                action.scope,
                recommendation_id,
                now=now,
                columns="recommendation_id,state,state_version,view_json,active_ms,"
                "accepted_json,retain_until",
            )
        except RecommendationError as exc:
            return None, exc
        view = self.recommendations._expire(db, parent, now).view
        if view["ready_at"] is None:
            return None, RecommendationError("invalid_state", recommendation_id)
        feedback = request["feedback"]
        accepted = json.loads(parent["accepted_json"])
        checked_revision = self.catalog.revision()
        current, target_hash = None, None
        if feedback["kind"] in ("correction", "rejection"):
            current, error = self._admit_target(db, view, accepted, feedback, recommendation_id)
            if error is not None:
                return None, error
            target_hash = current.content_hash
        fid = str(uuid.uuid4())
        retain_until = min(utc_iso(now + timedelta(days=RETENTION_DAYS)), parent["retain_until"])
        seqrow = db.execute(
            "SELECT next_sequence FROM feedback_scope_sequence WHERE user_id=? AND workspace_id=?",
            (action.scope["user_id"], action.scope["workspace_id"]),
        ).fetchone()
        sequence = seqrow[0] if seqrow else 1
        db.execute(
            "INSERT INTO feedback_scope_sequence VALUES(?,?,?) "
            "ON CONFLICT(user_id,workspace_id) DO UPDATE SET "
            "next_sequence=excluded.next_sequence",
            (action.scope["user_id"], action.scope["workspace_id"], sequence + 1),
        )
        new_revision = self._increment(db, action.scope)
        record = validate_feedback_record(
            dict(
                schema_version=1,
                feedback_id=fid,
                recommendation_id=recommendation_id,
                task_id=view["task_id"],
                scope=action.scope,
                adapter_id=action.adapter_id,
                event_id=request["event_id"],
                recorded_at=utc_iso(now),
                retain_until=retain_until,
                feedback_revision=new_revision,
                feedback=feedback,
                redacted_task_text=accepted["task_text"],
                asset_content_hash=target_hash,
                configured_primary=accepted["configured_primary"],
                catalog_revision=checked_revision,
            )
        )
        db.execute(
            "INSERT INTO feedback_records VALUES(?,?,?,?,?,?,?)",
            (
                fid,
                recommendation_id,
                action.scope["user_id"],
                action.scope["workspace_id"],
                sequence,
                retain_until,
                canonical_json(record),
            ),
        )
        if target_hash is not None:
            db.execute(
                "INSERT INTO feedback_bindings VALUES(?,?)",
                (fid, canonical_json(current.to_json_object())),
            )
        self.recommendations._event(
            db,
            recommendation_id,
            "feedback.recorded",
            dict(feedback_id=fid, feedback_revision=new_revision),
            now,
            parent["active_ms"],
            actor="adapter",
        )
        response = StoredResponse(
            200, dict(schema_version=1, feedback_id=fid, feedback_revision=new_revision)
        )
        self._receipt(
            db,
            claim,
            response,
            feedback_id=fid,
            event_id=request["event_id"],
            now=now,
            retain_until=retain_until,
        )
        db.execute(
            "INSERT INTO feedback_event_ids(user_id,workspace_id,adapter_id,event_id,"
            "request_digest,feedback_id,response_json,retain_until,recorded_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (
                action.scope["user_id"],
                action.scope["workspace_id"],
                action.adapter_id,
                request["event_id"],
                claim.request_digest,
                fid,
                canonical_json(response.body),
                retain_until,
                utc_iso(now),
            ),
        )
        return response, None

    def _admit_target(self, db, view, accepted, feedback, recommendation_id):
        """The current record a correction or rejection names: (current, error).

        A rejection binds an offered candidate and its original source identity;
        a correction may name any current in-scope asset (section 7.6).
        """
        asset_id = feedback["asset_id"]
        original = None
        if feedback["kind"] == "rejection":
            offered = {c["asset_id"]: c for c in view["result"]["final_candidates"]}
            target = offered.get(asset_id)
            if target is None:
                return None, RecommendationError("invalid_request", recommendation_id)
            if target["content_hash"] != feedback["content_hash"]:
                return None, RecommendationError("asset_changed", recommendation_id)
            original = offered_record(accepted, asset_id)
        current = current_asset(self.catalog, asset_id)
        if current is None:
            removed = db.execute(
                "SELECT 1 FROM asset_catalog_tombstones WHERE asset_id=?", (asset_id,)
            ).fetchone()
            known = offered_record(accepted, asset_id) is not None or removed is not None
            return None, RecommendationError(
                "asset_changed" if known else "invalid_request", recommendation_id
            )
        if current.content_hash != feedback["content_hash"] or not self.policy(current):
            return None, RecommendationError("asset_changed", recommendation_id)
        if feedback["kind"] == "rejection" and (
            original is None or not same_identity(original, current)
        ):
            return None, RecommendationError("asset_changed", recommendation_id)
        return current, None

    def list(self, action, *, after_id=None, limit=DEFAULT_EVENTS_PAGE, now):
        if type(limit) is not int or not 1 <= limit <= MAX_EVENTS_PAGE:
            raise RecommendationError("invalid_request")
        try:
            if after_id is not None and (
                type(after_id) is not str or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", after_id)
            ):
                raise ValueError
            sequence = (
                0
                if after_id is None
                else int(
                    base64.urlsafe_b64decode(after_id + "=" * (-len(after_id) % 4)).decode("ascii")
                )
            )
            if not 0 <= sequence <= MAX_SEQUENCE:
                raise ValueError
            if (
                after_id is not None
                and base64.urlsafe_b64encode(str(sequence).encode()).decode().rstrip("=")
                != after_id
            ):
                raise ValueError
        except (ValueError, UnicodeError):
            raise RecommendationError("invalid_request") from None
        self.recommendations._action_maintenance(self.registrations, action, now=now)
        self.recommendations._observe_scope_expiry(action.scope, now=now)
        with self.store.sql_read() as db:
            self.registrations.recheck(action, connection=db)
            rows = db.execute(
                "SELECT insertion_sequence,record_json FROM feedback_records WHERE user_id=? "
                "AND workspace_id=? AND insertion_sequence>? ORDER BY insertion_sequence LIMIT ?",
                (action.scope["user_id"], action.scope["workspace_id"], sequence, limit + 1),
            ).fetchall()
            revision = self._scope_revision(db, action.scope)
        # Decoding and validation run after the store lock is released.
        page = rows[:limit]
        cursor = (
            base64.urlsafe_b64encode(str(page[-1]["insertion_sequence"]).encode())
            .decode()
            .rstrip("=")
            if len(rows) > limit
            else None
        )
        return freeze_json(
            dict(
                schema_version=1,
                records=[validate_feedback_record(json.loads(row["record_json"])) for row in page],
                feedback_revision=revision,
                next_after_id=cursor,
            )
        )

    def expire(self, *, now):
        """Apply shared retention, including feedback with no expired parent."""
        return self.recommendations.sweep(now=now)

    def register_invalidator(self, callback):
        """Register synchronous private-state fencing before feedback acknowledgments."""
        with self.store._lock:
            if callback not in self._invalidators:
                self._invalidators.append(callback)

        def remove():
            with self.store._lock:
                if callback in self._invalidators:
                    self._invalidators.remove(callback)

        return remove

    def correction_snapshot(self, scope, current_catalog, *, now, check=lambda: None):
        """One store-owned revision/history read; filter before retaining latest20.

        Stream historical rows under the caller's unchanged total deadline; do
        not allocate an unbounded list or silently stop at a favorable prefix.
        """
        self.maintain_scope(scope, now=now)
        self.recommendations._observe_scope_expiry(scope, now=now)
        current = {asset.asset_id: asset for asset in current_catalog.assets}
        binding_fields = ("asset_id", *IDENTITY_FIELDS)
        result = []

        def order(record):
            return (
                datetime.fromisoformat(record["recorded_at"].replace("Z", "+00:00")),
                record["feedback_id"].encode("utf-8"),
            )

        with self.store.sql_read() as db:
            check()
            revision = self._scope_revision(db, scope)
            rows = db.execute(
                "SELECT r.record_json,b.asset_json FROM feedback_records r "
                "JOIN feedback_bindings b USING(feedback_id) WHERE r.user_id=? "
                "AND r.workspace_id=?",
                (scope["user_id"], scope["workspace_id"]),
            )
            for row in rows:
                check()
                record, binding = json.loads(row["record_json"]), json.loads(row["asset_json"])
                if record["feedback"]["kind"] != "correction":
                    continue
                if datetime.fromisoformat(record["retain_until"].replace("Z", "+00:00")) <= now:
                    continue
                target = current.get(binding["asset_id"])
                if target is None or not self.policy(target):
                    continue
                raw = target.to_json_object()
                if any(binding[field] != raw[field] for field in binding_fields):
                    continue
                result.append(validate_feedback_record(record))
                result.sort(key=order)
                if len(result) > 20:
                    del result[0]
            check()
        return CorrectionSnapshot(revision, current_catalog.catalog_revision, tuple(result))

    def eligible_corrections(self, scope, current_catalog, *, now):
        """Read-only exact-binding evidence; performs no ranking or adaptation."""
        current = {asset.asset_id: asset.to_json_object() for asset in current_catalog.assets}
        binding_fields = ("asset_id", *IDENTITY_FIELDS)
        self.maintain_scope(scope, now=now)
        self.recommendations._observe_scope_expiry(scope, now=now)
        with self.store.sql_read() as db:
            rows = db.execute(
                "SELECT r.record_json,b.asset_json FROM feedback_records r "
                "JOIN feedback_bindings b USING(feedback_id) WHERE r.user_id=? "
                "AND r.workspace_id=? ORDER BY r.insertion_sequence",
                (scope["user_id"], scope["workspace_id"]),
            ).fetchall()
        result = []
        for row in rows:
            record, binding = json.loads(row["record_json"]), json.loads(row["asset_json"])
            target = current.get(binding["asset_id"])
            if (
                record["feedback"]["kind"] == "correction"
                and target is not None
                and all(binding[key] == target[key] for key in binding_fields)
                and self.policy(validate_asset_record(target))
            ):
                result.append(validate_feedback_record(record))
        return tuple(result)

    def maintain_scope(self, scope: Mapping[str, str], *, now: datetime) -> None:
        with self.store.sql_read() as db:
            due = any(
                db.execute(
                    f"SELECT 1 FROM {table} WHERE user_id=? AND workspace_id=? "
                    "AND retain_until<=? LIMIT 1",
                    (scope["user_id"], scope["workspace_id"], utc_iso(now)),
                ).fetchone()
                for table in ("feedback_records", "action_receipts", "feedback_event_ids")
            )
        if due:
            with self.store.sql_transaction() as db:
                self._retention_hook(db, (), now=now, scope=scope)

    def _retention_hook(self, db, expired_parent_ids, *, now, scope=None):
        # Expired private payload and its replay ownership end together. Full
        # retention leaves no lookup tombstone and therefore needs no HMAC key.
        expired = tuple(expired_parent_ids)
        where, parameters = "retain_until<=?", [utc_iso(now)]
        if expired:
            where = f"({where} OR recommendation_id IN ({','.join('?' * len(expired))}))"
            parameters += expired
        if scope is not None:
            where += " AND user_id=? AND workspace_id=?"
            parameters += [scope["user_id"], scope["workspace_id"]]
        rows = db.execute(
            "SELECT feedback_id,recommendation_id,user_id,workspace_id FROM feedback_records "
            f"WHERE {where}",
            parameters,
        ).fetchall()
        removed = {}
        for row in rows:
            scope_key = (row["user_id"], row["workspace_id"])
            removed[scope_key] = removed.get(scope_key, 0) + 1
            fid = row["feedback_id"]
            db.execute("DELETE FROM action_receipts WHERE feedback_id=?", (fid,))
            db.execute("DELETE FROM feedback_event_ids WHERE feedback_id=?", (fid,))
            self._erase_recorded_event(db, row["recommendation_id"], fid)
            db.execute("DELETE FROM feedback_records WHERE feedback_id=?", (fid,))
        for (user, workspace), count in removed.items():
            affected_scope = dict(user_id=user, workspace_id=workspace)
            revision = self._increment(db, affected_scope)
            self._audit(db, affected_scope, "gateway", "retention", revision, count, None, now)
        for table in ("action_receipts", "feedback_event_ids"):
            predicate = "" if scope is None else " AND user_id=? AND workspace_id=?"
            table_parameters = (utc_iso(now),) + (
                () if scope is None else (scope["user_id"], scope["workspace_id"])
            )
            db.execute(f"DELETE FROM {table} WHERE retain_until<=?{predicate}", table_parameters)
