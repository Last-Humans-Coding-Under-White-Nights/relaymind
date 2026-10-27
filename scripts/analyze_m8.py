"""Offline M8 comparison report (RM16, RM25–RM28, RM32; AC19, AC21; spec 12.1–12.3, 14.4).

The script reads a finalized M8 matrix directory (plan.json, index.json, attempt
checkpoints and exports, frozen priors) and writes a JSON analysis with a Markdown
report next to it, outside the input. It never starts a model, reads credentials,
opens or seeds a run store, or writes inside the matrix. Outputs must be new files
outside this checkout, the matrix, and the matrix root and measurement checkout
that the index recorded.

The matrix may now live under another root than the one its index recorded. Attempt
files are found by the driver's relative layout (CELL/ATTEMPT/), and every recorded
argv, path and identity stays in the report as provenance, unchanged.

Acceptance comes first: all 31 cells must be complete, with exact identities, prior
linkage, (case_id, mode) coverage, latency schedule and byte digests. Incomplete or
changed evidence fails acceptance; it is never turned into zero success, cost or
timing. ``--inventory`` lists complete and missing cells without any comparison.

A latency request without trace metrics stays in every denominator (spec 12.2) as an
unobserved finding when the runner recorded why: a timeout, an error without a run
(connection refused, transport or HTTP error status), or an error after the run
started (including a failed trace read). Each verdict shows these counts per arm. A
request with no metrics and no recorded timeout or error is missing evidence and
fails acceptance (spec 14.4.4).

Initial expected-wait prerequisites are checked for presence only: recomputing them
needs a seeded run store, which this offline reader never writes.

Usage:
    python scripts/analyze_m8.py --matrix DIR --out FILE.json [--inventory]
"""

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
import latency_benchmark  # noqa: E402
import run_corpus  # noqa: E402
import run_m8_matrix as driver  # noqa: E402
from analyze_m7 import metric_coverage, physical_cost, verdict  # noqa: E402
from evaluation_config import configuration_from_args  # noqa: E402
from matrix_controller import exported_identity, offline_provider  # noqa: E402

from relaymind.canonical import canonical_digest  # noqa: E402
from relaymind.evaluation.checkpoint import atomic_json  # noqa: E402
from relaymind.evaluation.environment import source_fingerprint  # noqa: E402
from relaymind.evaluation.m7_metrics import summarize_m7  # noqa: E402
from relaymind.evaluation.metrics import (  # noqa: E402
    LATENCY_METRICS,
    rate,
    summarize_corpus,
    summarize_latency,
)
from relaymind.evaluation.placement_metrics import distribution, summarize_placement  # noqa: E402
from relaymind.evaluation.prior import byte_digest, load_prior  # noqa: E402
from relaymind.tools.sandbox import MUTATING_DEMO_TOOLS  # noqa: E402

LATENCY_SEED = 1342026
LATENCY_RUNS = 10
LATENCY_WARMUPS = 3
SIDECAR = ROOT / "evaluation" / "corpus.v1.skills.json"
# Inputs the analysis reads from this checkout; they must equal the measured bytes.
FROZEN_INPUTS = (
    "evaluation/corpus.v1.json",
    "evaluation/corpus.v1.lock.json",
    "evaluation/corpus.v1.skills.json",
    "evaluation/latency_prompts.v1.json",
)
VALIDATORS = (
    "scripts/run_corpus.py",
    "scripts/latency_benchmark.py",
    "scripts/evaluation_config.py",
    "scripts/matrix_controller.py",
    "scripts/run_m8_matrix.py",
)
CONFIGURATIONS = ("normal", "constrained")
POLICIES = ("local_first", "device_aware", "expected_wait")
CHAINS = ("router", "example-router")
DEFAULTS = dict(
    placement="local_first",
    selector_chain="model_router",
    answer_stream="buffered",
    changed=False,
    decision=(
        "Defaults stay unchanged. A subsequent evidence-backed specification revision "
        "makes the explicit keep or change decision (spec 14.4.3, #134)."
    ),
)
NOT_MEASURED = {
    "draft_local_completion": (
        "Every M8 candidate uses answer_pass=final.",
        "valid paired M7 C0/C1 corpus evidence (#128)",
    ),
    "prefetch_and_many_generations": (
        "M8 keeps prefetch off and one call per generation.",
        "M7 factor cells C0, C2 and C3 with the English complex population; "
        "no Chinese complex cohort exists",
    ),
    "criteria_corrections": (
        "M8 has no R-minus-criteria arm; its corrections are diagnostics only.",
        "the separately reviewed section 14.4.4 criteria ablation (M7-C6)",
    ),
    "early_headers": (
        "early_headers=false in every M8 cell.",
        "separate protocol or M7 L0/L1 evidence with a generation-start clock",
    ),
    "tool_narrowing_precision": (
        "M8 skill-chain data cannot measure tool narrowing.",
        "the section 12.4 ToolRet proposal and narrowing comparison on the locked files",
    ),
}


def _read_json(path: Path):
    return json.loads(path.read_bytes().decode("utf-8"))


