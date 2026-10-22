"""SQLite run store (spec section 5; RM17, RM22, RM25).

Section 5: SQLite stores runs, events, tool_operations, approvals, and
artifact_refs. Event sequence numbers increase within a run. Approval
records keep a token hash, not the plaintext token. Default retention is
seven days, with explicit deletion available. RM17: state changes are
atomic against a run state version. Section 8 / RM22: events are
validated against the TraceEvent schema at write time. RM25: event
timestamps are UTC and elapsed_ms comes from a monotonic clock.
Issue #7, part of #27.
"""

import hashlib
import sqlite3
import threading
from datetime import timedelta, timezone

import pytest

from helpers import ACCEPTED_PAYLOAD, T0, TERMINAL_EVIDENCE, FakeClock
from relaymind.store import (
    DEFAULT_APPROVAL_TTL,
    MIGRATIONS,
    RunNotFoundError,
    RunStore,
    RunStoreError,
    SlotUnavailableError,
    StaleVersionError,
)
from relaymind.store.run_store import split_sql_statements
from relaymind.store.trace_event import EventType, TraceEventError


def accept(store, run_id):
    """Write run.accepted, the first event of every run (RM22)."""
    return store.append_event(run_id, EventType.RUN_ACCEPTED, "gateway", ACCEPTED_PAYLOAD)


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def store(tmp_path, clock):
    with RunStore(tmp_path / "runs.sqlite", clock=clock) as opened:
        yield opened


def table_names(path):
    with sqlite3.connect(path) as conn:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {name for (name,) in rows}


def test_opening_creates_the_five_tables(tmp_path):
    with RunStore(tmp_path / "runs.sqlite"):
        pass
    assert {"runs", "events", "tool_operations", "approvals", "artifact_refs"} <= table_names(
        tmp_path / "runs.sqlite"
    )


def test_tables_owned_by_a_run_are_indexed_by_run_id(tmp_path):
    # Purge, deletion, and per-run reads look rows up by run_id.
    with RunStore(tmp_path / "runs.sqlite"):
        pass
    with sqlite3.connect(tmp_path / "runs.sqlite") as conn:
        for table in ("tool_operations", "approvals", "artifact_refs"):
            indexed = {
                column
                for (_seq, index, *_rest) in conn.execute(f"PRAGMA index_list({table})")
                for (_seqno, _cid, column) in conn.execute(f"PRAGMA index_info({index})")
            }
            assert "run_id" in indexed, table


def test_a_file_backed_store_uses_write_ahead_logging(tmp_path):
    # WAL lets readers proceed while one writer holds the lock; NORMAL
    # sync is durable at checkpoints and safe against corruption in WAL.
    with RunStore(tmp_path / "runs.sqlite") as store:
        (mode,) = store._conn.execute("PRAGMA journal_mode").fetchone()
        (synchronous,) = store._conn.execute("PRAGMA synchronous").fetchone()
    assert mode == "wal"
    assert synchronous == 1  # NORMAL


def test_an_in_memory_store_keeps_its_default_journal(clock):
    with RunStore(clock=clock) as store:
        (mode,) = store._conn.execute("PRAGMA journal_mode").fetchone()
        run = store.create_run("accepted")
        assert store.get_run(run.run_id) == run
    assert mode == "memory"


def test_migrations_are_versioned_and_applied_once(tmp_path):
    path = tmp_path / "runs.sqlite"
    with RunStore(path) as first:
        assert first.schema_version == len(MIGRATIONS)
        run = first.create_run("accepted")
    with RunStore(path) as second:
        assert second.schema_version == len(MIGRATIONS)
        assert second.get_run(run.run_id).state == "accepted"
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)


def test_migration_scripts_split_only_at_statement_ends():
    """Section 5 versioned migrations: a ';' inside a literal or trigger body is not an end."""
    script = """
        CREATE TABLE t (a TEXT DEFAULT 'x;y');  -- a comment; with a semicolon
        CREATE TRIGGER t_after AFTER INSERT ON t BEGIN
            UPDATE t SET a = 'b;c';
        END;
    """
    statements = split_sql_statements(script)
    assert len(statements) == 2
    assert statements[0].startswith("CREATE TABLE")
    assert "CREATE TRIGGER" in statements[1] and statements[1].endswith("END;")
    conn = sqlite3.connect(":memory:")
    for statement in statements:
        conn.execute(statement)
    conn.execute("INSERT INTO t DEFAULT VALUES")
    assert conn.execute("SELECT a FROM t").fetchone() == ("b;c",)


def test_an_unfinished_migration_statement_is_refused():
    with pytest.raises(RunStoreError, match="incomplete"):
        split_sql_statements("CREATE TABLE t (a TEXT); CREATE TABLE u (b TEXT")


def test_a_newer_schema_than_the_code_is_refused(tmp_path):
    path = tmp_path / "runs.sqlite"
    with sqlite3.connect(path) as conn:
        conn.execute(f"PRAGMA user_version = {len(MIGRATIONS) + 1}")
    with pytest.raises(RunStoreError, match="newer"):
        RunStore(path)


def test_a_new_run_starts_at_version_1(store):
    run = store.create_run("accepted")
    assert run.run_id.startswith("run_")
    assert (run.state, run.version, run.previous_run_id) == ("accepted", 1, None)
    assert run.created_at == T0
    assert store.get_run(run.run_id) == run


