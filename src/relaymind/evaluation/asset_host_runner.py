"""Preregistration, resumable host runner and external cost capture (#237).

Spec 0.65 section 12.6.5 (RM16, RM40–RM44; AC31–AC36). This is operator tooling
around the existing conserving driver and grader in ``asset_whole_task``:

* ``host_preregistration`` checks the frozen task set against its freeze and
  reports the digests a run is bound to (task document, freeze and plan) and
  whether the set is eligible for the final acceptance gate at all.
* ``run_host_plan`` executes the frozen plan through a caller-supplied host
  executor with a crash-safe run journal. Completed rows are kept and never
  rerun; a row whose execution started but was never recorded is exported as
  cancelled with unknown consumption, never rerun or fabricated; rows never
  started stay ``not_run``.
* ``command_executor`` adapts an external host executor program.
* ``attempts_from_trace`` turns the RelayMind gateway's own external model call
  events into HostAttempt objects for the provider arms.

Nothing here runs a host, executes a tool, grants an approval or establishes a
qualified result: a caller cannot flag a run ``qualified``.
"""

import copy
import os
import signal
import subprocess
import sys
import threading
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence

from relaymind.canonical import canonical_digest
from relaymind.evaluation.asset_benchmark_io import REPORT_BYTES, json_bytes
from relaymind.evaluation.asset_corpus_io import AssetCorpusError, parse_json
from relaymind.evaluation.asset_judgments import LANES
from relaymind.evaluation.asset_run_journal import RunJournal, bound_header
from relaymind.evaluation.asset_whole_task import (
    ACCEPTANCE_TASKS,
    Executor,
    WholeTaskRejected,
    _check_row,
    _not_run,
    _utf8,
    grade_host_report,
    host_summary,
    plan_host_trial,
    validate_whole_task,
    verify_host_tasks,
)

EXECUTOR_REQUEST = "relaymind.host_executor_request.v1"
REQUIRED_LANES = tuple(lane for lane in LANES if lane != "zh_hant_diagnostic")
EXECUTOR_OUTPUT_BYTES = REPORT_BYTES
_CHUNK = 64 * 1024
# Seconds the reader may take to finish after the program exits.
_DRAIN_GRACE = 0.5
RUN_QUALIFICATIONS = ("fixture", "provisional")
TOKEN_RELATIONSHIPS = (
    "component_disjoint",
    "cached_within_input_reasoning_within_output",
    "unknown",
)


