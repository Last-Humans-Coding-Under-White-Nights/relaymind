"""Preregistration, resumable host runner and external cost capture (#237).

Spec 0.65 section 12.6.5 (RM16, RM40–RM44; AC31–AC36). The runner executes the
frozen three-arm plan through a caller-supplied host executor, keeps crash-safe
progress in a run journal bound to the preregistered task, freeze and plan
digests, and conserves every planned row. Fixture data only: nothing here is a
host, a measured result or qualified evidence.
"""

import json
import sys

import pytest

from relaymind.canonical import canonical_digest
from relaymind.evaluation.asset_host_runner import (
    ExecutorFailed,
    attempts_from_trace,
    command_executor,
    host_preregistration,
    run_host_plan,
)
from relaymind.evaluation.asset_run_journal import JournalError, RunJournal
from relaymind.evaluation.asset_whole_task import (
    WholeTaskRejected,
    _not_run,
    grade_host_report,
    normalized_total_tokens,
    plan_host_trial,
    run_host_trial,
    validate_whole_task,
)
from whole_task_helpers import ENVIRONMENT, H, freeze_doc, h, row, tasks_doc

MANIFEST = dict(path="inputs.json", digest=H, bytes=1)


def setup(count=2, kind="early_trial", lanes=None):
    ids = tuple(f"t{i:03d}" for i in range(count))
    freeze = freeze_doc(task_ids=ids)
    lanes = lanes or ("en_to_en",) * count
    return freeze, tasks_doc(freeze, lanes=lanes, kind=kind)


def captured(r: dict) -> tuple[dict, list]:
    ids = sorted({*r["evidence_ids"], *(i for a in r["attempts"] for i in a["evidence_ids"])})
    return r, [dict(id=i, path=None, sha256=h(i), bytes=1, provenance="fixture") for i in ids]


def run(tasks, freeze, execute, journal, *, interrupted=lambda: False, qualification="fixture"):
    return run_host_plan(
        tasks,
        freeze,
        execute,
        journal,
        environment=ENVIRONMENT,
        artifacts=[],
        qualification=qualification,
        input_manifest=MANIFEST,
        interrupted=interrupted,
    )


# --- preregistration -------------------------------------------------------------


def test_preregistration_locks_the_task_freeze_and_plan_digests():
    freeze, tasks = setup(lanes=("en_to_en", "zh_hans_to_zh_hans"))
    record = host_preregistration(tasks, freeze)
    assert record["input_digest"] == canonical_digest(tasks)
    assert record["freeze_digest"] == canonical_digest(freeze)
    assert record["plan_digest"] == canonical_digest(plan_host_trial(tasks))
    assert record["planned_rows"] == 6 and record["tasks"] == 2
    assert record["lanes"] == [
        dict(lane="en_to_en", tasks=1),
        dict(lane="zh_hans_to_zh_hans", tasks=1),
    ]
    assert record["missing_lanes"] == ["code_switch", "en_to_zh_hans", "zh_hans_to_en"]
    # An early trial never asserts the full gate through a smaller set.
    assert record["acceptance_eligible"] is False
    assert record["reasons"] == [
        "kind_not_acceptance",
        "below_minimum_tasks",
        "missing_required_lanes",
    ]


def test_one_hundred_acceptance_tasks_are_eligible_for_acceptance():
    lanes = ("en_to_en", "zh_hans_to_zh_hans", "zh_hans_to_en", "en_to_zh_hans", "code_switch")
    freeze, tasks = setup(100, kind="acceptance", lanes=tuple(lanes[i % 5] for i in range(100)))
    record = host_preregistration(tasks, freeze)
    assert record["acceptance_eligible"] is True and record["reasons"] == []


def test_preregistration_rejects_unbound_inputs():
    freeze, tasks = setup()
    tasks["freeze_digest"] = H
    with pytest.raises(WholeTaskRejected, match="freeze_binding"):
        host_preregistration(tasks, freeze)


# --- runner ----------------------------------------------------------------------


def test_a_journaled_run_equals_the_conserving_driver_and_grades(tmp_path):
    freeze, tasks = setup()

    def execute(task, arm, config):
        return captured(row(task["id"], arm))

    report = run(tasks, freeze, execute, tmp_path / "run.jsonl")
    expected = run_host_trial(
        tasks,
        freeze,
        execute,
        environment=ENVIRONMENT,
        artifacts=[],
        qualification="fixture",
        input_manifest=MANIFEST,
        interrupted=lambda: False,
    )
    assert report == expected
    validate_whole_task("AssetHostReportV1", report)
    assert report["summary"] == grade_host_report(report, tasks, freeze)


