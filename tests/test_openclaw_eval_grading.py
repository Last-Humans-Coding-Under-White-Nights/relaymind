"""Grading one OpenClaw evaluation attempt and the arm summary (spec 0.50 section 12.5, #139).

RM16, RM25, RM26, RM33; AC24. Synthetic attempts only: the host report,
the plugin's diagnostics records, and the gateway runs are written here the
way the runner records them, so the rules are checked without a host or a
model. A header, role chunk, keep-alive, plugin load, or proposed tool call
is not a completed task; the host's ``"ok": true`` never decides a pass; a
host failover is counted apart; unknown usage stays unknown; every attempt,
retried and failed ones included, is in the denominator.
"""

import json
from pathlib import Path

import pytest

from relaymind.evaluation.openclaw import (
    RELAYMIND_HOST_MODEL,
    grade_attempt,
    load_tasks,
    render_markdown,
    summarize,
)

REPO = Path(__file__).resolve().parent.parent
TASKS = load_tasks(REPO / "evaluation" / "openclaw_tasks.v1.json")
DIRECT = {"provider": "relaymind-eval-direct", "model": "provider-model"}


def task(kind: str, *, language: str = "en", tool: str | None = None, policy: str | None = None):
    for t in TASKS.tasks:
        if t.kind != kind or t.language != language:
            continue
        tools = {c["tool"] for g in t.expected_tools for c in g}
        if tool is not None and tool not in tools:
            continue
        if policy is not None and t.exec_policy != policy:
            continue
        return t
    raise AssertionError("no such task")


TEXT = task("text")
TEXT_ZH = task("text", language="zh")
READ = task("host_tool", tool="read")
EXEC_FULL = task("host_tool", tool="exec", policy="full")
EXEC_DENY = task("host_tool", tool="exec", policy="deny")


def evidence_text(t) -> str:
    return " ".join(group[0] for group in t.required_evidence) or "done"


def report(t, *, ok=True, tools=(), failures=0, provider=None, model=None, final=None):
    identity = RELAYMIND_HOST_MODEL
    return {
        "ok": ok,
        "status": "ok",
        "final": evidence_text(t) if final is None else final,
        "usage": {"cacheRead": 0, "cacheWrite": 0, "cost": {"total": 0}},
        "assistantTurns": 1 + bool(tools),
        "toolSummary": {"calls": len(tools), "tools": list(tools), "failures": failures},
        "provider": provider or identity["provider"],
        "model": model or identity["model"],
    }


def event(kind, actor="gateway", elapsed=0, **payload):
    return {"event_type": kind, "actor": actor, "elapsed_ms": elapsed, "payload": payload}


def model_events(call_id, model="qwen", actor="local", usage=None, elapsed=5):
    return [
        event("model.started", actor, elapsed, call_id=call_id, model=model),
        event("model.finished", actor, elapsed + 10, call_id=call_id, model=model, usage=usage),
    ]


def text_run(run_id, *, text="answer", model="qwen", actor="local", usage=None, elapsed=40):
    calls = [{"actor": actor, "call_id": f"{run_id}_c1", "model": model}]
    return {
        "run_id": run_id,
        "state": "completed",
        "events": [
            event("run.accepted"),
            *model_events(f"{run_id}_c1", model, actor, usage),
            event("answer.delta", actor, elapsed, text=text),
            event(
                "run.completed",
                elapsed=elapsed + 1,
                answer=text,
                external_calls=sum(c["actor"] == "external" for c in calls),
                model_calls=calls,
            ),
        ],
    }


def tool_run(run_id, tool, arguments, *, proposal="call_1", elapsed=30, started=False):
    calls = [{"actor": "local", "call_id": f"{run_id}_c1", "model": "qwen"}]
    events = [
        event("run.accepted"),
        *model_events(f"{run_id}_c1"),
        event(
            "tool.proposed",
            "local",
            elapsed,
            tool_name=tool,
            arguments=arguments,
            proposal_id=proposal,
            owner="client",
        ),
    ]
    if started:
        events.append(event("tool.started", tool_name=tool))
    events.append(event("run.completed", elapsed=elapsed + 1, external_calls=0, model_calls=calls))
    return {"run_id": run_id, "state": "completed", "events": events}


