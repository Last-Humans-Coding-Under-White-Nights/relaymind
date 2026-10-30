"""SQLite run store (spec section 5; RM17, RM22, RM25).

The store persists runs, ordered trace events, tool operations, pending
approvals, and artifact references. It knows nothing about routing or
which state changes are allowed: the orchestrator's state machine owns
that and calls set_state, which is a compare-and-set on the run state
version (RM17).

The one active run of the baseline (section 11) is a holds_slot flag on
the run row. set_state sets or clears it in the same transaction as the
state change, and a unique index admits one holder, so every store and
process on the same file shares one slot; a second holder gets 429.
After a crash the slot stays with the unfinished run until restart
recovery fails it with interrupted (section 5).

A tool operation is recorded as pending, with its canonical arguments,
before its side effect, and is finished once with a section 8 outcome
(RM10). The store never stores a plaintext approval token; an approval
lives for the configured TTL (five minutes, RM11) and is consumed at
most once, atomically, and only while fresh, bound to its run, tool,
arguments, target, and the run's current state version. Every event is
validated against the TraceEvent schema before it is written (section
8), and run.accepted is always a run's first event (RM22).

An idempotency key (spec 0.18 section 7.4) is stored with the run it
creates, in the same transaction, and belongs to a caller. It leaves
with its run: retention and explicit deletion remove it, and a key whose
run is past the retention window is unknown even before a purge.

Retention defaults to seven days, with explicit deletion. Redaction of
configured secret fields happens before data reaches the store (RM12);
local storage is not claimed to be encrypted.
"""

import hashlib
import json
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import median
from types import TracebackType
from typing import Any, Self

from relaymind.canonical import canonical_json
from relaymind.store.model_timings import (
    ModelTiming,
    SkillOutcomeEstimate,
    TimingEstimate,
    timing_estimate,
)
from relaymind.store.outcomes import (
    StageOutcome,
    is_local_attempt,
    project_outcome,
    project_stage_outcome,
)
from relaymind.store.trace_event import (
    SCHEMA_VERSION,
    EventType,
    TraceEvent,
    validate_trace_event,
)

DEFAULT_MAX_INLINE_RESULT_BYTES = 16 * 1024

DEFAULT_RETENTION = timedelta(days=7)
# RM11: confirmation expires after five minutes.
DEFAULT_APPROVAL_TTL = timedelta(minutes=5)

# The RM10 lifecycle of a tool operation: pending before the side effect,
# then one section 8 ToolResult status.
PENDING = "pending"
TOOL_OUTCOMES: tuple[str, ...] = ("succeeded", "failed", "unknown_outcome")

# Versioned migrations: MIGRATIONS[i] moves the schema from version i to
# i + 1. The applied version is kept in PRAGMA user_version.
MIGRATIONS: tuple[str, ...] = (
    """
    CREATE TABLE runs (
        run_id TEXT PRIMARY KEY,
        state TEXT NOT NULL,
        version INTEGER NOT NULL,
        holds_slot INTEGER NOT NULL DEFAULT 0 CHECK (holds_slot IN (0, 1)),
        previous_run_id TEXT REFERENCES runs(run_id) ON DELETE SET NULL,
        resume_state TEXT,
        error_code TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE events (
        run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
        seq INTEGER NOT NULL,
        schema_version INTEGER NOT NULL,
        timestamp TEXT NOT NULL,
        elapsed_ms INTEGER NOT NULL,
        event_type TEXT NOT NULL,
        actor TEXT NOT NULL,
        payload TEXT NOT NULL,
        PRIMARY KEY (run_id, seq)
    );
    CREATE TABLE tool_operations (
        operation_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
        proposal_id TEXT,
        tool_name TEXT NOT NULL,
        model TEXT NOT NULL,
        arguments TEXT NOT NULL,
        arguments_sha256 TEXT NOT NULL,
        status TEXT NOT NULL
            CHECK (status IN ('pending', 'succeeded', 'failed', 'unknown_outcome')),
        result TEXT,
        created_at TEXT NOT NULL,
        finished_at TEXT,
        CHECK (
            (status = 'pending' AND result IS NULL AND finished_at IS NULL)
            OR (status <> 'pending' AND result IS NOT NULL AND finished_at IS NOT NULL)
        )
    );
    CREATE TABLE approvals (
        approval_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
        token_hash TEXT NOT NULL,
        tool_name TEXT NOT NULL,
        arguments TEXT NOT NULL,
        target TEXT NOT NULL,
        state_version INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        consumed_at TEXT
    );
    CREATE TABLE artifact_refs (
        artifact_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
        uri TEXT NOT NULL,
        size_bytes INTEGER NOT NULL,
        truncated INTEGER NOT NULL
    );
    CREATE INDEX runs_created_at ON runs(created_at);
    CREATE UNIQUE INDEX runs_one_slot_holder ON runs(holds_slot) WHERE holds_slot = 1;
    CREATE INDEX tool_operations_run_id ON tool_operations(run_id);
    CREATE INDEX approvals_run_id ON approvals(run_id);
    CREATE INDEX artifact_refs_run_id ON artifact_refs(run_id);
    """,
    """
    CREATE TABLE state_intervals (
        run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
        version INTEGER NOT NULL,
        state TEXT NOT NULL,
        elapsed_ms INTEGER NOT NULL,
        PRIMARY KEY (run_id, version)
    );
    INSERT INTO state_intervals (run_id, version, state, elapsed_ms)
        SELECT run_id, version, state,
            COALESCE((SELECT MAX(elapsed_ms) FROM events WHERE events.run_id = runs.run_id), 0)
        FROM runs;
    CREATE TABLE artifact_contents (
        artifact_id TEXT PRIMARY KEY REFERENCES artifact_refs(artifact_id) ON DELETE CASCADE,
        content TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE idempotency_keys (
        caller TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        request_hash TEXT NOT NULL,
        run_id TEXT NOT NULL UNIQUE REFERENCES runs(run_id) ON DELETE CASCADE,
        PRIMARY KEY (caller, idempotency_key)
    );
    """,
    """
    CREATE TABLE model_timings (
        sample_id INTEGER PRIMARY KEY,
        model TEXT NOT NULL,
        actor TEXT NOT NULL CHECK (actor IN ('local', 'external')),
        purpose TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('run', 'warm_up', 'probe')),
        input_tokens INTEGER,
        cached_tokens INTEGER,
        first_token_ms INTEGER,
        duration_ms INTEGER NOT NULL,
        output_tokens INTEGER,
        succeeded INTEGER NOT NULL CHECK (succeeded IN (0, 1)),
        run_id TEXT REFERENCES runs(run_id) ON DELETE CASCADE,
        recorded_at TEXT NOT NULL
    );
    CREATE INDEX model_timings_lookup
        ON model_timings(model, purpose, recorded_at, sample_id);
    CREATE INDEX model_timings_run_id ON model_timings(run_id);
    CREATE INDEX model_timings_recorded_at ON model_timings(recorded_at);
    CREATE TABLE model_run_progress (
        run_id TEXT PRIMARY KEY REFERENCES runs(run_id) ON DELETE CASCADE,
        local_model TEXT,
        skill_id TEXT,
        execution_mode TEXT,
        local_attempt INTEGER NOT NULL DEFAULT 0,
        handoff INTEGER NOT NULL DEFAULT 0,
        external_model TEXT,
        external_generations INTEGER NOT NULL DEFAULT 0,
        terminal INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE skill_outcomes (
        run_id TEXT PRIMARY KEY REFERENCES runs(run_id) ON DELETE CASCADE,
        local_model TEXT,
        skill_id TEXT NOT NULL,
        local_completed INTEGER NOT NULL,
        local_escalated INTEGER NOT NULL,
        external_model TEXT,
        external_generations INTEGER NOT NULL
    );
    CREATE INDEX skill_outcomes_local ON skill_outcomes(local_model, skill_id);
    CREATE INDEX skill_outcomes_external ON skill_outcomes(external_model, skill_id);
    """,
    """
    CREATE TABLE idle_probe (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        token TEXT UNIQUE,
        owner_instance TEXT,
        started_at TEXT,
        cancel_requested INTEGER NOT NULL DEFAULT 0,
        last_attempt_at TEXT
    );
    INSERT INTO idle_probe(singleton) VALUES (1);
    """,
    """
    CREATE TABLE imported_prior (
        singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
        bundle_digest TEXT NOT NULL,
        configuration_digest TEXT NOT NULL,
        report TEXT NOT NULL
    );
    CREATE TABLE imported_outcomes (
        source_digest TEXT NOT NULL,
        source_run_id TEXT NOT NULL,
        recorded_at TEXT NOT NULL,
        local_model TEXT,
        skill_id TEXT NOT NULL,
        local_completed INTEGER NOT NULL,
        local_escalated INTEGER NOT NULL,
        external_model TEXT,
        external_generations INTEGER NOT NULL,
        projector_version INTEGER NOT NULL,
        PRIMARY KEY(source_digest, source_run_id)
    );
    """,
    # Spec 0.48 section 14.4.7 (RM26): one outcome per local stage, projected from
    # the event that ends it; runs before this migration record none.
    """
    ALTER TABLE model_run_progress ADD COLUMN local_generation INTEGER NOT NULL DEFAULT 0;
    CREATE TABLE local_stage_outcomes (
        run_id TEXT PRIMARY KEY REFERENCES runs(run_id) ON DELETE CASCADE,
        local_model TEXT NOT NULL,
        ran_out_of_time INTEGER NOT NULL CHECK (ran_out_of_time IN (0, 1)),
        ended_at TEXT NOT NULL
    );
    CREATE INDEX local_stage_outcomes_model ON local_stage_outcomes(local_model, ended_at);
    """,
    # RM34/AC25: independent catalog/job storage; existing run/event tables are unchanged.
    """
    CREATE TABLE asset_catalog_meta (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
        revision INTEGER NOT NULL CHECK(revision>=0),
        published_at TEXT NOT NULL
    );
    INSERT INTO asset_catalog_meta VALUES(1, 0, '1970-01-01T00:00:00.000000Z');
    CREATE TABLE asset_catalog_sources (
        source_id TEXT PRIMARY KEY,
        source_version TEXT,
        content_hash TEXT,
        captured_at TEXT,
        last_success_at TEXT,
        stale INTEGER NOT NULL DEFAULT 1,
        complete INTEGER NOT NULL DEFAULT 0,
        accepted_count INTEGER NOT NULL DEFAULT 0,
        rejected_count INTEGER NOT NULL DEFAULT 0,
        exclusions_json TEXT NOT NULL DEFAULT '[]',
        config_digest TEXT,
        consecutive_failures INTEGER NOT NULL DEFAULT 0,
        next_attempt_at TEXT
    );
    CREATE TABLE asset_catalog_assets (
        asset_id TEXT PRIMARY KEY,
        source_id TEXT NOT NULL REFERENCES asset_catalog_sources(source_id),
        record_json TEXT NOT NULL,
        search_json TEXT NOT NULL,
        projection_digest TEXT NOT NULL,
        relative_package_path TEXT,
        stale INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX asset_catalog_assets_source ON asset_catalog_assets(source_id);
    CREATE TABLE asset_catalog_provenance (
        source_id TEXT NOT NULL REFERENCES asset_catalog_sources(source_id),
        relative_path TEXT NOT NULL,
        sha256 TEXT NOT NULL,
        size INTEGER NOT NULL,
        PRIMARY KEY(source_id, relative_path)
    );
    CREATE TABLE asset_catalog_tombstones (
        asset_id TEXT PRIMARY KEY,
        source_id TEXT NOT NULL REFERENCES asset_catalog_sources(source_id),
        removed_at TEXT NOT NULL
    );
    CREATE TABLE asset_catalog_jobs (
        job_id TEXT PRIMARY KEY,
        -- NULL marks a derived-only rebuild (spec 8.1 IndexJobEventV1).
        source_id TEXT REFERENCES asset_catalog_sources(source_id),
        config_digest TEXT NOT NULL,
        state TEXT NOT NULL CHECK(
            state IN ('running','succeeded','failed','cancelled','interrupted')
        ),
        started_at TEXT NOT NULL,
        finished_at TEXT,
        reason TEXT,
        duration_ms REAL,
        received_bytes INTEGER NOT NULL DEFAULT 0,
        batch_ms_json TEXT NOT NULL DEFAULT '[]',
        overlap_observed INTEGER NOT NULL DEFAULT 0,
        drain_ms REAL
    );
    CREATE UNIQUE INDEX asset_catalog_running_source ON asset_catalog_jobs(source_id)
        WHERE state='running';
    """,
    """
    CREATE TABLE recommendation_scope (
        user_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
        feedback_revision INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(user_id, workspace_id)
    );
    CREATE TABLE recommendations (
        recommendation_id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
        state TEXT NOT NULL CHECK(
            state IN ('computing','ready','failed','cancelled','expired','chosen')
        ),
        state_version INTEGER NOT NULL,
        created_at TEXT NOT NULL, retain_until TEXT NOT NULL,
        view_json TEXT NOT NULL, accepted_json TEXT NOT NULL,
        active_ms REAL NOT NULL DEFAULT 0
    );
    CREATE TABLE recommendation_events (
        recommendation_id TEXT NOT NULL REFERENCES recommendations ON DELETE CASCADE,
        seq INTEGER NOT NULL, event_json TEXT NOT NULL,
        PRIMARY KEY(recommendation_id, seq)
    );
    CREATE TABLE recommendation_claims (
        namespace TEXT NOT NULL, key_digest TEXT NOT NULL,
        request_digest TEXT NOT NULL,
        recommendation_id TEXT NOT NULL REFERENCES recommendations ON DELETE CASCADE,
        status INTEGER, body_json TEXT,
        PRIMARY KEY(namespace, key_digest)
    );
    CREATE TABLE recommendation_slot (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
        recommendation_id TEXT UNIQUE REFERENCES recommendations ON DELETE CASCADE
    );
    INSERT INTO recommendation_slot VALUES(1,NULL);
    CREATE INDEX recommendation_retention ON recommendations(retain_until);
    CREATE TABLE asset_index_job_events (
        job_id TEXT NOT NULL REFERENCES asset_catalog_jobs ON DELETE CASCADE,
        seq INTEGER NOT NULL, event_json TEXT NOT NULL,
        PRIMARY KEY(job_id, seq)
    );
    """,
)