class Crash(BaseException):
    """A process death during a host task (never caught by the runner)."""


def test_a_crash_resumes_without_rerunning_and_keeps_the_started_row(tmp_path):
    freeze, tasks = setup()
    journal = tmp_path / "run.jsonl"
    calls = []

    def crashing(task, arm, config):
        calls.append((task["id"], arm))
        if len(calls) == 3:
            raise Crash()
        return captured(row(task["id"], arm))

    with pytest.raises(Crash):
        run(tasks, freeze, crashing, journal)
    # A killed process would leave its lock; this in-process crash released it.
    assert not (journal.parent / (journal.name + ".lock")).exists()

    resumed = []

    def execute(task, arm, config):
        resumed.append((task["id"], arm))
        return captured(row(task["id"], arm))

    report = run(tasks, freeze, execute, journal)
    plan = [(p["task_id"], p["arm"]) for p in report["planned"]]
    # Completed rows are kept and the started row is never run again.
    assert resumed == plan[3:]
    third = report["rows"][2]
    assert third["status"] == "cancelled" and third["reason"] == "interrupted_unrecorded"
    assert third["success"] is None and third["attempts"] == []
    assert report["summary"] == grade_host_report(report, tasks, freeze)
    # Its consumption is unknown, so cost coverage cannot pass.
    arm = next(a for a in report["summary"]["arms"] if a["arm"] == third["arm"])
    assert arm["unknown_cost_rows"] >= 1


def test_an_interruption_conserves_not_run_rows_and_a_later_run_completes_them(tmp_path):
    freeze, tasks = setup()
    journal = tmp_path / "run.jsonl"
    calls = []

    def execute(task, arm, config):
        calls.append((task["id"], arm))
        return captured(row(task["id"], arm))

    partial = run(tasks, freeze, execute, journal, interrupted=lambda: len(calls) >= 2)
    assert [r["status"] for r in partial["rows"]] == ["completed"] * 2 + ["not_run"] * 4
    assert all(r["reason"] == "interrupted" for r in partial["rows"][2:])
    final = run(tasks, freeze, execute, journal)
    assert len(calls) == 6
    assert [r["status"] for r in final["rows"]] == ["completed"] * 6


def test_a_changed_task_set_cannot_resume_a_preregistered_journal(tmp_path):
    freeze, tasks = setup()
    journal = tmp_path / "run.jsonl"
    run(tasks, freeze, lambda t, a, c: captured(row(t["id"], a)), journal)
    tasks["seed"] = 8
    with pytest.raises(JournalError, match="journal_binding"):
        run(tasks, freeze, lambda t, a, c: captured(row(t["id"], a)), journal)


def test_an_executor_failure_is_a_failed_row_with_unknown_consumption(tmp_path):
    freeze, tasks = setup()

    def execute(task, arm, config):
        if arm == "native":
            raise ExecutorFailed("executor_exit")
        return captured(row(task["id"], arm))

    report = run(tasks, freeze, execute, tmp_path / "run.jsonl")
    native = [r for r in report["rows"] if r["arm"] == "native"]
    assert all(r["status"] == "failed" and r["reason"] == "executor_error" for r in native)
    assert all(r["success"] is None and r["attempts"] == [] for r in native)
    assert report["summary"]["success_gate"] == "blocked"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r.update(success=True, host_session_id=None),
        lambda r: r.update(status="not_run"),
        lambda r: r.update(arm="native" if r["arm"] != "native" else "provider_only"),
        lambda r: r["attempts"][0].update(ordinal=2),
    ],
)
def test_an_invalid_executor_row_is_never_journaled_as_host_output(tmp_path, mutate):
    freeze, tasks = setup(1)

    def execute(task, arm, config):
        r, refs = captured(row(task["id"], arm))
        mutate(r)
        return r, refs

    report = run(tasks, freeze, execute, tmp_path / "run.jsonl")
    assert {r["reason"] for r in report["rows"]} == {"executor_invalid_row"}
    assert all(r["status"] == "failed" and r["attempts"] == [] for r in report["rows"])


def test_a_reused_attempt_id_across_rows_is_an_invalid_row(tmp_path):
    freeze, tasks = setup(1)

    def execute(task, arm, config):
        r, refs = captured(row(task["id"], arm))
        r["attempts"][0]["id"] = "same"
        return r, refs

    report = run(tasks, freeze, execute, tmp_path / "run.jsonl")
    assert [r["reason"] for r in report["rows"]] == [
        None,
        "executor_invalid_row",
        "executor_invalid_row",
    ]