def test_a_retry_is_a_new_run_linked_to_the_previous_one(store):
    """RM17: a new attempt creates a new run linked to the previous one."""
    first = store.create_run("accepted")
    second = store.create_run("accepted", previous_run_id=first.run_id)
    assert second.run_id != first.run_id
    assert second.previous_run_id == first.run_id


def test_linking_to_a_missing_run_is_refused(store):
    with pytest.raises(RunNotFoundError):
        store.create_run("accepted", previous_run_id="run_missing")


def test_a_missing_run_is_not_found(store):
    with pytest.raises(RunNotFoundError) as caught:
        store.get_run("run_missing")
    assert caught.value.http_status == 404


def test_set_state_bumps_the_version(store):
    run = store.create_run("accepted")
    moved = store.set_state(
        run.run_id, expected_version=1, from_state="accepted", to_state="routing"
    )
    assert (moved.state, moved.version) == ("routing", 2)
    assert store.get_run(run.run_id) == moved


def test_a_stale_version_is_rejected_with_409(store):
    """RM17: a stale version never changes the run."""
    run = store.create_run("accepted")
    store.set_state(run.run_id, expected_version=1, from_state="accepted", to_state="routing")
    with pytest.raises(StaleVersionError) as caught:
        store.set_state(run.run_id, expected_version=1, from_state="accepted", to_state="failed")
    assert caught.value.http_status == 409
    assert caught.value.current_version == 2
    assert store.get_run(run.run_id).state == "routing"


def test_set_state_for_a_missing_run_is_not_found(store):
    with pytest.raises(RunNotFoundError):
        store.set_state("run_missing", expected_version=1, from_state="a", to_state="b")


