"""Operator-owned direct action registrations (RM37/AC28)."""

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from relaymind.recommendations.errors import RecommendationError
from relaymind.recommendations.private_files import (
    PrivateFileError,
    read_private_file,
    resolve_private_path,
)
from relaymind.selection.asset_http import strict_json
from relaymind.store.run_store import RunStore


class QualificationResolver(Protocol):
    def __call__(
        self, adapter_id: str, host_identity: str, session_mapping: str, contract: str
    ) -> bool: ...


def production_qualification(
    adapter_id: str, host_identity: str, session_mapping: str, contract: str
) -> bool:
    """Accept the one reviewed native contract; operator references remain opaque.

    The shipped adapter independently pins the host and exact implementation.
    A local registration still needs its private credential and exact active scope.
    """
    if contract != "openclaw-native-2026.10.7.v1":
        return False
    try:
        return all(
            type(value) is str and bool(value.strip()) and 1 <= len(value.encode("utf-8")) <= 256
            for value in (adapter_id, host_identity, session_mapping)
        )
    except UnicodeError:
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
    return UserActionSettings(resolve_private_path(Path(os.path.abspath(config_dir / path))))


@dataclass(frozen=True)
class AuthenticatedAction:
    adapter_id: str
    scope: dict[str, str]
    credential_sha256: str


def load_registrations(
    path: Path, *, qualification: QualificationResolver = production_qualification
) -> RegistrationSnapshot:
    try:
        raw = strict_json(read_private_file(path))
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
                if scope in enabled_scopes or not _qualified(qualification, item):
                    raise ValueError
                enabled_scopes.add(scope)
            parsed.append(Registration(**{k: item[k] for k in keys - {"schema_version"}}))
        return RegistrationSnapshot(tuple(parsed))
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError) as exc:
        if isinstance(exc, PrivateFileError):
            raise
        raise PrivateFileError(f"invalid registration file: {path}") from None


def _qualified(qualification: QualificationResolver, record) -> bool:
    """Whether the qualified adapter contract a registration names is accepted.

    record is any mapping by field name: a parsed item, a database row, or vars().
    """
    return qualification(
        record["adapter_id"],
        record["host_identity"],
        record["session_mapping"],
        record["qualified_contract_revision"],
    )


class ActionRegistrations:
    def __init__(
        self, store: RunStore, *, qualification: QualificationResolver = production_qualification
    ):
        self.store = store
        self.qualification = qualification

    def replace(self, snapshot: RegistrationSnapshot) -> None:
        if any(
            record.enabled and not _qualified(self.qualification, vars(record))
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
        if token is None or not token or not token.isascii():
            raise RecommendationError("user_action_required")
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with self.store.sql_read() as db:
            row = db.execute(
                "SELECT * FROM action_registrations WHERE credential_sha256=? AND enabled=1",
                (digest,),
            ).fetchone()
            # The row was selected by an exact match on the token's digest.
            if row is None or not _qualified(self.qualification, row):
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
            or not _qualified(self.qualification, row)
        ):
            raise RecommendationError("user_action_required")
