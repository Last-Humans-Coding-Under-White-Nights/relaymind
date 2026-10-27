"""Auditable trace observations and honest evaluation denominators (RM25–RM28)."""

from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from relaymind.evaluation.metrics import nearest_rank


def distribution(values: Sequence[int | float], *, eligible: int) -> dict[str, Any]:
    return dict(
        values=list(values),
        n=len(values),
        eligible=eligible,
        missing=eligible - len(values),
        p50=nearest_rank(values, 0.5),
        p95=nearest_rank(values, 0.95),
        min=min(values) if values else None,
        max=max(values) if values else None,
    )


def trace_metrics(
    events: Sequence[Mapping[str, Any]], *, expected_skill: str | None = None
) -> dict[str, Any]:
    """Extract gateway observations only; selector self-reports never become calls."""
    starts = [e for e in events if e["event_type"] == "model.started"]
    finished = {e["payload"]["call_id"]: e for e in events if e["event_type"] == "model.finished"}
    accepted = next((e["payload"] for e in events if e["event_type"] == "run.accepted"), {})
    terminal = (
        events[-1]
        if events and events[-1]["event_type"] in ("run.completed", "run.failed", "run.cancelled")
        else None
    )
    errors = [
        e["payload"]["predicted_ms"] - e["payload"]["duration_ms"]
        for e in finished.values()
        if e["actor"] == "local"
        and e["payload"].get("predicted_ms") is not None
        and e["payload"].get("duration_ms") is not None
    ]
    external = [e for e in starts if e["actor"] == "external"]
    usage = []
    for e in external:
        p = e["payload"]
        counts = finished.get(p["call_id"], {}).get("payload", {}).get("usage") or {}
        usage.append(
            dict(
                model=p["model"],
                input_tokens=counts.get("input_tokens"),
                output_tokens=counts.get("output_tokens"),
            )
        )
    wasted = False
    if terminal:
        p = terminal["payload"]
        wasted = terminal["event_type"] == "run.failed" and p.get("error_code") == "needs_external"
        if terminal["event_type"] == "run.completed":
            wasted = any(
                e["event_type"] == "answer.delta"
                and e["actor"] == "external"
                and e["payload"].get("model") == p.get("final_answer_model")
                for e in events
            )
    routes = [e["payload"] for e in events if e["event_type"] == "route.selected"]
    selected = next(
        (
            p.get("skill_id") or (p.get("decision") or {}).get("skill_id")
            for p in routes
            if p.get("skill_id") or (p.get("decision") or {}).get("skill_id")
        ),
        None,
    )
    predicted = set() if selected is None else {selected}
    expected = set() if expected_skill is None else {expected_skill}
    intersection = len(predicted & expected)
    selection = dict(
        predicted=sorted(predicted),
        expected=sorted(expected),
        precision=intersection / len(predicted) if predicted else 0,
        recall=intersection / len(expected) if expected else 0,
        jaccard=intersection / len(predicted | expected) if predicted | expected else 0,
    )
    placements = []
    for e in events:
        p = e["payload"].get("placement")
        if p and e["event_type"] in (
            "model.started",
            "route.selected",
            "handoff.started",
            "run.failed",
        ):
            if e["event_type"] == "model.started" or p.get("role") == "none":
                placements.append(p)
    return dict(
        trace_observed=bool(events),
        local_class=accepted.get("local_model_status", "unknown"),
        prediction_errors=errors,
        prediction_eligible=sum(e["actor"] == "local" for e in finished.values()),
        wasted_eligible=wasted,
        local_active_ms=terminal["payload"].get("local_active_ms") if terminal else None,
        generations=dict(
            Counter(f"{e['actor']}/{e['payload'].get('purpose', 'unknown')}" for e in starts)
        ),
        external_usage=usage,
        selection=selection,
        admitted_selector=next(
            (
                (p.get("selector") or {}).get("name")
                for p in routes
                if (p.get("selector") or {}).get("name")
            ),
            None,
        ),
        streamed_failure=bool(
            terminal
            and terminal["event_type"] == "run.failed"
            and any(
                e["event_type"] == "answer.delta" and e["payload"].get("call_id") for e in events
            )
        ),
        selector_attempts=[
            a for e in events for a in (e["payload"].get("selector") or {}).get("attempts", [])
        ],
        placements=placements,
    )


