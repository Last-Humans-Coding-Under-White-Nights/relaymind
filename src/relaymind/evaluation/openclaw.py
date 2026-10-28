"""The OpenClaw integration evaluation: frozen tasks, grading, and the arm summary.

Spec 0.50 section 12.5 (#139; RM16, RM25, RM26, RM33; AC24). The tasks are one
versioned file (``evaluation/openclaw_tasks.v1.json``) frozen by the SHA-256
digest of its canonical JSON (RM07) in ``evaluation/openclaw_tasks.v1.lock.json``.
It is not the managed-skill corpus of section 12.2: its tasks run through the
host, and a corpus score is never a host task result.

One attempt is one task in one arm and one repeat, as the runner
(``scripts/run_openclaw_eval.py``) records it: the host's exit status and its
own JSON report, the sandbox after the attempt, the plugin's diagnostics
records, and the gateway runs of the attempt's window with their trace events.
The host's ``ok`` never decides a pass (the pinned host reports ``ok: true``
for a denied tool), a host failover is counted apart, a proposal alone is not
a completed task, and an unknown token count stays unknown.
"""

import hashlib
import json
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from relaymind.canonical import canonical_digest
from relaymind.evaluation.metrics import nearest_rank

TASKS_FILE = "evaluation/openclaw_tasks.v1.json"
LOCK_FILE = "evaluation/openclaw_tasks.v1.lock.json"
SCHEMA_VERSION = 1
ARMS = ("direct", "relaymind_external_only", "relaymind_auto", "relaymind_no_local")
RELAYMIND_ARMS = ARMS[1:]
# The plugin's provider and alias as the host's JSON report names them.
RELAYMIND_HOST_MODEL = {"provider": "relaymind", "model": "relaymind-client-tools"}
KINDS = ("text", "host_tool")
LANGUAGES = ("en", "zh")
# A headless ask policy fails closed without a Gateway (macOS probe), so it is not used.
EXEC_POLICIES = ("deny", "full")
# The tools the pinned host offered on every request of the macOS probe.
HOST_TOOLS = frozenset(
    {
        "apply_patch",
        "edit",
        "exec",
        "ls",
        "process",
        "read",
        "sessions_yield",
        "tool_call",
        "tool_describe",
        "tool_search",
        "write",
    }
)
ANY_VALUE = "*"
MATCHERS = ("exact", "suffix", "contains")
# The host's exit status after SIGINT (macOS probe and qualification).
HOST_INTERRUPT_EXIT = 130
COMPARISONS = {
    "integration_overhead": ("relaymind_external_only", "direct"),
    "local_inference_auto": ("relaymind_auto", "relaymind_external_only"),
    "local_inference_no_local": ("relaymind_no_local", "relaymind_external_only"),
}

_DOCUMENT_FIELDS = (
    "schema_version",
    "suite",
    "version",
    "frozen_on",
    "note",
    "chinese_review",
    "host",
    "arms",
    "repeats",
    "sampling_seed",
    "time_limit_seconds",
    "tasks",
)
_HOST_FIELDS = ("release", "command", "options")
_TASK_FIELDS = (
    "id",
    "kind",
    "language",
    "best_effort",
    "english_task",
    "prompt",
    "sandbox",
    "exec_policy",
    "expected_tools",
    "expected_sandbox",
    "required_evidence",
    "forbidden_evidence",
    "rubric",
)
_SANDBOX_EXPECTATION_FIELDS = ("absent", "lines", "contains")


class TaskSetError(ValueError):
    """The task file breaks the section 12.5 fixture contract or its frozen digest."""


@dataclass(frozen=True)
class Task:
    task_id: str
    kind: str
    language: str
    best_effort: bool
    english_task: str | None
    prompt: str
    sandbox: Mapping[str, str]
    exec_policy: str
    # Groups of acceptable calls; each group needs one of its calls.
    expected_tools: tuple[tuple[Mapping[str, Any], ...], ...]
    expected_sandbox: Mapping[str, Mapping[str, Any]]
    required_evidence: tuple[tuple[str, ...], ...]
    forbidden_evidence: tuple[str, ...]
    rubric: str


