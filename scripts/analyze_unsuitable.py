"""Offline #170 analysis: unsuitable detection on recorded phase 2 evidence (RM26, RM27).

Spec 0.48 section 14.4.7. The script reads the phase 2 evidence package (the
per-attempt ``.tar.zst`` archives under ``m8/``) and reports two things, with no
model, no credential, and no write inside the package:

1. A replay of both detection rules of spec 0.48 over every recorded M8 corpus
   attempt: the startup check (routing-shaped plus skill-shaped warm-up within
   active_seconds) over each case's warm-up samples, and the local stage rule
   (three latest stages that ran out of local time, within
   placement.probe_after_seconds) over the stage outcomes of all cases in the
   order they ended. The gateway's own functions decide: ``classify_warmup``,
   ``project_stage_outcome``, and ``stage_status``. Recorded handoff.started
   events have no budget field (it is new in 0.48), so a local_budget handoff
   whose local_active_ms reached active_seconds counts as one that ran out of
   local time; the report says how many outcomes needed that.
2. The section 12.3 unsuitable hypothesis as section 14.4.7 defines it: the
   local-first auto cases of the constrained configuration with the
   example-router chain (control) against the none-class auto cases with the
   example chain (candidate), paired by case and by latency schedule, with the
   pairing and verdict code of ``scripts/analyze_m8.py``.

The replay ignores the feedback the rule would have had: once a model is
unsuitable, auto makes no local stage, so the recorded later stages would not
have happened. It shows when the rule fires and the margin of the threshold,
not what the run would have measured.

Usage:
    python scripts/analyze_unsuitable.py --evidence docs/evidence/2026-10-26-phase2 \\
        --out docs/evidence/2026-10-27-unsuitable/analysis.json
"""

import argparse
import json
import sqlite3
import subprocess
import sys
import tempfile
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from analyze_m8 import (  # noqa: E402
    latency_direction,
    latency_guard,
    pair_corpus,
    pair_latency,
)

from relaymind.evaluation.prior import byte_digest  # noqa: E402
from relaymind.orchestrator.local_status import (  # noqa: E402
    STAGE_STREAK,
    LocalModelClass,
    stage_status,
)
from relaymind.orchestrator.warmup import WarmupResult, WarmupSample, classify_warmup  # noqa: E402
from relaymind.store.outcomes import StageOutcome, project_stage_outcome  # noqa: E402

END_EVENTS = ("handoff.started", "run.completed", "run.failed", "run.cancelled")
CONTROL = "placement-constrained-local_first-example-router-buffered"
CANDIDATE = "streaming-none-local_first-example-buffered"
LATENCY_METRICS = ("first_useful_content_ms", "completion_ms")