def failed_run(run_id, code, reason=None):
    return {
        "run_id": run_id,
        "state": "failed",
        "events": [
            event("run.accepted"),
            event("run.failed", error_code=code, reason_code=reason, model_calls=[]),
        ],
    }


def diag(run_id, outcome="answer", attempt=1, status=200, code=None):
    return {
        "event": "relaymind.inference_attempt",
        "turn": "turn-1",
        "attempt": attempt,
        "runId": run_id,
        "outcome": outcome,
        "status": status,
        "code": code,
        "retried": attempt > 1,
        "usage": "unknown",
    }


def attempt(
    t,
    arm="relaymind_auto",
    *,
    host=None,
    exit_code=0,
    status="completed",
    elapsed_ms=1200,
    runs=(),
    diagnostics=None,
    window=None,
    sandbox=None,
    repeat=0,
    expected=None,
):
    runs = list(runs)
    sandbox_after = dict(t.sandbox) if sandbox is None else sandbox
    return {
        "index": 0,
        "repeat": repeat,
        "task_id": t.task_id,
        "arm": arm,
        "language": t.language,
        "expected_host_model": expected
        or (DIRECT if arm == "direct" else dict(RELAYMIND_HOST_MODEL)),
        "host": {
            "status": status,
            "exit_code": exit_code,
            "elapsed_ms": elapsed_ms,
            "report": host,
        },
        "sandbox_after": sandbox_after,
        "diagnostics": [diag(r["run_id"]) for r in runs] if diagnostics is None else diagnostics,
        "window_runs": [r["run_id"] for r in runs] if window is None else window,
        "runs": runs,
    }


def full_exec_sandbox(t):
    after = dict(t.sandbox)
    for name, expected in t.expected_sandbox.items():
        if expected.get("absent"):
            after.pop(name, None)
        else:
            lines = expected.get("lines", 1)
            text = " ".join(expected.get("contains", ["ready"]))
            after[name] = "".join(f"{text}\n" for _ in range(lines))
    return after


# -- one attempt -----------------------------------------------------------------------


def test_a_text_task_succeeds_on_the_answer_the_exit_status_and_the_trace():
    grade = grade_attempt(TEXT, attempt(TEXT, host=report(TEXT), runs=[text_run("run_a")]))
    assert grade["success"] is True, grade["failures"]
    assert grade["failures"] == []
    assert grade["gateway_runs"] == 1
    assert grade["inference_attempts"] == 1
    assert grade["first_useful_gateway_ms"] == 40
    assert grade["completion_ms"] == 1200
    assert grade["local_turns"] == 1
    assert grade["external_calls"] == 0


def test_the_hosts_ok_is_never_a_success():
    """The pinned host reports ok true for a denied tool (macOS probe)."""
    bad = report(EXEC_FULL, tools=["exec"], failures=1)
    grade = grade_attempt(
        EXEC_FULL,
        attempt(
            EXEC_FULL,
            host=bad,
            runs=[
                tool_run("run_a", "exec", {"command": "echo ready >> log.txt"}),
                text_run("run_b"),
            ],
        ),
    )
    assert grade["host_ok"] is True
    assert grade["success"] is False
    assert "sandbox" in grade["failures"]
    assert grade["host_ok_but_failed"] is True


def test_a_proposed_call_the_host_did_not_execute_is_not_a_completed_task():
    grade = grade_attempt(
        READ,
        attempt(
            READ,
            host=report(READ, tools=[], final=""),
            runs=[tool_run("run_a", "read", {"path": "notes.txt"})],
        ),
    )
    assert grade["success"] is False
    assert grade["proposals_match"] is True
    assert "host_tools" in grade["failures"]
    assert "answer" in grade["failures"]


def test_a_host_tool_task_succeeds_with_the_hosts_execution_and_the_sandbox():
    after = full_exec_sandbox(EXEC_FULL)
    grade = grade_attempt(
        EXEC_FULL,
        attempt(
            EXEC_FULL,
            host=report(EXEC_FULL, tools=["exec"]),
            runs=[
                tool_run("run_a", "exec", {"command": "echo ready >> log.txt"}),
                text_run("run_b"),
            ],
            sandbox=after,
        ),
    )
    assert grade["success"] is True, grade["failures"]
    assert grade["gateway_runs"] == 2
    assert grade["first_useful_gateway_ms"] == 30


