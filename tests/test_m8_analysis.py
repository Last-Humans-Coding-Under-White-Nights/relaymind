"""Offline M8 reporting contract (RM16, RM25–RM28, RM32; AC19, AC21; spec 12.1–12.3, 14.4).

Every row below is disposable fixture evidence. No test reads the real matrix,
launches a model, opens a run store, or changes measured exports.
"""

import copy
import hashlib
import json
import shutil
from pathlib import Path, PurePath

import pytest

from helpers import load_script_module


def analysis():
    return load_script_module("analyze_m8")


def grade(case_id, success, *, mode="auto", cls="ready", **fields):
    return {
        "case_id": case_id,
        "mode": mode,
        "language": "en",
        "case_class": "simple",
        "success": success,
        "state": "completed" if success else "failed",
        "answer_stream": "live",
        "answer_stream_effective": "live",
        "error_code": None,
        "metrics": {"local_class": cls},
        **fields,
    }


def sample(prompt_id, order, value, *, cls="ready", **fields):
    return {
        "prompt_id": prompt_id,
        "order": order,
        "label": "simple",
        "state": "completed",
        "error": None,
        "timed_out": False,
        "first_useful_content_ms": value,
        "completion_ms": value,
        "metrics": {"local_class": cls},
        **fields,
    }


def test_paired_success_preserves_discordance_and_measured_class_transitions():
    """RM25/RM27: equal rates do not imply per-case equivalence or fixed hardware class."""
    left = [grade("a", False), grade("b", True), grade("c", True), grade("d", False)]
    right = [
        grade("d", False),
        grade("c", True),
        grade("b", False, cls="unsuitable"),
        grade("a", True),
    ]
    result = analysis().pair_corpus(left, right)
    assert result["success_A"] == {"numerator": 2, "denominator": 4, "rate": 0.5}
    assert result["success_B"] == result["success_A"]
    assert (result["gain"], result["loss"]) == (1, 1)
    assert (result["unchanged_success"], result["unchanged_failure"]) == (1, 1)
    assert result["success_delta"] == 0
    assert result["parity"] == "equal observed task success"
    assert result["class_transitions"] == {"ready/ready": 3, "ready/unsuitable": 1}
    assert result["groups"]["auto/en/simple"]["N"] == 4
    assert result["unknown_class_pairs"] == 0
    assert result["by_matched_class"]["ready"]["N"] == 3


@pytest.mark.parametrize("change", ["duplicate", "missing", "language", "task_class"])
def test_pairing_rejects_changed_corpus_cohort(change):
    """RM16/RM25: a different or malformed cohort is not a negative success finding."""
    left = [grade("a", False), grade("b", True)]
    right = copy.deepcopy(left)
    if change == "duplicate":
        right[1] = copy.deepcopy(right[0])
    elif change == "missing":
        right.pop()
    elif change == "language":
        right[0]["language"] = "zh"
    else:
        right[0]["case_class"] = "complex"
    with pytest.raises(ValueError, match="cohort|duplicate|identity"):
        analysis().pair_corpus(left, right)


def test_effective_live_predicate_failures_include_no_published_statement():
    """RM32/AC21: configured-live fallback and generic partial failures have distinct scopes."""
    rows = [
        grade("a", False, error_code="predicate_unmet"),
        grade("b", False, error_code="external_failed"),
        grade("c", True),
        grade("d", False, answer_stream_effective="buffered", error_code="predicate_unmet"),
        grade("e", False, answer_stream_effective=None),
    ]
    rows[0]["metrics"]["streamed_failure"] = False
    rows[1]["metrics"]["streamed_failure"] = True
    result = analysis().stream_failures(rows)
    assert result["configured"] == {"live": 5}
    assert result["effective"] == {"live": 3, "buffered": 1, "unknown": 1}
    assert result["predicate_unmet"] == 1
    assert result["effective_live"] == 3
    assert result["fraction"] == pytest.approx(1 / 3)
    assert result["partial_stream_failures"] == 1
    assert result["partial_stream_unobserved"] == 3
    assert result["terminal_errors"] == {"predicate_unmet": 2, "external_failed": 1}
    assert analysis().stream_failures(rows[3:])["fraction"] is None