@dataclass(frozen=True)
class TaskSet:
    document: Mapping[str, Any]
    tasks: tuple[Task, ...]
    digest: str

    @property
    def arms(self) -> tuple[str, ...]:
        return tuple(self.document["arms"])

    @property
    def repeats(self) -> int:
        return self.document["repeats"]

    @property
    def sampling_seed(self) -> int:
        return self.document["sampling_seed"]

    @property
    def time_limit_seconds(self) -> float:
        return self.document["time_limit_seconds"]

    @property
    def host(self) -> Mapping[str, Any]:
        return self.document["host"]

    def task(self, task_id: str) -> Task:
        for task in self.tasks:
            if task.task_id == task_id:
                return task
        raise KeyError(task_id)


@dataclass(frozen=True)
class Slot:
    """One place in the frozen sampling order."""

    index: int
    repeat: int
    task_id: str
    arm: str


# -- loading and freezing --------------------------------------------------------------


def load_tasks(path: Path) -> TaskSet:
    return parse_tasks(json.loads(Path(path).read_text("utf-8")))


def _fields(where: str, data: object, fields: Sequence[str], required=None) -> Mapping:
    if not isinstance(data, Mapping):
        raise TaskSetError(f"{where}: expected an object")
    unknown = sorted(set(data) - set(fields))
    if unknown:
        raise TaskSetError(f"{where}: unknown field(s) {', '.join(unknown)}")
    missing = sorted(set(fields if required is None else required) - set(data))
    if missing:
        raise TaskSetError(f"{where}: missing field(s) {', '.join(missing)}")
    return data