def deny_runs():
    return [tool_run("q", "exec", {"command": "echo ready >> log.txt"}), text_run("r")]


def test_a_denied_exec_succeeds_only_when_nothing_was_written():
    clean = grade_attempt(
        EXEC_DENY,
        attempt(EXEC_DENY, host=report(EXEC_DENY, tools=["exec"], failures=1), runs=deny_runs()),
    )
    assert clean["success"] is True, clean["failures"]
    written = grade_attempt(
        EXEC_DENY,
        attempt(
            EXEC_DENY,
            host=report(EXEC_DENY, tools=["exec"], failures=1),
            runs=deny_runs(),
            sandbox={**EXEC_DENY.sandbox, "log.txt": "ready\n"},
        ),
    )
    assert written["success"] is False and "sandbox" in written["failures"]


def test_a_gateway_tool_execution_fails_the_attempt():
    grade = grade_attempt(
        READ,
        attempt(
            READ,
            host=report(READ, tools=["read"]),
            runs=[
                tool_run("run_a", "read", {"path": "notes.txt"}, started=True),
                text_run("run_b"),
            ],
        ),
    )
    assert grade["gateway_tool_executions"] == 1
    assert grade["success"] is False and "gateway_tool_execution" in grade["failures"]


def test_a_proposal_with_other_arguments_is_reported_the_same_way_in_both_arms():
    """Spec 0.50 section 12.5 (RM25, RM33; AC24): the arguments are reported, not graded.

    Under the deny policy the host refuses the call whatever its command, and the
    direct arm's host report names only the tool. A RelayMind arm whose model ran
    another command grades the same as the direct arm; its mismatch is reported.
    """
    runs = [tool_run("run_a", "exec", {"command": "ls"}), text_run("run_b")]
    host = {"tools": ["exec"], "failures": 1}
    relaymind = grade_attempt(
        EXEC_DENY, attempt(EXEC_DENY, host=report(EXEC_DENY, **host), runs=runs)
    )
    direct = grade_attempt(
        EXEC_DENY,
        attempt(
            EXEC_DENY,
            "direct",
            host=report(EXEC_DENY, **host, **DIRECT),
            runs=[],
            diagnostics=[],
            window=[],
        ),
    )
    assert relaymind["success"] is direct["success"] is True, relaymind["failures"]
    assert relaymind["proposals_match"] is False
    assert relaymind["unexpected_proposals"] == ["exec"]
    assert direct["proposals_match"] is None


def test_an_expected_tool_without_a_gateway_proposal_fails_a_relaymind_arm():
    """The host reports a call no gateway run of the attempt proposed: not RelayMind's call."""
    grade = grade_attempt(
        READ,
        attempt(READ, host=report(READ, tools=["read"]), runs=[text_run("run_a")]),
    )
    assert grade["proposals_match"] is False
    assert grade["success"] is False and grade["failures"] == ["proposals"]


WRITE = task("host_tool", tool="write")


def write_sandbox():
    return {**WRITE.sandbox, "done.txt": "ready\n"}


def test_an_extra_proposal_is_reported_not_failed_the_same_way_in_both_arms():
    """Spec 0.50 section 12.5 (RM25, RM33; AC24): extra calls never bias the comparison.

    A model that looks around with ``read`` before it calls ``write`` completes the
    task. The direct arm has no gateway trace to hold the extra call against, so a
    RelayMind arm is not failed for it either: both arms grade the same, and the
    extra call is reported, as a host tool in both arms and as a proposal in RelayMind.
    """
    host = {"tools": ["read", "write"], "final": "done"}
    relaymind = grade_attempt(
        WRITE,
        attempt(
            WRITE,
            "relaymind_external_only",
            host=report(WRITE, **host),
            runs=[
                tool_run("run_a", "read", {"path": "done.txt"}),
                tool_run("run_b", "write", {"path": "done.txt", "content": "ready"}),
                text_run("run_c"),
            ],
            sandbox=write_sandbox(),
        ),
    )
    direct = grade_attempt(
        WRITE,
        attempt(
            WRITE,
            "direct",
            host=report(WRITE, **host, **DIRECT),
            runs=[],
            diagnostics=[],
            window=[],
            sandbox=write_sandbox(),
        ),
    )
    assert relaymind["success"] is True, relaymind["failures"]
    assert direct["success"] is True, direct["failures"]
    assert relaymind["failures"] == direct["failures"] == []
    assert relaymind["proposals_match"] is True
    assert relaymind["unexpected_proposals"] == ["read"]
    assert direct["unexpected_proposals"] is None
    assert relaymind["unexpected_host_tools"] == direct["unexpected_host_tools"] == ["read"]


