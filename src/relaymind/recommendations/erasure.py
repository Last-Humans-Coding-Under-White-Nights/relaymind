"""Protected instance-local keyed erasure digests (RM37/AC28)."""

import hashlib
import hmac
import secrets
from contextlib import ExitStack, contextmanager
from pathlib import Path

from relaymind.canonical import canonical_json
from relaymind.recommendations.private_files import (
    PrivateFileError,
    create_private_file_exclusive,
    open_private_file,
    resolve_private_path,
)

_PURPOSES = frozenset({"lookup", "key", "object", "request", "event_id", "task_id"})


class HmacErasureDigester:
    def __init__(self, key: bytes):
        if type(key) is not bytes or len(key) < 32:
            raise ValueError("invalid erasure key")
        self._key = key

    @property
    def binding(self) -> str:
        return hmac.new(self._key, b"relaymind-erasure-binding-v1", hashlib.sha256).hexdigest()

    def digest(self, *, purpose: str, scope: dict, stable_caller: str, value: object) -> str:
        if purpose not in _PURPOSES:
            raise ValueError("invalid erasure purpose")
        message = canonical_json(
            dict(
                purpose=purpose,
                scope=dict(scope),
                stable_caller=stable_caller,
                value=value,
            )
        ).encode("utf-8")
        return hmac.new(self._key, message, hashlib.sha256).hexdigest()


def has_keyed_state(db) -> bool:
    return (
        db.execute(
            "SELECT 1 FROM action_receipts WHERE erased=1 UNION ALL "
            "SELECT 1 FROM feedback_event_ids WHERE erased=1 UNION ALL "
            "SELECT 1 FROM feedback_audits WHERE "
            "json_extract(audit_json,'$.idempotency_digest') IS NOT NULL LIMIT 1"
        ).fetchone()
        is not None
    )


@contextmanager
def protected_erasure_digester(store):
    """Keep a currently checked file/parent lease through one SQL operation."""
    if store._in_memory:
        raise PrivateFileError("in-memory erasure requires an injected provider")
    store_path = resolve_private_path(Path(store._path))
    path = store_path.with_name(store_path.name + ".erasure-key")
    with store.sql_transaction() as db:
        bound = db.execute("SELECT binding FROM erasure_key_binding WHERE singleton=1").fetchone()
        if bound is None and has_keyed_state(db):
            raise PrivateFileError("missing erasure key binding")
        remembered = getattr(store, "_erasure_key_identity", None)
        with ExitStack() as stack:
            try:
                lease = stack.enter_context(open_private_file(path, maximum=32))
            except FileNotFoundError:
                if bound is not None or remembered is not None:
                    raise PrivateFileError(f"missing erasure key: {path}") from None
                create_private_file_exclusive(path, secrets.token_bytes(32))
                lease = stack.enter_context(open_private_file(path, maximum=32))
            if len(lease.data) != 32:
                raise PrivateFileError(f"invalid erasure key file: {path}")
            digester = HmacErasureDigester(lease.data)
            if bound is not None and not hmac.compare_digest(bound[0], digester.binding):
                raise PrivateFileError(f"erasure key changed: {path}")
            identity = (lease.identity, digester.binding)
            if remembered is not None and remembered != identity:
                raise PrivateFileError(f"erasure key replaced: {path}")
            store._erasure_key_identity = identity
            yield digester
            lease.verify()