def test_a_caller_cannot_flag_a_run_qualified(tmp_path):
    freeze, tasks = setup()
    with pytest.raises(WholeTaskRejected, match="qualification"):
        run(tasks, freeze, None, tmp_path / "run.jsonl", qualification="qualified")


def test_a_second_runner_cannot_share_the_journal(tmp_path):
    journal = tmp_path / "run.jsonl"
    header = dict(type="header", kind="host", input_digest=H)
    with RunJournal(journal, header):
        with pytest.raises(JournalError, match="journal_locked"):
            RunJournal(journal, header)


def test_a_torn_trailing_record_is_dropped_and_a_corrupt_one_rejects(tmp_path):
    journal = tmp_path / "run.jsonl"
    header = dict(type="header", kind="host", input_digest=H)
    with RunJournal(journal, header) as j:
        j.start("1")
        j.finish("1", dict(value=1))
    with journal.open("ab") as f:
        f.write(b'{"type":"started","ke')
    with RunJournal(journal, header) as j:
        assert j.finished == {"1": dict(value=1)} and j.started == set()
        j.start("2")
    assert journal.read_bytes().endswith(b"\n")
    lines = journal.read_bytes().split(b"\n")
    lines[1] = b"not json"
    journal.write_bytes(b"\n".join(lines))
    with pytest.raises(JournalError, match="journal_corrupt"):
        RunJournal(journal, header)


# --- command executor ------------------------------------------------------------


def write_executor(tmp_path, body: str):
    script = tmp_path / "executor.py"
    script.write_text(body, encoding="utf-8")
    return [sys.executable, "-I", str(script)]


def test_the_command_executor_passes_the_planned_task_and_reads_its_row(tmp_path):
    freeze, tasks = setup(1)
    log = tmp_path / "requests.jsonl"
    argv = write_executor(
        tmp_path,
        f"""
import json, sys
request = json.load(sys.stdin)
with open({str(log)!r}, "a") as f:
    f.write(json.dumps(request) + "\\n")
task, arm = request["task"]["id"], request["arm"]
row = json.loads({json.dumps(row("X", "ARM"))!r})
row.update(task_id=task, arm=arm, host_session_id="s-" + arm, evidence_ids=["run-" + arm])
row["recommendation_ids"] = ["r"] if arm == "recommendations" else []
row["attempts"][0]["id"] = "a-" + arm
row["attempts"][0]["evidence_ids"] = ["ev-" + arm]
ids = sorted(row["evidence_ids"] + row["attempts"][0]["evidence_ids"])
evidence = [dict(id=i, path=None, sha256="b" * 64, bytes=1, provenance="fixture") for i in ids]
json.dump(dict(row=row, evidence=evidence), sys.stdout)
""",
    )
    report = run(tasks, freeze, command_executor(argv, tasks), tmp_path / "run.jsonl")
    assert [r["status"] for r in report["rows"]] == ["completed"] * 3
    requests = [json.loads(line) for line in log.read_text().splitlines()]
    assert [(q["ordinal"], q["arm"]) for q in requests] == [
        (p["ordinal"], p["arm"]) for p in report["planned"]
    ]
    native = next(c for c in freeze["configurations"] if c["arm"] == "native")
    assert requests[[q["arm"] for q in requests].index("native")]["arm_config"] == native


@pytest.mark.parametrize("body", ["import sys; sys.exit(4)", "print('not json')", "print('{}')"])
def test_a_failing_command_executor_raises_executor_failed(tmp_path, body):
    freeze, tasks = setup(1)
    execute = command_executor(write_executor(tmp_path, body), tasks)
    with pytest.raises(ExecutorFailed):
        execute(tasks["tasks"][0], "native", freeze["configurations"][0])


# --- external cost capture -------------------------------------------------------


def event(seq, event_type, actor, payload, run_id="run-1", second=0):
    return dict(
        schema_version=1,
        run_id=run_id,
        seq=seq + 1,  # seq 1 is the run's run.accepted, not needed here
        timestamp=f"2026-11-01T00:00:{second:02d}Z",
        elapsed_ms=seq,
        event_type=event_type,
        actor=actor,
        payload=payload,
    )


def started(seq, call, actor="external", second=0):
    payload = dict(call_id=call, model="model-x", actor=actor, purpose="external", tools_offered=[])
    return event(seq, "model.started", actor, payload, second=second)