def test_a_retry_after_a_mismatching_proposal_completes_the_group():
    """A wrong path then the right one: the group has its call, the first is reported."""
    grade = grade_attempt(
        READ,
        attempt(
            READ,
            host=report(READ, tools=["read"]),
            runs=[
                tool_run("run_a", "read", {"path": "other.txt"}),
                tool_run("run_b", "read", {"path": "notes.txt"}, proposal="call_2"),
                text_run("run_c"),
            ],
        ),
    )
    assert grade["success"] is True, grade["failures"]
    assert grade["proposals_match"] is True
    assert grade["unexpected_proposals"] == ["read"]
    assert grade["unexpected_host_tools"] == []


def test_a_proposal_on_a_text_task_is_reported_as_unexpected():
    grade = grade_attempt(
        TEXT,
        attempt(
            TEXT,
            host=report(TEXT, tools=["ls"]),
            runs=[tool_run("run_a", "ls", {"path": "."}), text_run("run_b")],
        ),
    )
    assert grade["success"] is True, grade["failures"]
    assert grade["proposals_match"] is None
    assert grade["unexpected_proposals"] == ["ls"]
    assert grade["unexpected_host_tools"] == ["ls"]


def test_a_host_failover_is_counted_apart_and_never_an_arm_success():
    grade = grade_attempt(
        TEXT,
        attempt(TEXT, host=report(TEXT, provider="anthropic", model="other"), runs=[]),
    )
    assert grade["host_failover"] is True
    assert grade["success"] is False
    assert grade["failures"] == ["host_failover"]


def test_a_host_error_report_with_no_provider_is_a_failed_attempt_not_a_failover():
    """The pinned host's error JSON names no provider or model (macOS probe runs/out-p2.json).

    A failover needs a reported provider or model that differs from the arm's;
    a null one is the host saying nothing, so the attempt is graded as failed.
    """
    error = {
        "ok": False,
        "status": "error",
        "model": None,
        "provider": None,
        "error": {"message": "Unknown model: relaymind/relaymind-client-tools"},
    }
    grade = grade_attempt(
        TEXT,
        attempt(TEXT, host=error, exit_code=1, status="failed", runs=[], diagnostics=[]),
    )
    assert grade["host_failover"] is False
    assert "host_failover" not in grade["failures"]
    assert "exit_status" in grade["failures"] and "answer" in grade["failures"]
    summary = summarize([grade])
    assert summary["arms"]["relaymind_auto"]["languages"]["en"]["host_failovers"] == 0


@pytest.mark.parametrize(
    "identity",
    [
        {"provider": None, "model": None},
        {"provider": RELAYMIND_HOST_MODEL["provider"], "model": None},
        {"provider": None, "model": RELAYMIND_HOST_MODEL["model"]},
    ],
)
def test_a_host_that_does_not_report_the_arms_provider_and_model_fails(identity):
    """Spec 0.50 section 12.5: the host's reported provider and model are the arm's.

    A report that names neither, or only one, is no failover, but it is not
    the arm's identity either, so even an exit 0 with the answer fails.
    """
    host = {**report(TEXT), **identity}
    grade = grade_attempt(TEXT, attempt(TEXT, host=host, runs=[text_run("run_a")]))
    assert grade["host_failover"] is False
    assert grade["success"] is False
    assert grade["failures"] == ["host_model"]


def test_a_completed_run_without_any_model_call_is_a_local_turn():
    """Spec 0.50 section 12.5: local turns are completed runs without an external call."""
    replay = {
        "run_id": "run_a",
        "state": "completed",
        "events": [
            event("run.accepted"),
            event("answer.delta", elapsed=3, text=evidence_text(TEXT)),
            event("run.completed", elapsed=4, external_calls=0, model_calls=[]),
        ],
    }
    external = text_run("run_b", model="ext", actor="external")
    grade = grade_attempt(TEXT, attempt(TEXT, host=report(TEXT), runs=[replay, external]))
    assert grade["local_turns"] == 1
    assert grade["external_calls"] == 1


