"""Evaluation summaries and reports (spec 0.28 sections 12.1 to 12.3, #32, #33; RM25).

Corpus summaries keep exact numerators and denominators per mode,
language, and class, and never merge Chinese into English. Latency
summaries give nearest-rank p50 and p95, the sample count of each
metric, and the timeout fraction; errors and timeouts are counted,
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
        "rejections": dict(sorted(rejections.items())),
    }
    if simple:
        block["local_completion"] = rate(
            sum(bool(g["local_completion"]) for g in simple), len(simple)
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
                met = value["rate"] is not None and value["rate"] >= target
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
    ("local calls", lambda b: str(b["provider_calls"]["local"])),
    ("external calls", lambda b: str(b["provider_calls"]["external"])),
)


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
    lines += ["## Section 12.3 targets (English simple subset)", ""]
    for mode, entry in summary["targets"].items():
        parts = []
        for name, _, _ in _TARGETS:
            if name in entry:
                t = entry[name]
                verdict = "met" if t["met"] else "not met"
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
        values = [s[metric] for s in samples if s.get(metric) is not None]
        metrics[metric] = {
            "n": len(values),
            "p50": nearest_rank(values, 0.5),
            "p95": nearest_rank(values, 0.95),
        }
    timeouts = sum(bool(s["timed_out"]) for s in samples)
    return {
        "samples": len(samples),
        "timeouts": timeouts,
        "errors": sum(s["error"] is not None for s in samples),
        "timeout_fraction": None if not samples else timeouts / len(samples),
        "metrics": metrics,
    }


def summarize_latency(samples: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Overall and per-label percentiles, and the 500 ms hypothesis (section 12.1)."""
    samples = list(samples)
    overall = _latency_block(samples)
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
    return {"overall": overall, "by_label": by_label, "hypothesis": hypothesis}


def _ms(value: float | None) -> str:
    return "-" if value is None else f"{value:.0f}"


def render_latency_markdown(summary: Mapping[str, Any], *, title: str) -> str:
    lines = [f"# {title}", ""]
    for name, block in [("all", summary["overall"]), *summary["by_label"].items()]:
        fraction = block["timeout_fraction"]
        shown = "-" if fraction is None else f"{fraction:.3f}"
        lines += [
            f"## {name}: {block['samples']} samples, {block['timeouts']} timeouts "
            f"(fraction {shown}), {block['errors']} errors",
            "",
            "| metric | n | p50 ms | p95 ms |",
            "|---|---|---|---|",
        ]
        for metric, stats in block["metrics"].items():
            lines.append(f"| {metric} | {stats['n']} | {_ms(stats['p50'])} | {_ms(stats['p95'])} |")
        lines.append("")
    h = summary["hypothesis"]
    verdict = {True: "met", False: "not met", None: "not determined"}[h["met"]]
    lines.append(
        f"Hypothesis: p95 {h['metric']} <= {h['threshold_ms']} ms: {verdict} "
        f"(p95 {_ms(h['p95'])} ms over {h['n']} values, {h['samples']} samples, "
        f"{h['timeouts']} timeouts)."
    )
    return "\n".join(lines) + "\n"