def finished(seq, call, usage, error=None, actor="external", second=1):
    return event(
        seq,
        "model.finished",
        actor,
        dict(
            call_id=call,
            model="model-x",
            purpose="external",
            duration_ms=5,
            first_token_ms=None,
            finish_reason="stop",
            tool_calls=0,
            proposed_calls=[],
            text=None,
            usage=usage,
            error=error,
        ),
        second=second,
    )


USAGE = dict(input_tokens=100, output_tokens=20, cached_tokens=40, reasoning_tokens=5)


def test_external_gateway_calls_become_reported_attempts_with_dispatch_evidence():
    events = [
        started(1, "c1"),
        finished(2, "c1", USAGE),
        started(3, "l1", actor="local"),
        finished(4, "l1", USAGE, actor="local"),
        started(5, "c2", second=2),
        finished(6, "c2", None, error=dict(code="provider_timeout", message="x"), second=3),
        started(7, "c3", second=4),
    ]
    attempts = attempts_from_trace(
        events, provider="provider-x", attempt_prefix="t1-rec-", evidence_id="trace-t1"
    )
    assert [a["id"] for a in attempts] == ["t1-rec-1", "t1-rec-2", "t1-rec-3"]
    assert [a["ordinal"] for a in attempts] == [1, 2, 3]
    first, timeout, unfinished = attempts
    assert first["external"] is True and first["usage_provenance"] == "reported"
    assert (first["input_tokens"], first["cached_tokens"], first["total_tokens"]) == (100, 40, None)
    assert first["calls"] == 1 and first["evidence_ids"] == ["trace-t1"]
    assert first["token_relationship"] == "unknown" and normalized_total_tokens(first) is None
    assert first["started_at"] == "2026-11-01T00:00:00Z"
    assert first["ended_at"] == "2026-11-01T00:00:01Z"
    assert timeout["outcome"] == "timeout" and timeout["usage_provenance"] == "unknown"
    assert timeout["input_tokens"] is None and timeout["calls"] == 1
    # Started but never finished: neither dispatch nor usage is evidenced.
    assert unfinished["outcome"] == "unknown" and unfinished["calls"] is None
    assert unfinished["ended_at"] is None
    for a in attempts:
        validate_whole_task("HostAttempt", a)


def test_a_token_relationship_needs_its_own_evidence():
    events = [started(1, "c1"), finished(2, "c1", USAGE)]
    with pytest.raises(ValueError, match="relationship_evidence"):
        attempts_from_trace(
            events,
            provider="p",
            attempt_prefix="a-",
            evidence_id="trace",
            token_relationship="cached_within_input_reasoning_within_output",
        )
    (attempt,) = attempts_from_trace(
        events,
        provider="p",
        attempt_prefix="a-",
        evidence_id="trace",
        token_relationship="cached_within_input_reasoning_within_output",
        relationship_evidence_id="provider-usage-doc",
    )
    assert attempt["evidence_ids"] == ["provider-usage-doc", "trace"]
    assert normalized_total_tokens(attempt) == 120


def test_an_unreachable_provider_has_no_evidenced_call():
    events = [
        started(1, "c1"),
        finished(2, "c1", None, error=dict(code="provider_unreachable", message="x")),
    ]
    (attempt,) = attempts_from_trace(events, provider="p", attempt_prefix="a-", evidence_id="t")
    assert attempt["calls"] is None and attempt["outcome"] == "failed"


def test_an_invalid_trace_event_rejects():
    events = [started(1, "c1"), dict(finished(2, "c1", USAGE), seq=1)]
    with pytest.raises(ValueError):
        attempts_from_trace(events, provider="p", attempt_prefix="a-", evidence_id="t")


def test_a_finish_before_its_start_rejects():
    """Review 4435556049: a finish must follow its own start."""
    events = [finished(1, "c1", USAGE), started(2, "c1")]
    with pytest.raises(ValueError, match="trace_order"):
        attempts_from_trace(events, provider="p", attempt_prefix="a-", evidence_id="t")


@pytest.mark.parametrize(
    "events",
    [
        lambda: [started(1, "c1"), started(2, "c1"), finished(3, "c1", USAGE)],
        lambda: [started(1, "c1"), finished(2, "c1", USAGE), finished(3, "c1", USAGE)],
    ],
)
def test_a_repeated_call_event_rejects(events):
    """Review 4435556054: one call is one attempt; repeats are not new attempts."""
    with pytest.raises(ValueError, match="trace_duplicate_call"):
        attempts_from_trace(events(), provider="p", attempt_prefix="a-", evidence_id="t")