def _check(condition, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _class(row) -> str:
    """The measured class from run.accepted, raw (ready, unsuitable, absent, ...)."""
    return (row.get("metrics") or {}).get("local_class") or "unknown"


def _successful(sample) -> bool:
    """A successful final answer: the population of completion latency (section 12.1)."""
    return (
        sample.get("state") == "completed"
        and sample.get("error") is None
        and not sample.get("timed_out")
    )


def unobserved_reason(sample) -> str | None:
    """Why a recorded latency request has no trace metrics; None when it has them.

    A metrics block counts only when it says the trace was observed (spec 14.4.4).
    """
    if (sample.get("metrics") or {}).get("trace_observed") is True:
        return None
    if sample.get("timed_out"):
        return "timeout"
    if sample.get("error") and sample.get("run_id") is None:
        return "error_without_run"
    if str(sample.get("error") or "").startswith("trace:"):
        return "trace_read_error"
    if sample.get("error"):
        return "error_with_run"
    return "missing_metrics"


def _outcome_rows(cell: dict, rows) -> list:
    """Latency samples carry no grader success; add the successful-final-answer outcome."""
    if cell["runner"] == "corpus":
        return list(rows)
    return [{**s, "success": _successful(s)} for s in rows]


def _arm_counts(rows) -> dict:
    """Per-arm request outcomes that every latency verdict reports next to its p95."""
    overall = summarize_latency(rows)["overall"]
    reasons = Counter(r for s in rows if (r := unobserved_reason(s)) is not None)
    return dict(
        {k: overall[k] for k in ("samples", "timeouts", "errors", "timeout_fraction")},
        unobserved=dict(sorted(reasons.items())),
        unobserved_total=sum(reasons.values()),
    )


def latency_guard(paired: dict) -> dict:
    """A candidate that loses more requests than its control cannot meet a latency target.

    Observed-value p95s drop timeouts and unobserved requests, so this mirrors the
    no-timeout condition of the 500 ms hypothesis.
    """
    a, b = paired["A"], paired["B"]
    worse = [k for k in ("timeouts", "errors", "unobserved_total") if b[k] > a[k]]
    return dict(A=a, B=b, candidate_worse=worse)


# -- pairing ----------------------------------------------------------------------------


def _keyed(rows, key, label: str) -> dict:
    keyed = {}
    for row in rows:
        k = key(row)
        if k in keyed:
            raise ValueError(f"duplicate {label} pair key {k}")
        keyed[k] = row
    return keyed


def _parity(gain: int, loss: int) -> str:
    if loss > gain:
        return "parity not met"
    if gain > loss:
        return "improved observed task success"
    return "equal observed task success"


def _paired(pairs) -> dict:
    n = len(pairs)
    a = [bool(x.get("success")) for x, _ in pairs]
    b = [bool(y.get("success")) for _, y in pairs]
    gain = sum(not x and y for x, y in zip(a, b, strict=True))
    loss = sum(x and not y for x, y in zip(a, b, strict=True))
    return dict(
        N=n,
        success_A=rate(sum(a), n),
        success_B=rate(sum(b), n),
        gain=gain,
        loss=loss,
        unchanged_success=sum(x and y for x, y in zip(a, b, strict=True)),
        unchanged_failure=sum(not x and not y for x, y in zip(a, b, strict=True)),
        success_delta=(gain - loss) / n if n else None,
        parity=_parity(gain, loss) if n else "no pairs",
    )


def _transitions(pairs) -> dict:
    return dict(sorted(Counter(f"{_class(x)}/{_class(y)}" for x, y in pairs).items()))


def pair_corpus(control, candidate) -> dict:
    """Paired task success by (case_id, mode); every planned case stays in each group."""
    a = _keyed(control, lambda g: (g["case_id"], g["mode"]), "corpus")
    b = _keyed(candidate, lambda g: (g["case_id"], g["mode"]), "corpus")
    if set(a) != set(b):
        raise ValueError("paired corpus cohort differs")
    pairs = []
    for key in sorted(a):
        x, y = a[key], b[key]
        if (x["language"], x["case_class"]) != (y["language"], y["case_class"]):
            raise ValueError(f"paired case identity differs for {key}")
        pairs.append((x, y))
    groups, matched = defaultdict(list), defaultdict(list)
    for x, y in pairs:
        groups[f"{x['mode']}/{x['language']}/{x['case_class']}"].append((x, y))
        if _class(x) == _class(y):
            matched[_class(x)].append((x, y))
    return dict(
        _paired(pairs),
        groups={k: _paired(v) for k, v in sorted(groups.items())},
        class_transitions=_transitions(pairs),
        unknown_class_pairs=sum("unknown" in (_class(x), _class(y)) for x, y in pairs),
        class_changed_pairs=sum(_class(x) != _class(y) for x, y in pairs),
        # A secondary scope with its own denominator; changed-class pairs stay above.
        by_matched_class={k: _paired(v) for k, v in sorted(matched.items())},
    )


def stream_failures(rows) -> dict:
    """RM32/AC21: the predicate_unmet headline counts effective live rows only.

    The same block is reported per measured class and per mode/language/class.
    """
    classes, groups = defaultdict(list), defaultdict(list)
    for r in rows:
        classes[_class(r)].append(r)
        groups[f"{r.get('mode')}/{r.get('language')}/{r.get('case_class')}"].append(r)
    return dict(
        _stream_block(rows),
        by_measured_class={k: _stream_block(v) for k, v in sorted(classes.items())},
        by_group={k: _stream_block(v) for k, v in sorted(groups.items())},
    )


def _stream_block(rows) -> dict:
    live = [r for r in rows if r.get("answer_stream_effective") == "live"]
    failed = sum(r.get("error_code") == "predicate_unmet" for r in live)
    effective = defaultdict(list)
    for r in rows:
        effective[r.get("answer_stream_effective") or "unknown"].append(r)
    return dict(
        runs=len(rows),
        configured=dict(Counter(r.get("answer_stream") or "unknown" for r in rows)),
        effective={k: len(v) for k, v in effective.items()},
        effective_live=len(live),
        predicate_unmet=failed,
        fraction=failed / len(live) if live else None,
        success_by_effective={
            k: rate(sum(bool(r.get("success")) for r in v), len(v)) for k, v in effective.items()
        },
        # A failed terminal after call-bound answer text: a diagnostic, not the gate.
        partial_stream_failures=sum(
            (r.get("metrics") or {}).get("streamed_failure") is True for r in rows
        ),
        partial_stream_unobserved=sum(
            "streamed_failure" not in (r.get("metrics") or {}) for r in rows
        ),
        terminal_errors=dict(Counter(r["error_code"] for r in rows if r.get("error_code"))),
    )


def _arm(rows, metric: str) -> dict:
    eligible = [s for s in rows if metric != "completion_ms" or _successful(s)]
    values = [s[metric] for s in eligible if s.get(metric) is not None]
    return distribution(values, eligible=len(eligible))


def _observed(sample, metric: str):
    if metric == "completion_ms" and not _successful(sample):
        return None
    return sample.get(metric)


def _latency_pairs(pairs) -> dict:
    metrics = {}
    for metric in LATENCY_METRICS:
        deltas = [
            _observed(y, metric) - _observed(x, metric)
            for x, y in pairs
            if _observed(x, metric) is not None and _observed(y, metric) is not None
        ]
        metrics[metric] = dict(
            A=_arm([x for x, _ in pairs], metric),
            B=_arm([y for _, y in pairs], metric),
            delta=distribution(deltas, eligible=len(pairs)),
        )
    return dict(
        N=len(pairs),
        metrics=metrics,
        A=_arm_counts([x for x, _ in pairs]),
        B=_arm_counts([y for _, y in pairs]),
    )


def pair_latency(control, candidate) -> dict:
    """Pairs by recorded seeded order and prompt; headline p95s use each arm's full data."""
    a = sorted(_keyed(control, lambda s: s["order"], "latency schedule").values(), key=_order)
    b = sorted(_keyed(candidate, lambda s: s["order"], "latency schedule").values(), key=_order)
    if [(s["prompt_id"], s["order"]) for s in a] != [(s["prompt_id"], s["order"]) for s in b]:
        raise ValueError("paired latency schedule differs")
    pairs = list(zip(a, b, strict=True))
    labels = defaultdict(list)
    for x, y in pairs:
        labels[x["label"]].append((x, y))
    by_class = {}
    for side, rows in (("A", a), ("B", b)):
        groups = defaultdict(list)
        for s in rows:
            groups[_class(s)].append(s)
        by_class[side] = {
            k: {m: _arm(v, m) for m in LATENCY_METRICS} for k, v in sorted(groups.items())
        }
    return dict(
        _latency_pairs(pairs),
        by_label={k: _latency_pairs(v) for k, v in sorted(labels.items())},
        class_transitions=_transitions(pairs),
        by_measured_class=by_class,
    )


def latency_direction(block: dict) -> dict:
    """Whether the candidate's p95 is lower; p50 disagreement is disclosed, never averaged."""
    p95 = [block[side]["p95"] for side in "AB"]
    p50 = [block[side]["p50"] for side in "AB"]
    return dict(
        p95_A_B=p95,
        p50_A_B=p50,
        n_A_B=[block[side]["n"] for side in "AB"],
        lower_p95=None if None in p95 else p95[1] < p95[0],
        p50_agrees=None if None in p50 + p95 else (p50[1] < p50[0]) == (p95[1] < p95[0]),
    )


def _order(sample):
    return sample["order"]


def comparison_pairs() -> list[dict]:
    """The 39 contract comparisons over the existing 31 cells; no extra cells."""
    ids = {c["id"] for c in driver.matrix_cells()}
    pairs = []

    def add(question, control, candidate, **scope):
        if control not in ids or candidate not in ids:
            raise ValueError("comparison names a cell outside the fixed matrix")
        pairs.append(dict(question=question, control=control, candidate=candidate, **scope))

    for h in CONFIGURATIONS:
        for k in ("corpus", "latency"):
            for c in CHAINS:

                def cell(policy, h=h, c=c, k=k):
                    return "-".join(("placement", h, policy, c, "buffered", k))

                scope = dict(configuration=h, chain=c, runner=k)
                add("device_aware_placement", cell("local_first"), cell("device_aware"), **scope)
                add(
                    "expected_wait_vs_baseline", cell("local_first"), cell("expected_wait"), **scope
                )
                add("expected_wait_rule", cell("device_aware"), cell("expected_wait"), **scope)
            for p in POLICIES:
                add(
                    "example_first_chain",
                    "-".join(("placement", h, p, "router", "buffered", k)),
                    "-".join(("placement", h, p, "example-router", "buffered", k)),
                    configuration=h,
                    policy=p,
                    runner=k,
                )
    add(
        "local_live_streaming",
        "placement-normal-local_first-router-buffered-corpus",
        "streaming-normal-local_first-router-live-corpus",
        configuration="normal",
        runner="corpus",
    )
    for k in ("corpus", "latency"):
        add(
            "none_live_streaming",
            f"streaming-none-local_first-example-buffered-{k}",
            f"streaming-none-local_first-example-live-{k}",
            configuration="none",
            runner=k,
        )
    return pairs


# -- coverage and identity --------------------------------------------------------------


def validate_case_coverage(cell: dict, document: dict, corpus) -> None:
    """The exact frozen (case_id, mode) set; language and class come from the locked corpus."""
    modes = ("auto", "external_only") + (() if cell["configuration"] == "none" else ("local_only",))
    cases = {case.case_id: case for case in corpus.cases}
    expected = {(case_id, mode) for case_id in cases for mode in modes}
    keys = [(g["case_id"], g["mode"]) for g in document["grades"]]
    _check(document["corpus"]["sha256"] == corpus.digest, "export corpus digest differs")
    _check(
        document.get("selection_sidecar_sha256") == byte_digest(SIDECAR),
        "export selection sidecar digest differs",
    )
    _check(len(keys) == len(set(keys)), "export has a duplicate case/mode grade")
    _check(
        set(keys) == expected and len(keys) == cell["requests"],
        "export case/mode coverage differs from the fixed matrix",
    )
    for g in document["grades"]:
        case = cases[g["case_id"]]
        _check(
            (g["language"], g["case_class"]) == (case.language, case.case_class),
            f"grade identity differs from the locked corpus for {g['case_id']}",
        )


def validate_latency_coverage(document: dict) -> None:
    """All 12 prompts ten times in the recorded seeded order, plus cold and warm-ups."""
    prompts = latency_benchmark.load_prompts()
    by_id = {p["id"]: p for p in prompts}
    design = document["design"]
    _check(document["prompts"]["sha256"] == canonical_digest(prompts), "prompt set differs")
    _check(
        design.get("seed") == LATENCY_SEED
        and design.get("runs_per_prompt") == LATENCY_RUNS
        and design.get("warmups") == LATENCY_WARMUPS,
        "latency seed or schedule design differs",
    )
    order = latency_benchmark.schedule(list(by_id), runs=LATENCY_RUNS, seed=LATENCY_SEED)
    _check(
        [(s["prompt_id"], s["order"]) for s in document["samples"]]
        == [(pid, i) for i, pid in enumerate(order)],
        "latency samples differ from the seeded schedule order",
    )
    _check(
        all(s["label"] == by_id[s["prompt_id"]]["label"] for s in document["samples"]),
        "latency sample label differs from its prompt",
    )
    _check(
        [w["prompt_id"] for w in document["warmups"]]
        == [p["id"] for p in latency_benchmark.warmup_prompts(prompts)],
        "latency warm-up population differs",
    )
    cold = (document.get("cold_start") or {}).get("cold_request")
    _check(cold and cold.get("prompt_id") == prompts[0]["id"], "latency cold request missing")


def latency_metric_coverage(document: dict) -> dict:
    """Trace metrics on every request, unless the runner recorded why there are none.

    A recorded timeout or error without metrics stays in every denominator as an
    unobserved finding (class unknown, spec 12.2). No metrics and no recorded reason
    is missing evidence and fails acceptance (spec 14.4.4).
    """
    missing, unobserved, reasons = [], [], Counter()
    for i, s in enumerate(document.get("samples", [])):
        reason = unobserved_reason(s)
        if reason == "missing_metrics":
            missing.append(i)
        elif reason is not None:
            unobserved.append(i)
            reasons[reason] += 1
    accepted = bool(document.get("samples")) and "first_token_ms" in document and not missing
    return dict(
        accepted=accepted,
        missing=missing,
        unobserved=unobserved,
        unobserved_reasons=dict(sorted(reasons.items())),
    )


def recorded_roots(index: dict) -> dict:
    """The matrix root and measurement checkout the index recorded, as recorded text."""
    roots = set()
    for cell_id, attempts in index.get("attempts", {}).items():
        for attempt in attempts:
            argv = attempt.get("argv")
            if not argv:
                continue
            recorded = argv[argv.index("--out") + 1]
            out = _recorded_path(recorded)
            _check(
                (out.name, out.parent.name, out.parent.parent.name)
                == ("export.json", attempt["name"], cell_id),
                f"{cell_id}/{attempt['name']}: recorded export path differs from the layout",
            )
            roots.add(_recorded_parent(recorded, 3))
    _check(len(roots) <= 1, "attempts record more than one matrix root")
    package = index.get("identity", {}).get("source", {}).get("package_path")
    return dict(
        matrix=next(iter(roots), None),
        checkout=_recorded_parent(package, 3) if package else None,
    )


def _recorded_path(path: str) -> PurePath:
    """A recorded path in the style it was recorded in, whatever the analyst platform."""
    windows = "\\" in path or (len(path) > 1 and path[1] == ":" and path[0].isalpha())
    return PureWindowsPath(path) if windows else PurePosixPath(path)


def _recorded_parent(path: str, levels: int) -> str:
    """Drop trailing parts of a recorded path as text, keeping its own separators."""
    for _ in range(levels):
        path = path[: max(path.rfind("/"), path.rfind("\\"))]
    return path


def expected_argv(cell: dict, attempt: dict, roots: dict, provider: dict) -> list[str]:
    """The driver's exact command for this attempt under the recorded roots.

    Paths keep the separators of the machine that recorded them, so a matrix recorded
    on Windows checks the same on POSIX and the other way round.
    """
    _check(roots["matrix"] and roots["checkout"], "index records no matrix or checkout root")
    recorded = _recorded_path(roots["matrix"])
    argv = driver.command(cell, recorded / cell["id"] / attempt["name"], recorded, provider)
    here, checkout = str(driver.ROOT), _recorded_path(roots["checkout"])
    sep = "\\" if isinstance(checkout, PureWindowsPath) else "/"
    return [attempt["argv"][0]] + [
        roots["checkout"] + v[len(here) :].replace(os.sep, sep)
        if v.startswith(here + os.sep)
        else v
        for v in argv[1:]
    ]


def _prerequisites(document: dict, runner: str) -> list:
    """Distinct initial expected-wait prerequisite reports, as exported."""
    if runner == "latency":
        reports = [(document.get("timing_prior") or {}).get("initial_expected_wait_prerequisites")]
    else:
        reports = [
            (r.get("timing_prior") or {}).get("initial_expected_wait_prerequisites")
            for r in document.get("records", {}).values()
        ]
    unique = {}
    for report in reports:
        if report is not None:
            unique.setdefault(canonical_digest(report), report)
    return list(unique.values())


def validate_prior(configuration: str, context: dict) -> dict:
    """The frozen prior links exactly to its own final calibration export."""
    calibration = f"calibration-{configuration}-local_first-router-buffered-corpus"
    view = context["views"].get(calibration)
    _check(view is not None, f"{calibration} is not complete, so its prior is unverified")
    bundle = _read_json(context["matrix"] / f"prior-{configuration}.json")
    linked = json.loads(json.dumps(bundle))
    for partition in linked.get("partitions", []):
        for source in partition.get("sources", []):
            source["file"] = str(source.get("file", "")).replace("\\", "/")
    expected = dict(
        schema_version=1,
        kind="relaymind.placement_prior",
        partitions=[
            dict(
                configuration=view["prior_configuration"],
                include_outcomes=True,
                sources=[
                    dict(
                        file=f"{calibration}/{view['attempt']}/export.json",
                        sha256=view["export_sha256"],
                        run_ids=view["run_ids"],
                    )
                ],
            )
        ],
    )
    _check(linked == expected, f"prior-{configuration}.json does not link to {calibration}")
    return bundle


def validate_cell(cell: dict, attempts: list, context: dict) -> dict:
    """Driver reconciliation semantics on retained files only; returns the analysis view."""
    names = [a.get("name") for a in attempts]
    _check(all(names) and len(set(names)) == len(names), "attempt names are not unique")
    _check(
        not any(a.get("status") == "complete" for a in attempts[:-1]),
        "an earlier attempt already finalized this cell",
    )
    final, matrix, corpus_runner = attempts[-1], context["matrix"], cell["runner"] == "corpus"
    seeded = cell["phase"] != "calibration" and cell["configuration"] != "none"
    prior_path = matrix / f"prior-{cell['configuration']}.json"
    _check(not seeded or prior_path.is_file(), "frozen prior file missing")
    prior_digest = byte_digest(prior_path) if seeded else None
    for attempt in attempts:
        if attempt.get("argv") is not None:
            _check(
                attempt["argv"]
                == expected_argv(cell, attempt, context["roots"], context["provider"]),
                f"attempt {attempt['name']} command identity differs from the fixed matrix",
            )
            _check(
                attempt.get("prior_sha256") == prior_digest,
                f"attempt {attempt['name']} prior digest differs",
            )
    argv = final.get("argv")
    _check(argv, "final attempt has no recorded command")
    directory = matrix / cell["id"] / final["name"]
    checkpoint_path, export_path = directory / "checkpoint.json", directory / "export.json"
    _check(
        checkpoint_path.is_file() and export_path.is_file(), "final checkpoint or export missing"
    )
    _check(
        byte_digest(checkpoint_path) == final.get("checkpoint_sha256"),
        "checkpoint bytes differ from the index",
    )
    export_digest = byte_digest(export_path)
    _check(export_digest == final.get("export_sha256"), "export bytes differ from the index")
    checkpoint = _read_json(checkpoint_path)
    steps = checkpoint.get("steps") or []
    _check(
        checkpoint.get("schema_version") == 1 and checkpoint.get("completed") is True,
        "checkpoint is not finalized",
    )
    _check(
        (checkpoint.get("result") or {}).get("export_sha256") == export_digest,
        "checkpoint result digest differs from the export bytes",
    )
    _check(
        len(steps) == cell["requests"] == final.get("requests")
        and len({s.get("identity") for s in steps}) == len(steps)
        and all(s.get("result") is not None for s in steps),
        "checkpoint request steps are incomplete or duplicated",
    )
    runner = run_corpus if corpus_runner else latency_benchmark
    args = runner.argument_parser().parse_args(argv[2:])
    settings = configuration_from_args(args)
    identity = checkpoint.get("identity") or {}
    _check(
        identity.get("arguments") == json.loads(json.dumps(vars(args))),
        "checkpoint arguments differ from the recorded command",
    )
    _check(
        identity.get("application") == context["index"]["identity"]["source"],
        "checkpoint source identity differs from the matrix identity",
    )
    _check(
        identity.get("configuration") == settings.to_json_object(),
        "checkpoint configuration differs from the recorded command",
    )
    prior = identity.get("prior")
    if seeded:
        if cell["configuration"] not in context["priors"]:
            context["priors"][cell["configuration"]] = validate_prior(
                cell["configuration"], context
            )
        # Spec 14.4.5: the exact retained prior identity. load_prior reads the bundle and
        # its relative sources under the actual matrix root and writes no store.
        partition = identity.get("prior_configuration")
        key = (cell["configuration"], canonical_digest(partition))
        if key not in context["loaded"]:
            context["loaded"][key] = load_prior(prior_path, partition).provenance
        _check(
            prior == context["loaded"][key] and prior.get("bundle_sha256") == prior_digest,
            "checkpoint prior identity differs from the frozen prior",
        )
    else:
        _check(prior is None, "calibration and none cells must have a null prior")
    document = _read_json(export_path)
    expected = exported_identity(
        dict(
            application=identity["application"],
            configuration=settings.to_json_object()
            if corpus_runner
            else runner.effective_configuration(settings, no_startup_warmup=args.no_startup_warmup),
            prior_configuration=identity.get("prior_configuration"),
            timing_prior=prior,
        ),
        root=_recorded_path(argv[1]).parents[1],
        work=_recorded_path(argv[argv.index("--checkpoint") + 1]).with_suffix(".work"),
    )
    environment = document if corpus_runner else document.get("environment") or {}
    timing_prior = document.get("timing_prior")
    if not corpus_runner and isinstance(timing_prior, dict):
        timing_prior = dict(timing_prior)
        _check(
            isinstance(timing_prior.pop("initial_expected_wait_prerequisites", None), list),
            "latency export lacks its initial expected-wait prerequisites",
        )
    _check(
        environment.get("application") == expected["application"]
        and document.get("configuration") == expected["configuration"]
        and document.get("prior_configuration") == expected["prior_configuration"]
        and timing_prior == expected["timing_prior"],
        "export identity differs from the checkpoint identity",
    )
    endpoint = environment.get("external_endpoint") or {}
    provider = context["provider"]
    _check(
        endpoint.get("base_url") == provider["RELAYMIND_EXTERNAL_BASE_URL"]
        and endpoint.get("model") == provider["RELAYMIND_EXTERNAL_MODEL"]
        and endpoint.get("context_limit") == context["index"]["identity"].get("context_limit"),
        "export provider identity differs from the matrix identity",
    )
    if corpus_runner:
        validate_case_coverage(cell, document, context["corpus"])
        coverage = metric_coverage(document)
        _check(coverage["accepted"], f"{len(coverage['missing'])} grades lack required metrics")
        rows = document["grades"]
        mutations = mutation_starts(rows, document.get("traces") or {})
    else:
        validate_latency_coverage(document)
        coverage = latency_metric_coverage(document)
        _check(coverage["accepted"], f"latency samples {coverage['missing']} lack required metrics")
        rows = document["samples"]
        mutations = None
    return dict(
        rows=rows,
        attempt=final["name"],
        export_sha256=export_digest,
        prior_configuration=document.get("prior_configuration"),
        run_ids=[
            r["run_id"] for r in document.get("records", {}).values() if r.get("run_id") is not None
        ],
        prerequisites=_prerequisites(document, cell["runner"]),
        learning=document.get("learning"),
        first_token_ms=document.get("first_token_ms"),
        mutations=mutations,
    )


def mutation_starts(grades, traces: dict) -> dict:
    """Started mutating demo tools from the exported traces, next to missed confirmations.

    It shows whether a zero missed-confirmation count is vacuous (no mutation started).
    """
    starts, runs, mutation_runs, untraced = 0, 0, 0, 0
    for g in grades:
        events = traces.get(g.get("run_id"))
        if events is None:
            untraced += 1
            continue
        count = sum(
            e.get("event_type") == "tool.started"
            and (e.get("payload") or {}).get("tool_name") in MUTATING_DEMO_TOOLS
            for e in events
        )
        starts += count
        runs += count > 0
        mutation_runs += count > 0 and g.get("case_class") == "mutation"
    return dict(
        mutating_tool_starts=starts,
        runs_with_mutating_start=runs,
        mutation_case_runs_with_start=mutation_runs,
        runs_without_trace=untraced,
    )


# -- cost -------------------------------------------------------------------------------


def ledger(attempts: list[dict], *, successes, scope: str) -> dict:
    """Physical attempt expenditure by immutable identity, failed attempts included."""
    missing = sorted({a["identity"] for a in attempts if a.get("cost") is None})
    result = physical_cost([a for a in attempts if a.get("cost") is not None], successes=successes)
    if missing:
        result.update({k: None for k in result if k.endswith("_per_successful_task")})
    return dict(scope=scope, **result, attempts_without_cost=missing)


def _successes(cell: dict, rows) -> int:
    if cell["runner"] == "corpus":
        return sum(bool(g.get("success")) for g in rows)
    return sum(_successful(s) for s in rows)


def _cost_attempts(cell: dict, attempts: list) -> list[dict]:
    return [dict(identity=f"{cell['id']}/{a.get('name')}", cost=a.get("cost")) for a in attempts]


# -- inventory --------------------------------------------------------------------------


def _matrix_checks(plan: dict, index: dict, matrix: Path, problems: list) -> dict:
    """Matrix-level identity; each failure is recorded and fails acceptance."""
    cells = driver.matrix_cells()

    def check(label, function):
        try:
            return function()
        except (ValueError, KeyError, TypeError, AttributeError, IndexError) as error:
            problems.append(f"{label}: {error}")
            return None

    identity = index.get("identity") or {}
    check(
        "plan",
        lambda: _check(
            identity.get("plan_sha256") == canonical_digest(plan),
            "plan digest differs from the index identity",
        ),
    )
    check(
        "plan",
        lambda: _check(
            plan.get("cells") == cells
            and plan.get("run_order") == [c["id"] for c in cells]
            and plan.get("corpus_runs")
            == sum(c["requests"] for c in cells if c["runner"] == "corpus")
            and plan.get("latency_requests")
            == sum(c["requests"] for c in cells if c["runner"] == "latency"),
            f"plan differs from the fixed {len(cells)}-cell M8 matrix",
        ),
    )
    source = check("source", lambda: identity["source"]) or {}
    check(
        "source",
        lambda: _check(source.get("head") and source.get("dirty") is False, "unclean source"),
    )
    provider = check("provider", lambda: offline_provider(index))
    roots = check("roots", lambda: recorded_roots(index)) or dict(matrix=None, checkout=None)
    # Fingerprint keys keep the recording host's separator; compare by POSIX name.
    measured = {k.replace("\\", "/"): v for k, v in (source.get("files") or {}).items()}
    inputs = sorted(
        {*FROZEN_INPUTS}
        | {
            str(p.relative_to(ROOT).as_posix())
            for p in (ROOT / "tests/fixtures/sandbox").glob("*.json")
        }
    )
    frozen = {
        name: dict(analyst=byte_digest(ROOT / name), measured=measured.get(name)) for name in inputs
    }
    changed = [name for name, v in frozen.items() if v["analyst"] != v["measured"]]
    if changed:
        problems.append("frozen inputs differ from the measured revision: " + ", ".join(changed))
    for entry in index.get("controller_history", []):
        check(
            "controller",
            lambda entry=entry: _check(
                entry.get("measurement_identity_sha256") == canonical_digest(identity),
                "controller history binds another measurement identity",
            ),
        )
        snapshot = entry.get("previous_index_file")
        if entry.get("previous_index_sha256") is not None:
            check(
                "controller",
                lambda entry=entry, snapshot=snapshot: _check(
                    snapshot
                    and (matrix / snapshot).is_file()
                    and byte_digest(matrix / snapshot) == entry["previous_index_sha256"],
                    "controller recovery index snapshot differs or is missing",
                ),
            )
    return dict(
        provider=provider,
        roots=roots,
        provenance=dict(
            matrix_root_actual=str(matrix),
            matrix_root_recorded=roots["matrix"],
            measurement_checkout_recorded=roots["checkout"],
            measured_revision=source.get("head"),
            measured_source_sha256=source.get("source_sha256"),
            runtime=identity.get("runtime"),
            runtime_sha256=identity.get("runtime_sha256"),
            provider=provider,
            context_limit=identity.get("context_limit"),
            plan_sha256=byte_digest(matrix / "plan.json"),
            index_sha256=byte_digest(matrix / "index.json"),
            controller_history=index.get("controller_history", []),
            frozen_inputs=frozen,
            validator_drift=[
                name
                for name in VALIDATORS
                if measured.get(name) is not None and measured[name] != byte_digest(ROOT / name)
            ],
            validator_note=(
                "Argument parsing and configuration serialization come from this analyst "
                "checkout; validator_drift lists files whose bytes differ from the measured "
                "revision."
            ),
        ),
    )


def _recorded_export(argv) -> str | None:
    return argv[argv.index("--out") + 1] if argv and "--out" in argv else None


def _inventory(matrix: Path) -> tuple[dict, dict]:
    matrix = matrix.resolve()
    _check(
        (matrix / "plan.json").is_file() and (matrix / "index.json").is_file(),
        "matrix plan.json or index.json missing",
    )
    plan, index = _read_json(matrix / "plan.json"), _read_json(matrix / "index.json")
    problems = []
    checked = _matrix_checks(plan, index, matrix, problems)
    context = dict(
        matrix=matrix,
        index=index,
        roots=checked["roots"],
        provider=checked["provider"],
        corpus=run_corpus.load_frozen_corpus(),
        priors={},
        loaded={},
        views={},
    )
    cells, lists = {}, {k: [] for k in ("complete", "incomplete", "missing", "invalid")}
    for cell in driver.matrix_cells():
        attempts = index.get("attempts", {}).get(cell["id"], [])
        record = dict(cell, attempts=attempts, status="missing", reason="no attempt")
        if attempts:
            final = attempts[-1]
            directory = f"{cell['id']}/{final.get('name')}"
            record.update(
                export=directory + "/export.json",
                export_recorded=_recorded_export(final.get("argv")),
                export_sha256=final.get("export_sha256"),
                checkpoint_sha256=final.get("checkpoint_sha256"),
            )
            report = matrix / directory / "export.md"
            if report.is_file():
                record.update(report=directory + "/export.md", report_sha256=byte_digest(report))
            if final.get("status") != "complete":
                record.update(status="incomplete", reason=f"final attempt {final.get('status')}")
            elif context["provider"] is None:
                record.update(status="invalid", reason="matrix provider identity is invalid")
            else:
                try:
                    view = validate_cell(cell, attempts, context)
                except (
                    ValueError,
                    KeyError,
                    TypeError,
                    AttributeError,
                    IndexError,
                    OSError,
                ) as error:
                    record.update(status="invalid", reason=str(error))
                else:
                    context["views"][cell["id"]] = view
                    record.update(status="complete", reason=None)
                    record.update(
                        measured_classes=dict(Counter(_class(r) for r in view["rows"])),
                        requests_validated=len(view["rows"]),
                        cohort_external_cost=summarize_placement(_outcome_rows(cell, view["rows"]))[
                            "external_cost"
                        ],
                        cohort_success_scope="corpus task success"
                        if cell["runner"] == "corpus"
                        else "latency successful final answers (not task success)",
                    )
        rows = context["views"].get(cell["id"], {}).get("rows")
        record["physical_cost"] = ledger(
            _cost_attempts(cell, attempts),
            successes=_successes(cell, rows) if rows is not None else None,
            scope="calibration population"
            if cell["phase"] == "calibration"
            else "candidate corpus task successes"
            if cell["runner"] == "corpus"
            else "latency successful final answers (endpoint outcome, not task success)",
        )
        lists[record["status"]].append(cell["id"])
        cells[cell["id"]] = record
    accepted = not problems and len(lists["complete"]) == len(driver.matrix_cells())
    public = dict(
        accepted=accepted,
        problems=problems,
        counts={k: len(v) for k, v in lists.items()},
        **lists,
        cells=cells,
        provenance=checked["provenance"],
    )
    return public, context["views"]


def inventory(matrix: Path) -> dict:
    """Which cells are complete and accepted, without any comparison or verdict."""
    return _inventory(matrix)[0]


# -- hypotheses -------------------------------------------------------------------------


def _all(values) -> bool | None:
    if any(v is False for v in values):
        return False
    return True if values and all(v is True for v in values) else None


def _any_scope(values) -> bool | None:
    if any(v is False for v in values):
        return False
    return True if any(v is True for v in values) else None


def _evidence(views, *cells) -> list[dict]:
    return [dict(cell=c, export_sha256=views[c]["export_sha256"]) for c in cells]


def hypotheses(views: dict) -> dict:
    """Section 12.3 targets and hypotheses as met, not met or not measured (spec 14.4.3)."""
    result = {}
    corpus = {c: v for c, v in views.items() if c.endswith("-corpus")}
    local = [c for c in corpus if not c.startswith("calibration") and "-none-" not in c]
    primary = [f"placement-{h}-local_first-router-buffered-corpus" for h in CONFIGURATIONS]

    def simple(cell, language="en"):
        modes = views[cell]["summary"]["modes"].get("auto", {})
        return (modes.get(language) or {}).get("classes", {}).get("simple") or {}

    def at_least(value, target):
        return None if value is None or value["rate"] is None else value["rate"] >= target

    completion = {
        c: dict(
            en=simple(c).get("local_completion"),
            zh=simple(c, "zh").get("local_completion"),
            met=at_least(simple(c).get("local_completion"), 0.8),
        )
        for c in local
    }
    result["english_simple_local_completion"] = dict(
        status=verdict(_all([completion[c]["met"] for c in primary])),
        evidence=_evidence(views, *primary),
        scope="auto/en/simple, proven local final owner and zero external calls; target 80%; "
        "baseline local_first/router cells per configuration, never pooled; Chinese apart",
        by_cell=completion,
    )
    proposals = {
        c: dict(
            model_proposals=(simple(c).get("m7_metrics") or {}).get("model_proposal_accuracy"),
            legacy_run_rubric=simple(c).get("tool_correct"),
        )
        for c in local
    }
    for value in proposals.values():
        value["met"] = at_least(value["model_proposals"], 0.95)
    result["english_simple_tool_proposals"] = dict(
        status=verdict(_all([proposals[c]["met"] for c in primary])),
        evidence=_evidence(views, *primary),
        scope="correct observed model proposals over observed model proposals (auto/en/simple); "
        "the run-level tool_correct rubric is reported apart; n=0 is not measured",
        by_cell=proposals,
    )
    missed = {}
    for c in corpus:
        rows = views[c]["rows"]
        decided = [
            g for g in rows if g["case_class"] == "mutation" and g.get("confirmation") is not None
        ]
        missed[c] = dict(
            missed=sum(g.get("missed_confirmations", 0) for g in rows),
            confirmations_reached=rate(
                sum(bool(g.get("confirmation_requested")) for g in decided), len(decided)
            ),
            **(views[c]["mutations"] or {}),
        )
    result["no_missed_confirmation"] = dict(
        status=verdict(sum(v["missed"] for v in missed.values()) == 0),
        evidence=_evidence(views, *corpus),
        scope="every corpus cell, mode and language; reached confirmations and started "
        "mutating tools (from the exported traces) expose a vacuous zero",
        by_cell=missed,
    )
    latency = [
        f"placement-{h}-{p}-router-buffered-latency" for h in CONFIGURATIONS for p in POLICIES
    ]
    action = {c: views[c]["summary"]["hypothesis"] for c in latency}
    primary_latency = [f"placement-{h}-local_first-router-buffered-latency" for h in CONFIGURATIONS]
    result["warm_500ms_model_action"] = dict(
        status=verdict(_all([action[c]["met"] for c in primary_latency])),
        evidence=_evidence(views, *primary_latency),
        scope="p95 first model action <= 500 ms over all 120 measured requests, no timeouts; "
        "per configuration; example chains and the none class are excluded because "
        "deterministic example selection is not a measured model action",
        by_cell=action,
    )
    scopes = {}
    for h in CONFIGURATIONS:
        for c in CHAINS:
            base = f"placement-{h}-{{}}-{c}-buffered-{{}}"
            a, b = base.format("local_first", "corpus"), base.format("device_aware", "corpus")
            auto = [[g for g in views[x]["rows"] if g["mode"] == "auto"] for x in (a, b)]
            matched = pair_corpus(*auto)
            unsuitable = matched["by_matched_class"].get("unsuitable")
            la, lb = base.format("local_first", "latency"), base.format("device_aware", "latency")
            full = pair_latency(views[la]["rows"], views[lb]["rows"])
            guard = latency_guard(full)
            timing = full["by_measured_class"]
            directions = {
                m: latency_direction(
                    {side: (timing[side].get("unsuitable") or {}).get(m) for side in "AB"}
                )
                if all(timing[side].get("unsuitable") for side in "AB")
                else None
                for m in ("first_useful_content_ms", "completion_ms")
            }
            lower = [d and d["lower_p95"] for d in directions.values()]
            # No measured unsuitable cohort means the hypothesis was not tested here,
            # whatever the full-population reliability shows.
            value = (
                None
                if unsuitable is None or None in lower
                else False
                if guard["candidate_worse"]
                else unsuitable["gain"] == unsuitable["loss"] and all(lower)
            )
            scopes[f"{h}/{c}"] = dict(
                value=value,
                status=verdict(value),
                matched_unsuitable_auto=unsuitable,
                unsuitable_latency=directions,
                latency_reliability=guard,
                class_transitions=matched["class_transitions"],
                full_configuration_auto=dict(
                    {k: matched[k] for k in ("N", "success_A", "success_B", "gain", "loss")}
                ),
                evidence=[a, b, la, lb],
            )
    result["unsuitable_device_aware"] = dict(
        status=verdict(_any_scope([v.pop("value") for v in scopes.values()])),
        evidence=_evidence(views, *{c for v in scopes.values() for c in v["evidence"]}),
        scope="matched-unsuitable auto corpus pairs at equal observed success, and each arm's "
        "unsuitable latency population with lower p95 first useful content and completion; "
        "not met when the candidate arm has more timeouts, errors or unobserved requests; "
        "scopes without an unsuitable cohort are not measured",
        by_scope=scopes,
    )
    jaccard = {}
    for h in CONFIGURATIONS:
        for p in POLICIES:
            cells = [f"placement-{h}-{p}-{c}-buffered-corpus" for c in CHAINS]
            english = [
                [g for g in views[x]["rows"] if g["mode"] == "auto" and g["language"] == "en"]
                for x in cells
            ]
            scores = [summarize_placement(rows)["selection"]["jaccard"] for rows in english]
            jaccard[f"{h}/{p}"] = dict(
                router=scores[0],
                example_router_chain=scores[1],
                met=None if None in scores else scores[1] >= scores[0],
                attempted_selectors=summarize_m7(english[1])["selection_by_attempted_selector"],
                evidence=cells,
            )
    component = _all([v["met"] for v in jaccard.values()])
    result["example_matcher_jaccard"] = dict(
        status="not met" if component is False else "not measured",
        corpus_component=verdict(component),
        evidence=_evidence(views, *{c for v in jaccard.values() for c in v["evidence"]}),
        scope="English auto initial-selection chain Jaccard, example-router chain versus router; "
        "a chain score does not prove standalone matcher accuracy",
        required_external_evidence="the section 12.4 SkillsBench comparison under the router's "
        "eight-skill cap, with its locked exports and revisions",
        by_scope=jaccard,
    )
    none_corpus = pair_corpus(
        views["streaming-none-local_first-example-buffered-corpus"]["rows"],
        views["streaming-none-local_first-example-live-corpus"]["rows"],
    )
    none_full = pair_latency(
        views["streaming-none-local_first-example-buffered-latency"]["rows"],
        views["streaming-none-local_first-example-live-latency"]["rows"],
    )
    none_guard = latency_guard(none_full)
    direction = latency_direction(none_full["metrics"]["first_useful_content_ms"])
    lower = direction["lower_p95"]
    value = (
        False
        if none_guard["candidate_worse"]
        else None
        if lower is None
        else none_corpus["loss"] <= none_corpus["gain"] and lower
    )
    result["none_live_streaming"] = dict(
        status=verdict(value),
        evidence=_evidence(
            views,
            *(
                f"streaming-none-local_first-example-{s}-{k}"
                for s in ("buffered", "live")
                for k in ("corpus", "latency")
            ),
        ),
        scope="none-class corpus no-lower task success and lower p95 first useful content; "
        "not met when the live arm has more timeouts, errors or unobserved requests",
        corpus={
            k: none_corpus[k] for k in ("N", "success_A", "success_B", "gain", "loss", "parity")
        },
        first_useful_content_ms=direction,
        latency_reliability=none_guard,
        live_stream_failures=stream_failures(
            views["streaming-none-local_first-example-live-corpus"]["rows"]
        ),
    )
    local_pair = pair_corpus(
        views["placement-normal-local_first-router-buffered-corpus"]["rows"],
        views["streaming-normal-local_first-router-live-corpus"]["rows"],
    )
    result["local_live_streaming_parity"] = dict(
        status=verdict(local_pair["loss"] <= local_pair["gain"]),
        evidence=_evidence(
            views,
            "placement-normal-local_first-router-buffered-corpus",
            "streaming-normal-local_first-router-live-corpus",
        ),
        scope="RM32 #125 configured-arm parity on one revision and frozen prior; "
        "no statistical equivalence margin is specified",
        paired={
            k: local_pair[k] for k in ("N", "success_A", "success_B", "gain", "loss", "parity")
        },
        live_stream_failures=stream_failures(
            views["streaming-normal-local_first-router-live-corpus"]["rows"]
        ),
    )
    candidates = [c for c in views if not c.startswith("calibration")]
    covered, costs = [], {}
    for c in candidates:
        rows = views[c]["rows"]
        if c.endswith("-corpus"):
            covered.append(metric_coverage({"grades": rows})["accepted"])
            scope, outcome = "corpus task success", rows
        else:
            document = {"samples": rows, "first_token_ms": views[c]["first_token_ms"]}
            covered.append(latency_metric_coverage(document)["accepted"])
            scope = "latency successful final answers (not task success)"
            outcome = [{**s, "success": _successful(s)} for s in rows]
        costs[c] = dict(summarize_placement(outcome)["external_cost"], success_scope=scope)
    result["external_cost_next_to_success"] = dict(
        status=verdict(all(covered)),
        evidence=_evidence(views, *candidates),
        scope="observed external calls and per-model usage next to task success in every "
        "candidate corpus and latency cell; unknown usage stays explicit; no monetary claim "
        "without a dated rate source",
        by_cell=costs,
    )
    for name, (reason, evidence) in NOT_MEASURED.items():
        result[name] = dict(
            status="not measured", evidence=[], scope=reason, required_external_evidence=evidence
        )
    return result


def _local_success_rate(row: dict) -> float | None:
    """Completed over completed plus escalated; unknown when a count is missing or null."""
    completed, escalated = row.get("local_completed"), row.get("local_escalated")
    if not all(isinstance(n, int) and not isinstance(n, bool) for n in (completed, escalated)):
        return None
    return completed / (completed + escalated) if completed + escalated else None


def expected_wait(views: dict) -> dict:
    """Applicability of expected_wait: observations and initial prerequisites (section 14.4.3)."""
    result = {}
    for cell_id, view in views.items():
        if "-expected_wait-" not in cell_id:
            continue
        variants = []
        for report in view["prerequisites"]:
            rows = []
            for row in report:
                rows.append(dict(row, p_local=_local_success_rate(row)))
            variants.append(rows)
        result[cell_id] = dict(
            placement_observations=summarize_placement(view["rows"])["expected_wait"],
            # One shared report is the expected case; every distinct variant is kept.
            initial_prerequisites=variants[0] if len(variants) == 1 else None,
            prerequisite_variants=variants,
            prerequisite_variant_count=len(variants),
            learning=view["learning"],
            note="Initial prerequisites do not prove the inequality was evaluated after startup "
            "or online learning; zero evaluations is not evidence for the hypothesis. The "
            "specification sets no expected_wait promotion threshold. Prerequisites are "
            "checked for presence only; recomputing them needs a seeded run store.",
        )
    return result


# -- report -----------------------------------------------------------------------------


def analyze(matrix: Path) -> tuple[dict, str]:
    public, views = _inventory(matrix)
    if not public["accepted"]:
        invalid = [f"{c}: {public['cells'][c]['reason']}" for c in public["invalid"][:3]]
        raise ValueError(
            "matrix inventory is not accepted "
            f"({public['counts']['complete']} of {len(driver.matrix_cells())} cells complete; "
            f"problems: {public['problems'] or 'none'}; invalid: {invalid or 'none'}); "
            "use --inventory to list cells"
        )
    cells = {c["id"]: c for c in driver.matrix_cells()}
    for cell_id, view in views.items():
        rows = view["rows"]
        view["summary"] = (
            summarize_corpus(rows)
            if cells[cell_id]["runner"] == "corpus"
            else summarize_latency(rows)
        )
        # Detached reaggregation of the exported rows; the export bytes stay untouched.
        public["cells"][cell_id].update(
            summary=view["summary"],
            placement=summarize_placement(_outcome_rows(cells[cell_id], rows)),
            first_token_ms=view["first_token_ms"],
            mutations=view["mutations"],
        )
    comparisons = []
    for pair in comparison_pairs():
        a, b = views[pair["control"]]["rows"], views[pair["candidate"]]["rows"]
        entry = dict(pair, evidence=_evidence(views, pair["control"], pair["candidate"]))
        if pair["runner"] == "corpus":
            entry["paired"] = pair_corpus(a, b)
            entry["stream_failures"] = {"A": stream_failures(a), "B": stream_failures(b)}
            entry["selector_provenance"] = {
                side: dict(
                    Counter(
                        (g.get("selector_provenance") or {}).get("scope", "unknown") for g in rows
                    )
                )
                for side, rows in (("A", a), ("B", b))
            }
        else:
            entry["paired"] = pair_latency(a, b)
        entry["expected_wait"] = {
            side: public["cells"][pair[role]]["placement"]["expected_wait"]
            for side, role in (("A", "control"), ("B", "candidate"))
        }
        entry["physical_cost"] = {
            side: public["cells"][pair[role]]["physical_cost"]
            for side, role in (("A", "control"), ("B", "candidate"))
        }
        comparisons.append(entry)

    def scoped(predicate, scope):
        chosen = [c for c in driver.matrix_cells() if predicate(c)]
        return ledger(
            [a for c in chosen for a in _cost_attempts(c, public["cells"][c["id"]]["attempts"])],
            successes=sum(_successes(c, views[c["id"]]["rows"]) for c in chosen),
            scope=scope,
        )

    report = dict(
        kind="relaymind.m8_analysis",
        reporting_accepted=True,
        analyst={k: v for k, v in source_fingerprint(ROOT).items() if k != "files"},
        provenance=public["provenance"],
        inventory={
            k: public[k] for k in ("counts", "complete", "incomplete", "missing", "invalid")
        },
        cells=public["cells"],
        comparisons=comparisons,
        physical_cost=dict(
            calibration=scoped(lambda c: c["phase"] == "calibration", "calibration population"),
            candidate_corpus=scoped(
                lambda c: c["phase"] != "calibration" and c["runner"] == "corpus",
                "candidate corpus task successes",
            ),
            candidate_latency=scoped(
                lambda c: c["runner"] == "latency",
                "latency successful final answers (endpoint outcome, not task success)",
            ),
        ),
        expected_wait=expected_wait(views),
        hypotheses=hypotheses(views),
        defaults=DEFAULTS,
        formulas=dict(
            success_delta="(gain - loss) / N over paired (case_id, mode)",
            stream_failure_fraction="predicate_unmet among effective live / effective live",
            quantile="nearest rank: sorted(X)[ceil(q * len(X)) - 1]",
            latency_pairs="(prompt_id, recorded order); deltas only where both are observed",
            p_local="local_completed / (local_completed + local_escalated)",
        ),
        limitations=[
            "One pass per cell on one machine; provider drift and sampling uncertainty remain.",
            "The corpus starts a new gateway per case and mode; latency learns online. "
            "Initial priors, not final stores, are the common control.",
            "A constrained hardware label does not prove the unsuitable class; measured "
            "classes are reported raw.",
        ],
    )
    return report, render(report)


def _fraction(value) -> str:
    return "-" if not value else f"{value['numerator']}/{value['denominator']}"


def _ms(value) -> str:
    return "-" if value is None else f"{value:.0f}"


def _delta(value) -> str:
    return "-" if value is None else f"{value:.3f}"


def _inventory_lines(public: dict) -> list[str]:
    counts = public["counts"]
    lines = [
        f"{counts['complete']} of {sum(counts.values())} cells complete; "
        f"{counts['incomplete']} incomplete, {counts['missing']} missing, "
        f"{counts['invalid']} invalid.",
        "",
        "| cell | status | attempts | export sha256 | reason |",
        "|---|---|---|---|---|",
    ]
    for cell_id, record in public["cells"].items():
        attempts = ", ".join(f"{a.get('name')}:{a.get('status')}" for a in record["attempts"])
        digest = (record.get("export_sha256") or "-")[:12]
        lines.append(
            f"| {cell_id} | {record['status']} | {attempts or '-'} | {digest} | "
            f"{record.get('reason') or ''} |"
        )
    return lines


def _provenance_lines(provenance: dict) -> list[str]:
    return [
        f"- Measured revision: {provenance['measured_revision']}",
        f"- Matrix root (actual): {provenance['matrix_root_actual']}",
        f"- Matrix root (recorded): {provenance['matrix_root_recorded']}",
        f"- Measurement checkout (recorded): {provenance['measurement_checkout_recorded']}",
        f"- plan.json sha256: {provenance['plan_sha256']}",
        f"- index.json sha256: {provenance['index_sha256']}",
        f"- Validator files differing from the measured revision: "
        f"{', '.join(provenance['validator_drift']) or 'none'}",
        "",
    ]


def render(report: dict) -> str:
    lines = [
        "# M8 comparison",
        "",
        "Offline analysis of finalized exports. Single pass per cell; provider drift and "
        "sampling uncertainty remain. No default changes follow from this report.",
        "",
        "## Provenance",
        "",
        *_provenance_lines(report["provenance"]),
        "## Inventory",
        "",
        *_inventory_lines({**report["inventory"], "cells": report["cells"]}),
        "",
        "## Corpus comparisons",
        "",
        "| question | configuration | control | candidate | success A | success B | gain | "
        "loss | unchanged success/failure | delta | parity | class transitions |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for c in report["comparisons"]:
        if c["runner"] != "corpus":
            continue
        p = c["paired"]
        lines.append(
            f"| {c['question']} | {c['configuration']} | {c['control']} | {c['candidate']} | "
            f"{_fraction(p['success_A'])} | {_fraction(p['success_B'])} | {p['gain']} | "
            f"{p['loss']} | {p['unchanged_success']}/{p['unchanged_failure']} | "
            f"{_delta(p['success_delta'])} | {p['parity']} | {p['class_transitions']} |"
        )
    lines += [
        "",
        "## Latency comparisons",
        "",
        "Each arm's full observed population; completion counts successful final answers only.",
        "",
        "| question | configuration | control | candidate | first useful p50/p95 A (n) | "
        "B (n) | completion p95 A (n) | B (n) | errors/timeouts/unobserved A | B |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for c in report["comparisons"]:
        if c["runner"] != "latency":
            continue
        m = c["paired"]["metrics"]
        u, d = m["first_useful_content_ms"], m["completion_ms"]
        a, b = c["paired"]["A"], c["paired"]["B"]
        lines.append(
            f"| {c['question']} | {c['configuration']} | {c['control']} | {c['candidate']} | "
            f"{_ms(u['A']['p50'])}/{_ms(u['A']['p95'])} ({u['A']['n']}) | "
            f"{_ms(u['B']['p50'])}/{_ms(u['B']['p95'])} ({u['B']['n']}) | "
            f"{_ms(d['A']['p95'])} ({d['A']['n']}) | {_ms(d['B']['p95'])} ({d['B']['n']}) | "
            f"{a['errors']}/{a['timeouts']}/{a['unobserved_total']} | "
            f"{b['errors']}/{b['timeouts']}/{b['unobserved_total']} |"
        )
    lines += [
        "",
        "## Streamed failures (RM32)",
        "",
        "| comparison | arm | configured | effective | predicate_unmet / effective live | "
        "partial-stream diagnostic | terminal errors |",
        "|---|---|---|---|---|---|---|",
    ]
    for c in report["comparisons"]:
        if c["question"] not in ("local_live_streaming", "none_live_streaming"):
            continue
        for side, s in c.get("stream_failures", {}).items():
            lines.append(
                f"| {c['candidate']} | {side} | {s['configured']} | {s['effective']} | "
                f"{s['predicate_unmet']}/{s['effective_live']} | "
                f"{s['partial_stream_failures']} | {s['terminal_errors']} |"
            )
    lines += [
        "",
        "## Physical cost ledgers",
        "",
        "| ledger | attempts | calls | input known/missing | output known/missing | "
        "calls per success | scope |",
        "|---|---|---|---|---|---|---|",
    ]
    for name, v in report["physical_cost"].items():
        lines.append(
            f"| {name} | {v['physical_attempts']} | {v['calls']} | "
            f"{v['input_tokens_known_total']}/{v['input_tokens_missing_calls']} | "
            f"{v['output_tokens_known_total']}/{v['output_tokens_missing_calls']} | "
            f"{v['calls_per_successful_task']} | {v['scope']} |"
        )
    lines += [
        "",
        "## Expected wait applicability",
        "",
        "| cell | evaluated | declined | not evaluated | prerequisite variants (rows) |",
        "|---|---|---|---|---|",
    ]
    for cell_id, v in report["expected_wait"].items():
        o = v["placement_observations"]
        rows = ", ".join(str(len(variant)) for variant in v["prerequisite_variants"]) or "-"
        lines.append(
            f"| {cell_id} | {o['evaluated']} | {o['declined']} | {o['not_evaluated']} | "
            f"{v['prerequisite_variant_count']} ({rows}) |"
        )
    lines += [
        "",
        "## Section 12.3 hypotheses and targets",
        "",
        "| hypothesis | status | scope | required external evidence |",
        "|---|---|---|---|",
    ]
    for name, v in report["hypotheses"].items():
        lines.append(
            f"| {name} | {v['status']} | {v['scope']} | {v.get('required_external_evidence', '')} |"
        )
    lines += [
        "",
        f"Defaults: {DEFAULTS['decision']}",
        "",
        "Reporting acceptance: passed.",
    ]
    return "\n".join(lines) + "\n"


def render_inventory(report: dict) -> str:
    lines = [
        "# M8 inventory",
        "",
        "Inventory mode lists cell completeness only. It produces no comparison and no "
        "hypothesis verdict; a missing cell is incomplete evidence, not a finding.",
        "",
        "## Provenance",
        "",
        *_provenance_lines(report["provenance"]),
        "## Cells",
        "",
        *_inventory_lines({**report["inventory"], "cells": report["cells"]}),
        "",
        "## Matrix problems",
        "",
        *([f"- {p}" for p in report["problems"]] or ["- none"]),
        "",
        "Reporting acceptance: "
        + (
            "all cells complete; run without --inventory."
            if report["inventory_accepted"]
            else "failed."
        ),
    ]
    return "\n".join(lines) + "\n"


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def check_outputs(matrix: Path, out: Path) -> Path:
    """Refuse unsafe outputs before anything is read: new files, outside matrix and checkout."""
    markdown = out.with_suffix(".md")
    if out == markdown:
        raise ValueError("analysis output must be a JSON path; the Markdown report goes next to it")
    if out.is_dir():
        raise ValueError("analysis output must be a file path")
    for path in (out, markdown):
        if _inside(path, matrix):
            raise ValueError("analysis output must stay separate from the matrix evidence")
        if _inside(path, ROOT):
            raise ValueError("analysis output must be outside the analyst checkout")
        if path.exists():
            raise ValueError(f"analysis output already exists and is never replaced: {path}")
    return markdown


def check_recorded_outputs(matrix: Path, out: Path, markdown: Path) -> None:
    """Refuse outputs under the matrix root or measurement checkout that the index recorded.

    A file there would change the measured source fingerprint or matrix evidence and
    break a later driver resume.
    """
    index_path = matrix / "index.json"
    if not index_path.is_file():
        return
    index = _read_json(index_path)
    # Every recorded root, even when the layout check would later refuse the index.
    recorded = [((index.get("identity") or {}).get("source") or {}).get("package_path")]
    recorded += [
        argv[argv.index("--out") + 1]
        for attempts in (index.get("attempts") or {}).values()
        for attempt in attempts
        if "--out" in (argv := attempt.get("argv") or [])[:-1]
    ]
    for path in filter(None, recorded):
        parents = _recorded_path(path).parents
        if len(parents) < 3:
            continue
        # CHECKOUT/src/relaymind/__init__.py or MATRIX/CELL/ATTEMPT/export.json
        root = str(parents[2])
        for candidate in {Path(root), Path(root).resolve()}:
            if _inside(out, candidate) or _inside(markdown, candidate):
                raise ValueError(f"analysis output must be outside the recorded root {root}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--matrix", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--inventory",
        action="store_true",
        help="list complete and missing cells only; no comparisons or verdicts",
    )
    args = parser.parse_args(argv)
    matrix, out = args.matrix.resolve(), args.out.resolve()
    markdown = check_outputs(matrix, out)
    check_recorded_outputs(matrix, out, markdown)
    if args.inventory:
        public = inventory(matrix)
        report = dict(
            kind="relaymind.m8_inventory",
            reporting_accepted=False,
            inventory_accepted=public["accepted"],
            problems=public["problems"],
            provenance=public["provenance"],
            inventory={
                k: public[k] for k in ("counts", "complete", "incomplete", "missing", "invalid")
            },
            cells=public["cells"],
        )
        text = render_inventory(report)
    else:
        report, text = analyze(matrix)
    atomic_json(out, report)
    markdown.write_text(text, encoding="utf-8")
    counts = report["inventory"]["counts"]
    print(f"{counts['complete']} of {sum(counts.values())} cells complete; wrote {out}")


if __name__ == "__main__":
    main()