def test_first_useful_content_comes_from_the_trace_never_from_a_header_or_keep_alive():
    """A run with headers sent but no answer or proposal has no first useful content."""
    run = {
        "run_id": "run_a",
        "state": "failed",
        "events": [
            event("run.accepted"),
            *model_events("run_a_c1"),
            event("run.failed", elapsed=90, error_code="external_failed", model_calls=[]),
        ],
    }
    grade = grade_attempt(TEXT, attempt(TEXT, host=None, exit_code=1, status="failed", runs=[run]))
    assert grade["first_useful_gateway_ms"] is None
    assert grade["success"] is False
    assert grade["error_codes"] == {"external_failed": 1}


def test_direct_has_unknown_gateway_measures_and_keeps_the_hosts_statements():
    grade = grade_attempt(
        TEXT,
        attempt(
            TEXT,
            "direct",
            host=report(TEXT, **DIRECT),
            runs=[],
            diagnostics=[],
            window=[],
        ),
    )
    assert grade["success"] is True, grade["failures"]
    assert grade["first_useful_gateway_ms"] is None
    assert grade["inference_attempts"] is None
    assert grade["external_calls"] is None
    assert grade["host_assistant_turns"] == 1
    assert grade["host_usage"] == {"cacheRead": 0, "cacheWrite": 0, "cost": {"total": 0}}
    assert grade["tokens"] == {}


def test_retried_and_failed_attempts_are_all_counted():
    runs = [failed_run("run_a", "overloaded"), text_run("run_b")]
    grade = grade_attempt(
        TEXT,
        attempt(
            TEXT,
            host=report(TEXT),
            runs=runs,
            diagnostics=[
                diag("run_a", "failed", status=429, code="overloaded"),
                diag("run_b", attempt=2),
            ],
        ),
    )
    assert grade["inference_attempts"] == 2
    assert grade["errors"] == 1
    assert grade["error_codes"] == {"overloaded": 1}
    assert grade["success"] is True


def test_a_record_without_a_run_id_stays_unavailable():
    grade = grade_attempt(
        TEXT,
        attempt(
            TEXT,
            host=None,
            exit_code=130,
            status="failed",
            runs=[],
            diagnostics=[diag("unavailable", "aborted", status=None)],
            window=[],
        ),
    )
    assert grade["unavailable_run_ids"] == 1
    assert grade["host_interrupted"] is True
    assert grade["success"] is False


def test_an_uncorrelated_gateway_run_fails_the_attempt():
    grade = grade_attempt(
        TEXT,
        attempt(
            TEXT,
            host=report(TEXT),
            runs=[text_run("run_a"), text_run("run_x")],
            diagnostics=[diag("run_a")],
        ),
    )
    assert grade["uncorrelated_runs"] == ["run_x"]
    assert grade["success"] is False and "correlation" in grade["failures"]


def test_needs_external_context_limit_and_cancellations_are_counted():
    runs = [
        failed_run("run_a", "needs_external", "context_limit"),
        failed_run("run_b", "context_limit_exceeded"),
        {
            "run_id": "run_c",
            "state": "cancelled",
            "events": [event("run.cancelled", reason="cancel_requested")],
        },
    ]
    grade = grade_attempt(TEXT, attempt(TEXT, host=None, exit_code=1, status="failed", runs=runs))
    assert grade["needs_external"] == 1
    assert grade["context_limit_exceeded"] == 1
    assert grade["cancellations"] == 1
    assert grade["local_turns"] == 0


def test_a_timeout_is_graded_never_dropped():
    grade = grade_attempt(
        TEXT, attempt(TEXT, host=None, exit_code=None, status="timed_out", runs=[])
    )
    assert grade["timed_out"] is True
    assert grade["success"] is False and "timed_out" in grade["failures"]


