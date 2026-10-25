"""M7 trace observations: proposals, initial selection, owners and call timing (RM25)."""

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

from relaymind.evaluation.corpus import Case
from relaymind.evaluation.grading import call_matches, terminal_event
from relaymind.evaluation.metrics import rate
from relaymind.evaluation.placement_metrics import distribution, trace_metrics
from relaymind.store.trace_event import is_model_proposal


def model_calls(events: Sequence[Mapping[str, Any]], *, phase: str = "measured") -> list[dict]:
    """Join call boundaries; unfinished starts keep null usage and first-token time."""
    starts = {e["payload"]["call_id"]: e for e in events if e["event_type"] == "model.started"}
    finishes = {e["payload"]["call_id"]: e for e in events if e["event_type"] == "model.finished"}
    calls = []
    for call_id in dict.fromkeys([*starts, *finishes]):
        start, finish = starts.get(call_id, {}), finishes.get(call_id, {})
        payload = {**start.get("payload", {}), **finish.get("payload", {})}
        usage = finish.get("payload", {}).get("usage") or {}
        calls.append(
            dict(
                call_id=call_id,
                phase=phase,
                actor=start.get("actor", finish.get("actor", "unknown")),
                model=payload.get("model") or "unknown",
                purpose=payload.get("purpose") or "unknown",
                started=bool(start),
                finished=bool(finish),
                duration_ms=finish.get("payload", {}).get("duration_ms"),
                first_token_ms=finish.get("payload", {}).get("first_token_ms"),
                **{
                    name: usage.get(name)
                    for name in (
                        "input_tokens",
                        "output_tokens",
                        "cached_tokens",
                        "reasoning_tokens",
                    )
                },
            )
        )
    return calls


def first_token_summary(calls: Sequence[Mapping[str, Any]]) -> dict:
    groups = defaultdict(list)
    for call in calls:
        groups[
            "/".join(str(call.get(k) or "unknown") for k in ("phase", "actor", "model", "purpose"))
        ].append(call)
    return {
        key: distribution(
            [c["first_token_ms"] for c in rows if c.get("first_token_ms") is not None],
            eligible=len(rows),
        )
        for key, rows in sorted(groups.items())
    }


def initial_selection(events: Sequence[Mapping[str, Any]]) -> tuple[list[Mapping], list[dict]]:
    """The initial round ends at its route/clarification; progress histories are prefixes."""
    initial = []
    for event in events:
        if event["payload"].get("mechanism") == "clarification_resume":
            break
        initial.append(event)
        if event["event_type"] in ("route.selected", "clarification.required"):
            break
    attempts = []
    for event in initial:
        history = (event["payload"].get("selector") or {}).get("attempts", [])
        # Histories are cumulative within one round. Preserve position as well as value.
        for position, attempt in enumerate(history):
            if position >= len(attempts):
                attempts.append(dict(attempt))
            elif attempts[position] != attempt:
                attempts[position] = dict(attempt)
    return initial, attempts


def selection_scores(predicted: set, expected: set) -> dict:
    intersection = len(predicted & expected)
    return {
        "precision": intersection / len(predicted) if predicted else 0,
        "recall": intersection / len(expected) if expected else 0,
        "jaccard": intersection / len(predicted | expected) if predicted | expected else 0,
    }


def enrich_metrics(
    case: Case,
    events: Sequence[Mapping[str, Any]],
    *,
    expected_skill: str | None,
    selector_chain: Sequence[str] = (),
) -> dict:
    """Shared live/regrade projection; missing traces remain explicitly unobserved."""
    metrics = trace_metrics(events, expected_skill=expected_skill)
    proposals = [e["payload"] for e in events if e["event_type"] == "tool.proposed"]
    model = [p for p in proposals if is_model_proposal(p)]
    prefetch = [p for p in proposals if p.get("mechanism") == "prefetch"]

    def correct(p):
        return any(
            call_matches(pattern, p["tool_name"], p.get("arguments") or {})
            for pattern in case.acceptable_calls
        )

    terminal = terminal_event(events) or {}
    owner = terminal.get("payload", {}).get("final_answer_model")
    actors = {
        e["actor"]
        for e in events
        if e["event_type"] == "model.started" and e["payload"].get("model") == owner
    }
    answer_owner = (
        "gateway"
        if owner == "gateway"
        else next(iter(actors))
        if len(actors) == 1 and actors <= {"local", "external"}
        else "unknown"
    )
    initial, attempts = initial_selection(events)
    routes = [e["payload"] for e in initial if e["event_type"] == "route.selected"]
    observed = bool(
        routes or attempts or any(e["event_type"] == "clarification.required" for e in initial)
    )
    records = []
    expected = {expected_skill} if expected_skill else set()
    for a in attempts:
        known = a.get("transport", "in_process") == "in_process" and a["name"] in (
            "model_router",
            "example_match",
        )
        prediction = set(metrics["selection"]["predicted"]) if a["outcome"] == "selected" else set()
        records.append(
            {
                **a,
                "attempted": True,
                "skipped": False,
                "observed": True,
                "scores": selection_scores(prediction, expected) if known else None,
            }
        )
    for name in selector_chain:
        if name not in {a["name"] for a in attempts}:
            records.append(
                dict(
                    name=name,
                    version="unknown",
                    transport="unknown",
                    attempted=False,
                    skipped=observed,
                    observed=observed,
                    scores=None,
                )
            )
    metrics.update(
        m7_observed=bool(events),
        model_proposal_correct=sum(map(correct, model)),
        model_proposals=len(model),
        gateway_proposals=len(proposals) - len(model),
        prefetch_correct=sum(map(correct, prefetch)),
        prefetch_proposals=len(prefetch),
        prefetch_diagnostics=dict(
            admitted=sum(bool(p.get("admissible")) for p in prefetch),
            rejected=sum(bool(p.get("rejection")) for p in prefetch),
            executed=sum(
                e["event_type"] == "tool.started"
                and e["payload"].get("proposal_id") in {p.get("proposal_id") for p in prefetch}
                for e in events
            ),
        ),
        answer_owner=answer_owner,
        model_calls=model_calls(events),
        initial_selector_records=records,
        selection_observed=observed,
    )
    return metrics


