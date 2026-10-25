"""Evaluation summaries and reports (spec 0.28 sections 12.1 to 12.3, #32, #33; RM25).

Corpus summaries keep exact numerators and denominators per mode,
language, and class, and never merge Chinese into English. Latency
summaries give nearest-rank p50 and p95, the sample count of each
metric, and the timeout fraction; timeouts and other errors are counted apart,
never dropped.
"""

import math
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from relaymind.evaluation.corpus import CLASSES, LANGUAGES, MODES

LATENCY_METRICS = (
    "first_transport_ms",
    "first_model_action_ms",
    "first_useful_content_ms",
    "first_answer_token_ms",
    "completion_ms",
)
TRANSPORT_METRIC = "first_transport_ms"
HYPOTHESIS_METRIC = "first_model_action_ms"
HYPOTHESIS_MS = 500
# Section 12.3 proposed model-quality targets on the English simple subset.
LOCAL_COMPLETION_TARGET = 0.8
TOOL_CORRECTNESS_TARGET = 0.95
# (target name, block key, threshold) on the English simple subset.
_TARGETS = (
    ("local_completion", "local_completion", LOCAL_COMPLETION_TARGET),
    ("tool_correctness", "tool_correct", TOOL_CORRECTNESS_TARGET),
)


def nearest_rank(values: Sequence[float], q: float) -> float | None:
    """The ceil(q n)-th smallest value (nearest-rank method), or None without values."""
    if not 0 < q <= 1:
        raise ValueError("the quantile must be in (0, 1]")
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(math.ceil(q * len(ordered)), 1) - 1]


def rate(numerator: int, denominator: int) -> dict[str, Any]:
    return {
        "numerator": numerator,
        "denominator": denominator,
        "rate": None if denominator == 0 else numerator / denominator,
    }


# -- the correctness corpus -------------------------------------------------------------