def test_unknown_usage_stays_unknown_and_models_are_never_merged():
    runs = [
        text_run("run_a", model="qwen", usage=None),
        text_run(
            "run_b",
            model="provider-model",
            actor="external",
            usage={"input_tokens": 100, "output_tokens": 7, "cached_tokens": None},
        ),
    ]
    grade = grade_attempt(TEXT, attempt(TEXT, host=report(TEXT), runs=runs))
    assert grade["tokens"] == {
        "qwen": {
            "calls": 1,
            "input_tokens": None,
            "input_unknown_calls": 1,
            "output_tokens": None,
            "output_unknown_calls": 1,
            "cached_tokens": None,
            "cached_unknown_calls": 1,
        },
        "provider-model": {
            "calls": 1,
            "input_tokens": 100,
            "input_unknown_calls": 0,
            "output_tokens": 7,
            "output_unknown_calls": 0,
            "cached_tokens": None,
            "cached_unknown_calls": 1,
        },
    }
    assert grade["external_calls"] == 1
    assert grade["local_calls"] == 1
    assert grade["local_turns"] == 1


# -- the summary -----------------------------------------------------------------------


def grades_for_summary():
    after = full_exec_sandbox(EXEC_FULL)
    out = []
    for repeat in range(2):
        for arm, identity, runs in (
            ("direct", DIRECT, []),
            (
                "relaymind_external_only",
                RELAYMIND_HOST_MODEL,
                [text_run("r", actor="external", model="provider-model")],
            ),
        ):
            out.append(
                grade_attempt(
                    TEXT,
                    attempt(
                        TEXT,
                        arm,
                        host=report(TEXT, **identity),
                        runs=runs,
                        repeat=repeat,
                        elapsed_ms=1000 + 500 * (arm != "direct") + repeat,
                        diagnostics=None if runs else [],
                        window=None if runs else [],
                    ),
                )
            )
    out.append(
        grade_attempt(
            TEXT_ZH,
            attempt(
                TEXT_ZH,
                "relaymind_external_only",
                host=report(TEXT_ZH, final="nothing"),
                runs=[text_run("z")],
            ),
        )
    )
    out.append(
        grade_attempt(
            EXEC_FULL,
            attempt(
                EXEC_FULL,
                "relaymind_external_only",
                host=report(EXEC_FULL, provider="other", model="m", tools=["exec"]),
                runs=[],
                sandbox=after,
            ),
        )
    )
    return out


def test_the_summary_gives_exact_counts_per_arm_and_language_with_chinese_apart():
    summary = summarize(
        grades_for_summary(),
        arms=("direct", "relaymind_external_only", "relaymind_auto"),
        not_run={"relaymind_auto": "no local model configured"},
    )
    direct = summary["arms"]["direct"]["languages"]["en"]
    assert direct["attempts"] == 2 and direct["successes"] == 2
    relay = summary["arms"]["relaymind_external_only"]["languages"]
    assert relay["en"]["attempts"] == 3
    assert relay["en"]["successes"] == 2
    assert relay["en"]["host_failovers"] == 1
    assert relay["zh"]["attempts"] == 1 and relay["zh"]["successes"] == 0
    assert relay["zh"]["best_effort"] is True
    assert relay["en"]["completion_ms"]["successful"] == {"count": 2, "p50": 1500, "p95": 1501}
    assert relay["en"]["tokens"]["all"]["provider-model"]["calls"] == 2
    auto = summary["arms"]["relaymind_auto"]
    assert auto["status"] == "not_run" and auto["reason"] == "no local model configured"
    assert auto["languages"] == {}


def test_an_arm_that_ran_without_an_ended_attempt_keeps_its_report_row():
    """An arm set up but stopped before any attempt ended (an operator
    interrupt) has no language rows; the report still names it with 0 attempts."""
    summary = summarize([], arms=("direct", "relaymind_external_only"))
    assert summary["arms"]["direct"] == {"status": "ran", "languages": {}}
    text = render_markdown(summary, title="OpenClaw evaluation")
    for arm in ("direct", "relaymind_external_only"):
        assert f"| {arm} | all | 0/0: no attempt ended |" in text