def summarize_m7(rows: Sequence[Mapping[str, Any]]) -> dict:
    values = [r.get("metrics", {}) for r in rows]
    observed = [v for v in values if v.get("m7_observed")]
    owners = Counter(
        r.get("metrics", {}).get("answer_owner", "unknown")
        for r in rows
        if r.get("state") == "completed"
    )
    groups = defaultdict(list)
    for v in values:
        for a in v.get("initial_selector_records", []):
            groups[
                "/".join(str(a.get(k) or "unknown") for k in ("name", "version", "transport"))
            ].append(a)
    selectors = {}
    for key, attempts in sorted(groups.items()):
        scored = [a["scores"] for a in attempts if a.get("scores") is not None]
        attempted = sum(a["attempted"] for a in attempts)
        unobserved = sum(not a["observed"] for a in attempts)
        selectors[key] = dict(
            attempted=attempted,
            failed=sum(
                a.get("outcome") in ("timeout", "error", "cancelled", "inadmissible")
                for a in attempts
            ),
            skipped=sum(a["skipped"] for a in attempts),
            unobserved=unobserved,
            scored=len(scored),
            **{
                k: sum(s[k] for s in scored) / len(scored)
                if scored and len(scored) == attempted and not unobserved
                else None
                for k in ("precision", "recall", "jaccard")
            },
        )
    return dict(
        observed_runs=len(observed),
        missing_runs=len(rows) - len(observed),
        model_proposal_accuracy=rate(
            sum(v["model_proposal_correct"] for v in observed),
            sum(v["model_proposals"] for v in observed),
        ),
        prefetch_accuracy=rate(
            sum(v["prefetch_correct"] for v in observed),
            sum(v["prefetch_proposals"] for v in observed),
        ),
        gateway_proposals=sum(v["gateway_proposals"] for v in observed),
        prefetch_diagnostics={
            k: sum(v["prefetch_diagnostics"][k] for v in observed)
            for k in ("admitted", "rejected", "executed")
        },
        owners={
            "eligible": sum(owners.values()),
            **{k: owners[k] for k in ("local", "external", "gateway", "unknown")},
        },
        first_token_ms=first_token_summary([c for v in values for c in v.get("model_calls", [])]),
        selection_by_attempted_selector=selectors,
    )


def render_m7_metrics(block: Mapping[str, Any]) -> list[str]:
    """Exact counts and unknowns, alongside the historical run-level rubric."""
    m = block["m7_metrics"]
    lines = ["| proposal metric | correct / observed |", "|---|---|"]
    for label, key in (
        ("model proposal accuracy", "model_proposal_accuracy"),
        ("prefetch accuracy", "prefetch_accuracy"),
    ):
        v = m[key]
        lines.append(f"| {label} | {v['numerator']} / {v['denominator']} |")
    lines += [
        "",
        f"Missing proposal traces: {m['missing_runs']}; "
        f"gateway proposals: {m['gateway_proposals']}; "
        f"prefetch diagnostics: {m['prefetch_diagnostics']}.",
        "",
        "| answer owner | completed runs |",
        "|---|---|",
    ]
    lines += [f"| {k} | {v} |" for k, v in m["owners"].items()]
    lines += [
        "",
        "| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |",
        "|---|---|---|---|",
    ]
    lines += [
        f"| {k} | {v['n']} / {v['eligible']} / {v['missing']} | {v['p50']} | {v['p95']} |"
        for k, v in m["first_token_ms"].items()
    ]
    lines += [
        "",
        "| attempted selector name/version/transport | attempted | failed | skipped | "
        "unobserved | scored | precision | recall | Jaccard |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    lines += [
        f"| {k} | "
        + " | ".join(
            str(v[x])
            for x in (
                "attempted",
                "failed",
                "skipped",
                "unobserved",
                "scored",
                "precision",
                "recall",
                "jaccard",
            )
        )
        + " |"
        for k, v in m["selection_by_attempted_selector"].items()
    ]
    return lines + [""]


def latency_first_tokens(document: Mapping[str, Any]) -> dict:
    """Keep the latency protocol's four populations apart, including launcher samples."""
    cold = document.get("cold_start", {})
    calls = []
    for phase, requests in (
        ("cold", [cold.get("cold_request", {})]),
        ("runner_warmup", document.get("warmups", [])),
        ("measured", document.get("samples", [])),
    ):
        for request in requests:
            calls.extend({**call, "phase": phase} for call in request.get("model_calls", []))
    startup = cold.get("startup_warmup", {})
    calls.extend(
        {
            "actor": "local",
            "model": document.get("environment", {}).get("model_id") or "unknown",
            **call,
            "phase": "startup_warmup",
        }
        for call in startup.get("samples") or []
    )
    return first_token_summary(calls)


def render_first_tokens(groups: Mapping[str, Any]) -> str:
    lines = [
        "",
        "| first token phase/actor/model/purpose | n / eligible / missing | p50 ms | p95 ms |",
        "|---|---|---|---|",
        *[
            f"| {key} | {v['n']} / {v['eligible']} / {v['missing']} | {v['p50']} | {v['p95']} |"
            for key, v in groups.items()
        ],
        "",
    ]
    return "\n".join(lines)