def _utc(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def extract(archive: Path, into: Path) -> Path:
    """Unpack one attempt archive (zstd --long=27, as the package README says)."""
    into.mkdir(parents=True, exist_ok=True)
    unzstd = subprocess.Popen(
        ["zstd", "-d", "--long=27", "-c", str(archive)], stdout=subprocess.PIPE
    )
    subprocess.run(["tar", "-x", "-C", str(into)], stdin=unzstd.stdout, check=True)
    if unzstd.wait() != 0:
        raise RuntimeError(f"zstd failed on {archive}")
    (export,) = into.glob("*/*/*/export.json")
    return export.parent


def _events(db: sqlite3.Connection, run_id: str) -> list[tuple[str, str, str, dict]]:
    return [
        (kind, actor, timestamp, json.loads(payload))
        for kind, actor, timestamp, payload in db.execute(
            "SELECT event_type, actor, timestamp, payload FROM events WHERE run_id = ?"
            " ORDER BY seq",
            (run_id,),
        )
    ]


def stage_of(run_id: str, events, active_seconds: float) -> tuple[StageOutcome | None, bool]:
    """The run's stage outcome by the gateway's projection, and whether the proxy was used."""
    accepted = events[0][3]
    progress = dict(
        execution_mode=accepted.get("effective_execution_mode"),
        local_generation=any(
            kind == "model.started" and actor == "local" and "branch" not in payload
            for kind, actor, _, payload in events
        ),
    )
    for kind, _, timestamp, payload in events:
        if kind not in END_EVENTS:
            continue
        proxy = False
        if (
            kind == "handoff.started"
            and "budget" not in payload
            and payload.get("reason_code") == "local_budget"
            and (payload.get("local_active_ms") or 0) >= active_seconds * 1000
        ):
            payload = {**payload, "budget": "active_seconds"}
            proxy = True
        ran_out = project_stage_outcome(progress, kind, payload)
        if ran_out is None:
            return None, False
        return StageOutcome(run_id, ran_out, _utc(timestamp)), proxy
    return None, False


def replay_attempt(directory: Path, configuration: dict) -> dict:
    budgets, placement = configuration["budgets"], configuration["placement"]
    active = float(budgets["active_seconds"])
    probe_after = float(placement["probe_after_seconds"])
    warmup_seconds = float(placement["warmup_seconds"])
    stages, accepts, warmups, proxies = [], [], Counter(), 0
    for store in sorted(directory.glob("checkpoint.work/*/runs.sqlite3")):
        db = sqlite3.connect(f"file:{store}?immutable=1", uri=True)
        samples = {
            purpose: duration
            for purpose, duration in db.execute(
                "SELECT purpose, duration_ms FROM model_timings WHERE kind = 'warm_up'"
                " AND succeeded = 1 ORDER BY sample_id"
            )
        }
        if "routing" in samples:
            result = WarmupResult(
                tuple(
                    WarmupSample(purpose, samples[purpose], None, None, None, None)
                    for purpose in ("routing", "skill")
                    if purpose in samples
                ),
                ready=True,
            )
            status = classify_warmup(
                result, seconds=warmup_seconds, active_seconds=active, stages=True
            )
            warmups[status.status.value] += 1
        for (run_id,) in db.execute("SELECT run_id FROM runs"):
            events = _events(db, run_id)
            accepted = events[0]
            accepts.append((_utc(accepted[2]), accepted[3].get("effective_execution_mode")))
            outcome, proxy = stage_of(run_id, events, active)
            if outcome is not None:
                stages.append(outcome)
                proxies += proxy
        db.close()
    stages.sort(key=lambda o: o.ended_at)
    flagged = auto = 0
    first = None
    for index, (moment, mode) in enumerate(sorted(accepts, key=lambda a: a[0])):
        if mode != "auto":
            continue
        auto += 1
        earlier = [o for o in stages if o.ended_at <= moment][::-1]
        status = stage_status(
            earlier, now=moment, probe_after_seconds=probe_after, active_seconds=active
        )
        if status is not None and status.status == LocalModelClass.UNSUITABLE:
            flagged += 1
            first = index if first is None else first
    streak = longest = 0
    for outcome in stages:
        streak = streak + 1 if outcome.ran_out_of_time else 0
        longest = max(longest, streak)
    return dict(
        active_seconds=active,
        probe_after_seconds=probe_after,
        warmup_classes=dict(sorted(warmups.items())),
        stages=len(stages),
        ran_out_of_time=sum(o.ran_out_of_time for o in stages),
        ran_out_by_budget_proxy=proxies,
        longest_streak=longest,
        auto_runs=auto,
        auto_runs_the_rule_classifies_unsuitable=flagged,
        first_unsuitable_acceptance_index=first,
    )


def _final_attempt(index: dict, cell: str) -> str:
    return index["attempts"][cell][-1]["name"]


def external_work(samples) -> dict:
    """Per latency label: external calls per request and the median time of one call.

    The mechanism behind the verdict: work a local stage leaves done is work the
    external stage does not repeat (section 14.4.7).
    """
    groups: dict[str, list] = {}
    for sample in samples:
        calls = [
            c
            for c in sample.get("model_calls") or []
            if c.get("actor") == "external" and c.get("phase", "measured") == "measured"
        ]
        groups.setdefault(sample["label"], []).append(calls)
    result = {}
    for label, requests in sorted(groups.items()):
        durations = sorted(
            c["duration_ms"] for calls in requests for c in calls if c.get("duration_ms")
        )
        result[label] = dict(
            requests=len(requests),
            external_calls_per_request=round(sum(map(len, requests)) / len(requests), 2),
            median_external_call_ms=durations[len(durations) // 2] if durations else None,
        )
    return result


def hypothesis(exports: dict) -> dict:
    """Section 12.3 as 14.4.7 defines it; the verdict code mirrors analyze_m8."""
    corpus = [
        [g for g in exports[f"{side}-corpus"]["grades"] if g["mode"] == "auto"]
        for side in (CONTROL, CANDIDATE)
    ]
    paired = pair_corpus(*corpus)
    latency = pair_latency(
        exports[f"{CONTROL}-latency"]["samples"], exports[f"{CANDIDATE}-latency"]["samples"]
    )
    guard = latency_guard(latency)
    directions = {
        m: latency_direction({side: latency["metrics"][m][side] for side in "AB"})
        for m in LATENCY_METRICS
    }
    lower = [d["lower_p95"] for d in directions.values()]
    value = (
        None
        if None in lower
        else False
        if guard["candidate_worse"]
        else paired["gain"] == paired["loss"] and all(lower)
    )
    status = {None: "not measured", True: "met", False: "not met"}[value]
    return dict(
        status=status,
        scope="constrained local_first auto (example-router chain, classified unsuitable by the"
        " spec 0.48 stage rule) against none-class auto (example chain), paired by case and"
        " latency schedule; equal observed task success, lower p95 first useful content and"
        " completion, and no more timeouts, errors or unobserved requests in the candidate",
        control=CONTROL,
        candidate=CANDIDATE,
        corpus_auto={k: paired[k] for k in ("N", "success_A", "success_B", "gain", "loss")},
        corpus_auto_groups={
            k: {f: v[f] for f in ("N", "success_A", "success_B", "gain", "loss")}
            for k, v in paired["groups"].items()
        },
        latency=directions,
        latency_reliability=guard,
        external_work={
            side: external_work(exports[f"{name}-latency"]["samples"])
            for side, name in (("control", CONTROL), ("candidate", CANDIDATE))
        },
        limitations=[
            "The arms ran at different times on 25 and 26 October 2026; provider latency may"
            " have drifted between them.",
            "Startup warm-up time is in neither arm.",
            "The control arm is the recorded local_first run; with the spec 0.48 rule live, its"
            " auto runs after the first streak would have been managed external execution.",
        ],
    )


def analyze(evidence: Path, work: Path) -> dict:
    index = json.loads((evidence / "m8" / "index.json").read_text())
    replay, exports = {}, {}
    wanted = {
        f"{side}-{runner}" for side in (CONTROL, CANDIDATE) for runner in ("corpus", "latency")
    }
    for cell in sorted(index["attempts"]):
        attempt = _final_attempt(index, cell)
        archive = evidence / "m8" / cell / f"{attempt}.tar.zst"
        if not (cell.endswith("-corpus") or cell in wanted):
            continue
        directory = extract(archive, work / cell)
        export = json.loads((directory / "export.json").read_text())
        if cell in wanted:
            exports[cell] = export
        if cell.endswith("-corpus") and export["configuration"]["local_model_configured"]:
            replay[cell] = dict(
                attempt=attempt,
                archive_sha256=byte_digest(archive),
                **replay_attempt(directory, export["configuration"]),
            )
        subprocess.run(["rm", "-rf", str(work / cell)], check=True)
    by_configuration = {}
    for configuration in ("normal", "constrained"):
        cells = [v for k, v in replay.items() if f"-{configuration}-" in k]
        by_configuration[configuration] = dict(
            cells=len(cells),
            longest_streak=max(c["longest_streak"] for c in cells),
            cells_where_the_rule_fires=sum(
                c["auto_runs_the_rule_classifies_unsuitable"] > 0 for c in cells
            ),
            warmup_classes=dict(
                sum((Counter(c["warmup_classes"]) for c in cells), Counter()).most_common()
            ),
        )
    return dict(
        kind="relaymind.unsuitable_detection_analysis",
        specification="0.48",
        stage_streak=STAGE_STREAK,
        evidence=evidence.relative_to(ROOT).as_posix()
        if evidence.is_relative_to(ROOT)
        else evidence.name,
        index_sha256=byte_digest(evidence / "m8" / "index.json"),
        replay=dict(by_configuration=by_configuration, by_cell=replay),
        hypothesis=hypothesis(exports),
    )


def render(report: dict) -> str:
    lines = [
        "# Unsuitable detection on the phase 2 evidence (#170, spec 0.48)",
        "",
        f"Stage rule: the latest {report['stage_streak']} local stages all ran out of local"
        " time, the latest within placement.probe_after_seconds. Index SHA-256"
        f" `{report['index_sha256']}`.",
        "",
        "## Replay per configuration",
        "",
        "| configuration | cells | cells where the stage rule fires | longest streak |"
        " startup classes (cases) |",
        "| --- | --- | --- | --- | --- |",
    ]
    for name, c in report["replay"]["by_configuration"].items():
        lines.append(
            f"| {name} | {c['cells']} | {c['cells_where_the_rule_fires']} |"
            f" {c['longest_streak']} | {c['warmup_classes']} |"
        )
    lines += [
        "",
        "## Replay per cell",
        "",
        "| cell | stages | ran out of time (by proxy) | longest streak | auto runs |"
        " auto runs classified unsuitable | first at acceptance | startup classes |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for cell, c in report["replay"]["by_cell"].items():
        lines.append(
            f"| {cell} | {c['stages']} | {c['ran_out_of_time']} ({c['ran_out_by_budget_proxy']})"
            f" | {c['longest_streak']} | {c['auto_runs']} |"
            f" {c['auto_runs_the_rule_classifies_unsuitable']} |"
            f" {c['first_unsuitable_acceptance_index']} | {c['warmup_classes']} |"
        )
    h = report["hypothesis"]
    corpus = h["corpus_auto"]
    lines += [
        "",
        "## Section 12.3 unsuitable hypothesis (section 14.4.7)",
        "",
        f"Status: **{h['status']}**. {h['scope']}.",
        "",
        f"- Control: `{h['control']}`; candidate: `{h['candidate']}`.",
        f"- Auto corpus pairs: N {corpus['N']}, success {corpus['success_A']} (control) vs"
        f" {corpus['success_B']} (candidate), gains {corpus['gain']}, losses {corpus['loss']}.",
    ]
    for metric, d in h["latency"].items():
        lines.append(
            f"- {metric}: p95 {d['p95_A_B'][0]} vs {d['p95_A_B'][1]} ms, p50 {d['p50_A_B'][0]}"
            f" vs {d['p50_A_B'][1]} ms, n {d['n_A_B'][0]} vs {d['n_A_B'][1]};"
            f" lower p95: {d['lower_p95']}; p50 agrees: {d['p50_agrees']}"
        )
    g = h["latency_reliability"]
    lines.append(
        f"- Latency reliability: control {g['A']['timeouts']} timeouts, {g['A']['errors']}"
        f" errors, {g['A']['unobserved_total']} unobserved; candidate {g['B']['timeouts']},"
        f" {g['B']['errors']}, {g['B']['unobserved_total']}; candidate worse:"
        f" {g['candidate_worse'] or 'no'}"
    )
    lines += ["", "External work per latency request (calls, median call ms):", ""]
    for side, labels in h["external_work"].items():
        parts = [
            f"{label} {w['external_calls_per_request']} calls, {w['median_external_call_ms']} ms"
            for label, w in labels.items()
        ]
        lines.append(f"- {side}: " + "; ".join(parts))
    lines += ["", "Limitations:", ""] + [f"- {item}" for item in h["limitations"]]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.out.exists():
        parser.error(f"{args.out} exists; the analysis never overwrites a file")
    with tempfile.TemporaryDirectory(prefix="relaymind-unsuitable-") as work:
        report = analyze(args.evidence.resolve(), Path(work))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n")
    args.out.with_suffix(".md").write_text(render(report))
    print(f"wrote {args.out} and {args.out.with_suffix('.md')}")


if __name__ == "__main__":
    main()