def test_the_paired_comparison_uses_only_pairs_both_arms_completed():
    summary = summarize(grades_for_summary(), arms=("direct", "relaymind_external_only"))
    overhead = summary["comparisons"]["integration_overhead"]
    assert overhead["arms"] == ["relaymind_external_only", "direct"]
    assert overhead["languages"]["en"]["pairs"] == 2
    assert overhead["languages"]["en"]["median_completion_ms_difference"] == 500
    assert overhead["languages"]["en"]["successes"] == {"relaymind_external_only": 2, "direct": 2}
    local = summary["comparisons"]["local_inference_auto"]
    assert local["languages"] == {}


def test_the_markdown_report_states_its_limits_and_counts():
    summary = summarize(
        grades_for_summary(),
        arms=("direct", "relaymind_external_only", "relaymind_auto"),
        not_run={"relaymind_auto": "no local model configured"},
    )
    text = render_markdown(summary, title="OpenClaw evaluation")
    assert "# OpenClaw evaluation" in text
    assert "2/2" in text and "2/3" in text
    assert "not_run: no local model configured" in text
    assert "host failover" in text.lower()
    assert "best-effort" in text
    assert "never" in text  # the clocks are never subtracted, ok is never success


def test_the_summary_and_report_count_unexpected_calls_in_both_arms():
    """Spec 0.50 section 12.5: extra calls are counted per arm, never hidden in a failure."""
    host = {"tools": ["read", "write"], "final": "done"}
    grades = [
        grade_attempt(
            WRITE,
            attempt(
                WRITE,
                "relaymind_external_only",
                host=report(WRITE, **host),
                runs=[
                    tool_run("run_a", "read", {"path": "done.txt"}),
                    tool_run("run_b", "write", {"path": "done.txt"}, proposal="call_2"),
                ],
                sandbox=write_sandbox(),
            ),
        ),
        grade_attempt(
            WRITE,
            attempt(
                WRITE,
                "direct",
                host=report(WRITE, **host, **DIRECT),
                runs=[],
                diagnostics=[],
                window=[],
                sandbox=write_sandbox(),
            ),
        ),
    ]
    summary = summarize(grades, arms=("direct", "relaymind_external_only"))
    relay = summary["arms"]["relaymind_external_only"]["languages"]["en"]
    direct = summary["arms"]["direct"]["languages"]["en"]
    assert relay["successes"] == direct["successes"] == 1
    assert relay["unexpected_proposals"] == 1
    assert direct["unexpected_proposals"] is None
    assert relay["unexpected_host_tools"] == direct["unexpected_host_tools"] == 1
    assert summary["comparisons"]["integration_overhead"]["languages"]["en"]["pairs"] == 1
    text = render_markdown(summary, title="OpenClaw evaluation")
    assert "Unexpected host tools" in text and "Unexpected proposals" in text
    assert "| direct | en | 1/1 |" in text


# -- the local stage at the 8192-token local context (D1; macOS qualification Q1, Q2) ---


def handed_off_run(run_id, reason="context_limit", *, text="answer", elapsed=60):
    """A run that skipped the local stage and was answered by the external model."""
    calls = [{"actor": "external", "call_id": f"{run_id}_c1", "model": "provider-model"}]
    return {
        "run_id": run_id,
        "state": "completed",
        "events": [
            event("run.accepted"),
            event("route.selected", route="external", reason_code=reason),
            event("handoff.started", reason_code=reason, model="provider-model"),
            *model_events(f"{run_id}_c1", "provider-model", "external"),
            event("answer.delta", "external", elapsed, text=text),
            event(
                "run.completed",
                elapsed=elapsed + 1,
                answer=text,
                external_calls=1,
                model_calls=calls,
            ),
        ],
    }


def test_a_turn_over_the_local_limit_is_zero_local_turns_with_its_reason():
    """Spec 0.50 section 12.5: why the runs left the local stage is reported.

    At the 8192-token local context every measured OpenClaw request is over the
    local limit (10,500 to 10,577 tokens, macOS qualification Q1), so the auto
    arm hands every turn off with reason context_limit. That is a result: the
    grade keeps the reason, so 0 local turns is never an unexplained blank.
    """
    grade = grade_attempt(TEXT, attempt(TEXT, host=report(TEXT), runs=[handed_off_run("run_a")]))
    assert grade["success"] is True, grade["failures"]
    assert grade["local_turns"] == 0
    assert grade["local_calls"] == 0
    assert grade["external_calls"] == 1
    assert grade["escalation_reasons"] == {"context_limit": 1}