def test_a_hung_executor_times_out_as_executor_failed(tmp_path):
    """Review 4435556059: an optional harness timeout ends a hung program."""
    freeze, tasks = setup(1)
    argv = write_executor(tmp_path, "import time; time.sleep(30)")
    execute = command_executor(argv, tasks, timeout=0.5)
    with pytest.raises(ExecutorFailed, match="executor_timeout"):
        execute(tasks["tasks"][0], "native", freeze["configurations"][0])


def test_a_timed_out_row_is_failed_with_unknown_consumption(tmp_path):
    freeze, tasks = setup(1)
    argv = write_executor(tmp_path, "import time; time.sleep(30)")
    report = run(tasks, freeze, command_executor(argv, tasks, timeout=0.2), tmp_path / "j.jsonl")
    assert {r["status"] for r in report["rows"]} == {"failed"}
    assert {r["reason"] for r in report["rows"]} == {"executor_error"}
    assert all(r["attempts"] == [] and r["success"] is None for r in report["rows"])


# --- review of PR #263 ------------------------------------------------------------


@pytest.mark.parametrize(
    "change",
    [
        dict(environment=dict(ENVIRONMENT, os="Darwin")),
        dict(artifacts=[dict(role="gateway", name="relaymind", version="1", digest=H)]),
        dict(input_manifest=dict(MANIFEST, digest="b" * 64)),
        dict(qualification="provisional"),
    ],
)
def test_a_changed_execution_identity_cannot_resume_a_journal(tmp_path, change):
    """A resumed run cannot attribute earlier rows to another environment,
    artifact set, input manifest or qualification label."""
    freeze, tasks = setup()
    journal = tmp_path / "run.jsonl"
    calls = []

    def execute(task, arm, config):
        calls.append(1)
        return captured(row(task["id"], arm))

    run(tasks, freeze, execute, journal, interrupted=lambda: len(calls) >= 2)
    arguments = dict(
        environment=ENVIRONMENT,
        artifacts=[],
        qualification="fixture",
        input_manifest=MANIFEST,
        interrupted=lambda: False,
    )
    arguments.update(change)
    with pytest.raises(JournalError, match="journal_binding"):
        run_host_plan(tasks, freeze, execute, journal, **arguments)
    assert len(calls) == 2


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_the_journal_is_private(tmp_path):
    journal = tmp_path / "new" / "run.jsonl"
    header = dict(type="header", kind="host", input_digest=H)
    with RunJournal(journal, header):
        pass
    assert journal.stat().st_mode & 0o777 == 0o600
    assert journal.parent.stat().st_mode & 0o777 == 0o700
    journal.chmod(0o644)
    with RunJournal(journal, header):
        pass
    assert journal.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("kind", ["symlink", "directory"])
def test_an_unsafe_journal_path_rejects(tmp_path, kind):
    journal = tmp_path / "run.jsonl"
    if kind == "symlink":
        (tmp_path / "target").write_text("", encoding="utf-8")
        journal.symlink_to(tmp_path / "target")
    else:
        journal.mkdir()
    with pytest.raises(JournalError, match="journal_unsafe"):
        RunJournal(journal, dict(type="header", kind="host", input_digest=H))
    assert not (tmp_path / "run.jsonl.lock").exists()


@pytest.mark.parametrize(
    "code, outcome, calls",
    [
        ("cancelled", "cancelled", None),
        ("deadline_exceeded", "timeout", None),
        ("time_budget", "timeout", None),
        ("interrupted", "failed", None),
        ("missing_credentials", "failed", None),
        ("provider_unreachable", "failed", None),
        ("provider_timeout", "timeout", 1),
        ("provider_error", "failed", 1),
        ("invalid_tool_arguments", "failed", 1),
        ("invalid_output", "failed", 1),
        ("incomplete_output", "failed", 1),
    ],
)
def test_every_gateway_error_code_keeps_its_own_outcome(code, outcome, calls):
    """A cancelled or bounded call is not a provider failure, and a gateway-side
    stop evidences no dispatch unless a token or usage arrived."""
    events = [started(1, "c1"), finished(2, "c1", None, error=dict(code=code, message="x"))]
    (attempt,) = attempts_from_trace(events, provider="p", attempt_prefix="a-", evidence_id="t")
    assert (attempt["outcome"], attempt["calls"]) == (outcome, calls)


@pytest.mark.parametrize("code", ["cancelled", "deadline_exceeded", "time_budget", "interrupted"])
def test_a_gateway_stop_after_a_token_evidences_the_dispatch(code):
    end = finished(2, "c1", None, error=dict(code=code, message="x"))
    end["payload"]["first_token_ms"] = 12
    (attempt,) = attempts_from_trace(
        [started(1, "c1"), end], provider="p", attempt_prefix="a-", evidence_id="t"
    )
    assert attempt["calls"] == 1