class RunStoreError(RuntimeError):
    """The run store cannot complete the operation."""


class RunNotFoundError(RunStoreError):
    """No run with that ID (section 11: 404)."""

    http_status = 404


class SlotUnavailableError(RunStoreError):
    """Another run holds the active execution slot (section 11: 429)."""

    http_status = 429

    def __init__(self, message: str, *, probe: bool = False) -> None:
        super().__init__(message)
        self.probe: bool = probe


class IdempotencyKeyTakenError(RunStoreError):
    """The caller's idempotency key already belongs to a run (spec 0.18 section 7.4).

    The unique key is the last guard against two requests creating work
    under one key; the API normally answers a stored key before it gets
    here. The run being created is not stored.
    """

    http_status = 409


class StaleVersionError(RunStoreError):
    """The expected run state version is not current (RM17: 409)."""

    http_status = 409

    def __init__(self, run_id: str, expected_version: int, current_version: int) -> None:
        super().__init__(f"run {run_id} is at version {current_version}, not {expected_version}")
        self.run_id = run_id
        self.expected_version = expected_version
        self.current_version = current_version


class TraceClock:
    """UTC wall clock for timestamps, monotonic clock for durations (RM25)."""

    def utc_now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()


@dataclass(frozen=True)
class IssuedApproval:
    """A new pending approval: its ID and when it expires (RM11)."""

    approval_id: str
    expires_at: datetime


@dataclass(frozen=True)
class IdempotencyClaim:
    """The key a run is created under: its caller, the key, and the request hash."""

    caller: str
    key: str
    request_hash: str


@dataclass(frozen=True)
class IdempotencyRecord:
    """What a stored key holds: the request hash and the run it created."""

    request_hash: str
    run_id: str


@dataclass(frozen=True)
class RunRecord:
    """The persisted state of one run."""

    run_id: str
    state: str
    version: int
    previous_run_id: str | None
    resume_state: str | None
    error_code: str | None
    created_at: datetime
    updated_at: datetime


EventDraft = tuple[EventType, str, Mapping[str, Any]]


@dataclass(frozen=True)
class ToolResultView:
    """The result of a tool.finished event, inline or read back by reference.

    result is None when an offloaded result is not retained (spec 0.30
    sections 8 and 9); result_ref is None for an inline result.
    """

    result: Mapping[str, Any] | None
    result_ref: Mapping[str, Any] | None
    retained: bool


def read_tool_result(
    payload: Mapping[str, Any], read_artifact: Callable[[str], Any | None] | None = None
) -> ToolResultView:
    """The result of a tool.finished payload, stored or exported (RM22).

    read_artifact reads a result stored by reference. Without it (an
    exported trace has no store), or once the artifact is gone, the
    result is not retained and only its reference is known.
    """
    ref = payload.get("result_ref")
    result = payload.get("result")
    if result is None and ref is not None and read_artifact is not None:
        result = read_artifact(ref["artifact_id"])
    return ToolResultView(result, ref, result is not None)