def summarize_placement(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Candidate costs include failures; only success count is the task denominator."""
    values = [r.get("metrics", {}) for r in rows]
    completed = [r.get("metrics", {}) for r in rows if r.get("state") == "completed"]
    success = sum(bool(r.get("success")) for r in rows)
    generations = Counter()
    measured_completed = [
        v for v in completed if v.get("trace_observed") is True and "generations" in v
    ]
    for v in measured_completed:
        generations.update(v.get("generations", {}))
    usage = [u for v in values for u in v.get("external_usage", [])]
    unobserved = sum(r.get("metrics", {}).get("trace_observed") is not True for r in rows)
    cost = dict(
        unobserved_runs=unobserved,
        completed_tasks=success,
        attempted_runs=len(rows),
        calls=len(usage),
        calls_per_completed_task=len(usage) / success if success and not unobserved else None,
    )
    for name in ("input_tokens", "output_tokens"):
        known = [u[name] for u in usage if u[name] is not None]
        cost[name + "_known_total"] = sum(known)
        cost[name + "_missing_calls"] = len(usage) - len(known)
        cost[name + "_per_completed_task"] = (
            sum(known) / success
            if success and not unobserved and len(known) == len(usage)
            else None
        )
    selections = [v["selection"] for v in values if "selection" in v]
    placements = [p for v in values for p in v.get("placements", [])]
    evaluated = [
        p
        for p in placements
        if p.get("inputs", {}).get("local_success_rate") is not None
        and p.get("inputs", {}).get("expected_external_ms") is not None
    ]
    wasted = [v for v in values if v.get("wasted_eligible")]
    return dict(
        prediction_error_ms=distribution(
            [x for v in values for x in v.get("prediction_errors", [])],
            eligible=sum(v.get("prediction_eligible", 0) for v in values),
        ),
        wasted_local_ms=distribution(
            [v["local_active_ms"] for v in wasted if v.get("local_active_ms") is not None],
            eligible=len(wasted),
        ),
        generations=dict(
            completed_runs=len(completed),
            by_actor_purpose=dict(generations),
            measured_completed_runs=len(measured_completed),
            unobserved_completed_runs=len(completed) - len(measured_completed),
            per_completed_run=(
                sum(generations.values()) / len(completed)
                if completed and len(measured_completed) == len(completed)
                else None
            ),
            per_measured_completed_run=(
                sum(generations.values()) / len(measured_completed) if measured_completed else None
            ),
        ),
        external_cost=cost,
        measured_classes=dict(Counter(v.get("local_class", "unknown") for v in values)),
        selection=dict(
            n=len(rows),
            measured=len(selections),
            **{
                k: sum(s[k] for s in selections) / len(rows) if rows else None
                for k in ("precision", "recall", "jaccard")
            },
        ),
        expected_wait=dict(
            evaluated=len(evaluated),
            declined=sum(p.get("reason") == "expected_wait" for p in evaluated),
            not_evaluated=len(placements) - len(evaluated),
        ),
        streamed_failures=sum(bool(v.get("streamed_failure")) for v in values),
        selection_by_admitted_selector={
            name: {
                "n": len(group),
                **{
                    key: sum(v["selection"][key] for v in group) / len(group)
                    for key in ("precision", "recall", "jaccard")
                },
            }
            for name in sorted({v.get("admitted_selector") or "none" for v in values})
            if (
                group := [
                    v
                    for v in values
                    if (v.get("admitted_selector") or "none") == name and "selection" in v
                ]
            )
        },
        selector_attempts=dict(
            Counter(a["outcome"] for v in values for a in v.get("selector_attempts", []))
        ),
    )
