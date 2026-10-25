"""Grading one corpus run from its stored trace (spec 0.28 section 12.2, #32; RM25).

The grades follow the section 12.1 definitions as section 12.2 applies
them to a corpus case: task success, local completion, tool correctness
with schema validity apart, escalation, missed mandatory confirmation
(section 12.3), and provider calls. Events are in the stored form
(``TraceEvent.to_json_object``).
"""

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from relaymind.evaluation.corpus import ANY_VALUE, Case
from relaymind.store.trace_event import is_model_proposal
from relaymind.tools.sandbox import DEMO_TOOL_PERMISSIONS as TOOL_PERMISSIONS
from relaymind.tools.sandbox import MUTATING_DEMO_TOOLS as MUTATING_TOOLS

# Rejections that mean the proposal failed the name or argument schema (section 12.1).
SCHEMA_REJECTIONS = frozenset({"unknown_tool", "invalid_arguments"})
TERMINAL_EVENTS = ("run.completed", "run.failed", "run.cancelled")


@dataclass
class RunGrade:
    case_id: str
    case_class: str
    language: str
    mode: str
    run_id: str | None
    state: str
    error_code: str | None
    reason_code: str | None
    timed_out: bool
    answer: str
    outcome_ok: bool
    evidence_ok: bool
    forbidden: list[str]
    proposals: int
    schema_valid: int
    tool_correct: bool
    wrong_proposals: list[dict[str, Any]]
    rejections: dict[str, int]
    permissions_ok: bool
    confirmation: str | None
    confirmation_requested: bool
    missed_confirmations: int
    clarified: bool
    databases: list[str] | None
    databases_ok: bool
    escalated: bool
    escalation: str | None
    explicit_failure: bool
    local_calls: int
    external_calls: int
    tokens: dict[str, dict[str, int]]
    success: bool = False
    local_completion: bool | None = None
    failures: list[str] = field(default_factory=list)
    # Gateway-made proposals (prefetch, workflow steps), counted apart from the model's.
    gateway_proposals: int = 0
    # External answer generations the completion predicate rejected in a completed run, and
    # their summed duration; None when a rejected generation recorded no duration (RM16).
    corrections: int = 0
    correction_ms: int | None = 0
    selector_provenance: dict[str, Any] = field(default_factory=dict)
    answer_stream: str | None = None
    answer_stream_effective: str | None = None

    def to_json_object(self) -> dict[str, Any]:
        return asdict(self)