def test_concurrent_set_state_lets_exactly_one_writer_win(tmp_path):
    """RM17: two writers with the same expected version; one wins, one gets 409."""
    path = tmp_path / "runs.sqlite"
    with RunStore(path) as setup:
        run_id = setup.create_run("accepted").run_id
    outcomes = []
    barrier = threading.Barrier(8)

    def writer(target):
        with RunStore(path) as own:
            barrier.wait()
            try:
                own.set_state(run_id, expected_version=1, from_state="accepted", to_state=target)
                outcomes.append("won")
            except StaleVersionError:
                outcomes.append("stale")

    threads = [threading.Thread(target=writer, args=(f"s{i}",)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == ["stale"] * 7 + ["won"]
    with RunStore(path) as check:
        assert check.get_run(run_id).version == 2


def test_one_shared_store_serializes_concurrent_writers(tmp_path):
    """RM17: threads sharing one store get a winner and 409s, never a SQLite error."""
    with RunStore(tmp_path / "runs.sqlite") as shared:
        run_ids = [shared.create_run("accepted").run_id for _ in range(20)]
        outcomes = []
        errors = []
        barrier = threading.Barrier(8)

        def writer():
            barrier.wait()
            for run_id in run_ids:
                try:
                    shared.set_state(
                        run_id, expected_version=1, from_state="accepted", to_state="routing"
                    )
                    outcomes.append("won")
                except StaleVersionError:
                    outcomes.append("stale")
                except Exception as error:
                    errors.append(repr(error))

        threads = [threading.Thread(target=writer) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert errors == []
        assert outcomes.count("won") == len(run_ids)
        assert all(shared.get_run(run_id).version == 2 for run_id in run_ids)


def test_set_state_records_resume_state_and_error_code(store):
    run = store.create_run("local_running")
    waiting = store.set_state(
        run.run_id,
        expected_version=1,
        from_state="local_running",
        to_state="waiting_input",
        resume_state="local_running",
    )
    assert waiting.resume_state == "local_running"
    failed = store.set_state(
        run.run_id,
        expected_version=2,
        from_state="waiting_input",
        to_state="failed",
        error_code="input_expired",
    )
    assert (failed.resume_state, failed.error_code) == (None, "input_expired")


def test_set_state_appends_its_event_in_the_same_transaction(store):
    run = store.create_run("accepted")
    accept(store, run.run_id)
    store.set_state(
        run.run_id,
        expected_version=1,
        from_state="accepted",
        to_state="failed",
        error_code="invalid_request",
        event=(
            EventType.RUN_FAILED,
            "gateway",
            {"error_code": "invalid_request", **TERMINAL_EVIDENCE},
        ),
    )
    assert [e.event_type for e in store.events(run.run_id)] == [
        EventType.RUN_ACCEPTED,
        EventType.RUN_FAILED,
    ]


def test_an_invalid_event_rolls_back_the_state_change(store):
    run = store.create_run("accepted")
    with pytest.raises(TraceEventError):
        store.set_state(
            run.run_id,
            expected_version=1,
            from_state="accepted",
            to_state="failed",
            event=(EventType.RUN_FAILED, "gateway", {}),
        )
    assert store.get_run(run.run_id).version == 1
    assert store.events(run.run_id) == []


def test_event_sequence_numbers_strictly_increase_per_run(store):
    """Section 5 / AC11: seq increases within each run, independently."""
    a = store.create_run("accepted").run_id
    b = store.create_run("accepted").run_id
    accept(store, a)
    accept(store, b)
    for run_id in (a, b, a, a, b):
        store.append_event(run_id, EventType.ANSWER_DELTA, "local", {"text": "x"})
    assert [e.seq for e in store.events(a)] == [1, 2, 3, 4]
    assert [e.seq for e in store.events(b)] == [1, 2, 3]


def test_run_accepted_is_the_first_event_the_store_writes(store):
    """RM22: run.accepted is the first event of a run (seq 1), and only once."""
    run_id = store.create_run("accepted").run_id
    with pytest.raises(TraceEventError, match="run.accepted"):
        store.append_event(run_id, EventType.ANSWER_DELTA, "local", {"text": "x"})
    assert store.events(run_id) == []
    assert accept(store, run_id).seq == 1
    with pytest.raises(TraceEventError, match="first event"):
        accept(store, run_id)
    assert [e.event_type for e in store.events(run_id)] == [EventType.RUN_ACCEPTED]


def test_events_replay_after_a_sequence_number(store):
    run_id = store.create_run("accepted").run_id
    accept(store, run_id)
    for _ in range(3):
        store.append_event(run_id, EventType.ANSWER_DELTA, "local", {"text": "x"})
    assert [e.seq for e in store.events(run_id, after_seq=2)] == [3, 4]


def test_events_survive_reopening_and_keep_counting(tmp_path, clock):
    path = tmp_path / "runs.sqlite"
    with RunStore(path, clock=clock) as first:
        run_id = first.create_run("accepted").run_id
        accept(first, run_id)
        first.append_event(run_id, EventType.ANSWER_DELTA, "local", {"text": "x"})
    with RunStore(path, clock=clock) as second:
        second.start_run_clock(run_id)
        event = second.append_event(run_id, EventType.ANSWER_DELTA, "local", {"text": "y"})
        assert event.seq == 3
        assert [e.seq for e in second.events(run_id)] == [1, 2, 3]


def test_a_batch_appends_events_in_order_with_consecutive_seqs(store):
    """RM22, section 5: a batch keeps order and continues the run's seq."""
    run_id = store.create_run("accepted").run_id
    written = store.append_events(
        run_id,
        [
            (EventType.RUN_ACCEPTED, "gateway", ACCEPTED_PAYLOAD),
            (EventType.ANSWER_DELTA, "local", {"text": "a"}),
            (EventType.ANSWER_DELTA, "local", {"text": "b"}),
        ],
    )
    assert [e.seq for e in written] == [1, 2, 3]
    store.append_event(run_id, EventType.ANSWER_DELTA, "local", {"text": "c"})
    more = store.append_events(
        run_id,
        [
            (EventType.ANSWER_DELTA, "local", {"text": "d"}),
            (EventType.ANSWER_DELTA, "local", {"text": "e"}),
        ],
    )
    assert [e.seq for e in more] == [5, 6]
    stored = store.events(run_id)
    assert stored == written + [stored[3]] + more
    assert [e.payload.get("text") for e in stored] == [None, "a", "b", "c", "d", "e"]


def test_a_batch_continues_the_seq_another_store_wrote(tmp_path, clock):
    """No in-memory seq cache: two stores on one file stay consistent."""
    path = tmp_path / "runs.sqlite"
    with RunStore(path, clock=clock) as first, RunStore(path, clock=clock) as second:
        run_id = first.create_run("accepted").run_id
        second.start_run_clock(run_id)
        accept(first, run_id)
        second.append_events(run_id, [(EventType.ANSWER_DELTA, "local", {"text": "a"})])
        first.append_events(run_id, [(EventType.ANSWER_DELTA, "local", {"text": "b"})])
        assert [e.seq for e in second.events(run_id)] == [1, 2, 3]


def test_an_invalid_event_rolls_back_the_whole_batch(store):
    """Section 8: every event in a batch is validated; one bad event writes none."""
    run_id = store.create_run("accepted").run_id
    accept(store, run_id)
    with pytest.raises(TraceEventError):
        store.append_events(
            run_id,
            [
                (EventType.ANSWER_DELTA, "local", {"text": "a"}),
                (EventType.TOOL_PROPOSED, "local", {}),
            ],
        )
    assert [e.seq for e in store.events(run_id)] == [1]
    assert (
        store.append_events(run_id, [(EventType.ANSWER_DELTA, "local", {"text": "b"})])[0].seq == 2
    )


def test_an_empty_batch_writes_nothing(store):
    run_id = store.create_run("accepted").run_id
    assert store.append_events(run_id, []) == []
    assert store.events(run_id) == []


def test_a_batch_for_a_missing_run_is_refused(store):
    with pytest.raises(RunNotFoundError):
        store.append_events("run_missing", [(EventType.ANSWER_DELTA, "local", {"text": "x"})])


def test_events_are_validated_at_write_time(store):
    """Section 8: a tool event without its proposal ID never reaches SQLite."""
    run_id = store.create_run("accepted").run_id
    accept(store, run_id)
    with pytest.raises(TraceEventError, match="operation_id or proposal_id"):
        store.append_event(run_id, EventType.TOOL_PROPOSED, "local", {"model": "qwen3"})
    assert [e.event_type for e in store.events(run_id)] == [EventType.RUN_ACCEPTED]


def test_events_for_a_missing_run_are_refused(store):
    with pytest.raises(RunNotFoundError):
        store.append_event("run_missing", EventType.RUN_ACCEPTED, "gateway", {})


def test_event_timestamps_are_utc_and_elapsed_uses_the_monotonic_clock(store, clock):
    """RM25: UTC timestamps; elapsed_ms is a monotonic difference."""
    run_id = store.create_run("accepted").run_id
    clock.mono += 0.25
    clock.now -= timedelta(hours=1)  # a wall-clock step back must not change elapsed_ms
    event = accept(store, run_id)
    assert event.elapsed_ms == 250
    assert event.timestamp == "2026-10-20T02:00:00.000000Z"
    assert event.schema_version == 1


def test_events_without_a_monotonic_baseline_are_refused(tmp_path, clock):
    """RM25: elapsed_ms is never derived from wall-clock timestamps."""
    path = tmp_path / "runs.sqlite"
    with RunStore(path, clock=clock) as first:
        run_id = first.create_run("accepted").run_id
    with RunStore(path, clock=clock) as second:
        with pytest.raises(RunStoreError, match="monotonic"):
            accept(second, run_id)


def test_elapsed_ms_never_goes_backwards_across_stores(tmp_path):
    """RM25, section 8: elapsed_ms is monotone within a run, whichever store writes.

    Each store measures with its own monotonic clock; readings of two
    monotonic clocks are not comparable, so a store that joins a run
    continues from the run's last persisted elapsed_ms. Time between the
    last event and the join is not counted: only the wall clock could
    measure it, and RM25 never derives durations from wall-clock time.
    """
    path = tmp_path / "runs.sqlite"
    clock_a, clock_b = FakeClock(), FakeClock()
    clock_b.mono = 5.0  # an unrelated monotonic base, as in another process
    with RunStore(path, clock=clock_a) as a, RunStore(path, clock=clock_b) as b:
        run_id = a.create_run("accepted").run_id
        clock_a.mono += 0.3
        accept(a, run_id)
        b.start_run_clock(run_id)
        clock_b.mono += 0.05
        b.append_event(run_id, EventType.ANSWER_DELTA, "local", {"text": "b"})
        a.append_event(run_id, EventType.ANSWER_DELTA, "local", {"text": "a"})
        b.append_event(run_id, EventType.ANSWER_DELTA, "local", {"text": "b2"})
        elapsed = [event.elapsed_ms for event in a.events(run_id)]
    assert elapsed == [300, 350, 350, 350]


def test_restarting_a_run_clock_keeps_its_baseline(store, clock):
    """RM25: a second start_run_clock in the same store never resets elapsed_ms."""
    run_id = store.create_run("accepted").run_id
    clock.mono += 0.2
    accept(store, run_id)
    store.start_run_clock(run_id)
    clock.mono += 0.1
    event = store.append_event(run_id, EventType.ANSWER_DELTA, "local", {"text": "x"})
    assert event.elapsed_ms == 300


def test_approval_tokens_are_stored_as_hashes_only(tmp_path, store):
    """Section 5: approval records keep a token hash, not the plaintext token."""
    token = "tok-plaintext-4f9a2c"
    run_id = store.create_run("waiting_confirmation").run_id
    store.create_approval(run_id, token=token, **BINDING)
    assert not store.consume_approval(run_id, token="tok-wrong", **BINDING)
    assert store.consume_approval(run_id, token=token, **BINDING)
    store.close()
    raw = b"".join(p.read_bytes() for p in tmp_path.iterdir() if p.is_file())
    assert token.encode() not in raw
    with sqlite3.connect(tmp_path / "runs.sqlite") as conn:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(approvals)")]
    assert "token_hash" in columns
    assert "token" not in columns
    assert "consumed_at" in columns


# What a confirmation is bound to (RM11): tool, arguments, target, and
# the run's current state version (a new run is at version 1), plus the
# run it was issued for.
BINDING = {
    "tool_name": "delete_sandbox_database",
    "arguments": {"database": "demo-db", "confirm": True},
    "target": "demo-db",
    "state_version": 1,
}


@pytest.fixture
def approval(store):
    """A pending approval for a waiting run: (run_id, token)."""
    run_id = store.create_run("waiting_confirmation").run_id
    store.create_approval(run_id, token="tok-1", **BINDING)
    return run_id, "tok-1"


def test_an_approval_is_consumed_once(store, approval):
    """RM11, AC05: a confirmation is single use; a replay is refused."""
    run_id, token = approval
    assert store.consume_approval(run_id, token=token, **BINDING)
    assert not store.consume_approval(run_id, token=token, **BINDING)


def test_argument_order_does_not_change_the_binding(store, approval):
    """RM11: arguments are compared in canonical form."""
    run_id, token = approval
    reordered = {**BINDING, "arguments": {"confirm": True, "database": "demo-db"}}
    assert store.consume_approval(run_id, token=token, **reordered)


@pytest.mark.parametrize(
    "change",
    [
        {"tool_name": "get_service_health"},
        {"arguments": {"database": "prod-db", "confirm": True}},
        {"arguments": {"database": "demo-db"}},
        {"target": "prod-db"},
        {"state_version": 2},
        {"state_version": 0},
    ],
    ids=["tool", "arguments", "missing-argument", "target", "newer-version", "older-version"],
)
def test_an_approval_only_matches_what_it_was_issued_for(store, approval, change):
    """RM11, AC05: bound to tool, arguments, target, and state version."""
    run_id, token = approval
    assert not store.consume_approval(run_id, token=token, **{**BINDING, **change})
    # A refused attempt does not use up the approval.
    assert store.consume_approval(run_id, token=token, **BINDING)


def test_an_approval_is_bound_to_its_run(store, approval):
    """RM11: an approval for one run never authorizes another."""
    _run_id, token = approval
    other = store.create_run("waiting_confirmation").run_id
    assert not store.consume_approval(other, token=token, **BINDING)


def test_an_approval_is_stale_once_the_run_moves_on(store, approval):
    """RM11, AC05: bound to the current state version; a run that moved on voids it.

    The caller presents exactly what the approval was issued for, but
    the run has changed state since, so its version is no longer current.
    """
    run_id, token = approval
    store.set_state(
        run_id, expected_version=1, from_state="waiting_confirmation", to_state="cancelled"
    )
    assert not store.consume_approval(run_id, token=token, **BINDING)


def test_an_approval_is_issued_only_for_the_current_state_version(store):
    """RM11, RM17: an approval for a version the run is not at is refused with 409."""
    run_id = store.create_run("waiting_confirmation").run_id
    for version in (0, 2):
        with pytest.raises(StaleVersionError) as caught:
            store.create_approval(run_id, token="t", **{**BINDING, "state_version": version})
        assert caught.value.http_status == 409
    with pytest.raises(RunNotFoundError):
        store.create_approval("run_missing", token="t", **BINDING)


def test_the_default_approval_ttl_is_five_minutes(store):
    """RM11: confirmation expires after five minutes."""
    assert DEFAULT_APPROVAL_TTL == timedelta(minutes=5)
    assert store.approval_ttl == DEFAULT_APPROVAL_TTL


@pytest.mark.parametrize(
    ("offset_seconds", "usable"),
    [(-1, True), (0, False)],
    ids=["just-before-expiry", "at-expiry"],
)
def test_an_approval_is_usable_only_until_it_expires(
    store, clock, approval, offset_seconds, usable
):
    """RM11: a confirmation expires five minutes after it is issued."""
    run_id, token = approval
    clock.advance(timedelta(minutes=5).total_seconds() + offset_seconds)
    assert store.consume_approval(run_id, token=token, **BINDING) is usable


def test_the_approval_ttl_is_configuration(tmp_path, clock):
    """RM11: the expiry window comes from store configuration, never the caller."""
    with RunStore(tmp_path / "r.sqlite", clock=clock, approval_ttl=timedelta(minutes=1)) as store:
        run_id = store.create_run("waiting_confirmation").run_id
        store.create_approval(run_id, token="t", **BINDING)
        clock.advance(60)
        assert not store.consume_approval(run_id, token="t", **BINDING)
    for ttl in (timedelta(0), timedelta(seconds=-1)):
        with pytest.raises(ValueError, match="approval_ttl"):
            RunStore(approval_ttl=ttl)


class NaiveClock(FakeClock):
    def utc_now(self):
        return self.now.replace(tzinfo=None)


class OffsetClock(FakeClock):
    def utc_now(self):
        return self.now.astimezone(timezone(timedelta(hours=8)))


@pytest.mark.parametrize("clock_cls", [NaiveClock, OffsetClock], ids=["naive", "non-utc"])
def test_store_times_must_be_timezone_aware_utc(clock_cls):
    """RM25, RM11: timestamps and approval expiry are UTC; a naive time is refused."""
    with RunStore(clock=clock_cls()) as store:
        with pytest.raises(ValueError, match="UTC"):
            store.create_run("accepted")


def record_pending(store, run_id, operation_id="op_1", **overrides):
    """Persist an operation before its side effect (RM10)."""
    store.record_tool_operation(
        run_id,
        **{
            "operation_id": operation_id,
            "proposal_id": "proposal_1",
            "tool_name": "get_service_health",
            "model": "qwen3-0.6b-q8_0",
            "arguments": {"service": "staging-api", "region": "eu"},
            **overrides,
        },
    )


def test_a_tool_operation_is_recorded_pending_before_its_outcome(store):
    """RM10: the operation ID and canonical arguments are persisted before the side effect."""
    run_id = store.create_run("local_running").run_id
    record_pending(store, run_id)
    op = store.get_tool_operation("op_1")
    canonical = '{"region":"eu","service":"staging-api"}'
    assert op == {
        "operation_id": "op_1",
        "run_id": run_id,
        "proposal_id": "proposal_1",
        "tool_name": "get_service_health",
        "model": "qwen3-0.6b-q8_0",
        "arguments": {"region": "eu", "service": "staging-api"},
        "arguments_sha256": hashlib.sha256(canonical.encode()).hexdigest(),
        "status": "pending",
        "result": None,
        "created_at": T0,
        "finished_at": None,
    }
    (raw,) = store._conn.execute("SELECT arguments FROM tool_operations").fetchone()
    assert raw == canonical


@pytest.mark.parametrize("status", ["succeeded", "failed", "unknown_outcome"])
def test_a_pending_operation_finishes_once_with_its_outcome(store, clock, status):
    """RM10, section 8: the outcome is recorded once; a late result never rewrites it."""
    run_id = store.create_run("local_running").run_id
    record_pending(store, run_id)
    clock.advance(2)
    assert store.finish_tool_operation("op_1", status=status, result={"n": 1}) is True
    assert store.finish_tool_operation("op_1", status="succeeded", result={"n": 2}) is False
    op = store.get_tool_operation("op_1")
    assert (op["status"], op["result"]) == (status, {"n": 1})
    assert op["finished_at"] == T0 + timedelta(seconds=2)


def test_finishing_an_unknown_operation_changes_nothing(store):
    assert store.finish_tool_operation("op_missing", status="failed", result={}) is False


@pytest.mark.parametrize("status", ["pending", "done", ""])
def test_an_operation_finishes_only_with_a_section_8_outcome(store, status):
    """Section 8 ToolResult status: succeeded, failed, or unknown_outcome."""
    run_id = store.create_run("local_running").run_id
    record_pending(store, run_id)
    with pytest.raises(ValueError, match="status"):
        store.finish_tool_operation("op_1", status=status, result={})
    assert store.get_tool_operation("op_1")["status"] == "pending"


def test_the_tool_operation_status_is_checked_by_sqlite(store):
    """RM10: the table itself admits only the lifecycle statuses."""
    run_id = store.create_run("local_running").run_id
    record_pending(store, run_id)
    with pytest.raises(sqlite3.IntegrityError):
        store._conn.execute("UPDATE tool_operations SET status = 'done'")
    with pytest.raises(sqlite3.IntegrityError):
        # A finished operation has its finish time and result.
        store._conn.execute("UPDATE tool_operations SET status = 'succeeded'")


def test_an_operation_id_is_recorded_once(store):
    """RM10, RM14: the same logical operation is never dispatched twice."""
    run_id = store.create_run("local_running").run_id
    record_pending(store, run_id)
    with pytest.raises(sqlite3.IntegrityError):
        record_pending(store, run_id)
    with pytest.raises(RunNotFoundError):
        record_pending(store, "run_missing", operation_id="op_2")


def test_artifact_refs_are_recorded(store):
    run_id = store.create_run("local_running").run_id
    store.add_artifact_ref(
        run_id, artifact_id="art_1", uri="file:///tmp/a.json", size_bytes=90000, truncated=True
    )
    ref = store.get_artifact_ref("art_1")
    assert (ref["size_bytes"], ref["truncated"]) == (90000, True)


def test_a_runs_tool_operations_are_listed_in_record_order(store, clock):
    """Section 8 HandoffEnvelope, RM13: the completed operations of a run
    are read back by run_id, in the order they were recorded, with
    decoded arguments and results; another run's operations are not."""
    run_id = store.create_run("local_running").run_id
    other = store.create_run("local_running").run_id
    record_pending(store, run_id, operation_id="op_b")
    record_pending(store, other, operation_id="op_other")
    clock.advance(1)
    record_pending(store, run_id, operation_id="op_a", arguments={"service": "billing"})
    store.finish_tool_operation("op_b", status="succeeded", result={"status": "healthy"})
    operations = store.tool_operations(run_id)
    assert [op["operation_id"] for op in operations] == ["op_b", "op_a"]
    assert operations[0] == store.get_tool_operation("op_b")
    assert operations[0]["result"] == {"status": "healthy"}
    assert operations[1]["arguments"] == {"service": "billing"}
    assert store.tool_operations("run_missing") == []


def test_a_runs_artifact_refs_are_listed_in_record_order(store):
    """Section 8 HandoffEnvelope, RM13: a run's evidence references are
    read back by run_id; another run's references are not."""
    run_id = store.create_run("local_running").run_id
    other = store.create_run("local_running").run_id
    store.add_artifact_ref(run_id, artifact_id="art_b", uri="u_b", size_bytes=2, truncated=True)
    store.add_artifact_ref(other, artifact_id="art_other", uri="u", size_bytes=1, truncated=False)
    store.add_artifact_ref(run_id, artifact_id="art_a", uri="u_a", size_bytes=1, truncated=False)
    refs = store.artifact_refs(run_id)
    assert [ref["artifact_id"] for ref in refs] == ["art_b", "art_a"]
    assert refs[0] == store.get_artifact_ref("art_b")
    assert refs[0]["truncated"] is True
    assert store.artifact_refs("run_missing") == []


def test_a_run_is_created_with_its_run_accepted_event(store):
    """RM22: run.accepted is the first event of a run. Written with the
    run in one transaction, it is seq 1 before any other event."""
    run = store.create_run(
        "accepted", accepted=(EventType.RUN_ACCEPTED, "gateway", ACCEPTED_PAYLOAD)
    )
    (event,) = store.events(run.run_id)
    assert (event.seq, event.event_type, event.payload) == (
        1,
        EventType.RUN_ACCEPTED,
        ACCEPTED_PAYLOAD,
    )


@pytest.mark.parametrize(
    "accepted",
    [
        (EventType.RUN_ACCEPTED, "gateway", {}),
        (EventType.RUN_FAILED, "gateway", {"error_code": "x"}),
    ],
    ids=["bad-payload", "not-run-accepted"],
)
def test_an_invalid_run_accepted_event_creates_no_run(store, accepted):
    """RM22, section 8: an event that fails validation writes nothing, so
    no run is left behind without its run.accepted event."""
    with pytest.raises(TraceEventError):
        store.create_run("accepted", accepted=accepted)
    assert store._conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_default_retention_is_seven_days(store):
    assert store.retention == timedelta(days=7)


def test_purge_removes_runs_past_retention_with_everything_they_own(store, clock):
    """Section 5: default retention is seven days."""
    old = store.create_run("completed").run_id
    accept(store, old)
    record_pending(store, old, operation_id="op_old", proposal_id=None)
    store.create_approval(old, token="t", tool_name="t", arguments={}, target="x", state_version=1)
    store.add_artifact_ref(old, artifact_id="art_old", uri="u", size_bytes=1, truncated=False)
    clock.advance(timedelta(days=7).total_seconds() - 1)
    recent = store.create_run("completed").run_id
    assert store.purge_expired() == []
    clock.advance(1)
    assert store.purge_expired() == [old]
    assert old not in store._run_started  # no per-run state outlives the run
    with pytest.raises(RunNotFoundError):
        store.get_run(old)
    assert store.events(old) == []
    assert store.get_tool_operation("op_old") is None
    assert store.get_artifact_ref("art_old") is None
    assert store.get_run(recent).state == "completed"


def test_explicit_deletion_removes_one_run(store):
    """Section 5: explicit deletion is available."""
    first = store.create_run("completed").run_id
    retry = store.create_run("accepted", previous_run_id=first).run_id
    accept(store, first)
    store.delete_run(first)
    assert first not in store._run_started
    with pytest.raises(RunNotFoundError):
        store.get_run(first)
    assert store.events(first) == []
    assert store.get_run(retry).previous_run_id is None
    with pytest.raises(RunNotFoundError):
        store.delete_run(first)


class FailingConnection:
    """Wraps a SQLite connection; COMMIT (and optionally ROLLBACK) raise once."""

    def __init__(self, conn, *, fail_rollback=False):
        self._conn = conn
        self.fail_commit = True
        self.fail_rollback = fail_rollback

    def execute(self, sql, *args):
        if sql == "COMMIT" and self.fail_commit:
            self.fail_commit = False
            raise sqlite3.OperationalError("disk I/O error")
        if sql == "ROLLBACK" and self.fail_rollback:
            self.fail_rollback = False
            raise sqlite3.OperationalError("rollback failed")
        return self._conn.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def run_count(store):
    return store._conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]


def test_a_failed_commit_rolls_back_and_later_writes_still_work(store):
    """RM17: a write that fails at COMMIT changes nothing and leaves no open transaction."""
    store._conn = FailingConnection(store._conn)
    with pytest.raises(sqlite3.OperationalError, match="disk I/O"):
        store.create_run("accepted")
    assert not store._conn.in_transaction
    assert run_count(store) == 0
    run = store.create_run("accepted")
    assert store.get_run(run.run_id) == run


def test_a_rollback_error_does_not_mask_the_original_error(store):
    """The caller sees why the write failed, not the cleanup error."""
    store._conn = FailingConnection(store._conn, fail_rollback=True)
    with pytest.raises(sqlite3.OperationalError, match="disk I/O"):
        store.create_run("accepted")


def test_a_failed_rollback_reopens_the_connection(store):
    """RM17: after a failed ROLLBACK the store reconnects, so no transaction stays open.

    Closing the old connection discards its open transaction; the
    reopened connection sees only committed data and later writes work.
    """
    failing = FailingConnection(store._conn, fail_rollback=True)
    store._conn = failing
    with pytest.raises(sqlite3.OperationalError, match="disk I/O"):
        store.create_run("accepted")
    assert store._conn is not failing
    assert not store._conn.in_transaction
    assert run_count(store) == 0
    run = store.create_run("accepted")
    assert store.get_run(run.run_id) == run
    (foreign_keys,) = store._conn.execute("PRAGMA foreign_keys").fetchone()
    assert foreign_keys == 1


def test_a_failed_rollback_closes_an_in_memory_store(clock):
    """An in-memory store cannot be reopened with its data, so it refuses further use."""
    store = RunStore(clock=clock)
    store._conn = FailingConnection(store._conn, fail_rollback=True)
    with pytest.raises(sqlite3.OperationalError, match="disk I/O"):
        store.create_run("accepted")
    with pytest.raises(RunStoreError, match="closed"):
        store.create_run("accepted")
    with pytest.raises(RunStoreError, match="closed"):
        store.get_run("run_any")


# -- the one active run (section 11), enforced in SQLite -------------------------


def take_slot(store, run, to_state="routing"):
    return store.set_state(
        run.run_id,
        expected_version=run.version,
        from_state=run.state,
        to_state=to_state,
        holds_slot=True,
    )


def test_one_active_run_is_enforced_across_stores_on_one_file(tmp_path, clock):
    """Section 11: one active run in the baseline; additional work gets 429.

    The slot is a column of the runs table, set in the same transaction
    as the state change, so two stores (or processes) on one file cannot
    both hold it.
    """
    path = tmp_path / "runs.sqlite"
    with RunStore(path, clock=clock) as a, RunStore(path, clock=clock) as b:
        first = take_slot(a, a.create_run("accepted"))
        second = b.create_run("accepted")
        with pytest.raises(SlotUnavailableError) as caught:
            take_slot(b, second)
        assert caught.value.http_status == 429
        assert b.get_run(second.run_id) == second
        assert a.active_run_id() == b.active_run_id() == first.run_id
        a.set_state(first.run_id, expected_version=2, from_state="routing", to_state="waiting")
        assert b.active_run_id() is None
        take_slot(b, second)
        assert a.active_run_id() == second.run_id


def test_a_run_keeps_the_slot_across_active_states(store):
    run = take_slot(store, store.create_run("accepted"))
    run = take_slot(store, run, "local_running")
    assert store.active_run_id() == run.run_id


def test_a_stale_writer_gets_409_even_when_the_slot_is_taken(store):
    """RM17 before section 11: a stale version is 409, not 429."""
    take_slot(store, store.create_run("accepted"))
    second = store.create_run("accepted")
    with pytest.raises(StaleVersionError):
        store.set_state(
            second.run_id, expected_version=7, from_state="accepted", to_state="r", holds_slot=True
        )


def test_sqlite_refuses_a_second_slot_holder(store):
    """Section 11: a unique index backs the check inside set_state."""
    take_slot(store, store.create_run("accepted"))
    second = store.create_run("accepted")
    with pytest.raises(sqlite3.IntegrityError):
        store._conn.execute("UPDATE runs SET holds_slot = 1 WHERE run_id = ?", (second.run_id,))


def test_deleting_the_slot_holder_frees_the_slot(store):
    run = take_slot(store, store.create_run("accepted"))
    store.delete_run(run.run_id)
    assert store.active_run_id() is None


# -- Review fixes: pending approval read API, run clock lock ------------------


def test_the_pending_approval_of_a_run_can_be_read_without_its_token(store, clock):
    """RM21, RM11: GET /relay/v1/runs/{id} returns a pending approval, so
    the store reads it by run: its ID, tool, canonical arguments, target,
    state version, and times. The token hash is never returned. An
    expired approval is still returned with its expires_at, so the caller
    can fail the run with approval_expired (section 5); a consumed one is
    not pending."""
    run_id = store.create_run("waiting_confirmation").run_id
    assert store.get_pending_approval(run_id) is None
    issued = store.create_approval(run_id, token="tok-1", **BINDING)
    approval_id = issued.approval_id
    assert issued.expires_at == clock.now + store.approval_ttl
    pending = store.get_pending_approval(run_id)
    assert pending == {
        "approval_id": approval_id,
        "run_id": run_id,
        "tool_name": BINDING["tool_name"],
        "arguments": BINDING["arguments"],
        "target": BINDING["target"],
        "state_version": BINDING["state_version"],
        "created_at": clock.now,
        "expires_at": clock.now + store.approval_ttl,
    }
    assert not any("token" in key for key in pending)
    clock.advance(store.approval_ttl.total_seconds() + 1)
    assert store.get_pending_approval(run_id)["approval_id"] == approval_id
    clock.now -= store.approval_ttl
    assert store.consume_approval(run_id, token="tok-1", **BINDING)
    assert store.get_pending_approval(run_id) is None


def test_an_approval_is_not_pending_once_its_run_moved_on(store):
    """RM11, RM21: a run that left the approval's version voids it, so it is not pending."""
    run = store.create_run("waiting_confirmation")
    accept(store, run.run_id)
    store.create_approval(run.run_id, token="tok-1", **BINDING)
    store.set_state(
        run.run_id,
        expected_version=run.version,
        from_state="waiting_confirmation",
        to_state="cancelled",
        error_code="cancel_requested",
        event=(
            EventType.RUN_CANCELLED,
            "gateway",
            {"reason": "cancel_requested", **TERMINAL_EVIDENCE},
        ),
    )
    assert store.get_pending_approval(run.run_id) is None


def test_a_transaction_rolls_back_every_write_inside_it(store):
    """RM11, RM17: consuming an approval and moving the run happen together or not at all."""
    run_id = store.create_run("waiting_confirmation").run_id
    store.create_approval(run_id, token="tok-1", **BINDING)
    with pytest.raises(RuntimeError), store.transaction():
        assert store.consume_approval(run_id, token="tok-1", **BINDING)
        raise RuntimeError("the resume failed")
    assert store.get_pending_approval(run_id) is not None
    with store.transaction():
        assert store.consume_approval(run_id, token="tok-1", **BINDING)
    assert store.get_pending_approval(run_id) is None


def test_the_pending_approval_of_a_missing_run_is_not_found(store):
    with pytest.raises(RunNotFoundError):
        store.get_pending_approval("run_missing")


def test_run_clocks_change_only_under_the_store_lock(store, monkeypatch):
    """RM25, RM17: the per-run clock map is shared by threads using one
    store, so delete_run and purge_expired change it under the lock."""
    run_id = store.create_run("accepted").run_id
    seen = []

    class Watched(dict):
        def pop(self, key, *default):
            seen.append(store._lock._is_owned())
            return super().pop(key, *default)

    monkeypatch.setattr(store, "_run_started", Watched(store._run_started))
    store.delete_run(run_id)
    other = store.create_run("accepted").run_id
    store.clock.advance(store.retention.total_seconds() + 1)
    assert store.purge_expired() == [other]
    assert seen == [True, True]