def test_trace_event_objects_are_accepted():
    from relaymind.store.trace_event import validate_trace_event

    events = [validate_trace_event(e) for e in (started(1, "c1"), finished(2, "c1", USAGE))]
    (attempt,) = attempts_from_trace(events, provider="p", attempt_prefix="a-", evidence_id="t")
    assert attempt["input_tokens"] == 100


def test_an_acceptance_set_missing_required_lanes_is_not_eligible():
    lanes = ("en_to_en", "zh_hans_to_zh_hans", "zh_hans_to_en", "en_to_zh_hans", "code_switch")
    freeze, tasks = setup(100, kind="acceptance", lanes=tuple(lanes[i % 5] for i in range(100)))
    assert host_preregistration(tasks, freeze)["acceptance_eligible"] is True
    freeze, tasks = setup(100, kind="acceptance")
    record = host_preregistration(tasks, freeze)
    assert record["acceptance_eligible"] is False
    assert record["reasons"] == ["missing_required_lanes"]


def test_a_flooding_executor_is_stopped_at_the_output_cap(tmp_path, monkeypatch):
    from relaymind.evaluation import asset_host_runner

    monkeypatch.setattr(asset_host_runner, "EXECUTOR_OUTPUT_BYTES", 1024 * 1024)
    freeze, tasks = setup(1)
    argv = write_executor(tmp_path, "import sys\nwhile True:\n    sys.stdout.write('x' * 65536)")
    execute = command_executor(argv, tasks, timeout=30)
    with pytest.raises(ExecutorFailed, match="executor_output"):
        execute(tasks["tasks"][0], "native", freeze["configurations"][0])


_DESCENDANT = """
import subprocess, sys, time
subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"])
time.sleep({parent})
"""


def test_a_descendant_holding_stdout_cannot_block_a_timed_out_executor(tmp_path):
    """Review of PR #263: a timeout kills the executor's whole process group.

    The descendant inherits standard output and outlives the direct child; the
    call must still return in bounded time and the row must be recorded.
    """
    import time

    freeze, tasks = setup(1)
    argv = write_executor(tmp_path, _DESCENDANT.format(parent=30))
    started = time.monotonic()
    report = run(tasks, freeze, command_executor(argv, tasks, timeout=0.2), tmp_path / "j.jsonl")
    elapsed = time.monotonic() - started
    assert elapsed < 0.2 * len(report["rows"]) + 2.0
    assert {r["status"] for r in report["rows"]} == {"failed"}
    assert {r["reason"] for r in report["rows"]} == {"executor_error"}


def test_a_descendant_holding_stdout_after_the_executor_exits_is_bounded(tmp_path):
    """Review of PR #263: an exited executor whose descendant keeps standard
    output open is unusable output, reported in bounded time."""
    import time

    freeze, tasks = setup(1)
    argv = write_executor(tmp_path, _DESCENDANT.format(parent=0))
    execute = command_executor(argv, tasks, timeout=0.5)
    started = time.monotonic()
    with pytest.raises(ExecutorFailed, match="executor_output"):
        execute(tasks["tasks"][0], "native", freeze["configurations"][0])
    assert time.monotonic() - started < 0.5 + 2.0


# --- review fixes -----------------------------------------------------------------


def test_an_executor_not_run_row_is_an_invalid_row(tmp_path):
    """Spec 0.65 section 12.6.5 (RM44; AC36): not_run means execution did not
    start, but the runner journaled the row as started before it called the
    executor, so that started work is failed with unknown consumption."""
    freeze, tasks = setup(1)

    def execute(task, arm, config):
        return _not_run(task["id"], arm, "interrupted"), []

    report = run(tasks, freeze, execute, tmp_path / "run.jsonl")
    assert {r["status"] for r in report["rows"]} == {"failed"}
    assert {r["reason"] for r in report["rows"]} == {"executor_invalid_row"}


@pytest.mark.parametrize("partial", [b"", b'{"format":"relaymind.s3_run'])
def test_a_journal_without_a_complete_header_is_started_again(tmp_path, partial):
    """A crash while the header was written leaves no complete record, so no row
    can have started: the journal is initialized again (section 12.6.5; AC36)."""
    journal = tmp_path / "run.jsonl"
    journal.write_bytes(partial)
    header = dict(type="header", kind="host", input_digest=H)
    with RunJournal(journal, header) as j:
        assert j.started == set() and j.finished == {}
        j.start("1")
    with RunJournal(journal, header) as j:
        assert j.started == {"1"}


