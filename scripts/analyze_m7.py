"""Offline M7 evidence validation and comparison; never launches models or reads keys."""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
from run_corpus import load_frozen_corpus, regrade  # noqa: E402
from run_m7_matrix import HISTORICAL  # noqa: E402
from run_m8_matrix import reconcile_attempt  # noqa: E402

from relaymind.canonical import canonical_digest  # noqa: E402
from relaymind.evaluation.checkpoint import atomic_json  # noqa: E402
from relaymind.evaluation.environment import source_fingerprint  # noqa: E402
from relaymind.evaluation.m7_metrics import latency_first_tokens, render_first_tokens  # noqa: E402
from relaymind.evaluation.metrics import render_corpus_markdown, render_latency_markdown  # noqa: E402
from relaymind.evaluation.prior import byte_digest  # noqa: E402

REQUIRED = (
    "model_proposals",
    "model_proposal_correct",
    "prefetch_proposals",
    "prefetch_correct",
    "prefetch_diagnostics",
    "answer_owner",
    "model_calls",
    "initial_selector_records",
    "generations",
    "external_usage",
)


def metric_coverage(document: dict) -> dict:
    missing = []
    for index, grade in enumerate(document.get("grades", [])):
        metrics = grade.get("metrics", {})
        absent = [key for key in REQUIRED if key not in metrics]
        if metrics.get("trace_observed") is not True:
            absent.append("trace_observed")
        if grade.get("state") == "completed" and grade.get("corrections") is None:
            absent.append("corrections")
        if absent:
            missing.append(
                dict(row=index, case_id=grade.get("case_id"), mode=grade.get("mode"), fields=absent)
            )
    return dict(accepted=bool(document.get("grades")) and not missing, missing=missing)


def physical_cost(attempts: list[dict], *, successes: int) -> dict:
    unique = {}
    for attempt in attempts:
        identity, cost = attempt["identity"], attempt["cost"]
        if identity in unique and unique[identity] != cost:
            raise ValueError("physical attempt has conflicting cost")
        unique[identity] = cost
    values = list(unique.values())
    unknown = sum(bool(v.get("unreadable_databases")) for v in values)
    calls = sum(v["calls"] for v in values)
    result = dict(
        physical_attempts=len(values),
        calls=calls,
        successful_tasks=successes,
        unreadable_attempts=unknown,
        calls_per_successful_task=calls / successes if successes and not unknown else None,
    )
    for key in ("input_tokens", "output_tokens"):
        total = sum(v[key + "_known_total"] for v in values)
        missing = sum(v[key + "_missing_calls"] for v in values)
        result.update(
            {
                key + "_known_total": total,
                key + "_missing_calls": missing,
                key + "_per_successful_task": total / successes
                if successes and not missing and not unknown
                else None,
            }
        )
    return result


def verdict(value: bool | None) -> str:
    return "not measured" if value is None else "met" if value else "not met"


