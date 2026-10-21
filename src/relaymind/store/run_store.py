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
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from relaymind.canonical import canonical_json
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
)


class RunStoreError(RuntimeError):
    """The run store cannot complete the operation."""


class RunNotFoundError(RunStoreError):
    """No run with that ID (section 11: 404)."""

    http_status = 404


class SlotUnavailableError(RunStoreError):
    """Another run holds the active execution slot (section 11: 429)."""

    http_status = 429


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


def _iso(moment: datetime) -> str:
    """A UTC timestamp; naive or non-UTC times are refused (RM25)."""
    if moment.tzinfo is None or moment.utcoffset() != timedelta(0):
        raise ValueError(f"store times must be timezone-aware UTC, not {moment!r}")
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


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
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield
                self._conn.execute("COMMIT")
            except BaseException:
                self._rollback()
                raise

    # -- runs ------------------------------------------------------------

    def create_run(
        self,
        initial_state: str,
        *,
        previous_run_id: str | None = None,
        accepted: EventDraft | None = None,
    ) -> RunRecord:
        """Create a run at version 1. A retry links to its previous run (RM17).

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
            if accepted is not None:
                # RM25: the run clock starts with the run, before seq 1.
                self.start_run_clock(run_id)
                try:
                    self._insert_events(run_id, [accepted])
                except BaseException:
                    self._run_started.pop(run_id, None)
                    raise
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
                    f"another run holds the execution slot; {run_id} is unchanged"
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

    def purge_expired(self) -> list[str]:
        """Delete runs created at or before now minus retention; return their IDs.

        The caller learns which runs are gone, so the state machine can
        release an execution slot a purged run still held.
        """
        cutoff = _iso(self.clock.utc_now() - self.retention)
        with self._lock:
            with self._write():
                rows = self._conn.execute(
                    "DELETE FROM runs WHERE created_at <= ? RETURNING run_id", (cutoff,)
                ).fetchall()
            run_ids = sorted(row["run_id"] for row in rows)
            for run_id in run_ids:
                self._run_started.pop(run_id, None)
        return run_ids

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
        return events

    def events(self, run_id: str, *, after_seq: int = 0) -> list[TraceEvent]:
        """Retained events for a run in seq order, after after_seq."""
        with self._lock:
            self._check_open()
            rows = self._conn.execute(
                "SELECT * FROM events WHERE run_id = ? AND seq > ? ORDER BY seq",
                (run_id, after_seq),
            ).fetchall()
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

    def tool_operations(self, run_id: str) -> list[dict[str, Any]]:
        """A run's tool operations in record order, decoded as by get_tool_operation.

        Section 8 HandoffEnvelope, RM13: a handoff carries the run's
        completed operations and their structured results. No operations,
        or no such run, gives an empty list.
        """
        with self._lock:
            self._check_open()
            rows = self._conn.execute(
                "SELECT * FROM tool_operations WHERE run_id = ? ORDER BY rowid", (run_id,)
            ).fetchall()
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
    ) -> str:
        """Store a pending approval for the run's current state version (RM11).

        Only the token's SHA-256 hash is kept. The approval expires
        approval_ttl after it is issued (five minutes by default). A
        state_version the run is not at is refused with
        StaleVersionError (409).
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
        return approval_id

    def get_pending_approval(self, run_id: str) -> dict[str, Any] | None:
        """The run's latest unconsumed approval, or None (RM11, RM21).

        GET /relay/v1/runs/{id} shows a pending approval, so this returns
        its ID, tool, decoded canonical arguments, target, state version,
        and creation and expiry times. The token hash is never returned.
        An expired approval is still returned, so the caller can tell it
        apart from a mismatch and fail the run with approval_expired
        (section 5). A missing run raises RunNotFoundError.
        """
        with self._lock:
            self._check_open()
            self._require_run(run_id)
            row = self._conn.execute(
                "SELECT approval_id, run_id, tool_name, arguments, target, state_version,"
                " created_at, expires_at FROM approvals"
                " WHERE run_id = ? AND consumed_at IS NULL"
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