def utc_iso(moment: datetime) -> str:
    """The store's UTC timestamp text; naive or non-UTC times are refused (RM25).

    Events that name a stored time, such as the expires_at of
    confirmation.required, use this form too (section 8).
    """
    if moment.tzinfo is None or moment.utcoffset() != timedelta(0):
        raise ValueError(f"store times must be timezone-aware UTC, not {moment!r}")
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


_iso = utc_iso


def _parse(text: str) -> datetime:
    return datetime.fromisoformat(text)


def _run_record(row: sqlite3.Row) -> RunRecord:
    return RunRecord(
        run_id=row["run_id"],
        state=row["state"],
        version=row["version"],
        previous_run_id=row["previous_run_id"],
        resume_state=row["resume_state"],
        error_code=row["error_code"],
        created_at=_parse(row["created_at"]),
        updated_at=_parse(row["updated_at"]),
    )


_TIMING_SELECT = (
    "SELECT model, actor, purpose, kind, input_tokens, cached_tokens, first_token_ms,"
    " duration_ms, output_tokens, succeeded, run_id, recorded_at FROM model_timings"
)


def _model_timing(row: sqlite3.Row) -> ModelTiming:
    return ModelTiming(
        **{
            key: value
            for key, value in dict(row).items()
            if key not in ("succeeded", "recorded_at")
        },
        succeeded=bool(row["succeeded"]),
        recorded_at=_parse(row["recorded_at"]),
    )


def _tool_operation_record(row: sqlite3.Row) -> dict[str, Any]:
    record = dict(row)
    record["arguments"] = json.loads(row["arguments"])
    record["result"] = None if row["result"] is None else json.loads(row["result"])
    record["created_at"] = _parse(row["created_at"])
    record["finished_at"] = None if row["finished_at"] is None else _parse(row["finished_at"])
    return record


def _artifact_ref_record(row: sqlite3.Row) -> dict[str, Any]:
    record = dict(row)
    record["truncated"] = bool(row["truncated"])
    return record


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def split_sql_statements(script: str) -> list[str]:
    """Split a migration script into whole statements.

    A ';' ends a statement only where sqlite3.complete_statement agrees,
    so semicolons inside string literals, comments, and trigger bodies
    stay inside their statement. Trailing text that is not a whole
    statement is refused rather than silently dropped.
    """
    statements: list[str] = []
    buffer = ""
    pieces = script.split(";")
    for index, piece in enumerate(pieces):
        buffer += piece
        if index == len(pieces) - 1:
            break
        buffer += ";"
        if sqlite3.complete_statement(buffer):
            statement = buffer.strip()
            if statement != ";":
                statements.append(statement)
            buffer = ""
    if buffer.strip() and not _only_comments(buffer):
        raise RunStoreError(f"incomplete migration statement: {buffer.strip()[:60]!r}")
    return statements


def _only_comments(text: str) -> bool:
    return all(not line.strip() or line.strip().startswith("--") for line in text.splitlines())