@pytest.mark.parametrize("code", ["cancelled", "deadline_exceeded", "time_budget", "interrupted"])
def test_a_usage_object_with_no_known_count_is_no_dispatch_evidence(code):
    """Section 12.6.5 (RM16; AC36): calls=1 requires actual dispatch evidence;
    a usage object whose counts are all null evidences nothing."""
    usage = dict(input_tokens=None, output_tokens=None)
    end = finished(2, "c1", usage, error=dict(code=code, message="x"))
    (attempt,) = attempts_from_trace(
        [started(1, "c1"), end], provider="p", attempt_prefix="a-", evidence_id="t"
    )
    assert attempt["calls"] is None and attempt["usage_provenance"] == "unknown"


@pytest.mark.parametrize("evidence", ["token", "usage"])
def test_a_transport_failure_after_a_token_or_usage_evidences_the_dispatch(evidence):
    """provider_unreachable also covers transport errors after streaming began
    (section 12.6.5; RM16; AC36): a received token or reported usage is dispatch."""
    usage = USAGE if evidence == "usage" else None
    end = finished(2, "c1", usage, error=dict(code="provider_unreachable", message="x"))
    if evidence == "token":
        end["payload"]["first_token_ms"] = 12
    (attempt,) = attempts_from_trace(
        [started(1, "c1"), end], provider="p", attempt_prefix="a-", evidence_id="t"
    )
    assert (attempt["outcome"], attempt["calls"]) == ("failed", 1)


def test_missing_credentials_never_evidences_a_dispatch():
    end = finished(2, "c1", None, error=dict(code="missing_credentials", message="x"))
    end["payload"]["first_token_ms"] = 12
    (attempt,) = attempts_from_trace(
        [started(1, "c1"), end], provider="p", attempt_prefix="a-", evidence_id="t"
    )
    assert attempt["calls"] is None


# --- review 5729343917 -------------------------------------------------------------


def test_a_missing_taskkill_still_kills_the_executor(monkeypatch):
    """Review 4436631691: a taskkill that cannot start never skips process.kill()."""
    from relaymind.evaluation import asset_host_runner

    killed = []

    class Process:
        pid = 4242

        def kill(self):
            killed.append(True)

    def missing(*args, **kwargs):
        raise FileNotFoundError("taskkill")

    monkeypatch.setattr(asset_host_runner.sys, "platform", "win32")
    monkeypatch.setattr(asset_host_runner.subprocess, "run", missing)
    asset_host_runner._kill_group(Process())
    assert killed == [True]


@pytest.mark.parametrize("drop", ["row", "evidence"])
def test_a_malformed_journaled_row_rejects_with_a_fixed_reason(tmp_path, drop):
    """Review 4436631695: a finished entry without row or evidence is rejected, not a KeyError."""
    from relaymind.canonical import canonical_json

    freeze, tasks = setup(1)
    journal = tmp_path / "run.jsonl"
    run(tasks, freeze, lambda t, a, c: captured(row(t["id"], a)), journal)
    lines = journal.read_bytes().rstrip(b"\n").split(b"\n")
    records = [json.loads(line) for line in lines]
    finished = next(r for r in records if r["type"] == "finished")
    del finished["value"][drop]
    journal.write_bytes(b"".join((canonical_json(r) + "\n").encode() for r in records))
    with pytest.raises(WholeTaskRejected, match="executor_output"):
        run(tasks, freeze, lambda t, a, c: captured(row(t["id"], a)), journal)


def test_relationship_evidence_needs_a_named_token_relationship():
    """Review 4436631701: relationship evidence is never recorded under an unknown relationship."""
    events = [started(1, "c1"), finished(2, "c1", USAGE)]
    with pytest.raises(ValueError, match="token_relationship"):
        attempts_from_trace(
            events,
            provider="p",
            attempt_prefix="a-",
            evidence_id="trace",
            relationship_evidence_id="provider-usage-doc",
        )


def test_a_journal_directory_created_concurrently_is_used(tmp_path, monkeypatch):
    """Review 4436631707: a parent created by another runner after the check is not an error."""
    import pathlib

    journal = tmp_path / "new" / "run.jsonl"
    journal.parent.mkdir(mode=0o700)
    real = pathlib.Path.exists
    monkeypatch.setattr(pathlib.Path, "exists", lambda p: False if p == journal.parent else real(p))
    header = dict(type="header", kind="host", input_digest=H)
    with RunJournal(journal, header):
        pass
    monkeypatch.undo()
    assert journal.is_file()