def hypotheses(documents: dict) -> dict:
    names = (
        "draft_local_completion",
        "prefetch_and_many_generations",
        "criteria_corrections",
        "early_headers",
        "none_live_streaming",
        "unsuitable_device_aware",
        "example_matcher_jaccard",
        "tool_narrowing_precision",
        "external_cost_next_to_success",
    )
    result = {name: dict(status="not measured", evidence=[]) for name in names}

    def block(cell, language="en", case_class="all"):
        data = documents[cell]["summary"]["modes"]["auto"][language]
        return data["all"] if case_class == "all" else data["classes"][case_class]

    if all(k in documents for k in ("M7-C0", "M7-C1")):
        a, b = block("M7-C0", case_class="simple"), block("M7-C1", case_class="simple")
        values = [
            a["local_completion"]["rate"],
            b["local_completion"]["rate"],
            a["success"]["rate"],
            b["success"]["rate"],
        ]
        value = None if None in values else values[1] > values[0] and values[3] >= values[2]
        result["draft_local_completion"] = dict(
            status=verdict(value),
            evidence=["M7-C0", "M7-C1"],
            population="auto/en/simple",
            values=values,
        )
    if all(k in documents for k in ("M7-C0", "M7-C2", "M7-C3")):
        values = {
            lang: [
                block(cell, lang, "complex")["placement_metrics"]["generations"][
                    "per_completed_run"
                ]
                for cell in ("M7-C0", "M7-C2", "M7-C3")
            ]
            for lang in ("en", "zh")
        }
        value = (
            None
            if any(None in v for v in values.values())
            else all(v[1] < v[0] and v[2] < v[0] for v in values.values())
        )
        result["prefetch_and_many_generations"] = dict(
            status=verdict(value),
            evidence=["M7-C0", "M7-C2", "M7-C3"],
            population="auto/complex, languages separate",
            values=values,
        )
    if all(k in documents for k in ("M7-C0", "M7-C6")):
        values = {
            lang: [
                block(cell, lang)["corrections"]["per_completed_run"] for cell in ("M7-C6", "M7-C0")
            ]
            for lang in ("en", "zh")
        }
        value = (
            None
            if any(None in v for v in values.values())
            else all(v[1] < v[0] for v in values.values())
        )
        result["criteria_corrections"] = dict(
            status=verdict(value),
            evidence=["M7-C6", "M7-C0"],
            scope="criteria and named correction bundle by explicit revision",
            values=values,
        )
    if "M7-L1" in documents:
        samples = documents["M7-L1"]["samples"]
        eligible = [s for s in samples if s.get("first_answer_token_ms") is not None]
        value = (
            all(
                s.get("first_transport_ms") is not None
                and s["first_transport_ms"] < s["first_answer_token_ms"]
                for s in eligible
            )
            if eligible
            else None
        )
        result["early_headers"] = dict(
            status=verdict(value),
            evidence=["M7-L1"],
            eligible=len(eligible),
            requests=len(samples),
            scope=(
                "header arrival before first answer token; generation-start ordering not measured"
            ),
        )
        # Header-vs-token does not prove before the generation START; preserve the
        # diagnostic but do not declare the stronger section12.3 target measured.
        result["early_headers"]["header_before_answer_token"] = result["early_headers"]["status"]
        result["early_headers"]["status"] = "not measured"
    corpus = [d for d in documents.values() if "grades" in d]
    if corpus:
        result["external_cost_next_to_success"] = dict(
            status=verdict(all(metric_coverage(d)["accepted"] for d in corpus)),
            evidence=[k for k, d in documents.items() if "grades" in d],
        )
    return result


def validate_case_coverage(cell: dict, document: dict, corpus) -> None:
    if cell["runner"] == "corpus":
        expected = {(case.case_id, mode) for case in corpus.cases for mode in cell["modes"]}
        actual = [(g["case_id"], g["mode"]) for g in document["grades"]]
        if (
            document["corpus"]["sha256"] != corpus.digest
            or len(actual) != cell["requests"]
            or set(actual) != expected
        ):
            raise ValueError("export case/mode coverage differs from frozen matrix")
    elif (
        len(document["samples"]) != 120
        or len(document["warmups"]) != 3
        or not document.get("cold_start", {}).get("cold_request")
    ):
        raise ValueError(
            "latency protocol requires 120 measured, three warmups and one cold request"
        )


def validate_cell_configuration(cell: dict, document: dict, provider: dict) -> None:
    """Compare measured effective settings with the fixed experiment, not its label."""
    import importlib

    from run_m7_matrix import command, configuration_from_args

    runner = importlib.import_module(
        "run_corpus" if cell["runner"] == "corpus" else "latency_benchmark"
    )
    args = runner.argument_parser().parse_args(command(cell, Path("/unused"), provider)[2:])
    settings = configuration_from_args(args)
    expected = (
        settings.to_json_object()
        if cell["runner"] == "corpus"
        else runner.effective_configuration(settings, no_startup_warmup=args.no_startup_warmup)
    )
    application = document.get(
        "application", document.get("environment", {}).get("application", {})
    )
    if (
        document.get("configuration") != expected
        or document.get("timing_prior") is not None
        or application.get("head") != cell["revision"]
    ):
        raise ValueError("cell configuration/source/prior identity differs")
    endpoint = (
        document.get("external_endpoint")
        if cell["runner"] == "corpus"
        else document.get("environment", {}).get("external_endpoint")
    )
    if cell["runner"] == "corpus":
        if (
            not endpoint
            or endpoint.get("base_url") != provider["RELAYMIND_EXTERNAL_BASE_URL"]
            or endpoint.get("model") != provider["RELAYMIND_EXTERNAL_MODEL"]
            or endpoint.get("context_limit") != 128000
        ):
            raise ValueError("cell provider identity differs")
    elif endpoint is not None:
        raise ValueError("M7 local latency cannot configure a provider")