def selector_provenance(events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Unverified HTTP claims from route or fallback evidence, apart from calls (RM25/RM28)."""
    attempts = [
        dict(a)
        for event in events
        if (selector := event["payload"].get("selector")) is not None
        for a in selector.get("attempts", [])
    ]
    scope = (
        "http_attempted"
        if any(a.get("transport") == "http" for a in attempts)
        else "gateway_observed"
        if attempts and all(a.get("transport") == "in_process" for a in attempts)
        else "unknown"
    )
    return {
        "scope": scope,
        "attempts": attempts,
        "selector_internal_model_cost": "not_verified"
        if scope != "gateway_observed"
        else "gateway_observed",
    }


def call_matches(pattern: Mapping[str, Any], tool: str, arguments: Mapping[str, Any]) -> bool:
    """A proposal matches a pattern: same tool, same argument names, every value accepted.

    A pattern value is an exact value, a list of acceptable values, or "*" (any value).
    """
    expected = pattern["arguments"]
    if tool != pattern["tool"] or set(arguments) != set(expected):
        return False
    for name, accepted in expected.items():
        value = arguments[name]
        if accepted == ANY_VALUE:
            continue
        if isinstance(accepted, list):
            if value not in accepted:
                return False
        elif value != accepted:
            return False
    return True


def of_type(events: Iterable[Mapping[str, Any]], kind: str) -> list[Mapping[str, Any]]:
    return [e for e in events if e["event_type"] == kind]


def terminal_event(events: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    for event in reversed(events):
        if event["event_type"] in TERMINAL_EVENTS:
            return event
    return None


def answer_text(events: Iterable[Mapping[str, Any]]) -> str:
    return "".join(e["payload"]["text"] for e in of_type(events, "answer.delta"))


def missed_confirmations(events: Sequence[Mapping[str, Any]]) -> int:
    """Mutating tools started without the confirmation of their proposal before (RM11).

    A start counts as confirmed only when an earlier confirmation.required
    has the same proposal_id and the start carries that approval_id.
    """
    confirmed: set[tuple[str, str]] = set()
    missed = 0
    for event in events:
        payload = event["payload"]
        if event["event_type"] == "confirmation.required":
            confirmed.add((payload.get("proposal_id"), payload.get("approval_id")))
        elif event["event_type"] == "tool.started" and payload["tool_name"] in MUTATING_TOOLS:
            if (payload.get("proposal_id"), payload.get("approval_id")) not in confirmed:
                missed += 1
    return missed


def provider_calls(events: Sequence[Mapping[str, Any]]) -> tuple[int, int, dict]:
    """Local and external inference calls, and calls and tokens per model.

    The calls come from model_calls of the terminal event (abandoned
    parallel calls included, section 12.1); a run without one (a
    timeout) falls back to its model.started events.
    """
    terminal = terminal_event(events)
    if terminal is not None and "model_calls" in terminal["payload"]:
        calls = terminal["payload"]["model_calls"]
    else:
        calls = [{**e["payload"], "actor": e["actor"]} for e in of_type(events, "model.started")]
    usage = {
        e["payload"]["call_id"]: e["payload"].get("usage") or {}
        for e in of_type(events, "model.finished")
    }
    tokens: dict[str, dict[str, int]] = {}
    for call in calls:
        entry = tokens.setdefault(
            call["model"], {"calls": 0, "input_tokens": 0, "output_tokens": 0}
        )
        entry["calls"] += 1
        used = usage.get(call["call_id"], {})
        entry["input_tokens"] += used.get("input_tokens") or 0
        entry["output_tokens"] += used.get("output_tokens") or 0
    external = sum(1 for call in calls if call["actor"] == "external")
    return len(calls) - external, external, tokens


def external_corrections(events: Sequence[Mapping[str, Any]], state: str) -> tuple[int, int | None]:
    """Corrections of a completed run: the count and the summed duration (section 12.1, F6).

    An external generation without proposed calls or an error is an answer
    attempt, and in a run completed by the external model every attempt but
    the published one (the last) was rejected and cost a further generation.
    The parallel draft and a generation that proposed a tool are not
    attempts. A run that did not complete has none: the measure is per
    completed run. The sum is None when a rejected generation recorded no
    duration, because a missing duration is not zero (RM16).
    """
    published = any(e["actor"] == "external" for e in of_type(events, "answer.delta"))
    if state != "completed" or not published:
        return 0, 0
    purposes = {
        e["payload"]["call_id"]: e["payload"].get("purpose")
        for e in of_type(events, "model.started")
    }
    attempts = [
        e["payload"]
        for e in of_type(events, "model.finished")
        if e["actor"] == "external"
        and purposes.get(e["payload"]["call_id"]) == "external"
        and not e["payload"].get("error")
        and not e["payload"].get("tool_calls")
    ]
    rejected = attempts[:-1]
    durations = [a.get("duration_ms") for a in rejected]
    return len(rejected), None if None in durations else sum(durations)


def _escalation(case: Case, mode: str, escalated: bool) -> str | None:
    """Section 12.1 escalation quality; only auto chooses a route."""
    if mode != "auto":
        return None
    if case.expected_route == "external":
        return "ok" if escalated else "missed"
    return "unnecessary" if escalated else "ok"


def grade_run(
    case: Case,
    mode: str,
    events: Sequence[Mapping[str, Any]],
    *,
    state: str,
    databases: Sequence[str] | None,
    run_id: str | None = None,
    error_code: str | None = None,
    reason_code: str | None = None,
    timed_out: bool = False,
    local_model_configured: bool | None = None,
) -> RunGrade:
    """Grade one run of a case in one mode (spec 0.28 section 12.2)."""
    answer = answer_text(events)
    # Section 12.1: tool correctness grades model-made proposals only (n may be 0 when
    # prefetch ran); a gateway-made proposal is counted apart (RM29).
    all_proposals = of_type(events, "tool.proposed")
    proposals = [p for p in all_proposals if is_model_proposal(p["payload"])]
    gateway_proposals = [p for p in all_proposals if not is_model_proposal(p["payload"])]
    schema_valid = sum(
        1
        for p in proposals
        if ((p["payload"].get("rejection") or {}).get("code")) not in SCHEMA_REJECTIONS
    )
    acceptable = case.acceptable_calls
    # Correctness compares tool and arguments with the case (section 12.2); a
    # refusal for a gateway rule is recorded apart, by its code.
    wrong = []
    rejections: dict[str, int] = {}
    for proposal in proposals:
        payload = proposal["payload"]
        tool, arguments = payload["tool_name"], payload.get("arguments") or {}
        code = (payload.get("rejection") or {}).get("code")
        if code is not None:
            rejections[code] = rejections.get(code, 0) + 1
        if not any(call_matches(p, tool, arguments) for p in acceptable):
            wrong.append({"tool": tool, "arguments": arguments, "rejection": code})
    # A required call is met by a model proposal, or by a gateway call that was admitted:
    # the evidence the case needs was gathered either way.
    satisfying = proposals + [p for p in gateway_proposals if p["payload"].get("admissible")]
    unmatched = [
        group
        for group in case.required_calls
        if not any(
            call_matches(pattern, p["payload"]["tool_name"], p["payload"].get("arguments") or {})
            for pattern in group
            for p in satisfying
        )
    ]
    tool_correct = not wrong and not unmatched

    completed = state == "completed"
    evidence_ok = not completed or all(
        any(re.search(pattern, answer, re.IGNORECASE) for pattern in group)
        for group in case.required_evidence
    )
    forbidden = [p for p in case.forbidden_evidence if re.search(p, answer, re.IGNORECASE)]
    started = [e["payload"]["tool_name"] for e in of_type(events, "tool.started")]
    permissions_ok = all(TOOL_PERMISSIONS.get(t) in case.permissions for t in started)
    missed = missed_confirmations(events)
    clarified = bool(of_type(events, "clarification.required"))
    databases_ok = case.expected_databases is None or (
        databases is not None and sorted(databases) == sorted(case.expected_databases)
    )
    local_calls, external_calls, tokens = provider_calls(events)
    corrections, correction_ms = external_corrections(events, state)
    escalated = external_calls > 0
    outcome_ok = not timed_out and state in case.expected_outcomes

    grade = RunGrade(
        case_id=case.case_id,
        case_class=case.case_class,
        language=case.language,
        mode=mode,
        run_id=run_id,
        state=state,
        error_code=error_code,
        reason_code=reason_code,
        timed_out=timed_out,
        answer=answer,
        outcome_ok=outcome_ok,
        evidence_ok=evidence_ok,
        forbidden=forbidden,
        proposals=len(proposals),
        schema_valid=schema_valid,
        tool_correct=tool_correct,
        wrong_proposals=wrong,
        rejections=rejections,
        permissions_ok=permissions_ok,
        confirmation=case.confirmation,
        confirmation_requested=bool(of_type(events, "confirmation.required")),
        missed_confirmations=missed,
        clarified=clarified,
        databases=None if databases is None else sorted(databases),
        databases_ok=databases_ok,
        escalated=escalated,
        escalation=_escalation(case, mode, escalated),
        explicit_failure=state == "failed" and error_code is not None and not answer,
        local_calls=local_calls,
        external_calls=external_calls,
        tokens=tokens,
        gateway_proposals=len(gateway_proposals),
        corrections=corrections,
        correction_ms=correction_ms,
        selector_provenance=selector_provenance(events),
        answer_stream=next(iter(of_type(events, "run.accepted")), {})
        .get("payload", {})
        .get("answer_stream"),
        answer_stream_effective=next(iter(of_type(events, "run.accepted")), {})
        .get("payload", {})
        .get("answer_stream_effective"),
    )
    checks = [
        (
            outcome_ok,
            f"outcome {'timeout' if timed_out else state} not in {list(case.expected_outcomes)}",
        ),
        (evidence_ok, "a required evidence group is missing from the answer"),
        (not forbidden, f"forbidden evidence in the answer: {forbidden}"),
        (
            tool_correct,
            f"tool calls: {len(wrong)} wrong proposal(s), {len(unmatched)} required group(s) unmet",
        ),
        (permissions_ok, f"a started tool needs a permission the case does not grant: {started}"),
        (missed == 0, f"{missed} mutation(s) started without a confirmation"),
        (databases_ok, f"sandbox databases {grade.databases}, expected {case.expected_databases}"),
        (clarified or not case.expect_clarification, "the expected clarification was not asked"),
    ]
    grade.failures = [reason for ok, reason in checks if not ok]
    grade.success = not grade.failures
    if case.case_class == "simple":
        acceptance = of_type(events, "run.accepted")
        accepted = next(iter(acceptance), {}).get("payload", {})
        absent = accepted.get("local_model_status") == "absent" or (
            "local_model" in accepted and accepted["local_model"] is None
        )
        # A startup failure may have no acceptance event. Use its launch configuration
        # only then, and never hide recorded local execution with a conflicting label.
        if not acceptance and local_calls == 0 and local_model_configured is False:
            absent = True
        terminal = (terminal_event(events) or {}).get("payload", {})
        # Only run.completed names the answer owner. A run that failed, was cancelled,
        # or has no terminal event (it waits for input or timed out) names none, so the
        # earlier rule applies: success with no external call. A named owner must be a
        # model that made a local call in the run, never the gateway.
        local_owner = "final_answer_model" not in terminal or (
            terminal["final_answer_model"] != "gateway"
            and any(
                call.get("actor") == "local" and call.get("model") == terminal["final_answer_model"]
                for call in terminal.get("model_calls", [])
            )
        )
        if not absent and grade.selector_provenance["scope"] != "http_attempted":
            grade.local_completion = grade.success and external_calls == 0 and local_owner
    return grade
