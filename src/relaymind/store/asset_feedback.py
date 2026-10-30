"""Scoped explicit feedback, evidence and privacy ownership (RM37/AC28)."""

import base64
import hmac
import json
import re
import uuid
from collections.abc import Mapping
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta

from jsonschema import FormatChecker

from relaymind.canonical import canonical_json
from relaymind.recommendations.errors import RecommendationError
from relaymind.recommendations.limits import DEFAULT_EVENTS_PAGE, MAX_EVENTS_PAGE, RETENTION_DAYS
from relaymind.schema_util import StrictValidator, freeze_json, load_packaged_schema
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
        row = db.execute(
            "SELECT feedback_revision FROM recommendation_scope WHERE user_id=? AND workspace_id=?",
            (scope["user_id"], scope["workspace_id"]),
        ).fetchone()
        if row is None:
            return 0
        return row[0]

    def _increment(self, db, scope):
        db.execute(
            "INSERT OR IGNORE INTO recommendation_scope(user_id,workspace_id) VALUES(?,?)",
            (scope["user_id"], scope["workspace_id"]),
        )
        if self.invalidate_derived is not None:
            self.invalidate_derived(dict(scope))
        db.execute(
            "UPDATE recommendation_scope SET feedback_revision=feedback_revision+1 "
            "WHERE user_id=? AND workspace_id=?",
            (scope["user_id"], scope["workspace_id"]),
        )
        return self._scope_revision(db, scope)

    def _lookup(self, db, claim):
        from relaymind.recommendations.erasure import has_keyed_state

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
        from relaymind.recommendations.erasure import has_keyed_state, protected_erasure_digester
        from relaymind.recommendations.private_files import PrivateFileError

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

    def _erase_feedback(self, db, row, *, digester, now):
        fid, rid = row["feedback_id"], row["recommendation_id"]
        scope = {"user_id": row["user_id"], "workspace_id": row["workspace_id"]}
        record = json.loads(row["record_json"])
        caller = record["adapter_id"]
        fid_digest = digester.digest(purpose="object", scope=scope, stable_caller=caller, value=fid)
        for receipt in db.execute(
            "SELECT * FROM action_receipts WHERE feedback_id=?", (fid,)
        ).fetchall():
            db.execute(
                "UPDATE action_receipts SET namespace=?,key_digest=?,request_digest=?,"
                "feedback_id=?,event_id=?,status=NULL,body_json=NULL,erased=1,erased_at=? "
                "WHERE namespace=? AND key_digest=?",
                (
                    digester.digest(
                        purpose="lookup",
                        scope=scope,
                        stable_caller=caller,
                        value=receipt["namespace"],
                    ),
                    digester.digest(
                        purpose="key",
                        scope=scope,
                        stable_caller=caller,
                        value=receipt["key_digest"],
                    ),
                    digester.digest(
                        purpose="request",
                        scope=scope,
                        stable_caller=caller,
                        value=receipt["request_digest"],
                    ),
                    fid_digest,
                    digester.digest(
                        purpose="event_id",
                        scope=scope,
                        stable_caller=caller,
                        value=receipt["event_id"],
                    ),
                    utc_iso(now),
                    receipt["namespace"],
                    receipt["key_digest"],
                ),
            )
        event = db.execute(
            "SELECT * FROM feedback_event_ids WHERE user_id=? AND workspace_id=? "
            "AND adapter_id=? AND feedback_id=?",
            (scope["user_id"], scope["workspace_id"], caller, fid),
        ).fetchone()
        if event is not None:
            db.execute(
                "UPDATE feedback_event_ids SET event_id=?,request_digest=?,feedback_id=?,"
                "response_json=NULL,erased=1,erased_at=? WHERE user_id=? AND workspace_id=? "
                "AND adapter_id=? AND event_id=?",
                (
                    digester.digest(
                        purpose="event_id",
                        scope=scope,
                        stable_caller=caller,
                        value=event["event_id"],
                    ),
                    digester.digest(
                        purpose="request",
                        scope=scope,
                        stable_caller=caller,
                        value=event["request_digest"],
                    ),
                    fid_digest,
                    utc_iso(now),
                    scope["user_id"],
                    scope["workspace_id"],
                    caller,
                    event["event_id"],
                ),
            )
        for item in db.execute(
            "SELECT seq,event_json FROM recommendation_events WHERE recommendation_id=?",
            (rid,),
        ).fetchall():
            evidence = json.loads(item["event_json"])
            if (
                evidence["event_type"] == "feedback.recorded"
                and evidence["payload"] is not None
                and evidence["payload"]["feedback_id"] == fid
            ):
                evidence["erased"], evidence["payload"] = True, None
                db.execute(
                    "UPDATE recommendation_events SET event_json=? "
                    "WHERE recommendation_id=? AND seq=?",
                    (canonical_json(evidence), rid, item["seq"]),
                )
        db.execute("DELETE FROM feedback_records WHERE feedback_id=?", (fid,))
        return fid_digest

    def delete(self, action, feedback_id, expected_feedback_revision, claim, *, now):
        error = None
        result = None
        self.recommendations._action_maintenance(self.registrations, action, now=now)
        with self._transaction() as db:
            self.registrations.recheck(action)
            prior = self._lookup(db, claim)
            if prior is None:
                self.recommendations._expire_scope(db, action.scope, now=now)
            if prior is not None:
                result = prior
            elif self._scope_revision(db, action.scope) != expected_feedback_revision:
                error = RecommendationError("stale_version")
            else:
                row = db.execute(
                    "SELECT * FROM feedback_records WHERE feedback_id=? AND user_id=? "
                    "AND workspace_id=?",
                    (feedback_id, action.scope["user_id"], action.scope["workspace_id"]),
                ).fetchone()
                if row is None:
                    error = RecommendationError("feedback_not_found")
                else:
                    digester = self._digester(db)
                    fid_digest = self._erase_feedback(db, row, digester=digester, now=now)
                    revision = self._increment(db, action.scope)
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
                    self._audit(
                        db,
                        action.scope,
                        action.adapter_id,
                        "delete",
                        revision,
                        1,
                        digester.digest(
                            purpose="key",
                            scope=action.scope,
                            stable_caller=action.adapter_id,
                            value=claim.key_digest,
                        ),
                        now,
                    )
                    result = StoredResponse(
                        200,
                        dict(
                            schema_version=1,
                            feedback_revision=revision,
                            deleted=True,
                        ),
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

    def reset(self, action, expected_feedback_revision, claim, *, now):
        error = None
        result = None
        self.recommendations._action_maintenance(self.registrations, action, now=now)
        with self._transaction() as db:
            self.registrations.recheck(action)
            prior = self._lookup(db, claim)
            if prior is None:
                self.recommendations._expire_scope(db, action.scope, now=now)
            if prior is not None:
                result = prior
            elif self._scope_revision(db, action.scope) != expected_feedback_revision:
                error = RecommendationError("stale_version")
            else:
                digester = self._digester(db)
                rows = db.execute(
                    "SELECT * FROM feedback_records WHERE user_id=? AND workspace_id=? "
                    "ORDER BY insertion_sequence",
                    (action.scope["user_id"], action.scope["workspace_id"]),
                ).fetchall()
                for row in rows:
                    self._erase_feedback(db, row, digester=digester, now=now)
                revision = self._increment(db, action.scope)
                self._audit(
                    db,
                    action.scope,
                    action.adapter_id,
                    "reset",
                    revision,
                    len(rows),
                    digester.digest(
                        purpose="key",
                        scope=action.scope,
                        stable_caller=action.adapter_id,
                        value=claim.key_digest,
                    ),
                    now,
                )
                result = StoredResponse(
                    200,
                    dict(
                        schema_version=1,
                        feedback_revision=revision,
                        removed_count=len(rows),
                    ),
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

    def create(self, action, recommendation_id, request, claim, *, now):
        error = None
        response = None
        self.recommendations._action_maintenance(self.registrations, action, now=now)
        with self._transaction() as db:
            self.registrations.recheck(action)
            existing = self._lookup(db, claim)
            if existing is not None:
                return existing
            event = self._event_id(db, action, request["event_id"])
            if event is not None:
                if event["erased"]:
                    error = RecommendationError("invalid_state")
                elif event["request_digest"] != claim.request_digest:
                    error = RecommendationError("idempotency_conflict")
                else:
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
            else:
                self.recommendations._expire_scope(db, action.scope, now=now)
                revision = self._scope_revision(db, action.scope)
                if revision != request["expected_feedback_revision"]:
                    error = RecommendationError("stale_version")
                else:
                    try:
                        parent = self.recommendations._row(
                            db,
                            action.scope,
                            recommendation_id,
                            now=now,
                            columns="recommendation_id,state,state_version,view_json,active_ms,accepted_json,retain_until",
                        )
                    except RecommendationError as exc:
                        error = exc
                    else:
                        view = self.recommendations._expire(db, parent, now).view
                        if view["ready_at"] is None:
                            error = RecommendationError("invalid_state", recommendation_id)
                        else:
                            feedback = request["feedback"]
                            original = None
                            target_hash = None
                            checked_revision = self.catalog.revision()
                            if feedback["kind"] == "rejection":
                                offered = {
                                    candidate["asset_id"]: candidate
                                    for candidate in view["result"]["final_candidates"]
                                }
                                target = offered.get(feedback["asset_id"])
                                if target is None:
                                    error = RecommendationError(
                                        "invalid_request", recommendation_id
                                    )
                                elif target["content_hash"] != feedback["content_hash"]:
                                    error = RecommendationError("asset_changed", recommendation_id)
                                else:
                                    original = next(
                                        (
                                            asset
                                            for asset in json.loads(parent["accepted_json"])[
                                                "catalog"
                                            ]
                                            if asset["asset_id"] == feedback["asset_id"]
                                        ),
                                        None,
                                    )
                            if feedback["kind"] in ("correction", "rejection") and error is None:
                                current = next(
                                    (
                                        asset
                                        for asset in self.catalog.snapshot().assets
                                        if asset.asset_id == feedback["asset_id"]
                                    ),
                                    None,
                                )
                                if current is None:
                                    known_offer = any(
                                        asset["asset_id"] == feedback["asset_id"]
                                        for asset in json.loads(parent["accepted_json"])["catalog"]
                                    )
                                    removed = db.execute(
                                        "SELECT 1 FROM asset_catalog_tombstones WHERE asset_id=?",
                                        (feedback["asset_id"],),
                                    ).fetchone()
                                    error = RecommendationError(
                                        "asset_changed"
                                        if known_offer or removed
                                        else "invalid_request",
                                        recommendation_id,
                                    )
                                elif current.content_hash != feedback[
                                    "content_hash"
                                ] or not self.policy(current):
                                    error = RecommendationError("asset_changed", recommendation_id)
                                elif feedback["kind"] == "rejection" and (
                                    original is None
                                    or any(
                                        getattr(current, name) != original[name]
                                        for name in (
                                            "schema_version",
                                            "kind",
                                            "source_id",
                                            "source_locator",
                                            "source_version",
                                            "content_hash",
                                            "identity",
                                        )
                                    )
                                ):
                                    error = RecommendationError("asset_changed", recommendation_id)
                                else:
                                    target_hash = current.content_hash
                            if error is None:
                                accepted = json.loads(parent["accepted_json"])
                                fid = str(uuid.uuid4())
                                retain_until = min(
                                    utc_iso(now + timedelta(days=RETENTION_DAYS)),
                                    parent["retain_until"],
                                )
                                seqrow = db.execute(
                                    "SELECT next_sequence FROM feedback_scope_sequence "
                                    "WHERE user_id=? AND workspace_id=?",
                                    (action.scope["user_id"], action.scope["workspace_id"]),
                                ).fetchone()
                                sequence = seqrow[0] if seqrow else 1
                                db.execute(
                                    "INSERT INTO feedback_scope_sequence VALUES(?,?,?) "
                                    "ON CONFLICT(user_id,workspace_id) DO UPDATE SET "
                                    "next_sequence=excluded.next_sequence",
                                    (
                                        action.scope["user_id"],
                                        action.scope["workspace_id"],
                                        sequence + 1,
                                    ),
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
                                    200,
                                    dict(
                                        schema_version=1,
                                        feedback_id=fid,
                                        feedback_revision=new_revision,
                                    ),
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
                                    "INSERT INTO feedback_event_ids(user_id,workspace_id,"
                                    "adapter_id,"
                                    "event_id,request_digest,feedback_id,response_json,retain_until,"
                                    "recorded_at) VALUES(?,?,?,?,?,?,?,?,?)",
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
        if error is not None:
            raise error
        return response

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
            if not 0 <= sequence <= 2**63 - 1:
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
                    records=[
                        validate_feedback_record(json.loads(row["record_json"])) for row in page
                    ],
                    feedback_revision=self._scope_revision(db, action.scope),
                    next_after_id=cursor,
                )
            )

    def expire(self, *, now):
        """Apply shared retention, including feedback with no expired parent."""
        return self.recommendations.sweep(now=now)

    def eligible_corrections(self, scope, current_catalog, *, now):
        """Read-only exact-binding evidence; performs no ranking or adaptation."""
        current = {asset.asset_id: asset.to_json_object() for asset in current_catalog.assets}
        identity_fields = (
            "schema_version",
            "asset_id",
            "kind",
            "source_id",
            "source_locator",
            "source_version",
            "content_hash",
            "identity",
        )
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
                    and all(binding[key] == target[key] for key in identity_fields)
                ):
                    from relaymind.assets.catalog import validate_asset_record

                    if self.policy(validate_asset_record(target)):
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
        expired = set(expired_parent_ids)
        rows = [
            row
            for row in db.execute(
                "SELECT feedback_id,recommendation_id,user_id,workspace_id,retain_until "
                "FROM feedback_records"
                + (" WHERE user_id=? AND workspace_id=?" if scope is not None else ""),
                () if scope is None else (scope["user_id"], scope["workspace_id"]),
            )
            if row["recommendation_id"] in expired or row["retain_until"] <= utc_iso(now)
        ]
        removed = {}
        for row in rows:
            scope_key = (row["user_id"], row["workspace_id"])
            removed[scope_key] = removed.get(scope_key, 0) + 1
            fid, rid = row["feedback_id"], row["recommendation_id"]
            db.execute("DELETE FROM action_receipts WHERE feedback_id=?", (fid,))
            db.execute("DELETE FROM feedback_event_ids WHERE feedback_id=?", (fid,))
            for event_row in db.execute(
                "SELECT seq,event_json FROM recommendation_events WHERE recommendation_id=?", (rid,)
            ).fetchall():
                event = json.loads(event_row["event_json"])
                if (
                    event["event_type"] == "feedback.recorded"
                    and event["payload"] is not None
                    and event["payload"]["feedback_id"] == fid
                ):
                    event.update(payload=None, erased=True)
                    db.execute(
                        "UPDATE recommendation_events SET event_json=? "
                        "WHERE recommendation_id=? AND seq=?",
                        (canonical_json(event), rid, event_row["seq"]),
                    )
            db.execute("DELETE FROM feedback_records WHERE feedback_id=?", (fid,))
        for (user, workspace), count in removed.items():
            affected_scope = dict(user_id=user, workspace_id=workspace)
            revision = self._increment(db, affected_scope)
            self._audit(db, affected_scope, "gateway", "retention", revision, count, None, now)
        for table in ("action_receipts", "feedback_event_ids"):
            predicate = "" if scope is None else " AND user_id=? AND workspace_id=?"
            parameters = (utc_iso(now),) + (
                () if scope is None else (scope["user_id"], scope["workspace_id"])
            )
            db.execute(f"DELETE FROM {table} WHERE retain_until<=?{predicate}", parameters)