def test_latency_pairing_uses_occurrences_and_each_arms_full_observed_distribution():
    """RM25: failures remain in timings; paired deltas have their own finite denominator."""
    left = [sample("p", 0, 1), sample("q", 1, None), sample("p", 2, 100)]
    right = [
        sample("p", 0, 2),
        sample("q", 1, 200),
        sample("p", 2, 500, state="failed", error="stream", completion_ms=900),
    ]
    result = analysis().pair_latency(left, right)
    useful = result["metrics"]["first_useful_content_ms"]
    assert useful["A"]["p95"] == 100
    assert useful["A"]["n"] == 2
    assert useful["A"]["missing"] == 1
    assert useful["B"]["p95"] == 500
    assert useful["delta"]["values"] == [1, 400]
    assert useful["delta"]["n"] == 2
    assert useful["delta"]["eligible"] == 3
    completion = result["metrics"]["completion_ms"]
    assert completion["B"]["values"] == [2, 200]
    assert completion["B"]["eligible"] == 2
    assert result["B"]["errors"] == 1
    assert result["by_label"]["simple"]["N"] == 3
    assert result["class_transitions"] == {"ready/ready": 3}
    right[1]["prompt_id"] = "different"
    with pytest.raises(ValueError, match="schedule|cohort"):
        analysis().pair_latency(left, right)


def test_latency_direction_uses_p95_headline_and_discloses_p50_disagreement():
    """Section 12.3/RM25: p95 is the conservative headline; a differing p50 is disclosed."""
    module = analysis()
    left = [sample("p", i, v) for i, v in enumerate([10, 20, 30, 40])]
    right = [sample("p", i, v) for i, v in enumerate([30, 30, 30, 35])]
    useful = module.pair_latency(left, right)["metrics"]["first_useful_content_ms"]
    result = module.latency_direction(useful)
    assert result["p95_A_B"] == [40, 35] and result["n_A_B"] == [4, 4]
    assert result["lower_p95"] is True
    assert result["p50_agrees"] is False
    empty = module.pair_latency([sample("p", 0, None)], [sample("p", 0, 5)])
    assert (
        module.latency_direction(empty["metrics"]["first_useful_content_ms"])["lower_p95"] is None
    )


def test_required_comparison_inventory_uses_seeded_local_streaming_control():
    """RM25/RM27/RM28/RM32: comparison selection follows the exact frozen 31 cells."""
    comparisons = analysis().comparison_pairs()
    assert len(comparisons) == 39
    assert len({(p["control"], p["candidate"], p["question"]) for p in comparisons}) == 39
    local = next(p for p in comparisons if p["question"] == "local_live_streaming")
    assert local["control"] == "placement-normal-local_first-router-buffered-corpus"
    assert local["candidate"] == "streaming-normal-local_first-router-live-corpus"
    assert not any("calibration" in p["control"] for p in comparisons)
    assert sum(p["question"] == "example_first_chain" for p in comparisons) == 12
    for question in ("device_aware_placement", "expected_wait_vs_baseline", "expected_wait_rule"):
        assert sum(p["question"] == question for p in comparisons) == 8
    assert sum(p["question"] == "none_live_streaming" for p in comparisons) == 2


def test_corpus_coverage_checks_exact_frozen_pairs_and_metadata():
    """RM16/RM25: count alone cannot hide a duplicated, relabeled or missing case."""
    module = analysis()
    corpus = load_script_module("run_corpus").load_frozen_corpus()
    cell = next(
        c for c in load_script_module("run_m8_matrix").matrix_cells() if c["phase"] == "placement"
    )
    rows = [
        grade(c.case_id, False, mode=mode, language=c.language, case_class=c.case_class)
        for c in corpus.cases
        for mode in ("auto", "external_only", "local_only")
    ]
    document = {
        "grades": rows,
        "corpus": {"sha256": corpus.digest},
        "selection_sidecar_sha256": module.byte_digest(
            module.ROOT / "evaluation/corpus.v1.skills.json"
        ),
    }
    module.validate_case_coverage(cell, document, corpus)
    relabeled = copy.deepcopy(document)
    relabeled["grades"][0]["case_class"] = "complex"
    with pytest.raises(ValueError, match="identity"):
        module.validate_case_coverage(cell, relabeled, corpus)
    document["grades"][-1] = copy.deepcopy(document["grades"][0])
    with pytest.raises(ValueError, match="coverage|duplicate"):
        module.validate_case_coverage(cell, document, corpus)