class ExecutorFailed(Exception):
    """The host executor returned no usable row; its consumption is unknown."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# --- preregistration --------------------------------------------------------------


def host_preregistration(tasks: Mapping, freeze: Mapping) -> dict:
    """The digests a host run is bound to, and its acceptance eligibility.

    Section 12.6.5: at least 100 preregistered paired tasks are required for
    acceptance and an early trial never asserts the full gate through a smaller
    set. AC36 pairs the whole tasks with the five required language lanes and
    RM44 reports per-language tradeoffs, so a set missing a required lane is not
    eligible; the spec states no per-lane host-task count, so one task per
    lane is the only lane condition checked here.
    """
    input_digest = verify_host_tasks(tasks, freeze)
    plan = plan_host_trial(tasks)
    lanes = Counter(task["lane"] for task in tasks["tasks"])
    reasons = []
    if tasks["kind"] != "acceptance":
        reasons.append("kind_not_acceptance")
    if len(tasks["tasks"]) < ACCEPTANCE_TASKS:
        reasons.append("below_minimum_tasks")
    missing = [lane for lane in REQUIRED_LANES if lane not in lanes]
    if missing:
        reasons.append("missing_required_lanes")
    return dict(
        kind=tasks["kind"],
        input_digest=input_digest,
        freeze_digest=tasks["freeze_digest"],
        plan_digest=canonical_digest(plan),
        tasks=len(tasks["tasks"]),
        planned_rows=len(plan),
        lanes=[dict(lane=lane, tasks=lanes[lane]) for lane in sorted(lanes, key=_utf8)],
        missing_lanes=missing,
        acceptance_eligible=not reasons,
        reasons=reasons,
    )


# --- runner -----------------------------------------------------------------------


def _unknown_row(task_id: str, arm: str, status: str, reason: str) -> dict:
    """Started work whose consumption was never recorded: no invented attempts."""
    row = _not_run(task_id, arm, reason)
    row["status"] = status
    return row


def _admit(row, refs, planned, config, seen: set, evidence: Mapping) -> tuple[dict, list]:
    """Check one executor row before it is journaled; raise on any contradiction."""
    if not isinstance(row, Mapping) or not isinstance(refs, list):
        raise WholeTaskRejected("executor_output")
    row = validate_whole_task("HostRow", copy.deepcopy(dict(row)))
    if (row["task_id"], row["arm"]) != (planned["task_id"], planned["arm"]):
        raise WholeTaskRejected("row_plan")
    if row["status"] == "not_run":
        # The row was journaled as started before the executor ran; not_run
        # means execution did not start, so it contradicts that record.
        raise WholeTaskRejected("row_started")
    refs = [validate_whole_task("EvidenceRef", copy.deepcopy(dict(r))) for r in refs]
    ids = [r["id"] for r in refs]
    if len(set(ids)) != len(ids):
        raise WholeTaskRejected("evidence_conflict")
    nested = set(row["evidence_ids"]) | {i for a in row["attempts"] for i in a["evidence_ids"]}
    if not nested <= set(ids):
        raise WholeTaskRejected("evidence_dangling")
    if any(evidence.get(r["id"], r) != r for r in refs):
        raise WholeTaskRejected("evidence_conflict")
    trial = set(seen)
    _check_row(row, config, trial)
    seen |= trial
    return row, refs


def run_host_plan(
    tasks: Mapping,
    freeze: Mapping,
    execute: Executor,
    journal_path,
    *,
    environment: Mapping,
    artifacts: Sequence[Mapping],
    qualification: str,
    input_manifest: Mapping,
    interrupted: Callable[[], bool],
) -> dict:
    """Execute the preregistered plan with crash-safe, resumable progress.

    Returns the AssetHostReportV1, exactly as the conserving driver would,
    after the grader has verified it (without the companion registry, which
    the offline grade-host command resolves).
    """
    if qualification not in RUN_QUALIFICATIONS:
        # Qualified needs resolved actual evidence, never a caller flag.
        raise WholeTaskRejected("qualification")
    record = host_preregistration(tasks, freeze)
    by_id = {t["id"]: t for t in tasks["tasks"]}
    configs = {c["arm"]: c for c in freeze["configurations"]}
    plan = plan_host_trial(tasks)
    rows: dict[int, dict] = {}
    evidence: dict[str, dict] = {}
    seen: set[str] = set()
    identity = dict(
        environment=environment,
        artifacts=list(artifacts),
        input_manifest=input_manifest,
        qualification=qualification,
    )
    with RunJournal(journal_path, bound_header("host", record, identity)) as journal:
        for planned in plan:
            key, ordinal = str(planned["ordinal"]), planned["ordinal"]
            task_id, arm = planned["task_id"], planned["arm"]
            config = configs[arm]
            if key in journal.finished:
                value = journal.finished[key]
                row, refs = _admit(value["row"], value["evidence"], planned, config, seen, evidence)
            elif key in journal.started:
                # Started before a crash and never recorded: kept, not rerun.
                row, refs = _unknown_row(task_id, arm, "cancelled", "interrupted_unrecorded"), []
                journal.finish(key, dict(row=row, evidence=refs))
            elif interrupted():
                break
            else:
                journal.start(key)
                try:
                    output, output_refs = execute(by_id[task_id], arm, copy.deepcopy(config))
                except ExecutorFailed:
                    row, refs = _unknown_row(task_id, arm, "failed", "executor_error"), []
                else:
                    try:
                        row, refs = _admit(output, output_refs, planned, config, seen, evidence)
                    except (WholeTaskRejected, ValueError, KeyError, TypeError):
                        row, refs = _unknown_row(task_id, arm, "failed", "executor_invalid_row"), []
                journal.finish(key, dict(row=row, evidence=refs))
            rows[ordinal] = row
            for ref in refs:
                evidence[ref["id"]] = ref
    report = dict(
        schema_version=1,
        kind=tasks["kind"],
        qualification=qualification,
        input_digest=record["input_digest"],
        freeze_digest=tasks["freeze_digest"],
        input_manifest=dict(input_manifest),
        environment=dict(environment),
        artifacts=[dict(a) for a in artifacts],
        planned=plan,
        rows=[
            rows.get(p["ordinal"]) or _not_run(p["task_id"], p["arm"], "interrupted") for p in plan
        ],
        summary=None,
        evidence=[evidence[i] for i in sorted(evidence, key=_utf8)],
    )
    report["summary"] = host_summary(report, tasks, freeze)
    grade_host_report(report, tasks, freeze)
    return report


def _start_group(argv: list[str]) -> subprocess.Popen:
    """Start the executor as the leader of its own process group."""
    if sys.platform == "win32":
        group = dict(creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    else:
        group = dict(start_new_session=True)
    return subprocess.Popen(  # noqa: S603 - the operator's own executor program
        argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, **group
    )


def _kill_group(process: subprocess.Popen) -> None:
    """Kill the executor and every descendant still in its process group."""
    if sys.platform == "win32":
        subprocess.run(  # noqa: S603, S607 - the system tool, fixed arguments
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass  # The group is already gone.
    process.kill()


def _run_program(argv: list[str], payload: bytes, timeout: float | None) -> bytes:
    """Run one executor program; its output is bounded while it is read.

    The program leads its own process group. A timeout or an output flood
    kills the whole group, and a descendant still holding standard output
    after the program exits is killed too. The reader thread reads the raw
    descriptor, so cleanup never waits behind it: a reader still blocked
    after a bounded join is abandoned, and its descriptor is not closed.
    """
    try:
        process = _start_group(argv)
    except OSError:
        raise ExecutorFailed("executor_start") from None
    chunks: list[bytes] = []
    flooded = threading.Event()
    fd = process.stdout.fileno()

    def feed() -> None:
        try:
            process.stdin.write(payload)
            process.stdin.close()
        except OSError:
            pass  # The program stopped reading; its exit status decides.

    def drain() -> None:
        size = 0
        try:
            while chunk := os.read(fd, _CHUNK):
                size += len(chunk)
                if size > EXECUTOR_OUTPUT_BYTES:
                    flooded.set()
                    _kill_group(process)
                    break
                chunks.append(chunk)
        except OSError:
            pass  # A failed read is incomplete output; the alive check decides.

    reader = threading.Thread(target=drain, daemon=True)
    threading.Thread(target=feed, daemon=True).start()
    reader.start()
    timed_out = False
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
    except BaseException:
        # The operator stopped the runner (a second signal): never leave the
        # executor's group running behind it.
        _kill_group(process)
        process.wait()
        raise
    if timed_out or flooded.is_set():
        _kill_group(process)
    process.wait()
    reader.join(_DRAIN_GRACE)
    if reader.is_alive():
        # A descendant still holds standard output: stop it, then give up.
        _kill_group(process)
        reader.join(_DRAIN_GRACE)
    if not reader.is_alive():
        process.stdout.close()
    if timed_out:
        raise ExecutorFailed("executor_timeout")
    if flooded.is_set() or reader.is_alive():
        raise ExecutorFailed("executor_output")
    if process.returncode != 0:
        raise ExecutorFailed("executor_exit")
    return b"".join(chunks)


def command_executor(
    argv: Sequence[str], tasks: Mapping, *, timeout: float | None = None
) -> Executor:
    """A host executor program: one planned row per invocation.

    The program reads ``{format, ordinal, task, arm, arm_config}`` as JSON on
    standard input, drives the real host for that task and arm, and writes
    ``{row, evidence}`` (a HostRow and its EvidenceRefs) on standard output.
    Standard error is passed through. A nonzero exit or unusable output is
    ExecutorFailed: the row is then recorded as failed with unknown consumption.

    ``timeout`` (seconds, default none) is a harness bound, not a task budget:
    a program still running when it expires is killed and the row is failed
    with unknown consumption, never recorded as a host task timeout. Set it
    above the frozen task budgets; it never replaces or changes them. The
    program leads its own process group, and the whole group is killed on a
    timeout; a descendant left holding standard output after the program exits
    is killed too, and that output is unusable (``executor_output``).
    Standard output is read incrementally and the program is killed as soon as
    it exceeds the report size bound (``executor_output``).
    """
    argv = [str(a) for a in argv]
    ordinals = {(p["task_id"], p["arm"]): p["ordinal"] for p in plan_host_trial(tasks)}

    def execute(task: Mapping, arm: str, config: Mapping):
        request = dict(
            format=EXECUTOR_REQUEST,
            ordinal=ordinals[(task["id"], arm)],
            task=dict(task),
            arm=arm,
            arm_config=dict(config),
        )
        stdout = _run_program(argv, json_bytes(request), timeout)
        try:
            reply = parse_json(stdout)
        except AssetCorpusError:
            raise ExecutorFailed("executor_output") from None
        if set(reply) != {"row", "evidence"}:
            raise ExecutorFailed("executor_output")
        return reply["row"], reply["evidence"]

    return execute


# --- external cost capture --------------------------------------------------------

_COUNTS = ("input_tokens", "output_tokens", "cached_tokens", "reasoning_tokens")


# Gateway-side stops of a call: the gateway cancelled it or a gateway bound
# (placement deadline, time budget) ended it, or the run was interrupted. They
# are not provider failures, and they alone do not show that the request
# reached the provider.
_GATEWAY_STOPS = {
    "cancelled": "cancelled",
    "deadline_exceeded": "timeout",
    "time_budget": "timeout",
    "interrupted": "failed",
}
# The request was never sent: no dispatch.
_NOT_DISPATCHED = {"missing_credentials"}
# A transport error: a connection failure before the request was sent, or a
# read/protocol error after streaming began. It alone shows no dispatch.
_TRANSPORT = {"provider_unreachable": "failed"}


def _received(payload: Mapping) -> bool:
    """A received token or a known reported count: evidence of dispatch."""
    usage = payload.get("usage") or {}
    return payload.get("first_token_ms") is not None or any(
        usage.get(k) is not None for k in _COUNTS
    )


def _outcome(finished: Mapping | None) -> tuple[str, int | None]:
    """The attempt outcome and its evidenced call count."""
    if finished is None:
        # Started without a recorded finish: neither dispatch nor usage known.
        return "unknown", None
    payload = finished["payload"]
    error = payload.get("error")
    if error is None:
        return "success", 1
    code = error["code"]
    if code in _NOT_DISPATCHED:
        return "failed", None
    stops = _GATEWAY_STOPS | _TRANSPORT
    if code in stops:
        # Dispatch is evidenced only by a received token or reported usage.
        return stops[code], 1 if _received(payload) else None
    if code == "provider_timeout":
        return "timeout", 1
    # A provider error or an invalid/incomplete response came from the provider.
    return "failed", 1


def attempts_from_trace(
    events: Iterable[Mapping],
    *,
    provider: str,
    attempt_prefix: str,
    evidence_id: str,
    token_relationship: str = "unknown",
    relationship_evidence_id: str | None = None,
    first_ordinal: int = 1,
) -> list[dict]:
    """HostAttempts for every external model call in RelayMind trace events.

    ``events`` are the trace events of one or more gateway runs in their
    recorded order, as ``TraceEvent`` instances or as the JSON objects of
    ``TraceEvent.to_json_object()`` (the native API and store export shape).
    ``evidence_id`` names the registered export holding them, which is the
    dispatch evidence. Counts are those the provider reported to
    the gateway (``reported``); absent counts stay null, never zero. The
    gateway does not record a total, and the component relationship is unknown
    unless the operator names evidence for it. Local model calls are local cost
    and are not attempts here.
    """
    from relaymind.store.trace_event import validate_trace_event

    if token_relationship not in TOKEN_RELATIONSHIPS:
        raise ValueError("token_relationship")
    if token_relationship != "unknown" and relationship_evidence_id is None:
        raise ValueError("relationship_evidence")
    checked = []
    last: dict[str, int] = {}
    for item in events:
        if hasattr(item, "to_json_object"):
            item = item.to_json_object()
        data = validate_trace_event(dict(item)).to_json_object()
        if data["seq"] <= last.get(data["run_id"], 0):
            raise ValueError("trace_order")
        last[data["run_id"]] = data["seq"]
        checked.append(data)
    started_calls: dict[tuple, Mapping] = {}
    finished: dict[tuple, Mapping] = {}
    for e in checked:
        if e["actor"] != "external" or e["event_type"] not in ("model.started", "model.finished"):
            continue
        call = (e["run_id"], e["payload"]["call_id"])
        own = started_calls if e["event_type"] == "model.started" else finished
        if call in own:
            # A repeated receipt is not a new attempt; the trace is contradictory.
            raise ValueError("trace_duplicate_call")
        if own is finished and call not in started_calls:
            # A finish must follow its own start in the same run.
            raise ValueError("trace_order")
        own[call] = e
    evidence_ids = sorted({evidence_id, relationship_evidence_id} - {None}, key=_utf8)
    attempts = []
    for start in checked:
        if start["event_type"] != "model.started" or start["actor"] != "external":
            continue
        end = finished.get((start["run_id"], start["payload"]["call_id"]))
        outcome, calls = _outcome(end)
        usage = None if end is None else end["payload"].get("usage")
        counts = {k: None if usage is None else usage.get(k) for k in _COUNTS}
        known = any(v is not None for v in counts.values())
        attempts.append(
            dict(
                id=f"{attempt_prefix}{first_ordinal + len(attempts)}",
                ordinal=first_ordinal + len(attempts),
                model=start["payload"]["model"],
                provider=provider,
                external=True,
                started_at=start["timestamp"],
                ended_at=None if end is None else end["timestamp"],
                **counts,
                total_tokens=None,
                token_relationship=token_relationship,
                calls=calls,
                usage_provenance="reported" if known else "unknown",
                evidence_ids=evidence_ids,
                outcome=outcome,
            )
        )
    for attempt in attempts:
        validate_whole_task("HostAttempt", attempt)
    return attempts
