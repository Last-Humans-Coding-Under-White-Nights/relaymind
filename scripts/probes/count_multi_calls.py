"""Count the round trips that several read-only calls per generation would have saved (#119).

RM09, RM11, spec 0.31 sections 4.3 and 12.1; finding F4 of the 2026-10-23
analysis in docs/EVIDENCE.md.

A spike, offline: it reads a stored corpus export (``scripts/run_corpus.py``)
and calls no model. For every generation that proposed two or more calls
(the baseline refused each with ``multiple_tool_calls``) it classifies the
calls and applies the section 4.3 rule for ``calls_per_generation: many``:

- every call is read-only (``DEMO_TOOL_PERMISSIONS``); a mutating call keeps
  the rejection, because a confirmation binds one call (RM11);
- the set has at most the limit: the per-stage tool limit
  (``tool_calls`` of ``run.accepted``) in a local stage, the remaining
  entire-run limit (``run_tool_executions`` less the ``tool.started``
  events before the generation) in the external stage;
- every call passes its own validation: a known, offered tool, arguments
  that match the input schema (RM09), and identifiers that come from the
  request, a clarification answer, or an earlier tool result (RM05); one
  invalid call keeps the rejection for the set.

Saved generations are the later generations of the same run that
re-proposed a refused call one at a time (the same tool and canonical
arguments), when the set is admitted. They are a prediction from the
recorded behavior of the model, never a measurement: a model that sees all
results at once may act differently. A re-proposal of the same tool with
other arguments is counted apart and is not saved.

Usage:
    python scripts/probes/count_multi_calls.py \\
        --export docs/evidence/2026-10-22-corpus-apple-m1.json \\
        --out docs/evidence/<date>-multi-calls-<machine>.json
"""

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from relaymind.canonical import canonical_digest
from relaymind.evaluation.corpus import CLASSES, MODES
from relaymind.evaluation.environment import application_revision
from relaymind.orchestrator.identifiers import unsupported_identifiers
from relaymind.tools.registry import StrictValidator
from relaymind.tools.sandbox import (
    DEMO_TOOL_PERMISSIONS,
    DEMO_TOOL_SCHEMAS,
    MUTATE_PERMISSION,
    READ_PERMISSION,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_EXPORT = REPO_ROOT / "docs" / "evidence" / "2026-10-22-corpus-apple-m1.json"
LABEL = "offline analysis of recorded traces, not a measured run"
KIND = "relaymind.probe.multi_call_admission"
REFUSAL = "multiple_tool_calls"
_CLASSES = {READ_PERMISSION: "read", MUTATE_PERMISSION: "mutate"}
_VALIDATORS = {
    name: StrictValidator(schema["input_schema"]) for name, schema in DEMO_TOOL_SCHEMAS.items()
}


def call_key(name: str, arguments: Mapping[str, Any]) -> str:
    """One call's identity: the tool and the canonical arguments (RM10)."""
    return canonical_digest({"tool": name, "arguments": arguments})


def provenance_before(events: Sequence[Mapping[str, Any]], index: int) -> str:
    """The RM05 provenance at an event: user turns, clarification answers, tool result data."""
    parts: list[str] = []
    for event in events[:index]:
        kind, payload = event["event_type"], event["payload"]
        if kind == "run.accepted":
            parts += [
                m["content"]
                for m in payload.get("original_messages", [])
                if m.get("role") == "user" and m.get("content")
            ]
        elif kind == "clarification.answered" and payload.get("input"):
            parts.append(payload["input"])
        elif kind == "tool.finished":
            data = (payload.get("result") or {}).get("data")
            if data:
                parts.append(json.dumps(data, ensure_ascii=False))
    return "\n".join(parts)


def classify_call(
    name: str, arguments: Mapping[str, Any], *, offered: Sequence[str], provenance: str
) -> dict[str, Any]:
    """A call's class (read, mutate, or unknown) and its first validation problem, if any."""
    permission = DEMO_TOOL_PERMISSIONS.get(name)
    problem = None
    if permission is None:
        problem = "unknown_tool"
    elif name not in offered:
        problem = "tool_not_offered"
    elif not _VALIDATORS[name].is_valid(arguments):
        problem = "invalid_arguments"
    elif unsupported_identifiers(arguments, DEMO_TOOL_SCHEMAS[name]["input_schema"], provenance):
        problem = "unsupported_identifier"
    return {
        "tool": name,
        "arguments": dict(arguments),
        "class": _CLASSES.get(permission, "unknown"),
        "problem": problem,
    }


def generation_limit(actor: str, budgets: Mapping[str, Any], started_before: int) -> int:
    """Section 4.3: the per-stage tool limit locally, the remaining run limit externally."""
    if actor == "local":
        return int(budgets["tool_calls"])
    return max(int(budgets["run_tool_executions"]) - started_before, 0)


def decide(calls: Sequence[Mapping[str, Any]], limit: int) -> tuple[bool, str | None]:
    """Whether the set is admitted under calls_per_generation many, and why not (section 4.3)."""
    if any(call["class"] == "mutate" for call in calls):
        return False, "mutating"
    if len(calls) > limit:
        return False, "over_limit"
    if any(call["problem"] for call in calls):
        return False, "invalid_call"
    return True, None


def _calls_of(event: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return event["payload"].get("proposed_calls") or []


def _saved(
    later: Sequence[tuple[int, Mapping[str, Any]]],
    calls: Sequence[Mapping[str, Any]],
    used: set[int],
) -> tuple[int, int]:
    """The later one-call generations that re-proposed a refused call, and those that changed it.

    later holds (position, model.finished event) pairs. A later generation
    matches at most one refused call, and a refused call at most one
    generation; used holds the positions that an earlier set of the run
    already claimed. Same tool and canonical arguments is a re-proposal
    (saved); the same tool with other arguments is counted apart.
    """
    remaining = [(c["tool"], call_key(c["tool"], c["arguments"])) for c in calls]
    singles = [(p, _calls_of(e)[0]) for p, e in later if p not in used and len(_calls_of(e)) == 1]
    saved = changed = 0
    for position, proposed in singles:
        key = call_key(proposed["tool_name"], proposed["arguments"])
        if match := next((r for r in remaining if r[1] == key), None):
            remaining.remove(match)
            used.add(position)
            saved += 1
    for position, proposed in singles:
        if position not in used and (
            match := next((r for r in remaining if r[0] == proposed["tool_name"]), None)
        ):
            remaining.remove(match)
            used.add(position)
            changed += 1
    return saved, changed


def analyse_run(events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The multi-call generations of one run, and the refusals recorded in its trace."""
    accepted = next(e for e in events if e["event_type"] == "run.accepted")
    budgets = accepted["payload"]["budgets"]
    offered = {
        e["payload"]["call_id"]: e["payload"].get("tools_offered") or []
        for e in events
        if e["event_type"] == "model.started"
    }
    finished = [(i, e) for i, e in enumerate(events) if e["event_type"] == "model.finished"]
    refused = sum(
        1
        for e in events
        if e["event_type"] == "tool.proposed"
        and (e["payload"].get("rejection") or {}).get("code") == REFUSAL
    )
    used: set[int] = set()
    generations = []
    for index, event in finished:
        calls = _calls_of(event)
        if len(calls) < 2:
            continue
        actor = event["actor"]
        provenance = provenance_before(events, index)
        classified = [
            classify_call(
                c["tool_name"],
                c["arguments"],
                offered=offered.get(event["payload"]["call_id"], []),
                provenance=provenance,
            )
            for c in calls
        ]
        started = sum(1 for e in events[:index] if e["event_type"] == "tool.started")
        limit = generation_limit(actor, budgets, started)
        admitted, reason = decide(classified, limit)
        saved = changed = 0
        if admitted:
            saved, changed = _saved([(i, e) for i, e in finished if i > index], classified, used)
        generations.append(
            {
                "call_id": event["payload"]["call_id"],
                "actor": actor,
                "calls": classified,
                "limit": limit,
                "admitted": admitted,
                "reason": reason,
                "mixed": {c["class"] for c in classified} >= {"read", "mutate"},
                "saved_generations": saved,
                "reproposed_changed": changed,
            }
        )
    return {"generations": generations, "refused": refused, "model_calls": len(finished)}


def _counts() -> dict[str, Any]:
    return {
        "generations": 0,
        "proposals": 0,
        "admitted": 0,
        "kept_rejected": {},
        "mixed": 0,
        "saved_generations": 0,
        "reproposed_changed": 0,
    }


def _tally(target: dict[str, Any], generation: Mapping[str, Any]) -> None:
    target["generations"] += 1
    target["proposals"] += len(generation["calls"])
    target["admitted"] += generation["admitted"]
    target["mixed"] += generation["mixed"]
    target["saved_generations"] += generation["saved_generations"]
    target["reproposed_changed"] += generation["reproposed_changed"]
    if generation["reason"]:
        kept = target["kept_rejected"]
        kept[generation["reason"]] = kept.get(generation["reason"], 0) + 1


def analyse_export(document: Mapping[str, Any]) -> dict[str, Any]:
    """Classify every multi-call generation of a corpus export and count the saved round trips.

    The counts are per mode, language, and class, and per language; the
    all-language totals serve the cross-check against the trace alone,
    because Chinese results are never merged into English ones (section 12.2).
    """
    runs, rows = [], {}
    totals = {**_counts(), "actors": {}}
    by_language: dict[str, dict[str, Any]] = {}
    refused = 0
    for grade in document["grades"]:
        events = document["traces"].get(grade["run_id"], [])
        if not events:
            continue
        analysis = analyse_run(events)
        refused += analysis["refused"]
        for generation in analysis["generations"]:
            generation = {
                "case_id": grade["case_id"],
                "mode": grade["mode"],
                "language": grade["language"],
                "case_class": grade["case_class"],
                "run_id": grade["run_id"],
                **generation,
            }
            runs.append(generation)
            row = rows.setdefault(
                (grade["mode"], grade["language"], grade["case_class"]),
                {
                    "mode": grade["mode"],
                    "language": grade["language"],
                    "case_class": grade["case_class"],
                    "runs": set(),
                    "model_calls": 0,
                    **_counts(),
                },
            )
            if grade["run_id"] not in row["runs"]:
                row["runs"].add(grade["run_id"])
                row["model_calls"] += analysis["model_calls"]
            for target in (row, totals, by_language.setdefault(grade["language"], _counts())):
                _tally(target, generation)
            actors = totals["actors"]
            actors[generation["actor"]] = actors.get(generation["actor"], 0) + 1
    table = sorted(
        ({**row, "runs": len(row["runs"])} for row in rows.values()),
        key=lambda r: (MODES.index(r["mode"]), r["language"], CLASSES.index(r["case_class"])),
    )
    in_sets = totals["proposals"]
    return {
        "kind": KIND,
        "label": LABEL,
        "note": "Generations that proposed two or more calls, graded against the section 4.3 "
        "rule for calls_per_generation many. Saved generations are a prediction from recorded "
        "behavior, not a measurement.",
        "source": {"corpus": document.get("corpus"), "application": document.get("application")},
        "generations": runs,
        "totals": totals,
        "totals_by_language": dict(sorted(by_language.items())),
        "table": table,
        "cross_check": {
            "generations": totals["generations"],
            "proposals_in_multi_call_generations": in_sets,
            "proposals_refused_multiple_tool_calls": refused,
            "agree": in_sets == refused,
        },
    }


def _kept(counts: Mapping[str, int]) -> str:
    return "; ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "-"


def render_markdown(result: Mapping[str, Any]) -> str:
    """The report: per mode, language, and class, with the totals and the cross-check."""
    check, totals = result["cross_check"], result["totals"]
    lines = [
        f"# Several read-only calls per generation: {LABEL}",
        "",
        result["note"],
        "",
        "| mode | language | class | runs | model calls in these runs | generations "
        "| proposals | admitted | kept rejected | mixed | saved generations "
        "| re-proposed with other arguments |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in result["table"]:
        lines.append(
            f"| {row['mode']} | {row['language']} | {row['case_class']} | {row['runs']} "
            f"| {row['model_calls']} | {row['generations']} | {row['proposals']} "
            f"| {row['admitted']} | {_kept(row['kept_rejected'])} | {row['mixed']} "
            f"| {row['saved_generations']} | {row['reproposed_changed']} |"
        )
    for language, counts in result["totals_by_language"].items():
        lines.append(
            f"| all {language} | | | | | {counts['generations']} | {counts['proposals']} "
            f"| {counts['admitted']} | {_kept(counts['kept_rejected'])} | {counts['mixed']} "
            f"| {counts['saved_generations']} | {counts['reproposed_changed']} |"
        )
    lines += [
        "",
        f"Generations by actor: {_kept(totals['actors'])}.",
        "",
        f"Cross-check: {check['generations']} generations with "
        f"{check['proposals_in_multi_call_generations']} proposals; the trace records "
        f"{check['proposals_refused_multiple_tool_calls']} proposals refused with "
        f"{REFUSAL} ({'agree' if check['agree'] else 'DO NOT AGREE'}).",
        "",
    ]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--export", default=str(DEFAULT_EXPORT), help="a corpus export (JSON)")
    parser.add_argument("--out", required=True, help="the analysis (JSON); the report is .md")
    args = parser.parse_args(argv)
    source, out = Path(args.export), Path(args.out)
    if out.resolve() == source.resolve():
        parser.error("--out must differ from --export: the export is never changed")
    application = application_revision(REPO_ROOT)
    result = analyse_export(json.loads(source.read_text("utf-8")))
    result["analysed_with"] = {"application": application}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    report = render_markdown(result)
    out.with_suffix(".md").write_text(report, encoding="utf-8")
    sys.stdout.write(report)


if __name__ == "__main__":
    main()