def test_latency_coverage_checks_seed_schedule_and_cold_warmup_populations():
    """RM25/AC19: retain all 120 measured requests, errors, cold and three warm-ups."""
    module = analysis()
    runner = load_script_module("latency_benchmark")
    prompts = runner.load_prompts()
    labels = {p["id"]: p["label"] for p in prompts}
    order = runner.schedule([p["id"] for p in prompts], runs=10, seed=1342026)
    rows = [
        sample(pid, i, None, label=labels[pid], state="failed", error="timeout", timed_out=True)
        for i, pid in enumerate(order)
    ]
    document = {
        "prompts": {"sha256": module.canonical_digest(prompts)},
        "design": {"seed": 1342026, "runs_per_prompt": 10, "warmups": 3},
        "samples": rows,
        "warmups": [
            sample(p["id"], i, None, label=p["label"])
            for i, p in enumerate(runner.warmup_prompts(prompts))
        ],
        "cold_start": {"cold_request": sample(prompts[0]["id"], 0, None)},
    }
    module.validate_latency_coverage(document)
    relabeled = copy.deepcopy(document)
    relabeled["samples"][0]["label"] = "routing" if labels[order[0]] != "routing" else "simple"
    with pytest.raises(ValueError, match="label"):
        module.validate_latency_coverage(relabeled)
    document["samples"][0]["order"] = 1
    with pytest.raises(ValueError, match="schedule|order"):
        module.validate_latency_coverage(document)


def test_latency_metric_coverage_keeps_unobservable_timeouts_as_findings():
    """RM16/RM25: a timeout without a run has no trace; an observed run must carry metrics."""
    module = analysis()
    timeout = sample("p", 1, None, state=None, error="timeout", timed_out=True, run_id=None)
    del timeout["metrics"]
    document = {"first_token_ms": {}, "samples": [sample("p", 0, 5, run_id="run-a"), timeout]}
    assert module.latency_metric_coverage(document) == dict(
        accepted=True, missing=[], unobserved=[1]
    )
    del document["samples"][0]["metrics"]
    assert module.latency_metric_coverage(document)["accepted"] is False
    assert module.latency_metric_coverage(document)["missing"] == [0]
    assert module.latency_metric_coverage({"samples": []})["accepted"] is False


def test_physical_cost_separates_calibration_and_deduplicates_shared_controls():
    """RM16/RM25: interrupted calls cost money; missing usage is never a zero rate."""
    module = analysis()
    known = dict(
        calls=2,
        unreadable_databases=[],
        input_tokens_known_total=8,
        input_tokens_missing_calls=0,
        output_tokens_known_total=3,
        output_tokens_missing_calls=0,
    )
    unknown = {**known, "calls": 1, "output_tokens_missing_calls": 1}
    attempts = [
        dict(identity="control/initial", cost=known),
        dict(identity="control/initial", cost=known),
        dict(identity="candidate/interrupted", cost=unknown),
    ]
    result = module.physical_cost(attempts, successes=2)
    assert result["physical_attempts"] == 2
    assert result["calls_per_successful_task"] == 1.5
    assert result["output_tokens_per_successful_task"] is None
    ledger = module.ledger(
        [*attempts, dict(identity="candidate/declared", cost=None)], successes=2, scope="fixture"
    )
    assert ledger["attempts_without_cost"] == ["candidate/declared"]
    assert ledger["calls"] == 3
    assert ledger["calls_per_successful_task"] is None


@pytest.mark.parametrize("name", ["report.json", "export.md", None])
def test_cli_rejects_source_overwrite_before_reading_or_writing(tmp_path, name):
    """RM16/RM25: both JSON and Markdown outputs must remain outside evidence input."""
    module = analysis()
    matrix = tmp_path / "immutable"
    matrix.mkdir()
    target = matrix / name if name else matrix
    with pytest.raises(ValueError, match="separate|outside|output"):
        module.main(["--matrix", str(matrix), "--out", str(target)])
    with pytest.raises(ValueError, match="separate|outside|output"):
        module.main(["--inventory", "--matrix", str(matrix), "--out", str(target)])
    assert list(matrix.iterdir()) == []


def test_cli_refuses_to_overwrite_frozen_evaluation_inputs(tmp_path):
    """RM16/RM25: the frozen corpus and prompt files read for validation stay unchanged."""
    module = analysis()
    corpus = module.ROOT / "evaluation" / "corpus.v1.json"
    before = corpus.read_bytes()
    with pytest.raises(ValueError, match="output"):
        module.main(["--matrix", str(tmp_path), "--out", str(corpus)])
    assert corpus.read_bytes() == before