def _corrections(grades: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """Corrections per completed run, with their summed duration (section 12.1, F6).

    None when no grade measured them (an export made before this metric),
    because that is not zero corrections (RM16). The duration is None when
    a rejected generation recorded none.
    """
    if not any("corrections" in g for g in grades):
        return None
    completed = [g for g in grades if g.get("state") == "completed"]
    measured = [
        g
        for g in completed
        if g.get("corrections") is not None
        and g.get("metrics", {}).get("trace_observed") is not False
    ]
    count = sum(g["corrections"] for g in measured)
    durations = [g.get("correction_ms") for g in measured if g["corrections"]]
    missing = len(completed) - len(measured)
    return {
        "completed_runs": len(completed),
        "measured_completed_runs": len(measured),
        "missing_completed_runs": missing,
        "corrections": count,
        "per_completed_run": count / len(completed) if completed and not missing else None,
        "duration_ms": None if missing or None in durations else sum(durations),
        "known_duration_ms": sum(v for v in durations if v is not None),
        "missing_duration_runs": durations.count(None),
    }


def _block(grades: Sequence[Mapping[str, Any]], routes: Mapping[str, str]) -> dict[str, Any]:
    simple = [g for g in grades if g["case_class"] == "simple"]
    decided = [g for g in grades if g["confirmation"] is not None and g["case_class"] == "mutation"]
    necessary = [g for g in grades if g["escalation"] and routes.get(g["case_id"]) == "external"]
    optional = [
        g for g in grades if g["escalation"] and routes.get(g["case_id"], "local") != "external"
    ]
    tokens: dict[str, dict[str, int]] = {}
    for g in grades:
        for model, used in g["tokens"].items():
            entry = tokens.setdefault(model, {"calls": 0, "input_tokens": 0, "output_tokens": 0})
            for key in entry:
                entry[key] += used[key]
    rejections: dict[str, int] = {}
    for g in grades:
        for code, count in g.get("rejections", {}).items():
            rejections[code] = rejections.get(code, 0) + count
    block = {
        "selector_provenance": {
            scope: {
                "runs": sum(
                    g.get("selector_provenance", {}).get("scope", "unknown") == scope
                    for g in grades
                ),
                "gateway_local_calls": sum(
                    g["local_calls"]
                    for g in grades
                    if g.get("selector_provenance", {}).get("scope", "unknown") == scope
                ),
                "gateway_external_calls": sum(
                    g["external_calls"]
                    for g in grades
                    if g.get("selector_provenance", {}).get("scope", "unknown") == scope
                ),
            }
            for scope in ("http_attempted", "gateway_observed", "unknown")
        },
        "answer_stream_policies": {
            policy: {
                "runs": len(group),
                "success": rate(sum(g["success"] for g in group), len(group)),
                "predicate_unmet": sum(g.get("error_code") == "predicate_unmet" for g in group),
            }
            for policy in ("buffered", "live", "unknown")
            if (
                group := [
                    g for g in grades if (g.get("answer_stream_effective") or "unknown") == policy
                ]
            )
        },
        "runs": len(grades),
        "success": rate(sum(g["success"] for g in grades), len(grades)),
        "tool_correct": rate(sum(g["tool_correct"] for g in grades), len(grades)),
        "schema_valid": rate(
            sum(g["schema_valid"] for g in grades), sum(g["proposals"] for g in grades)
        ),
        "timeouts": sum(g["timed_out"] for g in grades),
        "explicit_failures": sum(g["explicit_failure"] for g in grades),
        "missed_confirmations": sum(g["missed_confirmations"] for g in grades),
        "provider_calls": {
            "local": sum(g["local_calls"] for g in grades),
            "external": sum(g["external_calls"] for g in grades),
        },
        "tokens": dict(sorted(tokens.items())),
        "token_totals_scope": "known subtotals; missing counts in external_cost",
        "rejections": dict(sorted(rejections.items())),
    }
    if any("metrics" in g for g in grades):
        from relaymind.evaluation.m7_metrics import summarize_m7
        from relaymind.evaluation.placement_metrics import summarize_placement

        block["m7_metrics"] = summarize_m7(grades)
        block["m7_by_measured_class"] = {
            name: summarize_m7(
                [g for g in grades if g.get("metrics", {}).get("local_class", "unknown") == name]
            )
            for name in sorted({g.get("metrics", {}).get("local_class", "unknown") for g in grades})
        }
        block["placement_metrics"] = summarize_placement(grades)
        block["by_measured_class"] = {
            name: summarize_placement(
                [g for g in grades if g.get("metrics", {}).get("local_class", "unknown") == name]
            )
            for name in sorted({g.get("metrics", {}).get("local_class", "unknown") for g in grades})
        }
    corrections = _corrections(grades)
    if corrections is not None:
        block["corrections"] = corrections
    if simple:
        applicable = [g for g in simple if g["local_completion"] is not None]
        block["local_completion"] = rate(
            sum(bool(g["local_completion"]) for g in applicable), len(applicable)
        )
    if decided:
        block["confirmations_reached"] = rate(
            sum(g["confirmation_requested"] for g in decided), len(decided)
        )
    if necessary:
        block["missed_escalations"] = rate(
            sum(g["escalation"] == "missed" for g in necessary), len(necessary)
        )
    if optional:
        block["unnecessary_escalations"] = rate(
            sum(g["escalation"] == "unnecessary" for g in optional), len(optional)
        )
    return block


def summarize_corpus(
    grades: Iterable[Mapping[str, Any]], *, expected_routes: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """Per mode and language: an overall block and one block per class, plus the targets.

    expected_routes maps case IDs to their expected route; without it a
    case's route is taken from its class (complex cases are external).
    """
    grades = list(grades)
    routes = dict(expected_routes or {})
    for g in grades:
        routes.setdefault(g["case_id"], "external" if g["case_class"] == "complex" else "local")
    modes: dict[str, Any] = {}
    for mode in MODES:
        for language in LANGUAGES:
            subset = [g for g in grades if g["mode"] == mode and g["language"] == language]
            if not subset:
                continue
            classes = {
                name: _block([g for g in subset if g["case_class"] == name], routes)
                for name in CLASSES
                if any(g["case_class"] == name for g in subset)
            }
            modes.setdefault(mode, {})[language] = {
                "all": _block(subset, routes),
                "classes": classes,
            }
    return {"modes": modes, "targets": _targets(modes)}


def _targets(modes: Mapping[str, Any]) -> dict[str, Any]:
    """Section 12.3 targets on the English simple subset, and missed confirmations, per mode."""
    targets = {}
    for mode, languages in modes.items():
        english = languages.get("en")
        if english is None:
            continue
        entry: dict[str, Any] = {}
        simple = english["classes"].get("simple")
        if simple is not None:
            for name, key, target in _TARGETS:
                value = simple[key]
                met = None if value["rate"] is None else value["rate"] >= target
                entry[name] = {**value, "target": target, "met": met}
        # Section 12.3: no missed mandatory confirmation, in any language.
        missed = sum(data["all"]["missed_confirmations"] for data in languages.values())
        entry["no_missed_confirmation"] = {"missed": missed, "met": missed == 0}
        targets[mode] = entry
    return targets


def _fraction(value: Mapping[str, Any] | None) -> str:
    if value is None:
        return "-"
    return f"{value['numerator']}/{value['denominator']}"


def _rate_cell(key: str):
    return lambda block: _fraction(block.get(key))


def _corrections_cell(block: Mapping[str, Any]) -> str:
    value = block.get("corrections")
    if value is None:
        return "-"
    duration = "not measured" if value["duration_ms"] is None else f"{value['duration_ms']} ms"
    missing = value.get("missing_completed_runs", 0)
    suffix = f", missing completed: {missing}, rate unknown" if missing else ""
    return f"{value['corrections']}/{value['completed_runs']}, {duration}{suffix}"


# The corpus report columns: (label, cell of a summary block).
_COLUMNS = (
    ("runs", lambda b: str(b["runs"])),
    ("success", _rate_cell("success")),
    ("local completion", _rate_cell("local_completion")),
    ("tool correct", _rate_cell("tool_correct")),
    ("schema-valid proposals", _rate_cell("schema_valid")),
    ("missed escalations", _rate_cell("missed_escalations")),
    ("unnecessary escalations", _rate_cell("unnecessary_escalations")),
    ("confirmations reached", _rate_cell("confirmations_reached")),
    ("missed confirmations", lambda b: str(b["missed_confirmations"])),
    ("timeouts", lambda b: str(b["timeouts"])),
    ("explicit failures", lambda b: str(b["explicit_failures"])),
    ("corrections per completed run", _corrections_cell),
    ("local calls", lambda b: str(b["provider_calls"]["local"])),
    ("external calls", lambda b: str(b["provider_calls"]["external"])),
)


def _provenance_rows(groups: Mapping[str, Any], count: str) -> list[str]:
    return [
        "HTTP selector-internal model cost is not verified; "
        "model calls and tokens are gateway-observed only.",
        "",
        "| selector provenance | " + count + " |",
        "|---|---|",
        *[f"| {scope} | {block[count]} |" for scope, block in groups.items()],
        "",
    ]


def render_corpus_markdown(summary: Mapping[str, Any], *, title: str) -> str:
    """A Markdown report of a corpus summary: exact numerators and denominators."""
    lines = [f"# {title}", ""]
    for mode, languages in summary["modes"].items():
        for language, data in languages.items():
            lines += [f"## {mode}, {language}", ""]
            lines.append("| class | " + " | ".join(label for label, _ in _COLUMNS) + " |")
            lines.append("|---" * (len(_COLUMNS) + 1) + "|")
            for name, block in [*data["classes"].items(), ("all", data["all"])]:
                cells = [cell(block) for _, cell in _COLUMNS]
                lines.append(f"| {name} | " + " | ".join(cells) + " |")
            lines.append("")
            lines += _provenance_rows(data["all"].get("selector_provenance", {}), "runs")
            if "m7_metrics" in data["all"]:
                from relaymind.evaluation.m7_metrics import render_m7_metrics

                for class_name, class_block in [*data["classes"].items(), ("all", data["all"])]:
                    lines += [f"### M7 observations: {class_name}", ""]
                    lines += render_m7_metrics(class_block)

            if "placement_metrics" in data["all"]:
                lines += ["| measured metric | n / eligible | p50 | p95 |", "|---|---|---|---|"]
                measured = data["all"]["placement_metrics"]
                for name in ("prediction_error_ms", "wasted_local_ms"):
                    value = measured[name]
                    lines.append(
                        f"| {name} | {value['n']} / {value['eligible']} | "
                        f"{_ms(value['p50'])} | {_ms(value['p95'])} |"
                    )
                generations = measured["generations"]
                lines += [
                    "",
                    f"generations per completed run: {generations['per_completed_run']}; "
                    f"completed: {generations['completed_runs']}; "
                    f"measured: {generations['measured_completed_runs']}; "
                    f"missing: {generations['unobserved_completed_runs']}; "
                    f"actor/purpose totals: {generations['by_actor_purpose']}.",
                    "",
                ]
                cost = measured["external_cost"]
                lines += [
                    "| external cost | known total | missing calls | per successful task |",
                    "|---|---|---|---|",
                ]
                lines += [
                    f"| {key} (known token subtotal) | {cost[key + '_known_total']} | "
                    f"{cost[key + '_missing_calls']} | {cost[key + '_per_completed_task']} |"
                    for key in ("input_tokens", "output_tokens")
                ]

                lines += [
                    "",
                    f"External calls: {cost['calls']} across all attempts; "
                    f"successful tasks: {cost['completed_tasks']}; "
                    f"unobserved runs: {cost['unobserved_runs']}. "
                    "Unknown token counts stay null in the JSON export.",
                    "",
                ]

            lines += [
                "| effective answer stream | runs | task success | predicate_unmet |",
                "|---|---|---|---|",
            ]
            for policy, entry in data["all"].get("answer_stream_policies", {}).items():
                lines.append(
                    f"| {policy} | {entry['runs']} | {_fraction(entry['success'])} | "
                    f"{entry['predicate_unmet']} |"
                )
            lines.append("")
    lines += ["## Section 12.3 targets (English simple subset)", ""]
    for mode, entry in summary["targets"].items():
        parts = []
        for name, _, _ in _TARGETS:
            if name in entry:
                t = entry[name]
                verdict = {True: "met", False: "not met", None: "not applicable"}[t["met"]]
                parts.append(f"{name} {_fraction(t)} (target {t['target']:.0%}, {verdict})")
        missed = entry["no_missed_confirmation"]
        parts.append(
            f"missed confirmations {missed['missed']} ({'met' if missed['met'] else 'not met'})"
        )
        lines.append(f"- {mode}: " + "; ".join(parts))
    return "\n".join(lines) + "\n"


# -- latency ----------------------------------------------------------------------------


def _latency_block(samples: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    metrics = {}
    for metric in LATENCY_METRICS:
        values = [
            s[metric]
            for s in samples
            if s.get(metric) is not None
            and (
                metric != "completion_ms"
                or (
                    s.get("state") == "completed"
                    and s.get("error") is None
                    and not s.get("timed_out")
                )
            )
        ]
        metrics[metric] = {
            "n": len(values),
            "p50": nearest_rank(values, 0.5),
            "p95": nearest_rank(values, 0.95),
        }
    timeouts = sum(bool(s["timed_out"]) for s in samples)
    return {
        "samples": len(samples),
        "timeouts": timeouts,
        # A timeout is counted under timeouts only (section 12.2 names the two apart).
        "errors": sum(s["error"] is not None and not s["timed_out"] for s in samples),
        "timeout_fraction": None if not samples else timeouts / len(samples),
        "metrics": metrics,
    }


def summarize_latency(samples: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Overall and per-label percentiles, and the 500 ms hypothesis (section 12.1)."""
    samples = list(samples)
    overall = _latency_block(samples)
    if any("metrics" in s for s in samples):
        from relaymind.evaluation.placement_metrics import summarize_placement

        overall["placement_metrics"] = summarize_placement(
            [
                {**s, "success": s.get("state") == "completed" and s.get("error") is None}
                for s in samples
            ]
        )
    labels = sorted({s["label"] for s in samples})
    by_label = {
        label: _latency_block([s for s in samples if s["label"] == label]) for label in labels
    }
    action = overall["metrics"][HYPOTHESIS_METRIC]
    met = None
    if samples:
        # A timeout or a sample without an action may hide a slow action, so the
        # claim needs every measured sample to have one.
        met = (
            overall["timeouts"] == 0
            and action["n"] == overall["samples"]
            and action["p95"] is not None
            and action["p95"] <= HYPOTHESIS_MS
        )
    hypothesis = {
        "metric": HYPOTHESIS_METRIC,
        "threshold_ms": HYPOTHESIS_MS,
        "p95": action["p95"],
        "n": action["n"],
        "samples": overall["samples"],
        "timeouts": overall["timeouts"],
        "met": met,
    }
    scopes = {
        scope: _latency_block(
            [
                s
                for s in samples
                if s.get("selector_provenance", {}).get("scope", "unknown") == scope
            ]
        )
        for scope in ("http_attempted", "gateway_observed", "unknown")
    }
    return {
        "overall": overall,
        "by_label": by_label,
        "by_measured_class": {
            name: _latency_block(
                [s for s in samples if s.get("metrics", {}).get("local_class", "unknown") == name]
            )
            for name in sorted(
                {s.get("metrics", {}).get("local_class", "unknown") for s in samples}
            )
        },
        "hypothesis": hypothesis,
        "by_selector_provenance": scopes,
    }


def _ms(value: float | None) -> str:
    return "-" if value is None else f"{value:.0f}"


HEADER_TIME_NOTE = (
    "Early response headers were on: first_transport_ms is the header time, the arrival of "
    "the status line at acceptance; it is not first useful content (spec 0.32 section 7.3)."
)


def render_latency_markdown(
    summary: Mapping[str, Any], *, title: str, early_headers: bool = False
) -> str:
    """The report; under early headers the first transport event is labeled header time."""
    lines = [f"# {title}", ""]
    if early_headers:
        lines += [HEADER_TIME_NOTE, ""]
    for name, block in [("all", summary["overall"]), *summary["by_label"].items()]:
        fraction = block["timeout_fraction"]
        shown = "-" if fraction is None else f"{fraction:.3f}"
        lines += [
            f"## {name}: {block['samples']} samples, {block['timeouts']} timeouts "
            f"(fraction {shown}), {block['errors']} other errors",
            "",
            "| metric | n | p50 ms | p95 ms |",
            "|---|---|---|---|",
        ]
        for metric, stats in block["metrics"].items():
            metric_label = (
                f"{metric} (header time)"
                if early_headers and metric == TRANSPORT_METRIC
                else metric
            )
            lines.append(
                f"| {metric_label} | {stats['n']} | {_ms(stats['p50'])} | {_ms(stats['p95'])} |"
            )
        lines.append("")
    lines += _provenance_rows(summary.get("by_selector_provenance", {}), "samples")
    h = summary["hypothesis"]
    verdict = {True: "met", False: "not met", None: "not determined"}[h["met"]]
    lines.append(
        f"Hypothesis: p95 {h['metric']} <= {h['threshold_ms']} ms: {verdict} "
        f"(p95 {_ms(h['p95'])} ms over {h['n']} values, {h['samples']} samples, "
        f"{h['timeouts']} timeouts)."
    )
    return "\n".join(lines) + "\n"