def test_needs_external_runs_keep_their_reason():
    """Without an external model the same turn ends needs_external / context_limit (Q2)."""
    runs = [failed_run("run_a", "needs_external", "context_limit")]
    grade = grade_attempt(TEXT, attempt(TEXT, host=None, exit_code=1, status="failed", runs=runs))
    assert grade["needs_external"] == 1
    assert grade["escalation_reasons"] == {"context_limit": 1}
    assert grade["local_turns"] == 0


def test_the_direct_arm_has_no_escalation_reasons():
    grade = grade_attempt(
        TEXT,
        attempt(TEXT, "direct", host=report(TEXT, **DIRECT), runs=[], diagnostics=[], window=[]),
    )
    assert grade["escalation_reasons"] is None


def test_the_report_states_zero_local_turns_and_their_reasons():
    grades = [
        grade_attempt(TEXT, attempt(TEXT, host=report(TEXT), runs=[handed_off_run("a")], repeat=r))
        for r in range(3)
    ]
    summary = summarize(grades, arms=("relaymind_auto",))
    auto = summary["arms"]["relaymind_auto"]["languages"]["en"]
    assert auto["local_turns"] == 0
    assert auto["escalation_reasons"] == {"context_limit": 3}
    text = render_markdown(summary, title="OpenClaw evaluation")
    assert "relaymind_auto, en: 0 local turns of 3 gateway runs" in text
    assert "context_limit: 3" in text
    assert "0 local turns is a result" in text


def test_a_local_inference_comparison_without_a_local_turn_says_so():
    grades = []
    for r in range(2):
        grades.append(
            grade_attempt(
                TEXT, attempt(TEXT, host=report(TEXT), runs=[handed_off_run("a")], repeat=r)
            )
        )
        grades.append(
            grade_attempt(
                TEXT,
                attempt(
                    TEXT,
                    "relaymind_external_only",
                    host=report(TEXT),
                    runs=[handed_off_run("b", "execution_mode")],
                    repeat=r,
                ),
            )
        )
    summary = summarize(grades, arms=("relaymind_external_only", "relaymind_auto"))
    local = summary["comparisons"]["local_inference_auto"]["languages"]["en"]
    assert local["pairs"] == 2
    assert local["local_turns"] == 0
    text = render_markdown(summary, title="OpenClaw evaluation")
    line = next(x for x in text.splitlines() if x.startswith("- local_inference_auto"))
    assert "no local turn in relaymind_auto: compares no local inference" in line
    overhead = next(x for x in text.splitlines() if x.startswith("- integration_overhead"))
    assert "local turn" not in overhead


# -- host interrupts -----------------------------------------------------------------


@pytest.mark.parametrize("exit_code", [130, -2, 0xC000013A])
def test_a_host_interrupt_is_counted_apart_from_other_exit_statuses(exit_code):
    """Exit 130, a host that died of SIGINT (-2 from the runner), or on Windows
    one ended by Ctrl-C (STATUS_CONTROL_C_EXIT, unsigned as Python reports it)
    is an interrupt."""
    grade = grade_attempt(
        TEXT,
        attempt(TEXT, host=None, exit_code=exit_code, status="failed", runs=[], window=[]),
    )
    assert grade["host_interrupted"] is True
    assert "host_interrupted" in grade["failures"]
    assert "exit_status" not in grade["failures"]


def test_another_exit_status_is_not_an_interrupt():
    grade = grade_attempt(
        TEXT, attempt(TEXT, host=None, exit_code=1, status="failed", runs=[], window=[])
    )
    assert grade["host_interrupted"] is False
    assert "exit_status" in grade["failures"]


@pytest.mark.parametrize("missing", ["host", "sandbox_after", "runs"])
def test_an_incomplete_attempt_record_is_refused(missing):
    record = attempt(TEXT, host=report(TEXT), runs=[text_run("r")])
    del record[missing]
    with pytest.raises(KeyError):
        grade_attempt(TEXT, record)


def test_a_grade_is_plain_json():
    grade = grade_attempt(TEXT, attempt(TEXT, host=report(TEXT), runs=[text_run("r")]))
    assert json.loads(json.dumps(grade)) == grade