def _text(where: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TaskSetError(f"{where}: expected a non-blank string")
    return value


def _sandbox_name(where: str, name: object) -> str:
    name = _text(where, name)
    if "/" in name or "\\" in name or name in (".", "..") or name.startswith("."):
        raise TaskSetError(f"{where}: sandbox file {name!r} must be a plain name in the sandbox")
    return name


def _pattern(where: str, value: object) -> Any:
    if value == ANY_VALUE:
        return value
    if (
        isinstance(value, Mapping)
        and len(value) == 1
        and next(iter(value)) in MATCHERS
        and isinstance(next(iter(value.values())), str)
    ):
        return dict(value)
    raise TaskSetError(f"{where}: an argument pattern is '*' or one of {', '.join(MATCHERS)}")


def _call(where: str, raw: object) -> dict[str, Any]:
    raw = _fields(where, raw, ("tool", "arguments"))
    tool = _text(f"{where}.tool", raw["tool"])
    if tool not in HOST_TOOLS:
        raise TaskSetError(f"{where}: {tool!r} is not a host tool of the pinned release")
    arguments = raw["arguments"]
    if not isinstance(arguments, Mapping):
        raise TaskSetError(f"{where}.arguments: expected an object")
    return {
        "tool": tool,
        "arguments": {k: _pattern(f"{where}.arguments.{k}", v) for k, v in arguments.items()},
    }


def _parse_task(index: int, raw: object) -> Task:
    where = f"tasks[{index}]"
    raw = _fields(where, raw, _TASK_FIELDS)
    task_id = _text(f"{where}.id", raw["id"])
    where = f"task {task_id}"
    if raw["kind"] not in KINDS:
        raise TaskSetError(f"{where}: kind must be one of {', '.join(KINDS)}")
    if raw["language"] not in LANGUAGES:
        raise TaskSetError(f"{where}: language must be one of {', '.join(LANGUAGES)}")
    if raw["exec_policy"] not in EXEC_POLICIES:
        raise TaskSetError(f"{where}: exec_policy must be one of {', '.join(EXEC_POLICIES)}")
    zh = raw["language"] == "zh"
    if raw["best_effort"] is not zh:
        raise TaskSetError(f"{where}: best_effort is true exactly for a zh task")
    if zh != isinstance(raw["english_task"], str):
        raise TaskSetError(f"{where}: english_task names the English task of a zh task only")
    sandbox = raw["sandbox"]
    if not isinstance(sandbox, Mapping) or not all(isinstance(v, str) for v in sandbox.values()):
        raise TaskSetError(f"{where}.sandbox: expected file names mapped to text")
    sandbox = {_sandbox_name(f"{where}.sandbox", k): v for k, v in sandbox.items()}
    groups = raw["expected_tools"]
    if not isinstance(groups, list) or not all(isinstance(g, list) and g for g in groups):
        raise TaskSetError(f"{where}.expected_tools: expected a list of non-empty groups")
    expected_tools = tuple(
        tuple(_call(f"{where}.expected_tools[{i}][{j}]", c) for j, c in enumerate(group))
        for i, group in enumerate(groups)
    )
    if (raw["kind"] == "text") != (not expected_tools):
        raise TaskSetError(f"{where}: a host_tool task expects tools and a text task none")
    expected_sandbox = {}
    if not isinstance(raw["expected_sandbox"], Mapping):
        raise TaskSetError(f"{where}.expected_sandbox: expected an object")
    for name, expectation in raw["expected_sandbox"].items():
        name = _sandbox_name(f"{where}.expected_sandbox", name)
        expectation = _fields(
            f"{where}.expected_sandbox.{name}", expectation, _SANDBOX_EXPECTATION_FIELDS, ()
        )
        expected_sandbox[name] = dict(expectation)
    required = raw["required_evidence"]
    if not isinstance(required, list) or not all(
        isinstance(g, list) and g and all(isinstance(p, str) and p for p in g) for g in required
    ):
        raise TaskSetError(f"{where}.required_evidence: expected groups of non-empty strings")
    forbidden = raw["forbidden_evidence"]
    if not isinstance(forbidden, list) or not all(isinstance(p, str) and p for p in forbidden):
        raise TaskSetError(f"{where}.forbidden_evidence: expected non-empty strings")
    return Task(
        task_id=task_id,
        kind=raw["kind"],
        language=raw["language"],
        best_effort=raw["best_effort"],
        english_task=raw["english_task"],
        prompt=_text(f"{where}.prompt", raw["prompt"]),
        sandbox=sandbox,
        exec_policy=raw["exec_policy"],
        expected_tools=expected_tools,
        expected_sandbox=expected_sandbox,
        required_evidence=tuple(tuple(g) for g in required),
        forbidden_evidence=tuple(forbidden),
        rubric=_text(f"{where}.rubric", raw["rubric"]),
    )


def parse_tasks(document: Mapping[str, Any]) -> TaskSet:
    """Check the section 12.5 fixture contract (spec 0.50); unknown fields are refused."""
    document = _fields("task file", document, _DOCUMENT_FIELDS)
    if document["schema_version"] != SCHEMA_VERSION:
        raise TaskSetError(f"schema_version must be {SCHEMA_VERSION}")
    if list(document["arms"]) != list(ARMS):
        raise TaskSetError(f"arms must be exactly {', '.join(ARMS)}")
    repeats = document["repeats"]
    if type(repeats) is not int or repeats < 1:
        raise TaskSetError("repeats must be a positive integer")
    if type(document["sampling_seed"]) is not int:
        raise TaskSetError("sampling_seed must be an integer")
    limit = document["time_limit_seconds"]
    if type(limit) not in (int, float) or limit <= 0:
        raise TaskSetError("time_limit_seconds must be positive")
    host = _fields("host", document["host"], _HOST_FIELDS)
    _text("host.release", host["release"])
    if not isinstance(host["command"], list) or not all(
        isinstance(a, str) for a in host["command"]
    ):
        raise TaskSetError("host.command must be a list of strings")
    raw_tasks = document["tasks"]
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise TaskSetError("tasks must be a non-empty list")
    tasks = tuple(_parse_task(i, raw) for i, raw in enumerate(raw_tasks))
    ids = [t.task_id for t in tasks]
    duplicates = sorted(i for i, n in Counter(ids).items() if n > 1)
    if duplicates:
        raise TaskSetError(f"duplicate task id(s) {', '.join(duplicates)}")
    english = {t.task_id: t for t in tasks if t.language == "en"}
    for task in tasks:
        if task.english_task is None:
            continue
        source = english.get(task.english_task)
        if source is None:
            raise TaskSetError(f"task {task.task_id}: english_task {task.english_task} is missing")
        if source.kind != task.kind or source.exec_policy != task.exec_policy:
            raise TaskSetError(f"task {task.task_id}: kind and exec_policy follow its English task")
    return TaskSet(dict(document), tasks, canonical_digest(document))


def sampling_order(tasks: TaskSet) -> list[Slot]:
    """The frozen order: per repeat the tasks shuffled, per task its arms shuffled.

    The shuffle ranks by SHA-256 of the seed and the item, not by Python's
    random module, whose algorithms may change between versions; the same file
    gives the same order on every machine.
    """

    def rank(*parts: object) -> str:
        return hashlib.sha256(
            ":".join(map(str, (tasks.sampling_seed, *parts))).encode()
        ).hexdigest()

    slots: list[Slot] = []
    for repeat in range(tasks.repeats):
        order = sorted(tasks.tasks, key=lambda t: rank("task", repeat, t.task_id))
        for task in order:
            arms = sorted(tasks.arms, key=lambda a: rank("arm", repeat, task.task_id, a))
            base = len(slots)
            slots.extend(Slot(base + i, repeat, task.task_id, a) for i, a in enumerate(arms))
    return slots


def _order_digest(tasks: TaskSet) -> str:
    return canonical_digest([[s.repeat, s.task_id, s.arm] for s in sampling_order(tasks)])


def lock_document(tasks: TaskSet, name: str) -> dict[str, Any]:
    """The lock content: the file, its digest, and the digest of its sampling order."""
    return {
        "tasks": name,
        "version": tasks.document["version"],
        "sha256": tasks.digest,
        "order_sha256": _order_digest(tasks),
        "frozen_on": tasks.document["frozen_on"],
    }


def check_lock(tasks: TaskSet, lock_path: Path) -> None:
    """Refuse a task file whose digest or order differs from the frozen one."""
    lock = json.loads(Path(lock_path).read_text("utf-8"))
    if lock.get("sha256") != tasks.digest:
        raise TaskSetError(
            f"task file digest {tasks.digest} differs from the frozen digest {lock.get('sha256')} "
            f"in {Path(lock_path).name}; a changed task is a new version (section 12.5)"
        )
    if lock.get("order_sha256") != _order_digest(tasks):
        raise TaskSetError(
            "the sampling order differs from the frozen order; a changed order is a new version"
        )


# -- matching --------------------------------------------------------------------------


def argument_matches(pattern: Any, value: Any) -> bool:
    if pattern == ANY_VALUE:
        return True
    ((kind, expected),) = pattern.items()
    if not isinstance(value, str):
        return False
    if kind == "exact":
        return value == expected
    if kind == "suffix":
        return value.endswith(expected)
    return expected in value


def call_matches(call: Mapping[str, Any], tool: str, arguments: Mapping[str, Any]) -> bool:
    if call["tool"] != tool or not isinstance(arguments, Mapping):
        return False
    return all(
        argument_matches(pattern, arguments.get(name))
        for name, pattern in call["arguments"].items()
    )


def _evidence(text: str, pattern: str) -> bool:
    return pattern.casefold() in text.casefold()


def _sandbox_ok(task: Task, after: Mapping[str, str | None]) -> bool:
    for name, expected in task.expected_sandbox.items():
        content = after.get(name)
        if expected.get("absent"):
            if content is not None:
                return False
            continue
        if content is None:
            return False
        if "lines" in expected and len(content.splitlines()) != expected["lines"]:
            return False
        if not all(_evidence(content, p) for p in expected.get("contains", ())):
            return False
    return True


# -- the trace -------------------------------------------------------------------------


def _events(run: Mapping, kind: str) -> list[Mapping]:
    return [e for e in run["events"] if e["event_type"] == kind]


def _terminal(run: Mapping) -> Mapping | None:
    for event in reversed(run["events"]):
        if event["event_type"] in ("run.completed", "run.failed", "run.cancelled"):
            return event
    return None


def _model_calls(run: Mapping) -> list[Mapping]:
    terminal = _terminal(run)
    if terminal is not None and "model_calls" in terminal["payload"]:
        return list(terminal["payload"]["model_calls"])
    return [{**e["payload"], "actor": e["actor"]} for e in _events(run, "model.started")]


_USAGE_FIELDS = ("input_tokens", "output_tokens", "cached_tokens")


def _unknown(name: str) -> str:
    """The key counting the calls that reported no value for a usage field."""
    return f"{name.removesuffix('_tokens')}_unknown_calls"


def _empty_tokens() -> dict[str, Any]:
    entry: dict[str, Any] = {"calls": 0}
    for name in _USAGE_FIELDS:
        entry[name] = None
        entry[_unknown(name)] = 0
    return entry


def _add_tokens(into: dict[str, dict], model: str, entry: Mapping[str, Any]) -> None:
    """Add one per-model entry; a sum stays None until some call reported a count."""
    target = into.setdefault(model, _empty_tokens())
    target["calls"] += entry["calls"]
    for name in _USAGE_FIELDS:
        target[_unknown(name)] += entry[_unknown(name)]
        if entry[name] is not None:
            target[name] = (target[name] or 0) + entry[name]


def run_tokens(runs: Iterable[Mapping]) -> dict[str, dict[str, Any]]:
    """Tokens per model from model.finished usage; never merged across models (RM16)."""
    tokens: dict[str, dict] = {}
    for run in runs:
        usage = {
            e["payload"].get("call_id"): e["payload"].get("usage")
            for e in _events(run, "model.finished")
        }
        for call in _model_calls(run):
            used = usage.get(call.get("call_id")) or {}
            entry = {"calls": 1}
            for name in _USAGE_FIELDS:
                value = used.get(name)
                entry[name] = value if isinstance(value, int) else None
                entry[_unknown(name)] = int(entry[name] is None)
            _add_tokens(tokens, call["model"], entry)
    return tokens


def _first_useful_ms(run: Mapping) -> int | None:
    for event in run["events"]:
        if event["event_type"] == "tool.proposed":
            return event["elapsed_ms"]
        if event["event_type"] == "answer.delta" and event["payload"].get("text"):
            return event["elapsed_ms"]
    return None


# -- grading one attempt ---------------------------------------------------------------


def _host_report(report: Any) -> Mapping[str, Any]:
    return report if isinstance(report, Mapping) else {}


def grade_attempt(task: Task, attempt: Mapping[str, Any]) -> dict[str, Any]:
    """Grade one attempt (spec 0.50 section 12.5); the host's ok never decides it."""
    arm = attempt["arm"]
    host = attempt["host"]
    report = _host_report(host["report"])
    after = attempt["sandbox_after"]
    runs_by_id = {run["run_id"]: run for run in attempt["runs"]}
    diagnostics = list(attempt["diagnostics"])
    relaymind = arm in RELAYMIND_ARMS
    expected = attempt["expected_host_model"]

    named = {d.get("runId") for d in diagnostics} - {"unavailable", None}
    window = list(attempt["window_runs"])
    uncorrelated = [run_id for run_id in window if run_id not in named]
    runs = [runs_by_id[run_id] for run_id in window if run_id in runs_by_id]
    summary = report.get("toolSummary") if isinstance(report.get("toolSummary"), Mapping) else {}
    host_tools = [t for t in summary.get("tools") or [] if isinstance(t, str)]
    final = report.get("final") if isinstance(report.get("final"), str) else ""

    # A failover is a reported identity other than the arm's; the pinned host's
    # error report names none (provider and model null), which is a failed attempt.
    failover = any(
        report.get(key) is not None and report.get(key) != expected[key]
        for key in ("provider", "model")
    )
    timed_out = host["status"] == "timed_out"
    proposals = [e["payload"] for run in runs for e in _events(run, "tool.proposed")]
    if not relaymind or not task.expected_tools:
        proposals_match = None
    else:
        acceptable = [c for group in task.expected_tools for c in group]
        proposals_match = (
            bool(proposals)
            and all(
                any(call_matches(c, p["tool_name"], p.get("arguments")) for c in acceptable)
                for p in proposals
            )
            and all(
                any(
                    call_matches(c, p["tool_name"], p.get("arguments"))
                    for c in group
                    for p in proposals
                )
                for group in task.expected_tools
            )
        )
    executions = sum(len(_events(run, "tool.started")) for run in runs)

    failures: list[str] = []
    if failover:
        failures.append("host_failover")
    else:
        if timed_out:
            failures.append("timed_out")
        elif host["exit_code"] != 0:
            failures.append("exit_status")
        if not all(any(_evidence(final, p) for p in group) for group in task.required_evidence) or (
            not final.strip()
        ):
            failures.append("answer")
        if any(_evidence(final, p) for p in task.forbidden_evidence):
            failures.append("forbidden_evidence")
        if not all(any(c["tool"] in host_tools for c in group) for group in task.expected_tools):
            failures.append("host_tools")
        if not _sandbox_ok(task, after):
            failures.append("sandbox")
        if relaymind:
            if not runs:
                failures.append("no_gateway_run")
            if uncorrelated:
                failures.append("correlation")
            if executions:
                failures.append("gateway_tool_execution")
            if proposals_match is False:
                failures.append("proposals")

    states = Counter(run["state"] for run in runs)
    failed_codes = Counter(
        (_terminal(run) or {"payload": {}})["payload"].get("error_code")
        for run in runs
        if run["state"] == "failed"
    )
    error_codes = Counter({k: v for k, v in failed_codes.items() if k})
    for record in diagnostics:
        if record.get("outcome") == "failed" and record.get("runId") in ("unavailable", None):
            if record.get("code"):
                error_codes[record["code"]] += 1
    calls_by_run = [(run, _model_calls(run)) for run in runs]
    calls = [call for _, of_run in calls_by_run for call in of_run]
    local_turns = sum(
        1
        for run, of_run in calls_by_run
        if run["state"] == "completed" and not any(call["actor"] == "external" for call in of_run)
    )
    success = not failures
    return {
        "index": attempt["index"],
        "repeat": attempt["repeat"],
        "task_id": task.task_id,
        "kind": task.kind,
        "language": task.language,
        "arm": arm,
        "success": success,
        "failures": failures,
        "timed_out": timed_out,
        "host_exit_code": host["exit_code"],
        "host_interrupted": host["exit_code"] == HOST_INTERRUPT_EXIT,
        "host_ok": report.get("ok") if report else None,
        "host_ok_but_failed": report.get("ok") is True and not success,
        "host_failover": failover,
        "host_provider": report.get("provider"),
        "host_model": report.get("model"),
        "host_tool_calls": summary.get("calls"),
        "host_tools": host_tools,
        "host_tool_failures": summary.get("failures"),
        "host_assistant_turns": report.get("assistantTurns"),
        "host_usage": report.get("usage"),
        "completion_ms": host["elapsed_ms"],
        "first_useful_gateway_ms": _first_useful_ms(runs[0]) if relaymind and runs else None,
        "gateway_runs": len(runs) if relaymind else None,
        "uncorrelated_runs": uncorrelated,
        "unavailable_run_ids": sum(
            1 for d in diagnostics if d.get("runId") in ("unavailable", None)
        ),
        "inference_attempts": len(diagnostics) if relaymind else None,
        "errors": sum(1 for d in diagnostics if d.get("outcome") == "failed")
        if relaymind
        else None,
        "error_codes": dict(sorted(error_codes.items())),
        "cancellations": states.get("cancelled", 0),
        "needs_external": failed_codes.get("needs_external", 0),
        "context_limit_exceeded": failed_codes.get("context_limit_exceeded", 0),
        "local_turns": local_turns if relaymind else None,
        "local_calls": sum(c["actor"] == "local" for c in calls) if relaymind else None,
        "external_calls": sum(c["actor"] == "external" for c in calls) if relaymind else None,
        "proposals": [
            {"tool_name": p["tool_name"], "arguments": p.get("arguments")} for p in proposals
        ],
        "proposals_match": proposals_match,
        "gateway_tool_executions": executions,
        "tokens": run_tokens(runs) if relaymind else {},
    }


# -- the summary -----------------------------------------------------------------------


def _distribution(values: Sequence[float | None]) -> dict[str, Any]:
    present = [v for v in values if v is not None]
    return {
        "count": len(present),
        "p50": nearest_rank(present, 0.5),
        "p95": nearest_rank(present, 0.95),
    }


def _sum(grades: Sequence[Mapping], key: str) -> int | None:
    values = [g[key] for g in grades]
    if any(v is None for v in values):
        return None
    return sum(values)


def _tokens(grades: Iterable[Mapping]) -> dict[str, dict]:
    total: dict[str, dict] = {}
    for grade in grades:
        for model, entry in grade["tokens"].items():
            _add_tokens(total, model, entry)
    return dict(sorted(total.items()))


def _language_metrics(grades: Sequence[Mapping], language: str) -> dict[str, Any]:
    successes = [g for g in grades if g["success"]]
    codes: Counter = Counter()
    for grade in grades:
        codes.update(grade["error_codes"])
    return {
        "best_effort": language == "zh",
        "attempts": len(grades),
        "successes": len(successes),
        "host_failovers": sum(g["host_failover"] for g in grades),
        "timeouts": sum(g["timed_out"] for g in grades),
        "host_interrupts": sum(g["host_interrupted"] for g in grades),
        "host_ok_but_failed": sum(g["host_ok_but_failed"] for g in grades),
        "gateway_runs": _sum(grades, "gateway_runs"),
        "uncorrelated_runs": sum(len(g["uncorrelated_runs"]) for g in grades),
        "unavailable_run_ids": sum(g["unavailable_run_ids"] for g in grades),
        "inference_attempts": _sum(grades, "inference_attempts"),
        "errors": _sum(grades, "errors"),
        "error_codes": dict(sorted(codes.items())),
        "cancellations": sum(g["cancellations"] for g in grades),
        "needs_external": sum(g["needs_external"] for g in grades),
        "context_limit_exceeded": sum(g["context_limit_exceeded"] for g in grades),
        "local_turns": _sum(grades, "local_turns"),
        "local_calls": _sum(grades, "local_calls"),
        "external_calls": _sum(grades, "external_calls"),
        "gateway_tool_executions": sum(g["gateway_tool_executions"] for g in grades),
        "failures": dict(sorted(Counter(f for g in grades for f in g["failures"]).items())),
        "completion_ms": {
            "all": _distribution([g["completion_ms"] for g in grades]),
            "successful": _distribution([g["completion_ms"] for g in successes]),
        },
        "first_useful_gateway_ms": {
            "all": _distribution([g["first_useful_gateway_ms"] for g in grades]),
            "successful": _distribution([g["first_useful_gateway_ms"] for g in successes]),
        },
        "tokens": {"all": _tokens(grades), "successful": _tokens(successes)},
        "host_usage_reported": sum(1 for g in grades if g["host_usage"] is not None),
    }


def _comparison(grades: Sequence[Mapping], first: str, second: str) -> dict[str, Any]:
    languages: dict[str, Any] = {}
    for language in LANGUAGES:
        of = {
            arm: {
                (g["task_id"], g["repeat"]): g
                for g in grades
                if g["arm"] == arm and g["language"] == language
            }
            for arm in (first, second)
        }
        if not of[first] or not of[second]:
            continue
        pairs = [
            of[first][key]["completion_ms"] - of[second][key]["completion_ms"]
            for key in sorted(set(of[first]) & set(of[second]))
            if of[first][key]["success"] and of[second][key]["success"]
        ]
        languages[language] = {
            "pairs": len(pairs),
            "median_completion_ms_difference": nearest_rank(pairs, 0.5),
            "successes": {
                arm: sum(g["success"] for g in of[arm].values()) for arm in (first, second)
            },
            "attempts": {arm: len(of[arm]) for arm in (first, second)},
        }
    return {"arms": [first, second], "languages": languages}


def summarize(
    grades: Sequence[Mapping[str, Any]],
    *,
    arms: Sequence[str] = ARMS,
    not_run: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Exact counts per arm and language; Chinese apart; comparisons on completed pairs."""
    not_run = dict(not_run or {})
    result: dict[str, Any] = {}
    for arm in arms:
        if arm in not_run:
            result[arm] = {"status": "not_run", "reason": not_run[arm], "languages": {}}
            continue
        of_arm = [g for g in grades if g["arm"] == arm]
        result[arm] = {
            "status": "ran",
            "languages": {
                language: _language_metrics(
                    [g for g in of_arm if g["language"] == language], language
                )
                for language in LANGUAGES
                if any(g["language"] == language for g in of_arm)
            },
        }
    return {
        "arms": result,
        "comparisons": {
            name: _comparison(grades, first, second)
            for name, (first, second) in COMPARISONS.items()
        },
    }


# -- the report ------------------------------------------------------------------------


def _cell(value: Any) -> str:
    return "unknown" if value is None else str(value)


def _tokens_cell(tokens: Mapping[str, Mapping]) -> str:
    if not tokens:
        return "none recorded"
    parts = []
    for model, entry in tokens.items():
        parts.append(
            f"{model}: {entry['calls']} calls, in {_cell(entry['input_tokens'])} "
            f"(+{entry['input_unknown_calls']} unknown), out {_cell(entry['output_tokens'])} "
            f"(+{entry['output_unknown_calls']} unknown)"
        )
    return "; ".join(parts)


def render_markdown(summary: Mapping[str, Any], *, title: str) -> str:
    lines = [
        f"# {title}",
        "",
        "Spec 0.50 section 12.5. Counts are exact over every attempt, failed, timed-out, "
        "and retried ones included. Success is judged from the host's exit status, its final "
        "answer, its tool calls, the sandbox, and the gateway trace; the host's `ok` is never "
        "a success. A host failover (the host answered through another provider or model) is "
        "counted apart and is never an arm success. Completion latency is the runner's clock "
        "around the host command (host startup and host tool time included); first useful "
        "content is the gateway's clock; the two are never subtracted. Tokens are per model, "
        "summed only over calls that reported a count, with unknown calls beside the sum; "
        "host-reported usage is never added to them. Chinese results are best-effort and "
        "reported apart.",
        "",
        "| Arm | Language | Successes | Host failovers | Timeouts | Interrupts | ok but failed "
        "| Gateway runs | Inference attempts | Errors | needs_external | context_limit_exceeded "
        "| Local turns | External calls | Completion p50/p95 ms (successful) "
        "| First useful p50/p95 ms (gateway) |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- "
        "| --- | --- |",
    ]
    for arm, entry in summary["arms"].items():
        if entry["status"] == "not_run":
            lines.append(f"| {arm} | all | not_run: {entry['reason']} |" + " |" * 13)
            continue
        for language, m in entry["languages"].items():
            label = f"{language} (best-effort)" if m["best_effort"] else language
            done = m["completion_ms"]["successful"]
            first = m["first_useful_gateway_ms"]["successful"]
            lines.append(
                f"| {arm} | {label} | {m['successes']}/{m['attempts']} | {m['host_failovers']} "
                f"| {m['timeouts']} | {m['host_interrupts']} | {m['host_ok_but_failed']} "
                f"| {_cell(m['gateway_runs'])} | {_cell(m['inference_attempts'])} "
                f"| {_cell(m['errors'])} | {m['needs_external']} | {m['context_limit_exceeded']} "
                f"| {_cell(m['local_turns'])} | {_cell(m['external_calls'])} "
                f"| {_cell(done['p50'])}/{_cell(done['p95'])} (n={done['count']}) "
                f"| {_cell(first['p50'])}/{_cell(first['p95'])} (n={first['count']}) |"
            )
    lines += ["", "## Tokens per model (gateway usage, successful attempts)", ""]
    for arm, entry in summary["arms"].items():
        for language, m in entry["languages"].items():
            lines.append(f"- {arm}, {language}: {_tokens_cell(m['tokens']['successful'])}")
    lines += ["", "## Paired comparisons (task and repeat pairs both arms completed)", ""]
    for name, comparison in summary["comparisons"].items():
        first, second = comparison["arms"]
        if not comparison["languages"]:
            lines.append(f"- {name} ({first} against {second}): no pairs recorded")
        for language, c in comparison["languages"].items():
            lines.append(
                f"- {name} ({first} against {second}), {language}: {c['pairs']} pairs, median "
                f"completion difference {_cell(c['median_completion_ms_difference'])} ms; "
                f"successes {c['successes'][first]}/{c['attempts'][first]} against "
                f"{c['successes'][second]}/{c['attempts'][second]}"
            )
    return "\n".join(lines) + "\n"