def test_incomplete_inventory_cannot_produce_an_accepted_report(tmp_path):
    """RM16/RM25: a partial matrix fails acceptance before interpretation or store access."""
    module = analysis()
    driver = load_script_module("run_m8_matrix")
    plan = {"cells": driver.matrix_cells()[:-1]}
    (tmp_path / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    (tmp_path / "index.json").write_text(
        json.dumps({"identity": {"plan_sha256": module.canonical_digest(plan)}, "attempts": {}}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="31|inventory|matrix"):
        module.analyze(tmp_path)


# -- a complete relocated fixture matrix --------------------------------------------------

RECORDED = "/recorded/m8"
CHECKOUT = "/recorded/checkout"
PROVIDER = {
    "RELAYMIND_EXTERNAL_BASE_URL": "https://example.invalid/v1",
    "RELAYMIND_EXTERNAL_MODEL": "fixture-remote",
}
COST = dict(
    calls=2,
    unreadable_databases=[],
    input_tokens_known_total=10,
    input_tokens_missing_calls=0,
    output_tokens_known_total=5,
    output_tokens_missing_calls=0,
)
INTERRUPTED = "placement-constrained-device_aware-router-buffered-latency"


def full_grade(case, mode, index, cell):
    success = index % 3 != 0
    live = cell["stream"] == "live"
    error = None if success else ("predicate_unmet" if live and index % 6 == 0 else "fixture")
    simple = case.case_class == "simple"
    return {
        "case_id": case.case_id,
        "mode": mode,
        "language": case.language,
        "case_class": case.case_class,
        "run_id": f"run-{cell['id']}-{index}",
        "success": success,
        "state": "completed" if success else "failed",
        "error_code": error,
        "timed_out": False,
        "answer": "中文 fixture answer" if case.language == "zh" else "fixture answer",
        "answer_stream": cell["stream"],
        "answer_stream_effective": cell["stream"],
        "tool_correct": success,
        "proposals": 1,
        "schema_valid": 1,
        "local_completion": (success and mode != "external_only") if simple else None,
        "escalation": None,
        "missed_confirmations": 0,
        "confirmation_requested": case.case_class == "mutation",
        "confirmation": "approve" if case.case_class == "mutation" else None,
        "explicit_failure": not success,
        "local_calls": 1,
        "external_calls": 1,
        "tokens": {},
        "corrections": 0 if success else None,
        "correction_ms": None,
        "selector_provenance": {"scope": "gateway_observed"},
        "metrics": {
            "trace_observed": True,
            "m7_observed": True,
            "local_class": {"normal": "ready", "constrained": "unsuitable"}.get(
                cell["configuration"], "absent"
            ),
            "model_proposals": 1,
            "model_proposal_correct": 1 if success else 0,
            "prefetch_proposals": 0,
            "prefetch_correct": 0,
            "prefetch_diagnostics": {"admitted": 0, "rejected": 0, "executed": 0},
            "gateway_proposals": 0,
            "answer_owner": "local" if success else "unknown",
            "model_calls": [],
            "initial_selector_records": [],
            "generations": {"local/routing": 1},
            "external_usage": [dict(model="fixture-remote", input_tokens=4, output_tokens=None)],
            "selection": dict(predicted=[], expected=[], precision=1, recall=1, jaccard=1),
            "selection_observed": True,
            "placements": [],
            "streamed_failure": False,
        },
    }


def latency_sample(prompt, order, cell):
    base = (
        50
        if cell["configuration"] == "none" and cell["stream"] == "live"
        else 80
        if cell["policy"] == "device_aware"
        else 100
    )
    completed = prompt["label"] == "simple"
    return {
        "prompt_id": prompt["id"],
        "label": prompt["label"],
        "order": order,
        "run_id": f"run-{cell['id']}-{order}",
        "state": "completed" if completed else "waiting_input",
        "error": None,
        "error_code": None,
        "timed_out": False,
        "first_transport_ms": base + order + 1,
        "first_model_action_ms": 10,
        "first_useful_content_ms": base + order,
        "first_answer_token_ms": base + order + 2,
        "completion_ms": base + order + 3 if completed else None,
        "metrics": {
            "trace_observed": True,
            "local_class": {"normal": "ready", "constrained": "unsuitable"}.get(
                cell["configuration"], "absent"
            ),
            "placements": [],
            "external_usage": [],
        },
    }


def write_attempt(out, cell, name, *, source, prior_conf, prior, prior_digest, modules):
    driver, corpus, runner_corpus, runner_latency, config, controller = modules
    recorded_dir = Path(RECORDED) / cell["id"] / name
    argv = driver.command(cell, recorded_dir, Path(RECORDED), PROVIDER, None)
    here = str(driver.ROOT)
    argv = [argv[0]] + [CHECKOUT + v[len(here) :] if v.startswith(here) else v for v in argv[1:]]
    runner = runner_corpus if cell["runner"] == "corpus" else runner_latency
    args = runner.argument_parser().parse_args(argv[2:])
    settings = config.configuration_from_args(args)
    identity = dict(
        application=source,
        configuration=settings.to_json_object(),
        prior_configuration=prior_conf,
        prior=prior,
        arguments=json.loads(json.dumps(vars(args))),
    )
    exported_prior = prior
    prerequisites = [
        dict(
            skill_id="check-service-health",
            local_completed=3,
            local_escalated=1,
            external_completed=2,
            median_external_generations=1.0,
            external_timing_count=4,
            missing=[],
        )
    ]
    if prior is not None and cell["runner"] == "latency":
        exported_prior = {**prior, "initial_expected_wait_prerequisites": prerequisites}
    exported = controller.exported_identity(
        dict(
            application=source,
            configuration=identity["configuration"]
            if cell["runner"] == "corpus"
            else runner.effective_configuration(settings, no_startup_warmup=args.no_startup_warmup),
            prior_configuration=prior_conf,
            timing_prior=exported_prior,
        ),
        root=PurePath(CHECKOUT),
        work=PurePath(RECORDED) / cell["id"] / name / "checkpoint.work",
    )
    endpoint = dict(
        base_url=PROVIDER["RELAYMIND_EXTERNAL_BASE_URL"],
        model=PROVIDER["RELAYMIND_EXTERNAL_MODEL"],
        context_limit=128000,
        region=None,
    )
    if cell["runner"] == "corpus":
        modes = ("auto", "external_only") + (
            () if cell["configuration"] == "none" else ("local_only",)
        )
        grades = [
            full_grade(case, mode, i * len(modes) + j, cell)
            for i, case in enumerate(corpus.cases)
            for j, mode in enumerate(modes)
        ]
        record_prior = (
            None
            if prior is None
            else {**prior, "initial_expected_wait_prerequisites": prerequisites}
        )
        document = dict(
            kind="relaymind.correctness_corpus",
            corpus={"sha256": corpus.digest},
            selection_sidecar_sha256=driver.byte_digest(
                driver.ROOT / "evaluation/corpus.v1.skills.json"
            ),
            external_endpoint=endpoint,
            grades=grades,
            records={
                f"{g['case_id']}/{g['mode']}": dict(run_id=g["run_id"], timing_prior=record_prior)
                for g in grades
            },
            **exported,
        )
    else:
        prompts = runner_latency.load_prompts()
        by_id = {p["id"]: p for p in prompts}
        order = runner_latency.schedule(list(by_id), runs=10, seed=1342026)
        document = dict(
            kind="relaymind.latency_benchmark",
            learning="one gateway, online sequence after frozen initial prior",
            prompts={"sha256": driver.canonical_digest(prompts)},
            design={"seed": 1342026, "warmups": 3, "runs_per_prompt": 10},
            environment={"application": exported.pop("application"), "external_endpoint": endpoint},
            cold_start={"cold_request": latency_sample(prompts[0], 0, cell)},
            warmups=[
                latency_sample(p, i, cell)
                for i, p in enumerate(runner_latency.warmup_prompts(prompts))
            ],
            samples=[latency_sample(by_id[pid], i, cell) for i, pid in enumerate(order)],
            first_token_ms={},
            **exported,
        )
    directory = out / cell["id"] / name
    controller_atomic = driver.atomic_json
    controller_atomic(directory / "export.json", document)
    (directory / "export.md").write_text("# fixture 中文\n", encoding="utf-8")
    export_digest = driver.byte_digest(directory / "export.json")
    checkpoint = dict(
        schema_version=1,
        identity=identity,
        started_at_utc="2026-10-25T00:00:00+00:00",
        completed=True,
        steps=[dict(identity=f"step/{i}", result={}) for i in range(cell["requests"])],
        result={"export_sha256": export_digest},
        finished_at_utc="2026-10-25T01:00:00+00:00",
    )
    controller_atomic(directory / "checkpoint.json", checkpoint)
    attempt = dict(
        name=name,
        status="complete",
        declared_at_utc="2026-10-25T00:00:00+00:00",
        reason="initial attempt",
        argv=argv,
        prior_sha256=prior_digest,
        started_at_utc="2026-10-25T00:00:00+00:00",
        cost=dict(COST),
        finished_at_utc="2026-10-25T01:00:00+00:00",
        export_sha256=export_digest,
        checkpoint_sha256=driver.byte_digest(directory / "checkpoint.json"),
        requests=cell["requests"],
    )
    return attempt, document


def build_matrix(out):
    """Write all 31 finalized fixture cells as if recorded under another root."""
    module = analysis()
    driver = load_script_module("run_m8_matrix")
    controller = load_script_module("matrix_controller")
    config = load_script_module("evaluation_config")
    runner_corpus = load_script_module("run_corpus")
    runner_latency = load_script_module("latency_benchmark")
    corpus = runner_corpus.load_frozen_corpus()
    modules = (driver, corpus, runner_corpus, runner_latency, config, controller)
    from relaymind.evaluation.environment import source_fingerprint

    cells = driver.matrix_cells()
    plan = dict(
        schema_version=1,
        cells=cells,
        run_order=[c["id"] for c in cells],
        corpus_runs=2548,
        latency_requests=1736,
        external_inference_ceiling=21024,
        token_ceiling=None,
        defaults_changed=False,
    )
    out.mkdir(parents=True)
    driver.atomic_json(out / "plan.json", plan)
    fingerprint = source_fingerprint(module.ROOT)
    source = dict(
        head="f" * 40,
        dirty=False,
        source_sha256=fingerprint["source_sha256"],
        files=fingerprint["files"],
        package_path=CHECKOUT + "/src/relaymind/__init__.py",
    )
    identity = dict(
        source=source,
        runtime_sha256="0" * 64,
        runtime="/recorded/llama-server",
        provider=PROVIDER,
        context_limit=128000,
        plan_sha256=driver.canonical_digest(plan),
    )
    index = dict(identity=identity, attempts={})
    prior_conf = {c: {"fixture_partition": c} for c in ("normal", "constrained", "none")}
    priors = {}
    for cell in cells:
        seeded = cell["phase"] != "calibration" and cell["configuration"] != "none"
        prior = prior_digest = None
        if seeded:
            path = out / f"prior-{cell['configuration']}.json"
            prior_digest = driver.byte_digest(path)
            # Loaded prior provenance also names each source export's application.
            prior = dict(
                bundle_sha256=prior_digest,
                sources=[dict(s, application=source) for s in priors[cell["configuration"]]],
                timings=1,
            )
        attempts = []
        name = "initial"
        if cell["id"] == INTERRUPTED:
            recorded_dir = Path(RECORDED) / cell["id"] / "initial"
            argv = driver.command(cell, recorded_dir, Path(RECORDED), PROVIDER, None)
            here = str(driver.ROOT)
            argv = [argv[0]] + [
                CHECKOUT + v[len(here) :] if v.startswith(here) else v for v in argv[1:]
            ]
            (out / cell["id"] / "initial").mkdir(parents=True)
            attempts.append(
                dict(
                    name="initial",
                    status="interrupted",
                    reason="initial attempt",
                    argv=argv,
                    prior_sha256=prior_digest,
                    cost={**COST, "calls": 7, "output_tokens_missing_calls": 1},
                )
            )
            name = "retry"
        attempt, document = write_attempt(
            out,
            cell,
            name,
            source=source,
            prior_conf=prior_conf[cell["configuration"]],
            prior=prior,
            prior_digest=prior_digest,
            modules=modules,
        )
        attempts.append(attempt)
        index["attempts"][cell["id"]] = attempts
        if cell["phase"] == "calibration":
            sources = [
                dict(
                    file=f"{cell['id']}/{name}/export.json",
                    sha256=attempt["export_sha256"],
                    run_ids=[r["run_id"] for r in document["records"].values()],
                )
            ]
            priors[cell["configuration"]] = sources
            driver.atomic_json(
                out / f"prior-{cell['configuration']}.json",
                dict(
                    schema_version=1,
                    kind="relaymind.placement_prior",
                    partitions=[
                        dict(
                            configuration=prior_conf[cell["configuration"]],
                            include_outcomes=True,
                            sources=sources,
                        )
                    ],
                ),
            )
    index["controller_history"] = [
        dict(
            controller={"head": "c" * 40, "dirty": False},
            measurement_identity_sha256=driver.canonical_digest(identity),
            recorded_at_utc="2026-10-25T00:00:00+00:00",
            reason="initial controller",
            previous_index_sha256=None,
            previous_index_file=None,
            artifacts=[],
        )
    ]
    driver.atomic_json(out / "index.json", index)
    return out


@pytest.fixture(scope="module")
def fixture_matrix(tmp_path_factory):
    return build_matrix(tmp_path_factory.mktemp("relocated") / "m8")


def digests(root):
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def copy_matrix(source, tmp_path):
    target = tmp_path / "copy" / "m8"
    shutil.copytree(source, target)
    return target


def rewrite_index(matrix, change):
    path = matrix / "index.json"
    index = json.loads(path.read_text(encoding="utf-8"))
    change(index)
    path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")


def test_relocated_complete_matrix_produces_accepted_comparisons_and_retains_provenance(
    fixture_matrix, tmp_path
):
    """RM16/RM25–RM28/RM32, AC19/AC21: relocation keeps recorded paths as provenance."""
    module = analysis()
    before = digests(fixture_matrix)
    out = tmp_path / "analysis" / "m8.json"
    module.main(["--matrix", str(fixture_matrix), "--out", str(out)])
    assert digests(fixture_matrix) == before
    report = json.loads(out.read_text(encoding="utf-8"))
    markdown = out.with_suffix(".md").read_text(encoding="utf-8")
    assert report["kind"] == "relaymind.m8_analysis"
    assert report["reporting_accepted"] is True
    assert report["inventory"]["complete"] == [c["id"] for c in module.driver.matrix_cells()]
    provenance = report["provenance"]
    assert provenance["matrix_root_recorded"] == RECORDED
    assert provenance["matrix_root_actual"] == str(fixture_matrix.resolve())
    assert provenance["measurement_checkout_recorded"] == CHECKOUT
    assert provenance["measured_revision"] == "f" * 40
    cell = report["cells"]["placement-normal-local_first-router-buffered-corpus"]
    assert cell["attempts"][-1]["argv"][3] == (
        RECORDED + "/placement-normal-local_first-router-buffered-corpus/initial/export.json"
    )
    assert (
        cell["export"] == "placement-normal-local_first-router-buffered-corpus/initial/export.json"
    )
    assert len(report["comparisons"]) == 39
    local = next(c for c in report["comparisons"] if c["question"] == "local_live_streaming")
    assert local["paired"]["N"] == 156
    assert local["stream_failures"]["B"]["effective_live"] == 156
    assert local["stream_failures"]["B"]["predicate_unmet"] > 0
    assert local["stream_failures"]["A"]["effective_live"] == 0
    assert local["stream_failures"]["A"]["fraction"] is None
    none_latency = next(
        c
        for c in report["comparisons"]
        if c["question"] == "none_live_streaming" and c["runner"] == "latency"
    )
    useful = none_latency["paired"]["metrics"]["first_useful_content_ms"]
    assert useful["A"]["n"] == useful["B"]["n"] == 120
    # Physical cost keeps the interrupted predecessor; calibration is a separate ledger.
    ledgers = report["physical_cost"]
    assert ledgers["calibration"]["physical_attempts"] == 2
    assert ledgers["candidate_latency"]["physical_attempts"] == 15
    assert ledgers["candidate_latency"]["calls"] == 14 * 2 + 7
    assert ledgers["candidate_latency"]["output_tokens_per_successful_task"] is None
    assert ledgers["candidate_corpus"]["physical_attempts"] == 15
    interrupted = report["cells"][INTERRUPTED]["attempts"]
    assert [a["status"] for a in interrupted] == ["interrupted", "complete"]
    assert cell["summary"]["modes"]["auto"]["zh"]["all"]["runs"] == 12
    assert "corrections" in cell["summary"]["modes"]["auto"]["en"]["all"]
    # Cohort efficiency keeps unknown output usage unknown.
    efficiency = cell["cohort_external_cost"]
    assert efficiency["output_tokens_missing_calls"] == 156
    assert efficiency["output_tokens_per_completed_task"] is None
    hypotheses = report["hypotheses"]
    assert hypotheses["none_live_streaming"]["status"] == "met"
    assert hypotheses["none_live_streaming"]["first_useful_content_ms"]["p50_agrees"] is True
    assert hypotheses["local_live_streaming_parity"]["status"] == "met"
    assert hypotheses["unsuitable_device_aware"]["status"] == "met"
    assert hypotheses["example_matcher_jaccard"]["status"] == "not measured"
    assert hypotheses["example_matcher_jaccard"]["corpus_component"] == "met"
    assert hypotheses["no_missed_confirmation"]["status"] == "met"
    for name in (
        "draft_local_completion",
        "prefetch_and_many_generations",
        "criteria_corrections",
        "early_headers",
        "tool_narrowing_precision",
    ):
        assert hypotheses[name]["status"] == "not measured"
        assert hypotheses[name]["required_external_evidence"]
    assert {v["status"] for v in hypotheses.values()} <= {"met", "not met", "not measured"}
    expected_wait = report["expected_wait"]["placement-normal-expected_wait-router-buffered-corpus"]
    assert expected_wait["placement_observations"]["evaluated"] == 0
    assert expected_wait["initial_prerequisites"][0]["p_local"] == 0.75
    assert report["defaults"]["changed"] is False
    assert "Reporting acceptance: passed" in markdown
    assert "not measured" in markdown


def test_inventory_mode_reports_complete_and_missing_cells_without_comparisons(
    fixture_matrix, tmp_path
):
    """RM16/RM25: partial evidence is listed honestly and never becomes an accepted report."""
    module = analysis()
    matrix = copy_matrix(fixture_matrix, tmp_path)
    order = [c["id"] for c in module.driver.matrix_cells()]
    keep = set(order[:13])

    def partial(index):
        index["attempts"] = {k: v for k, v in index["attempts"].items() if k in keep}
        index["attempts"][order[13]] = [
            dict(name="initial", status="started", argv=None, prior_sha256=None)
        ]

    rewrite_index(matrix, partial)
    before = digests(matrix)
    out = tmp_path / "inventory.json"
    module.main(["--inventory", "--matrix", str(matrix), "--out", str(out)])
    assert digests(matrix) == before
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["kind"] == "relaymind.m8_inventory"
    assert report["reporting_accepted"] is False
    assert "comparisons" not in report and "hypotheses" not in report
    inventory = report["inventory"]
    assert inventory["complete"] == order[:13]
    assert inventory["incomplete"] == [order[13]]
    assert inventory["missing"] == order[14:]
    assert inventory["counts"] == {"complete": 13, "incomplete": 1, "missing": 17, "invalid": 0}
    assert "13 of 31" in out.with_suffix(".md").read_text(encoding="utf-8")
    full = tmp_path / "full.json"
    with pytest.raises(ValueError, match="inventory"):
        module.main(["--matrix", str(matrix), "--out", str(full)])
    assert not full.exists() and not full.with_suffix(".md").exists()


@pytest.mark.parametrize("change", ["export_bytes", "prior_bytes", "argv", "plan_cells"])
def test_changed_evidence_fails_acceptance_instead_of_becoming_a_finding(
    fixture_matrix, tmp_path, change
):
    """RM16/RM17/RM25: identity, digest or linkage drift is invalid evidence, not zeros."""
    module = analysis()
    matrix = copy_matrix(fixture_matrix, tmp_path)
    target = "placement-normal-device_aware-router-buffered-corpus"
    if change == "export_bytes":
        with (matrix / target / "initial" / "export.json").open("a", encoding="utf-8") as f:
            f.write(" ")
    elif change == "prior_bytes":
        with (matrix / "prior-normal.json").open("a", encoding="utf-8") as f:
            f.write(" ")
    elif change == "argv":
        rewrite_index(
            matrix,
            lambda index: index["attempts"][target][-1]["argv"].__setitem__(
                index["attempts"][target][-1]["argv"].index("policy=device_aware"),
                "policy=local_first",
            ),
        )
    else:
        plan = json.loads((matrix / "plan.json").read_text(encoding="utf-8"))
        plan["cells"][5]["requests"] = 155
        (matrix / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
        rewrite_index(
            matrix,
            lambda index: index["identity"].__setitem__(
                "plan_sha256", module.canonical_digest(plan)
            ),
        )
    result = module.inventory(matrix)
    assert result["accepted"] is False
    if change == "plan_cells":
        assert any("31" in p or "plan" in p for p in result["problems"])
    else:
        assert target in result["invalid"]
        assert result["cells"][target]["status"] == "invalid"
    with pytest.raises(ValueError, match="inventory"):
        module.analyze(matrix)


def test_cli_writes_utf8_reports_under_cp1252_default(fixture_matrix, tmp_path, monkeypatch):
    """RM16/RM25: a non-ASCII evidence root survives a Windows-style text default."""
    module = analysis()
    matrix = tmp_path / "证据" / "m8"
    shutil.copytree(fixture_matrix, matrix)
    original_read, original_write = Path.read_text, Path.write_text

    def read_text(path, encoding=None, errors=None, **kwargs):
        implicit = encoding in (None, "locale")
        return original_read(path, "cp1252" if implicit else encoding, errors, **kwargs)

    def write_text(path, data, encoding=None, errors=None, **kwargs):
        implicit = encoding in (None, "locale")
        return original_write(path, data, "cp1252" if implicit else encoding, errors, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    monkeypatch.setattr(Path, "write_text", write_text)
    out = tmp_path / "报告" / "m8.json"
    module.main(["--matrix", str(matrix), "--out", str(out)])
    report = json.loads(out.read_bytes().decode("utf-8"))
    assert report["provenance"]["matrix_root_actual"] == str(matrix.resolve())
    assert str(matrix.resolve()) in out.with_suffix(".md").read_bytes().decode("utf-8")
