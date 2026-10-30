"""Operator-owned direct action registrations (RM37/AC28)."""

import hashlib
import hmac
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from relaymind.recommendations.errors import RecommendationError
from relaymind.recommendations.private_files import PrivateFileError, read_private_file
from relaymind.store.run_store import RunStore


class QualificationResolver(Protocol):
    def __call__(
        self, adapter_id: str, host_identity: str, session_mapping: str, contract: str
    ) -> bool: ...


def no_production_qualification(
    adapter_id: str, host_identity: str, session_mapping: str, contract: str
) -> bool:
    """No production action adapter has passed the separate host qualification."""
    return False


@dataclass(frozen=True)
class Registration:
    adapter_id: str
    user_id: str
    workspace_id: str
    credential_sha256: str
    enabled: bool
    host_identity: str
    session_mapping: str
    qualified_contract_revision: str


@dataclass(frozen=True)
class RegistrationSnapshot:
    registrations: tuple[Registration, ...]


@dataclass(frozen=True)
class UserActionSettings:
    registrations_path: Path | None = None


def parse_user_action_settings(value: dict, *, config_dir: Path) -> UserActionSettings:
    if set(value) - {"registrations_path"}:
        raise ValueError("user_actions: unknown setting")
    path = value.get("registrations_path")
    if path is None and "registrations_path" not in value:
        return UserActionSettings()
    if not isinstance(path, str) or not path.strip() or len(path.encode("utf-8")) > 2048:
        raise ValueError("user_actions registrations_path must be a nonempty path")
    return UserActionSettings(Path(os.path.abspath(config_dir / path)))


@dataclass(frozen=True)
class AuthenticatedAction:
    adapter_id: str
    scope: dict[str, str]
    credential_sha256: str


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate registration field")
        result[key] = value
    return result


def load_registrations(
    path: Path, *, qualification: QualificationResolver = no_production_qualification
) -> RegistrationSnapshot:
    try:
        raw = json.loads(read_private_file(path).decode("utf-8"), object_pairs_hook=_pairs)
        if type(raw) is not dict or set(raw) != {"schema_version", "registrations"}:
            raise ValueError
        if type(raw["schema_version"]) is not int or raw["schema_version"] != 1:
            raise ValueError
        records = raw["registrations"]
        if type(records) is not list or len(records) > 1000:
            raise ValueError
        parsed = []
        digests = set()
        enabled_scopes = set()
        keys = {
            "schema_version",
            "adapter_id",
            "user_id",
            "workspace_id",
            "credential_sha256",
            "enabled",
            "host_identity",
            "session_mapping",
            "qualified_contract_revision",
        }
        for item in records:
            if (
                type(item) is not dict
                or set(item) != keys
                or type(item["schema_version"]) is not int
                or item["schema_version"] != 1
            ):
                raise ValueError
            for name in keys - {"schema_version", "enabled"}:
                value = item[name]
                if (
                    type(value) is not str
                    or not value.strip()
                    or not 1 <= len(value.encode("utf-8")) <= 256
                ):
                    raise ValueError
            if type(item["enabled"]) is not bool:
                raise ValueError
            digest = item["credential_sha256"]
            if not re.fullmatch(r"[0-9a-f]{64}", digest) or digest in digests:
                raise ValueError
            digests.add(digest)
            scope = (item["adapter_id"], item["user_id"], item["workspace_id"])
            if item["enabled"]:
                if scope in enabled_scopes or not qualification(
                    item["adapter_id"],
                    item["host_identity"],
                    item["session_mapping"],
                    item["qualified_contract_revision"],
                ):
                    raise ValueError
                enabled_scopes.add(scope)
            parsed.append(Registration(**{k: item[k] for k in keys - {"schema_version"}}))
        return RegistrationSnapshot(tuple(parsed))
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError) as exc:
        if isinstance(exc, PrivateFileError):
            raise
        raise PrivateFileError(f"invalid registration file: {path}") from None


class ActionRegistrations:
    def __init__(
        self, store: RunStore, *, qualification: QualificationResolver = no_production_qualification
    ):
        self.store = store
        self.qualification = qualification

    def replace(self, snapshot: RegistrationSnapshot) -> None:
        if any(
            record.enabled
            and not self.qualification(
                record.adapter_id,
                record.host_identity,
                record.session_mapping,
                record.qualified_contract_revision,
            )
            for record in snapshot.registrations
        ):
            raise ValueError("action contract is not qualified")
        with self.store.sql_transaction() as db:
            db.execute("UPDATE action_registrations SET enabled=0")
            for record in snapshot.registrations:
                db.execute(
                    "INSERT INTO action_registrations VALUES(?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(credential_sha256) DO UPDATE SET "
                    "adapter_id=excluded.adapter_id,user_id=excluded.user_id,"
                    "workspace_id=excluded.workspace_id,enabled=excluded.enabled,"
                    "host_identity=excluded.host_identity,session_mapping=excluded.session_mapping,"
                    "qualified_contract_revision=excluded.qualified_contract_revision",
                    (
                        record.credential_sha256,
                        record.adapter_id,
                        record.user_id,
                        record.workspace_id,
                        int(record.enabled),
                        record.host_identity,
                        record.session_mapping,
                        record.qualified_contract_revision,
                    ),
                )

    def authenticate(self, token: str | None, active_scope: dict) -> AuthenticatedAction:
        if token is None or not token or len(token) > 255 or not token.isascii():
            raise RecommendationError("user_action_required")
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with self.store.sql_read() as db:
            row = db.execute(
                "SELECT * FROM action_registrations WHERE credential_sha256=? AND enabled=1",
                (digest,),
            ).fetchone()
            if (
                row is None
                or not hmac.compare_digest(row["credential_sha256"], digest)
                or not self.qualification(
                    row["adapter_id"],
                    row["host_identity"],
                    row["session_mapping"],
                    row["qualified_contract_revision"],
                )
            ):
                raise RecommendationError("user_action_required")
            scope = {"user_id": row["user_id"], "workspace_id": row["workspace_id"]}
            if scope != active_scope:
                raise RecommendationError("user_action_required")
            return AuthenticatedAction(row["adapter_id"], scope, digest)

    def recheck(self, action: AuthenticatedAction, *, connection=None) -> None:
        if connection is None:
            with self.store.sql_read() as db:
                self.recheck(action, connection=db)
            return
        db = connection
        row = db.execute(
            "SELECT * FROM action_registrations WHERE credential_sha256=?",
            (action.credential_sha256,),
        ).fetchone()
        if (
            row is None
            or not row["enabled"]
            or row["adapter_id"] != action.adapter_id
            or row["user_id"] != action.scope["user_id"]
            or row["workspace_id"] != action.scope["workspace_id"]
            or not self.qualification(
                row["adapter_id"],
                row["host_identity"],
                row["session_mapping"],
                row["qualified_contract_revision"],
            )
        ):
            raise RecommendationError("user_action_required")