class RunStore:
    """Runs, events, tool operations, approvals, and artifact refs in SQLite."""

    def __init__(
        self,
        path: str | Path = ":memory:",
        *,
        clock: TraceClock | None = None,
        retention: timedelta = DEFAULT_RETENTION,
        approval_ttl: timedelta = DEFAULT_APPROVAL_TTL,
        max_inline_result_bytes: int = DEFAULT_MAX_INLINE_RESULT_BYTES,
    ) -> None:
        if approval_ttl <= timedelta(0):
            raise ValueError("approval_ttl must be positive (RM11)")
        if type(max_inline_result_bytes) is not int or max_inline_result_bytes < 1:
            raise ValueError("max_inline_result_bytes must be a positive integer")
        self.max_inline_result_bytes = max_inline_result_bytes
        self.clock = clock or TraceClock()
        self.retention = retention
        self.approval_ttl = approval_ttl
        # Per run this store writes events for: the monotonic reading at
        # its start here, and the run's elapsed_ms at that moment (RM25).
        self._run_started: dict[str, tuple[float, int]] = {}
        # One connection is shared by every thread using this store; the
        # lock keeps each transaction and read whole (RM17: a racing
        # writer gets 409, never a nested-transaction error).
        self._lock = threading.RLock()
        # Nesting depth of the open write transaction; see transaction().
        self._depth = 0
        # Runs whose baseline the open write transaction started. A rollback
        # drops them, so a refused run leaves nothing behind in this process.
        self._transaction_clocks: set[str] = set()
        self._path = str(path)
        self._in_memory = self._path in (":memory:", "")
        self._closed = False
        self._conn = self._connect()
        try:
            self._migrate()
        except BaseException:
            self.close()
            raise

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self._path, isolation_level=None, timeout=10.0, check_same_thread=False
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        if not self._in_memory:
            # Readers do not block the writer; NORMAL sync is safe in WAL.
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    # -- lifecycle -------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._closed = True
                self._conn.close()

    def _check_open(self) -> None:
        """A closed store refuses every operation."""
        if self._closed:
            raise RunStoreError("the run store is closed")

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    @property
    def schema_version(self) -> int:
        with self._lock:
            return self._conn.execute("PRAGMA user_version").fetchone()[0]

    def _migrate(self) -> None:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            current = self.schema_version
            if current > len(MIGRATIONS):
                raise RunStoreError(
                    f"store schema version {current} is newer than this code ({len(MIGRATIONS)})"
                )
            for version in range(current, len(MIGRATIONS)):
                for statement in split_sql_statements(MIGRATIONS[version]):
                    self._conn.execute(statement)
                self._conn.execute(f"PRAGMA user_version = {version + 1}")
            self._conn.execute("COMMIT")
        except BaseException:
            self._rollback()
            raise

    def _rollback(self) -> None:
        """Roll back after a failure; a ROLLBACK error never masks the original one.

        If ROLLBACK itself fails and the transaction is still open, the
        connection is closed, which discards the transaction, and a file
        store reconnects. An in-memory store cannot reconnect to its
        data, so it stays closed and refuses later use.
        """
        try:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
        except sqlite3.Error:
            if self._conn.in_transaction:
                self._reset_connection()

    def _reset_connection(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:
            pass
        if self._in_memory:
            self._closed = True
            return
        try:
            self._conn = self._connect()
        except sqlite3.Error:
            self._closed = True

    @contextmanager
    def _write(self) -> Iterator[None]:
        """BEGIN IMMEDIATE ... COMMIT, or ROLLBACK on any error, under the store lock.

        A failed COMMIT rolls back too, so the connection never stays
        inside an open transaction that would break every later write.
        """
        with self._lock:
            self._check_open()
            if self._depth:
                # Inside transaction(): the outermost write commits or
                # rolls back everything, so an inner one only joins it.
                self._depth += 1
                try:
                    yield
                finally:
                    self._depth -= 1
                return
            self._conn.execute("BEGIN IMMEDIATE")
            self._depth = 1
            try:
                yield
                self._conn.execute("COMMIT")
            except BaseException as exc:
                self._rollback()
                for run_id in self._transaction_clocks:
                    self._run_started.pop(run_id, None)
                if isinstance(exc, SlotUnavailableError) and exc.probe:
                    # The admission transaction (including an approval) rolled back.
                    # Persist cancellation separately, while holding the connection lock.
                    self._conn.execute("BEGIN IMMEDIATE")
                    try:
                        self._conn.execute(
                            "UPDATE idle_probe SET cancel_requested=1 WHERE token IS NOT NULL"
                        )
                        self._conn.execute("COMMIT")
                    except BaseException:
                        self._rollback()
                        raise
                raise
            finally:
                self._depth = 0
                self._transaction_clocks.clear()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Run several store writes as one transaction (RM11, RM17).

        Every write inside it joins one BEGIN IMMEDIATE ... COMMIT, under
        the store lock; an exception that leaves the block rolls all of
        them back. Consuming an approval and moving the run use it, so an
        approval is never spent on a resume or cancel that did not happen.
        """
        with self._write():
            yield

    @contextmanager
    def sql_transaction(self) -> Iterator[sqlite3.Connection]:
        """Storage facades share this connection, lock and rollback boundary (RM17).

        This internal storage boundary does not reserve the execution/probe slot.
        Existing transaction() callers continue to receive None.
        """
        with self._write():
            yield self._conn

    # -- idle maintenance slot (RM17, RM27) ---------------------------------

    def probe_reservation(self) -> dict[str, Any] | None:
        """Read the current owner; opening a store never reclaims its reservation."""
        with self._lock:
            self._check_open()
            row = self._conn.execute("SELECT * FROM idle_probe WHERE token IS NOT NULL").fetchone()
            return None if row is None else dict(row)

    def try_reserve_probe(
        self, token: str, owner_instance: str, *, model: str, after_seconds: float
    ) -> bool:
        """Atomically reserve only stale, idle work, with persisted attempt cooldown."""
        # This optimistic read only rejects work; it never grants a reservation.
        if not self._probe_eligible(model=model, after_seconds=after_seconds):
            return False
        with self._write():
            # Another connection may have admitted a run, reserved a probe, or
            # refreshed timings/cooldown since the read. Recheck all conditions.
            if not self._probe_eligible(model=model, after_seconds=after_seconds):
                return False
            self._conn.execute(
                "UPDATE idle_probe SET token=?, owner_instance=?, started_at=?,"
                " cancel_requested=0 WHERE singleton=1",
                (token, owner_instance, _iso(self.clock.utc_now())),
            )
            return True

    def _probe_eligible(self, *, model: str, after_seconds: float) -> bool:
        with self._lock:
            self._check_open()
            row = self._conn.execute("SELECT * FROM idle_probe").fetchone()
            if row["token"] is not None or self.active_run_id() is not None:
                return False
            now = self.clock.utc_now()
            if row["last_attempt_at"] is not None:
                last = datetime.fromisoformat(row["last_attempt_at"].replace("Z", "+00:00"))
                if (now - last).total_seconds() < after_seconds:
                    return False
            latest = self.latest_model_timing(model=model, purpose="routing", actor="local")
            return latest is None or (now - latest.recorded_at).total_seconds() > after_seconds

    def probe_wait_seconds(self, *, model: str, after_seconds: float) -> float:
        """Seconds until cooldown and estimate age allow an idle probe; 0 when they do.

        Only the timed conditions are counted. An active run or another
        probe's reservation has no known end, so a scheduler polls for it.
        """
        with self._lock:
            self._check_open()
            row = self._conn.execute("SELECT last_attempt_at FROM idle_probe").fetchone()
            now = self.clock.utc_now()
            wait = 0.0
            if row["last_attempt_at"] is not None:
                last = datetime.fromisoformat(row["last_attempt_at"].replace("Z", "+00:00"))
                wait = max(wait, after_seconds - (now - last).total_seconds())
            latest = self.latest_model_timing(model=model, purpose="routing", actor="local")
            if latest is not None:
                wait = max(wait, after_seconds - (now - latest.recorded_at).total_seconds())
            return wait

    def finish_probe(self, token: str, owner_instance: str) -> bool:
        """Release only after the matching owner's generation task has actually ended."""
        with self._write():
            changed = self._conn.execute(
                "UPDATE idle_probe SET token=NULL, owner_instance=NULL, started_at=NULL,"
                " cancel_requested=0, last_attempt_at=? WHERE token=? AND owner_instance=?",
                (_iso(self.clock.utc_now()), token, owner_instance),
            )
            return changed.rowcount == 1

    def recover_probe_after_restart(self) -> None:
        """Startup only: the previous process must have ended (section 14.2.6)."""
        with self._write():
            self._conn.execute(
                "UPDATE idle_probe SET token=NULL, owner_instance=NULL,"
                " started_at=NULL, cancel_requested=0, last_attempt_at=?"
                " WHERE token IS NOT NULL",
                (_iso(self.clock.utc_now()),),
            )

    # -- runs ------------------------------------------------------------

    def create_run(
        self,
        initial_state: str,
        *,
        previous_run_id: str | None = None,
        accepted: EventDraft | None = None,
        idempotency: IdempotencyClaim | None = None,
    ) -> RunRecord:
        """Create a run at version 1. A retry links to its previous run (RM17).

        idempotency, when given, stores the caller's key with the run in
        the same transaction (spec 0.18 section 7.4). A key that is
        already stored raises IdempotencyKeyTakenError and no run is
        created. A key whose run is past retention is released first,
        as a purge would.

        accepted, when given, is the run.accepted event (RM22), written as
        seq 1 in the same transaction as the run row. The run can then
        fail or be cancelled straight from accepted (section 5), because
        its terminal event is never seq 1. An invalid event creates no
        run, so no run is left without its run.accepted event.
        """
        run_id = f"run_{uuid.uuid4().hex}"
        now = _iso(self.clock.utc_now())
        with self._write():
            if previous_run_id is not None:
                self._require_run(previous_run_id)
            self._conn.execute(
                "INSERT INTO runs (run_id, state, version, previous_run_id, created_at,"
                " updated_at) VALUES (?, ?, 1, ?, ?, ?)",
                (run_id, initial_state, previous_run_id, now, now),
            )
            self._conn.execute(
                "INSERT INTO state_intervals (run_id, version, state, elapsed_ms)"
                " VALUES (?, 1, ?, 0)",
                (run_id, initial_state),
            )
            if idempotency is not None:
                self._conn.execute(
                    "DELETE FROM idempotency_keys WHERE caller = ? AND idempotency_key = ?"
                    " AND run_id IN (SELECT run_id FROM runs WHERE created_at <= ?)",
                    (idempotency.caller, idempotency.key, self._retention_cutoff()),
                )
                try:
                    self._conn.execute(
                        "INSERT INTO idempotency_keys (caller, idempotency_key, request_hash,"
                        " run_id) VALUES (?, ?, ?, ?)",
                        (idempotency.caller, idempotency.key, idempotency.request_hash, run_id),
                    )
                except sqlite3.IntegrityError:
                    raise IdempotencyKeyTakenError(
                        "this idempotency key already belongs to a run"
                    ) from None
            if accepted is not None:
                # RM25: the run clock starts with the run, before seq 1.
                self.start_run_clock(run_id)
                self._insert_events(run_id, [accepted])
        self.start_run_clock(run_id)
        return RunRecord(
            run_id=run_id,
            state=initial_state,
            version=1,
            previous_run_id=previous_run_id,
            resume_state=None,
            error_code=None,
            created_at=_parse(now),
            updated_at=_parse(now),
        )

    def find_idempotency_key(self, caller: str, key: str) -> IdempotencyRecord | None:
        """The stored key of a caller and the run it created, or None (spec 0.18 section 7.4).

        A key is valid for the retention window of its run (section 5):
        once the run is past it, the key is unknown, purged or not.
        """
        with self._lock:
            self._check_open()
            row = self._conn.execute(
                "SELECT k.request_hash, k.run_id FROM idempotency_keys AS k"
                " JOIN runs AS r ON r.run_id = k.run_id"
                " WHERE k.caller = ? AND k.idempotency_key = ? AND r.created_at > ?",
                (caller, key, self._retention_cutoff()),
            ).fetchone()
        if row is None:
            return None
        return IdempotencyRecord(row["request_hash"], row["run_id"])

    def start_run_clock(self, run_id: str) -> None:
        """Start measuring a run's elapsed_ms with this store's monotonic clock (RM25).

        The baseline continues from the run's last persisted elapsed_ms,
        so a store joining a run (another process, or a reopened file)
        never writes a smaller elapsed_ms. The time between the last
        event and this call is not counted: only the wall clock could
        measure it across processes. A run this store already measures
        keeps its baseline.
        """
        with self._lock:
            if run_id in self._run_started:
                return
            self._check_open()
            (offset,) = self._conn.execute(
                "SELECT MAX(elapsed_ms) FROM ("
                " SELECT COALESCE(MAX(elapsed_ms), 0) AS elapsed_ms FROM events WHERE run_id = ?"
                " UNION ALL SELECT COALESCE(MAX(elapsed_ms), 0) FROM state_intervals"
                " WHERE run_id = ?)",
                (run_id, run_id),
            ).fetchone()
            self._run_started[run_id] = (self.clock.monotonic(), offset)
            if self._depth:
                self._transaction_clocks.add(run_id)

    def elapsed_ms(self, run_id: str) -> int:
        """Measured elapsed time, never inferred from wall time (RM25)."""
        with self._lock:
            self._check_open()
            baseline = self._run_started.get(run_id)
            if baseline is None:
                raise RunStoreError(f"run {run_id} has no monotonic baseline in this process")
            started, offset = baseline
            (last,) = self._conn.execute(
                "SELECT MAX(elapsed_ms) FROM ("
                " SELECT COALESCE(MAX(elapsed_ms), 0) AS elapsed_ms FROM events WHERE run_id = ?"
                " UNION ALL SELECT COALESCE(MAX(elapsed_ms), 0) FROM state_intervals"
                " WHERE run_id = ?)",
                (run_id, run_id),
            ).fetchone()
            return max(last, offset + round((self.clock.monotonic() - started) * 1000))

    def state_timings(self, run_id: str) -> dict[str, int]:
        """Sum measured state intervals, excluding waits from active time (RM25).

        A reopened store continues from its recorded baseline; downtime
        and pre-migration intervals are not reconstructed from UTC.
        """
        with self._lock:
            self._check_open()
            self._require_run(run_id)
            self.start_run_clock(run_id)
            intervals = self._conn.execute(
                "SELECT state, elapsed_ms FROM state_intervals WHERE run_id = ? ORDER BY version",
                (run_id,),
            ).fetchall()
            end = self.elapsed_ms(run_id)
            totals = {"active_ms": 0, "waiting_ms": 0}
            for index, interval in enumerate(intervals):
                state, start = interval
                stop = intervals[index + 1]["elapsed_ms"] if index + 1 < len(intervals) else end
                if state in ("completed", "failed", "cancelled"):
                    continue
                key = (
                    "waiting_ms"
                    if state in ("waiting_input", "waiting_confirmation")
                    else "active_ms"
                )
                totals[key] += max(0, stop - start)
            return totals

    def get_run(self, run_id: str) -> RunRecord:
        with self._lock:
            self._check_open()
            row = self._conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise RunNotFoundError(f"no run {run_id}")
        return _run_record(row)

    def unfinished_runs(self, terminal_states: Collection[str]) -> list[RunRecord]:
        """Runs whose state is not terminal, oldest first (section 5 restart recovery).

        The state machine owns which states are terminal, so the caller
        names them.
        """
        marks = ", ".join("?" for _ in terminal_states)
        with self._lock:
            self._check_open()
            rows = self._conn.execute(
                f"SELECT * FROM runs WHERE state NOT IN ({marks}) ORDER BY created_at, rowid",
                tuple(terminal_states),
            ).fetchall()
        return [_run_record(row) for row in rows]

    def active_run_id(self) -> str | None:
        """The run that holds the one active execution slot, if any (section 11)."""
        with self._lock:
            self._check_open()
            row = self._conn.execute("SELECT run_id FROM runs WHERE holds_slot = 1").fetchone()
        return None if row is None else row["run_id"]

    def _require_run(self, run_id: str) -> sqlite3.Row:
        row = self._conn.execute(
            "SELECT version, state FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise RunNotFoundError(f"no run {run_id}")
        return row

    def set_state(
        self,
        run_id: str,
        *,
        expected_version: int,
        from_state: str,
        to_state: str,
        resume_state: str | None = None,
        error_code: str | None = None,
        event: EventDraft | None = None,
        holds_slot: bool = False,
    ) -> RunRecord:
        """Compare-and-set the run state and bump its version (RM17).

        The change applies only if the run is still at expected_version in
        from_state; otherwise StaleVersionError (409). holds_slot says
        whether the run holds the one active execution slot in to_state
        (section 11); if another run holds it, SlotUnavailableError (429)
        and nothing changes. A stale version is reported before a busy
        slot. The optional event is appended in the same transaction, so
        all of it happens or none.
        """
        with self._write():
            rows = self._conn.execute(
                "UPDATE runs SET state = ?, version = version + 1, resume_state = ?,"
                " error_code = ?, holds_slot = ?, updated_at = ?"
                " WHERE run_id = ? AND version = ? AND state = ?"
                " AND NOT (? AND EXISTS ("
                "  SELECT 1 FROM runs AS other WHERE other.holds_slot = 1 AND other.run_id <> ?))"
                " AND NOT (? AND EXISTS (SELECT 1 FROM idle_probe WHERE token IS NOT NULL))"
                " RETURNING *",
                (
                    to_state,
                    resume_state,
                    error_code,
                    int(holds_slot),
                    _iso(self.clock.utc_now()),
                    run_id,
                    expected_version,
                    from_state,
                    int(holds_slot),
                    run_id,
                    int(holds_slot),
                ),
            ).fetchall()
            if not rows:
                # Nothing changed: the run is missing (404), not at the
                # expected version and state (409, with its version), or
                # another run holds the slot (429).
                current = self._require_run(run_id)
                if current["version"] != expected_version or current["state"] != from_state:
                    raise StaleVersionError(run_id, expected_version, current["version"])
                raise SlotUnavailableError(
                    "another active run or idle maintenance probe holds the execution slot; "
                    f"{run_id} is unchanged",
                    probe=self.probe_reservation() is not None,
                )
            self.start_run_clock(run_id)
            self._conn.execute(
                "INSERT INTO state_intervals (run_id, version, state, elapsed_ms)"
                " VALUES (?, ?, ?, ?)",
                (run_id, expected_version + 1, to_state, self.elapsed_ms(run_id)),
            )
            if event is not None:
                self._insert_events(run_id, [event])
        return _run_record(rows[0])

    def delete_run(self, run_id: str) -> None:
        """Explicitly delete one run and everything it owns (section 5)."""
        with self._lock:
            with self._write():
                self._require_run(run_id)
                self._conn.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))
            self._run_started.pop(run_id, None)

    def _retention_cutoff(self) -> str:
        """Runs created at or before this time are past retention (section 5)."""
        return _iso(self.clock.utc_now() - self.retention)

    def purge_expired(self) -> list[str]:
        """Delete runs created at or before now minus retention; return their IDs.

        The caller learns which runs are gone, so the state machine can
        release an execution slot a purged run still held.
        """
        cutoff = self._retention_cutoff()
        with self._lock:
            with self._write():
                self._conn.execute("DELETE FROM model_timings WHERE recorded_at <= ?", (cutoff,))
                self._conn.execute(
                    "DELETE FROM imported_outcomes WHERE recorded_at <= ?", (cutoff,)
                )
                rows = self._conn.execute(
                    "DELETE FROM runs WHERE created_at <= ? RETURNING run_id", (cutoff,)
                ).fetchall()
            run_ids = sorted(row["run_id"] for row in rows)
            for run_id in run_ids:
                self._run_started.pop(run_id, None)
        return run_ids

    # -- measured model calls and terminal skill outcomes (RM27) ----------

    def add_model_timing(self, sample: ModelTiming) -> None:
        """Validate and retain one sample, using the same boundary for imported data."""
        if not isinstance(sample, ModelTiming):
            raise ValueError("sample must be a ModelTiming")
        data = sample.to_json_object()
        data.pop("schema_version")
        data["succeeded"] = int(sample.succeeded)
        with self._write():
            if sample.run_id is not None:
                self._require_run(sample.run_id)
            columns = list(data)
            self._conn.execute(
                f"INSERT INTO model_timings ({', '.join(columns)})"
                f" VALUES ({', '.join(':' + c for c in columns)})",
                data,
            )
            self._conn.execute(
                "DELETE FROM model_timings WHERE recorded_at <= ?", (self._retention_cutoff(),)
            )
            # Spec section 8: actors share the model/purpose retention quota;
            # estimates below filter by actor independently of that bound.
            self._conn.execute(
                "DELETE FROM model_timings WHERE model = ? AND purpose = ? AND sample_id NOT IN"
                " (SELECT sample_id FROM model_timings WHERE model = ? AND purpose = ?"
                " ORDER BY recorded_at DESC, sample_id DESC LIMIT 1000)",
                (sample.model, sample.purpose, sample.model, sample.purpose),
            )

    def model_timings(
        self, *, model: str | None = None, purpose: str | None = None, actor: str | None = None
    ) -> list[ModelTiming]:
        """Unexpired observations, oldest first, then insertion order for timestamp ties."""
        query = _TIMING_SELECT + " WHERE recorded_at > ?"
        params: list[Any] = [self._retention_cutoff()]
        for name, value in (("model", model), ("purpose", purpose), ("actor", actor)):
            if value is not None:
                query += f" AND {name} = ?"
                params.append(value)
        with self._lock:
            self._check_open()
            rows = self._conn.execute(query + " ORDER BY recorded_at, sample_id", params).fetchall()
        return [_model_timing(row) for row in rows]

    def latest_model_timing(self, *, model: str, purpose: str, actor: str) -> ModelTiming | None:
        """The newest unexpired successful observation, read by one indexed lookup."""
        with self._lock:
            self._check_open()
            row = self._conn.execute(
                _TIMING_SELECT + " WHERE recorded_at > ? AND model = ? AND purpose = ?"
                " AND actor = ? AND succeeded = 1"
                " ORDER BY recorded_at DESC, sample_id DESC LIMIT 1",
                (self._retention_cutoff(), model, purpose, actor),
            ).fetchone()
        return None if row is None else _model_timing(row)

    def timing_estimate(self, model: str, purpose: str, *, actor: str = "local") -> TimingEstimate:
        """Read measured prediction inputs without inference or run/event-history scans."""
        return timing_estimate(
            model, purpose, actor, self.model_timings(model=model, purpose=purpose, actor=actor)
        )

    def outcome_estimate(
        self, model: str | None, skill_id: str, *, external_model: str | None = None
    ) -> SkillOutcomeEstimate:
        """Local outcomes by local model/skill; external costs by external model/skill.

        An unspecified external model has no external estimate. Both sets
        omit expired runs before physical maintenance deletes their rows.
        """
        with self._lock:
            self._check_open()
            completed, escalated = self._conn.execute(
                "SELECT COALESCE(SUM(local_completed), 0), COALESCE(SUM(local_escalated), 0)"
                " FROM skill_outcomes JOIN runs USING (run_id)"
                " WHERE local_model IS ? AND skill_id = ? AND runs.created_at > ?",
                (model, skill_id, self._retention_cutoff()),
            ).fetchone()
            external = (
                []
                if external_model is None
                else self._conn.execute(
                    "SELECT external_generations FROM skill_outcomes JOIN runs USING (run_id)"
                    " WHERE external_model = ? AND skill_id = ? AND external_generations > 0"
                    " AND runs.created_at > ? ORDER BY runs.created_at, run_id",
                    (external_model, skill_id, self._retention_cutoff()),
                ).fetchall()
            )
            imported = self._conn.execute(
                "SELECT COALESCE(SUM(local_completed),0), COALESCE(SUM(local_escalated),0)"
                " FROM imported_outcomes WHERE local_model IS ? AND skill_id = ?"
                " AND recorded_at > ?",
                (model, skill_id, self._retention_cutoff()),
            ).fetchone()
            completed += imported[0]
            escalated += imported[1]
            if external_model is not None:
                external = (
                    list(external)
                    + self._conn.execute(
                        "SELECT external_generations FROM imported_outcomes"
                        " WHERE external_model = ?"
                        " AND skill_id = ? AND external_generations > 0 AND recorded_at > ?",
                        (external_model, skill_id, self._retention_cutoff()),
                    ).fetchall()
                )
        return SkillOutcomeEstimate(
            local_completed=completed,
            local_escalated=escalated,
            local_success_rate=completed / (completed + escalated)
            if completed + escalated
            else None,
            external_completed=len(external),
            median_external_generations=float(median([r[0] for r in external]))
            if external
            else None,
        )

    def import_placement_prior(
        self,
        *,
        bundle_digest: str,
        configuration_digest: str,
        timings: list[ModelTiming],
        outcomes: list[dict[str, Any]],
        report: dict[str, Any],
    ) -> dict[str, Any]:
        """Atomically seed a fresh evaluation store; no synthetic run/event is written."""
        for digest in (bundle_digest, configuration_digest):
            if (
                not isinstance(digest, str)
                or len(digest) != 64
                or any(c not in "0123456789abcdef" for c in digest)
            ):
                raise ValueError("invalid prior digest")
        identities = set()
        fields = {
            "source_digest",
            "source_run_id",
            "recorded_at",
            "local_model",
            "skill_id",
            "local_completed",
            "local_escalated",
            "external_model",
            "external_generations",
        }
        for row in outcomes:
            if not isinstance(row, dict) or set(row) != fields:
                raise ValueError("invalid imported outcome fields")
            identity = (row["source_digest"], row["source_run_id"])
            if identity in identities:
                raise ValueError("duplicate imported outcome")
            identities.add(identity)
            for name in ("local_completed", "local_escalated", "external_generations"):
                if type(row[name]) is not int or row[name] < 0:
                    raise ValueError("invalid imported outcome count")
            if row["local_completed"] + row["local_escalated"] > 1:
                raise ValueError("conflicting local outcome")
            if row["local_completed"] and row["external_generations"]:
                raise ValueError("local completion cannot include external generations")
            for name in ("source_digest", "source_run_id", "skill_id"):
                if not isinstance(row[name], str) or not row[name].strip():
                    raise ValueError("invalid imported outcome identity")
            for name in ("local_model", "external_model"):
                if row[name] is not None and (
                    not isinstance(row[name], str) or not row[name].strip()
                ):
                    raise ValueError("invalid imported model identity")
            if row["external_generations"] and row["external_model"] is None:
                raise ValueError("external outcome requires model identity")
            timestamp = datetime.fromisoformat(row["recorded_at"])
            if timestamp.utcoffset() is None or _iso(timestamp) != row["recorded_at"]:
                raise ValueError("imported timestamp must be canonical UTC")
        if report.get("timings") != len(timings) or report.get("outcomes") != len(outcomes):
            raise ValueError("import report counts disagree")
        for sample in timings:
            sample.to_json_object()
            if sample.run_id is not None:
                raise ValueError("imported timing must be unowned")
        with self._write():
            cutoff = self._retention_cutoff()
            if any(_iso(t.recorded_at) <= cutoff for t in timings) or any(
                o["recorded_at"] <= cutoff for o in outcomes
            ):
                raise ValueError("prior retention expired; recalibrate without redating")
            previous = self._conn.execute("SELECT * FROM imported_prior").fetchone()
            if previous is not None:
                if (
                    previous["bundle_digest"] != bundle_digest
                    or previous["configuration_digest"] != configuration_digest
                ):
                    raise ValueError("conflicting immutable prior")
                if self.model_timings() != sorted(
                    timings, key=lambda t: t.recorded_at
                ) or self._conn.execute("SELECT COUNT(*) FROM imported_outcomes").fetchone()[
                    0
                ] != len(outcomes):
                    raise ValueError("prior retention or subsequent measurements changed")
                stored_outcomes = [
                    dict(row)
                    for row in self._conn.execute(
                        "SELECT source_digest,source_run_id,recorded_at,local_model,skill_id,"
                        "local_completed,local_escalated,external_model,external_generations "
                        "FROM imported_outcomes ORDER BY source_digest,source_run_id"
                    ).fetchall()
                ]
                if (
                    stored_outcomes
                    != sorted(outcomes, key=lambda r: (r["source_digest"], r["source_run_id"]))
                    or json.loads(previous["report"]) != report
                ):
                    raise ValueError("conflicting immutable prior projection")
                return json.loads(previous["report"])
            if self._conn.execute("SELECT 1 FROM runs LIMIT 1").fetchone() or self.model_timings():
                raise ValueError("prior import requires a fresh isolated store")
            for sample in timings:
                self.add_model_timing(sample)
            if len(self.model_timings()) != len(timings):
                raise ValueError("prior retention limits truncated timing rows")
            for row in outcomes:
                self._conn.execute(
                    "INSERT INTO imported_outcomes (source_digest,source_run_id,recorded_at,"
                    "local_model,skill_id,local_completed,local_escalated,external_model,"
                    "external_generations,projector_version)"
                    " VALUES (:source_digest,:source_run_id,:recorded_at,:local_model,:skill_id,"
                    ":local_completed,:local_escalated,:external_model,:external_generations,1)",
                    row,
                )
            self._conn.execute(
                "INSERT INTO imported_prior VALUES (1,?,?,?)",
                (bundle_digest, configuration_digest, json.dumps(report, sort_keys=True)),
            )
        return report

    def _record_outcome_event(self, event: TraceEvent) -> None:
        """Update bounded per-run progress inside the event transaction; finalize once.

        Old databases are not reinterpreted as measured outcomes. A run
        still active during migration can start collecting new evidence.
        """
        run_id, kind, payload = event.run_id, event.event_type, event.payload
        if kind not in (
            EventType.RUN_ACCEPTED,
            EventType.SKILL_LOADED,
            EventType.MODEL_STARTED,
            EventType.HANDOFF_STARTED,
            EventType.RUN_COMPLETED,
            EventType.RUN_FAILED,
            EventType.RUN_CANCELLED,
        ):
            return
        self._conn.execute(
            "INSERT OR IGNORE INTO model_run_progress (run_id) VALUES (?)", (run_id,)
        )
        progress = self._conn.execute(
            "SELECT * FROM model_run_progress WHERE run_id = ?", (run_id,)
        ).fetchone()
        if progress["terminal"]:
            return
        if kind is EventType.RUN_ACCEPTED:
            self._conn.execute(
                "UPDATE model_run_progress SET local_model = ?, execution_mode = ?"
                " WHERE run_id = ?",
                (payload.get("local_model"), payload["effective_execution_mode"], run_id),
            )
        elif kind is EventType.SKILL_LOADED:
            self._conn.execute(
                "UPDATE model_run_progress SET skill_id = ? WHERE run_id = ?",
                (payload["name"], run_id),
            )
        elif kind is EventType.MODEL_STARTED:
            if event.actor == "local":
                # Spec 0.38.1: a parallel workflow branch call is not a local
                # placement attempt, so its join or failure is no escalation.
                attempt = is_local_attempt(payload)
                # Spec 0.48: any local generation of the run's own, routing included,
                # makes a local stage; a parallel branch is not one.
                own = "branch" not in payload
                self._conn.execute(
                    "UPDATE model_run_progress SET local_model = COALESCE(local_model, ?),"
                    " local_attempt = MAX(local_attempt, ?),"
                    " local_generation = MAX(local_generation, ?) WHERE run_id = ?",
                    (payload["model"], int(attempt), int(own), run_id),
                )
            elif event.actor == "external":
                # A configured gateway has one external model for a run;
                # provider changes across runs retain separate outcome identities.
                self._conn.execute(
                    "UPDATE model_run_progress SET external_model = ?,"
                    " external_generations = external_generations + 1 WHERE run_id = ?",
                    (payload["model"], run_id),
                )
        elif kind is EventType.HANDOFF_STARTED:
            self._record_stage_outcome(event, progress)
            self._conn.execute(
                "UPDATE model_run_progress SET handoff = 1 WHERE run_id = ?", (run_id,)
            )
        elif kind in (EventType.RUN_COMPLETED, EventType.RUN_FAILED, EventType.RUN_CANCELLED):
            if not progress["handoff"]:
                self._record_stage_outcome(event, progress)
            self._conn.execute(
                "UPDATE model_run_progress SET terminal = 1 WHERE run_id = ?", (run_id,)
            )
            outcome = project_outcome(dict(progress), kind.value, payload)
            if outcome is None:
                return
            self._conn.execute(
                "INSERT OR IGNORE INTO skill_outcomes"
                " (run_id, local_model, skill_id, local_completed, local_escalated,"
                " external_model, external_generations) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (run_id, *outcome.values()),
            )

    def _record_stage_outcome(self, event: TraceEvent, progress: sqlite3.Row) -> None:
        """Spec 0.48 section 14.4.7: the outcome of the local stage this event ends, once."""
        ran_out = project_stage_outcome(progress, event.event_type.value, event.payload)
        if ran_out is None or progress["local_model"] is None:
            return
        self._conn.execute(
            "INSERT OR IGNORE INTO local_stage_outcomes"
            " (run_id, local_model, ran_out_of_time, ended_at) VALUES (?, ?, ?, ?)",
            (event.run_id, progress["local_model"], int(ran_out), event.timestamp),
        )

    def latest_local_stage_outcomes(self, model: str, *, limit: int) -> list[StageOutcome]:
        """The model's latest local stage outcomes, newest first (RM26, spec 0.48)."""
        with self._lock:
            self._check_open()
            rows = self._conn.execute(
                "SELECT run_id, ran_out_of_time, ended_at FROM local_stage_outcomes"
                " WHERE local_model = ? ORDER BY ended_at DESC, rowid DESC LIMIT ?",
                (model, limit),
            ).fetchall()
        return [
            StageOutcome(
                run_id=row["run_id"],
                ran_out_of_time=bool(row["ran_out_of_time"]),
                ended_at=_parse(row["ended_at"].replace("Z", "+00:00")),
            )
            for row in rows
        ]

    # -- events ----------------------------------------------------------

    def append_event(
        self,
        run_id: str,
        event_type: EventType,
        actor: str,
        payload: Mapping[str, Any],
    ) -> TraceEvent:
        """Validate and append an event with the next seq for the run."""
        return self.append_events(run_id, [(event_type, actor, payload)])[0]

    def append_events(self, run_id: str, drafts: Sequence[EventDraft]) -> list[TraceEvent]:
        """Validate and append events in order, in one transaction.

        The run is checked and its last seq read once; the events take the
        next seqs in order. Every event is validated, and one invalid
        event writes none of them. An empty batch writes nothing.
        """
        if not drafts:
            return []
        with self._write():
            self._require_run(run_id)
            return self._insert_events(run_id, drafts)

    def _insert_events(self, run_id: str, drafts: Sequence[EventDraft]) -> list[TraceEvent]:
        """Validate and insert events with the next seqs, in the caller's transaction.

        The caller has checked the run exists. The last seq and elapsed_ms
        are read from SQLite, never cached, so another store on the same
        file stays consistent: elapsed_ms is this store's monotonic
        measure, never less than the run's last persisted value (RM25).
        """
        baseline = self._run_started.get(run_id)
        if baseline is None:
            raise RunStoreError(
                f"run {run_id} has no monotonic baseline in this process; "
                "elapsed_ms is never derived from wall-clock time"
            )
        last, last_elapsed = self._conn.execute(
            "SELECT COALESCE(MAX(seq), 0), COALESCE(MAX(elapsed_ms), 0)"
            " FROM events WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        events: list[TraceEvent] = []
        rows: list[dict[str, Any]] = []
        for seq, (event_type, actor, payload) in enumerate(drafts, start=last + 1):
            measured = self.elapsed_ms(run_id)
            last_elapsed = max(last_elapsed, measured)
            event = validate_trace_event(
                {
                    "schema_version": SCHEMA_VERSION,
                    "run_id": run_id,
                    "seq": seq,
                    "timestamp": _iso(self.clock.utc_now()),
                    "elapsed_ms": last_elapsed,
                    "event_type": str(event_type),
                    "actor": actor,
                    "payload": dict(payload),
                }
            )
            event = self._reference_large_result(event)
            # Validate the persisted representation too, including artifact metadata.
            event = validate_trace_event(event.to_json_object())
            row = event.to_json_object()
            row["payload"] = json.dumps(row["payload"], ensure_ascii=False, sort_keys=True)
            events.append(event)
            rows.append(row)
        columns = list(rows[0])
        self._conn.executemany(
            f"INSERT INTO events ({', '.join(columns)})"
            f" VALUES ({', '.join(f':{c}' for c in columns)})",
            rows,
        )
        for event in events:
            self._record_outcome_event(event)
        return events

    def events(
        self, run_id: str, *, after_seq: int = 0, event_type: EventType | None = None
    ) -> list[TraceEvent]:
        """Retained events for a run in seq order, after after_seq.

        event_type, when given, keeps only events of that type; the filter
        runs in SQL, so other payloads are not decoded.
        """
        query = "SELECT * FROM events WHERE run_id = ? AND seq > ?"
        params: tuple[Any, ...] = (run_id, after_seq)
        if event_type is not None:
            query += " AND event_type = ?"
            params += (event_type.value,)
        with self._lock:
            self._check_open()
            rows = self._conn.execute(query + " ORDER BY seq", params).fetchall()
        # Rows were validated when they were written.
        return [
            TraceEvent(
                **{
                    **dict(row),
                    "event_type": EventType(row["event_type"]),
                    "payload": json.loads(row["payload"]),
                }
            )
            for row in rows
        ]

    # -- tool operations and artifacts -------------------------------------

    def record_tool_operation(
        self,
        run_id: str,
        *,
        operation_id: str,
        proposal_id: str | None,
        tool_name: str,
        model: str,
        arguments: Mapping[str, Any],
    ) -> None:
        """Persist a logical operation as pending, before its side effect (RM10).

        The arguments are stored in canonical form with their SHA-256, so
        a changed argument set is visibly a different operation. An
        operation ID is recorded once: a second record raises
        sqlite3.IntegrityError, so the same operation is never dispatched
        twice (RM14).
        """
        canonical = canonical_json(dict(arguments))
        with self._write():
            self._require_run(run_id)
            self._conn.execute(
                "INSERT INTO tool_operations (operation_id, run_id, proposal_id, tool_name,"
                " model, arguments, arguments_sha256, status, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    operation_id,
                    run_id,
                    proposal_id,
                    tool_name,
                    model,
                    canonical,
                    hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
                    PENDING,
                    _iso(self.clock.utc_now()),
                ),
            )

    def finish_tool_operation(self, operation_id: str, *, status: str, result: Any) -> bool:
        """Record the outcome of a pending operation once (RM10, section 8).

        status is succeeded, failed, or unknown_outcome. Only a pending
        record changes, so a late completion never rewrites an outcome
        already recorded (for example unknown_outcome after a timeout).
        Returns whether the record changed.
        """
        if status not in TOOL_OUTCOMES:
            raise ValueError(f"status must be one of {', '.join(TOOL_OUTCOMES)}, not {status!r}")
        with self._write():
            cursor = self._conn.execute(
                "UPDATE tool_operations SET status = ?, result = ?, finished_at = ?"
                " WHERE operation_id = ? AND status = ?",
                (
                    status,
                    json.dumps(result, ensure_ascii=False),
                    _iso(self.clock.utc_now()),
                    operation_id,
                    PENDING,
                ),
            )
        return cursor.rowcount == 1

    def get_tool_operation(self, operation_id: str) -> dict[str, Any] | None:
        """The recorded operation with decoded arguments and result, or None."""
        with self._lock:
            self._check_open()
            row = self._conn.execute(
                "SELECT * FROM tool_operations WHERE operation_id = ?", (operation_id,)
            ).fetchone()
        return None if row is None else _tool_operation_record(row)

    def tool_operations(self, run_id: str, *, status: str | None = None) -> list[dict[str, Any]]:
        """A run's tool operations in record order, decoded as by get_tool_operation.

        Section 8 HandoffEnvelope, RM13: a handoff carries the run's
        completed operations and their structured results. status, when
        given, keeps only operations with that status (restart recovery
        reads the pending ones). No operations, or no such run, gives an
        empty list.
        """
        query = "SELECT * FROM tool_operations WHERE run_id = ?"
        params: tuple[Any, ...] = (run_id,)
        if status is not None:
            query += " AND status = ?"
            params += (status,)
        with self._lock:
            self._check_open()
            rows = self._conn.execute(query + " ORDER BY rowid", params).fetchall()
        return [_tool_operation_record(row) for row in rows]

    def _reference_large_result(self, event: TraceEvent) -> TraceEvent:
        """Store large tool evidence atomically with its event (RM22, section 8).

        The producer supplies redacted results (RM12). This changes only
        trace storage: the executor and model still receive the complete
        result, and the source's truncation flag remains explicit.
        """
        if event.event_type is not EventType.TOOL_FINISHED or "result" not in event.payload:
            return event
        result = event.payload["result"]
        content = canonical_json(result)
        size = len(content.encode("utf-8"))
        if size <= self.max_inline_result_bytes:
            return event
        digest = hashlib.sha256((event.run_id + "\n" + content).encode("utf-8")).hexdigest()
        artifact_id = f"artifact_{digest}"
        ref = {
            "artifact_id": artifact_id,
            "uri": f"sqlite:artifact/{artifact_id}",
            "size_bytes": size,
            "truncated": result["truncated"],
        }
        self._conn.execute(
            "INSERT OR IGNORE INTO artifact_refs"
            " (artifact_id, run_id, uri, size_bytes, truncated) VALUES (?, ?, ?, ?, ?)",
            (artifact_id, event.run_id, ref["uri"], size, int(ref["truncated"])),
        )
        self._conn.execute(
            "INSERT OR IGNORE INTO artifact_contents (artifact_id, content) VALUES (?, ?)",
            (artifact_id, content),
        )
        payload = {key: value for key, value in event.payload.items() if key != "result"}
        return replace(event, payload={**payload, "result_ref": ref})

    def read_artifact(self, artifact_id: str) -> Any | None:
        """Read retained JSON evidence by its opaque ID, or None after deletion."""
        with self._lock:
            self._check_open()
            row = self._conn.execute(
                "SELECT content FROM artifact_contents WHERE artifact_id = ?", (artifact_id,)
            ).fetchone()
        return None if row is None else json.loads(row["content"])

    def tool_result(self, payload: Mapping[str, Any]) -> ToolResultView:
        """The result of a tool.finished payload, read back when stored by reference."""
        return read_tool_result(payload, self.read_artifact)

    def add_artifact_ref(
        self,
        run_id: str,
        *,
        artifact_id: str,
        uri: str,
        size_bytes: int,
        truncated: bool,
    ) -> None:
        """Reference a large result with its size and truncation (section 8)."""
        with self._write():
            self._require_run(run_id)
            self._conn.execute(
                "INSERT INTO artifact_refs (artifact_id, run_id, uri, size_bytes, truncated)"
                " VALUES (?, ?, ?, ?, ?)",
                (artifact_id, run_id, uri, size_bytes, int(truncated)),
            )

    def get_artifact_ref(self, artifact_id: str) -> dict[str, Any] | None:
        with self._lock:
            self._check_open()
            row = self._conn.execute(
                "SELECT * FROM artifact_refs WHERE artifact_id = ?", (artifact_id,)
            ).fetchone()
        return None if row is None else _artifact_ref_record(row)

    def artifact_refs(self, run_id: str) -> list[dict[str, Any]]:
        """A run's artifact references in record order (section 8 HandoffEnvelope; RM13).

        No references, or no such run, gives an empty list.
        """
        with self._lock:
            self._check_open()
            rows = self._conn.execute(
                "SELECT * FROM artifact_refs WHERE run_id = ? ORDER BY rowid", (run_id,)
            ).fetchall()
        return [_artifact_ref_record(row) for row in rows]

    # -- approvals ---------------------------------------------------------

    def create_approval(
        self,
        run_id: str,
        *,
        token: str,
        tool_name: str,
        arguments: Mapping[str, Any],
        target: str,
        state_version: int,
    ) -> IssuedApproval:
        """Store a pending approval for the run's current state version (RM11).

        Only the token's SHA-256 hash is kept. The approval expires
        approval_ttl after it is issued (five minutes by default); the
        result carries its ID and that expiry. A state_version the run is
        not at is refused with StaleVersionError (409).
        """
        approval_id = f"approval_{uuid.uuid4().hex}"
        now = self.clock.utc_now()
        with self._write():
            current = self._require_run(run_id)["version"]
            if current != state_version:
                raise StaleVersionError(run_id, state_version, current)
            self._conn.execute(
                "INSERT INTO approvals (approval_id, run_id, token_hash, tool_name, arguments,"
                " target, state_version, created_at, expires_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    approval_id,
                    run_id,
                    _token_hash(token),
                    tool_name,
                    canonical_json(dict(arguments)),
                    target,
                    state_version,
                    _iso(now),
                    _iso(now + self.approval_ttl),
                ),
            )
        return IssuedApproval(approval_id, _parse(_iso(now + self.approval_ttl)))

    def get_pending_approval(self, run_id: str) -> dict[str, Any] | None:
        """The run's latest unconsumed approval, or None (RM11, RM21).

        GET /relay/v1/runs/{id} shows a pending approval, so this returns
        its ID, tool, decoded canonical arguments, target, state version,
        and creation and expiry times. The token hash is never returned.
        An expired approval is still returned, so the caller can tell it
        apart from a mismatch and fail the run with approval_expired
        (section 5). An approval whose run moved past its state version is
        void, as consume_approval treats it, so it is not pending. A
        missing run raises RunNotFoundError.
        """
        with self._lock:
            self._check_open()
            self._require_run(run_id)
            row = self._conn.execute(
                "SELECT approval_id, run_id, tool_name, arguments, target, state_version,"
                " created_at, expires_at FROM approvals"
                " WHERE run_id = ? AND consumed_at IS NULL"
                " AND EXISTS (SELECT 1 FROM runs WHERE runs.run_id = approvals.run_id"
                " AND runs.version = approvals.state_version)"
                " ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        record = dict(row)
        record["arguments"] = json.loads(row["arguments"])
        record["created_at"] = _parse(row["created_at"])
        record["expires_at"] = _parse(row["expires_at"])
        return record

    def expire_approvals(self, run_id: str) -> int:
        """End the run's pending approvals now; return how many were still live (RM11).

        Restart recovery uses it: an approval that is unused and not yet
        past its expiry gets that expiry moved to the current time, so no
        token can be consumed, and the record shows when it ended. A used
        or already expired approval is left as it is, and so is a void one
        whose run moved past its state version (get_pending_approval).
        """
        now = _iso(self.clock.utc_now())
        with self._write():
            cursor = self._conn.execute(
                "UPDATE approvals SET expires_at = ?"
                " WHERE run_id = ? AND consumed_at IS NULL AND expires_at > ?"
                " AND EXISTS (SELECT 1 FROM runs WHERE runs.run_id = approvals.run_id"
                " AND runs.version = approvals.state_version)",
                (now, run_id, now),
            )
        return cursor.rowcount

    def consume_approval(
        self,
        run_id: str,
        *,
        token: str,
        tool_name: str,
        arguments: Mapping[str, Any],
        target: str,
        state_version: int,
    ) -> bool:
        """Use up a matching pending approval in one atomic step (RM11).

        True only if an approval for this run, token, tool, canonical
        arguments, target, and state version exists, that version is
        still the run's current version, it has not expired, and it has
        not been used; it is then marked consumed, so a replay gets
        False. One UPDATE does the match, the run check, and the mark, so
        two callers presenting the same token cannot both succeed and a
        run that moved on voids the approval. Whether a consumed approval
        lets the run continue is the orchestrator's decision.
        """
        now = _iso(self.clock.utc_now())
        with self._write():
            rows = self._conn.execute(
                "UPDATE approvals SET consumed_at = ?"
                " WHERE token_hash = ? AND run_id = ? AND tool_name = ? AND arguments = ?"
                " AND target = ? AND state_version = ? AND expires_at > ?"
                " AND consumed_at IS NULL"
                " AND EXISTS (SELECT 1 FROM runs WHERE runs.run_id = approvals.run_id"
                " AND runs.version = approvals.state_version)"
                " RETURNING approval_id",
                (
                    now,
                    _token_hash(token),
                    run_id,
                    tool_name,
                    canonical_json(dict(arguments)),
                    target,
                    state_version,
                    now,
                ),
            ).fetchall()
        return bool(rows)