def test_a_failed_journal_open_closes_its_file(tmp_path, monkeypatch):
    """Review 4436631717: the journal file handle is closed when opening fails."""
    opened = []

    def failing(self, record):
        opened.append(self._file)
        raise OSError("no space")

    monkeypatch.setattr(RunJournal, "_append", failing)
    journal = tmp_path / "run.jsonl"
    with pytest.raises(OSError, match="no space"):
        RunJournal(journal, dict(type="header", kind="host", input_digest=H))
    assert opened and opened[0].closed
    assert not (tmp_path / "run.jsonl.lock").exists()


@pytest.mark.parametrize("key", [1, None, ("1",)])
def test_a_journal_key_must_be_a_string(tmp_path, key):
    """Review 4436631720: a key the journal could not reopen is never written."""
    journal = tmp_path / "run.jsonl"
    header = dict(type="header", kind="host", input_digest=H)
    with RunJournal(journal, header) as j:
        before = journal.read_bytes()
        with pytest.raises(JournalError, match="journal_corrupt"):
            j.start(key)
        j.start("1")
        after_start = journal.read_bytes()
        with pytest.raises(JournalError, match="journal_corrupt"):
            j.finish(key, dict(value=1))
        assert journal.read_bytes() == after_start != before
    with RunJournal(journal, header) as j:
        assert j.started == {"1"}


@pytest.mark.parametrize("ref", [None, 123, "ev", ["id", "x"]])
def test_a_non_object_journaled_evidence_ref_rejects_with_a_fixed_reason(tmp_path, ref):
    """Review 4437319307: a scalar evidence ref is executor_output, never a TypeError."""
    from relaymind.canonical import canonical_json

    freeze, tasks = setup(1)
    journal = tmp_path / "run.jsonl"
    run(tasks, freeze, lambda t, a, c: captured(row(t["id"], a)), journal)
    records = [json.loads(line) for line in journal.read_bytes().rstrip(b"\n").split(b"\n")]
    finished_record = next(r for r in records if r["type"] == "finished")
    finished_record["value"]["evidence"].append(ref)
    journal.write_bytes(b"".join((canonical_json(r) + "\n").encode() for r in records))
    with pytest.raises(WholeTaskRejected, match="executor_output"):
        run(tasks, freeze, lambda t, a, c: captured(row(t["id"], a)), journal)


def test_a_broken_pipe_on_the_executor_input_still_closes_it(tmp_path, monkeypatch):
    """Review 4437319309: stdin is closed even when writing the request fails."""
    import time

    from relaymind.evaluation import asset_host_runner

    class BrokenStdin:
        closed = False

        def write(self, data):
            raise BrokenPipeError("reader gone")

        def close(self):
            self.closed = True

    stdin = BrokenStdin()
    real = asset_host_runner._start_group

    def start(argv):
        process = real(argv)
        process.stdin.close()
        process.stdin = stdin
        return process

    monkeypatch.setattr(asset_host_runner, "_start_group", start)
    argv = write_executor(tmp_path, "pass")
    assert asset_host_runner._run_program(argv, b"{}", 10) == b""
    deadline = time.monotonic() + 5
    while not stdin.closed and time.monotonic() < deadline:
        time.sleep(0.01)
    assert stdin.closed


@pytest.mark.parametrize("error", ["boom", dict(message="no code"), [1]])
def test_a_malformed_model_error_is_an_invalid_trace(error):
    """Review 4437319313: a malformed model.finished error rejects with a fixed reason."""
    from relaymind.evaluation.asset_host_runner import _outcome

    end = finished(2, "c1", None, error=dict(code="provider_error", message="x"))
    end["payload"]["error"] = error
    with pytest.raises(ValueError, match="trace_error"):
        _outcome(end)


def test_a_failed_lock_write_leaves_no_stale_lock(tmp_path, monkeypatch):
    """Review 4437319316: a failed PID write removes the lock so a later run can open."""
    from relaymind.evaluation import asset_run_journal

    real = asset_run_journal.os.fdopen

    def failing(fd, *args, **kwargs):
        os_file = real(fd, *args, **kwargs)
        os_file.close()
        raise OSError("no space")

    monkeypatch.setattr(asset_run_journal.os, "fdopen", failing)
    journal = tmp_path / "run.jsonl"
    header = dict(type="header", kind="host", input_digest=H)
    with pytest.raises(OSError, match="no space"):
        RunJournal(journal, header)
    monkeypatch.undo()
    assert not (tmp_path / "run.jsonl.lock").exists()
    with RunJournal(journal, header):
        pass