def analyze(matrix: Path) -> tuple[dict, str]:
    plan = json.loads((matrix / "plan.json").read_text())
    index = json.loads((matrix / "index.json").read_text())
    if index["identity"]["plan_sha256"] != canonical_digest(plan):
        raise ValueError("matrix plan identity differs")
    corpus = load_frozen_corpus()
    documents = {}
    records = {}
    all_cost = []
    lines = [
        "# M7 comparison",
        "",
        "Single-pass results retain provider drift and sampling uncertainty. "
        "Historical comparisons are confounded; defaults remain unchanged.",
        "",
    ]
    for cell in plan["cells"]:
        attempts = index["attempts"].get(cell["id"], [])
        if not attempts:
            records[cell["id"]] = dict(coverage=dict(accepted=False, missing=["complete attempt"]))
            continue
        latest = attempts[-1]
        costs = []
        if latest.get("reused"):
            source_attempt = latest["source_attempt"]
            export = Path(latest["export_path"])
            reconcile_attempt(
                source_attempt,
                cell,
                export.parent,
                source_attempt["argv"],
                latest["reuse_identity"]["source"],
            )
            costs = latest["physical_attempts"]
        else:
            for attempt in attempts:
                directory = matrix / cell["id"] / attempt["name"]
                checkpoint = (
                    json.loads((directory / "checkpoint.json").read_text())
                    if (directory / "checkpoint.json").exists()
                    else None
                )
                source = (checkpoint or {}).get("identity", {}).get("application")
                if source is None:
                    source = next(
                        s
                        for s in index["identity"]["sources"].values()
                        if s["head"] == cell["revision"]
                    )
                if (
                    source not in index["identity"]["sources"].values()
                    or source["head"] != cell["revision"]
                ):
                    raise ValueError("cell source identity differs")
                reconcile_attempt(attempt, cell, directory, attempt["argv"], source)
                costs.append(dict(identity=str(directory.resolve()), cost=attempt["cost"]))
            export = matrix / cell["id"] / latest["name"] / "export.json"
        all_cost.extend(costs)
        if latest["status"] != "complete":
            records[cell["id"]] = dict(
                coverage=dict(accepted=False, missing=["complete attempt"]),
                cost=physical_cost(costs, successes=0),
            )
            continue
        if byte_digest(export) != latest["export_sha256"]:
            raise ValueError("immutable export digest differs")
        doc = json.loads(export.read_text())
        validate_case_coverage(cell, doc, corpus)
        validate_cell_configuration(cell, doc, index["identity"]["provider"])
        if cell["runner"] == "corpus":
            coverage = metric_coverage(doc)
            analysis = regrade(doc, corpus, source_sha256=byte_digest(export))
            successes = sum(g["success"] for g in analysis["grades"])
            lines += [render_corpus_markdown(analysis["summary"], title=cell["id"])]
        else:
            coverage = dict(
                accepted="first_token_ms" in doc,
                missing=[] if "first_token_ms" in doc else ["first_token_ms"],
            )
            analysis = doc
            successes = sum(
                s.get("state") == "completed" and not s.get("error") for s in doc["samples"]
            )
            lines += [
                render_latency_markdown(
                    doc["summary"],
                    title=cell["id"],
                    early_headers=cell["performance"]["early_headers"],
                ),
                render_first_tokens(latency_first_tokens(doc)),
            ]
        documents[cell["id"]] = analysis
        records[cell["id"]] = dict(
            export=str(export),
            sha256=byte_digest(export),
            source=doc.get("application", doc.get("environment", {}).get("application")),
            configuration=doc["configuration"],
            reused=latest.get("reused", False),
            coverage=coverage,
            cost=physical_cost(costs, successes=successes),
        )
    historical = ROOT / HISTORICAL["corpus"]
    if byte_digest(historical) != HISTORICAL["corpus_sha256"]:
        raise ValueError("historical source bytes differ")
    before = regrade(
        json.loads(historical.read_text()), corpus, source_sha256=byte_digest(historical)
    )
    lines += [
        "## Historical corrections versus current",
        "",
        "These are confounded revision/date comparisons, not isolated causal evidence.",
        "",
        "| source | mode | language | class | corrections | completed | missing | summed ms |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for label, doc in [
        ("historical", before),
        *([("M7-C0", documents["M7-C0"])] if "M7-C0" in documents else []),
    ]:
        for mode, languages in doc["summary"]["modes"].items():
            for language, data in languages.items():
                for cls, block in [("all", data["all"]), *data["classes"].items()]:
                    v = block["corrections"]
                    lines.append(
                        f"| {label} | {mode} | {language} | {cls} | {v['corrections']} | "
                        f"{v['completed_runs']} | {v['missing_completed_runs']} | "
                        f"{v['duration_ms']} |"
                    )
    history = []
    for name in HISTORICAL["latency"]:
        path = ROOT / name
        doc = json.loads(path.read_text())
        history.append(
            dict(
                file=name,
                sha256=byte_digest(path),
                application=doc.get("environment", {}).get("application"),
                summary=doc["summary"],
            )
        )
    decisions = hypotheses(documents)
    lines += [
        "",
        "## Section 12.3 hypotheses",
        "",
        "| hypothesis | status | evidence / limits |",
        "|---|---|---|",
    ]
    lines += [
        f"| {name} | {v['status']} | {json.dumps(v, ensure_ascii=False)} |"
        for name, v in decisions.items()
    ]
    lines += [
        "",
        "## Retained attempt cost",
        "",
        "| cell | reused | cost (known subtotals and missing counts) |",
        "|---|---|---|",
    ]
    lines += [f"| {cell} | {v.get('reused')} | {v.get('cost')} |" for cell, v in records.items()]
    accepted = all(v["coverage"]["accepted"] for v in records.values())
    lines += [
        "",
        f"Reporting acceptance: {'passed' if accepted else 'failed: missing metrics or cells'}. "
        "Historical latency sources retain their measured revisions and protocol; "
        "see JSON historical_latency.",
    ]
    report = dict(
        kind="relaymind.m7_analysis",
        analyst=source_fingerprint(ROOT),
        matrix_plan_sha256=byte_digest(matrix / "plan.json"),
        matrix_index_sha256=byte_digest(matrix / "index.json"),
        cells=records,
        hypotheses=decisions,
        reporting_accepted=accepted,
        historical_corpus=dict(
            file=str(historical),
            sha256=byte_digest(historical),
            application=before["application"],
            regraded_with=before["regraded_with"],
        ),
        historical_latency=history,
        physical_cost=physical_cost(
            all_cost,
            successes=sum(
                sum(g["success"] for g in d.get("grades", [])) for d in documents.values()
            ),
        ),
    )
    return report, "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    if (
        args.out.resolve() == args.matrix.resolve()
        or args.matrix.resolve() in args.out.resolve().parents
    ):
        raise ValueError("analysis output must be separate from immutable matrix evidence")
    result, markdown = analyze(args.matrix.resolve())
    atomic_json(args.out, result)
    args.out.with_suffix(".md").write_text(markdown)
    if not result["reporting_accepted"]:
        raise SystemExit("reporting acceptance failed; inspect missing coverage in report")


if __name__ == "__main__":
    main()
