"""Offline M8 reporting contract (RM16, RM25–RM28, RM32; AC19, AC21; spec 12.1–12.3, 14.4).

Every row below is disposable fixture evidence. No test reads the real matrix,
launches a model, or changes measured exports. Only the real-loader prior test
builds a disposable gateway store, through the existing prior test fixture.
"""

import copy
import hashlib
import json
import os
import shutil
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import SimpleNamespace

import pytest

from gateway_helpers import close_gateway_stores  # noqa: F401 - autouse store cleanup
from helpers import load_script_module
from relaymind.tools.sandbox import MUTATING_DEMO_TOOLS
from test_placement_prior import fixture_source

MUTATING = sorted(MUTATING_DEMO_TOOLS)[0]


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
        "metrics": {"trace_observed": True, "local_class": cls},
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
    rows[0]["metrics"]["local_class"] = "unsuitable"
    by_class = analysis().stream_failures(rows)["by_measured_class"]
    assert by_class["unsuitable"]["predicate_unmet"] == 1
    assert by_class["unsuitable"]["effective_live"] == 1
    assert by_class["ready"]["predicate_unmet"] == 0
    assert by_class["ready"]["effective_live"] == 2
    groups = analysis().stream_failures(rows)["by_group"]
    assert groups["auto/en/simple"]["effective_live"] == 3


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


def test_latency_metric_coverage_keeps_recorded_failures_as_unobserved_findings():
    """RM16/RM25, spec 12.2/14.4.4: recorded failures stay; silent metric gaps fail."""
    module = analysis()

    def unobserved(order, **fields):
        row = sample("p", order, None, state=None, **fields)
        del row["metrics"]
        return row

    document = {
        "first_token_ms": {},
        "samples": [
            sample("p", 0, 5, run_id="run-a"),
            unobserved(1, error="timeout", timed_out=True, run_id=None),
            unobserved(2, error="HTTP 503", run_id=None),
            unobserved(3, error="trace: ReadTimeout: read", run_id="run-b"),
            unobserved(4, error="ended failed (x), expected completed", run_id="run-c"),
        ],
    }
    assert module.latency_metric_coverage(document) == dict(
        accepted=True,
        missing=[],
        unobserved=[1, 2, 3, 4],
        unobserved_reasons={
            "error_with_run": 1,
            "error_without_run": 1,
            "timeout": 1,
            "trace_read_error": 1,
        },
    )
    del document["samples"][0]["metrics"]
    assert module.latency_metric_coverage(document)["accepted"] is False
    assert module.latency_metric_coverage(document)["missing"] == [0]
    assert module.latency_metric_coverage({"samples": []})["accepted"] is False


def test_latency_metrics_without_an_observed_trace_are_not_observed():
    """RM16/RM25, spec 12.2/14.4.4: a metrics block counts only with trace_observed true.

    An empty block or trace_observed false is no trace: a recorded reason keeps the
    request as an unobserved finding, no reason fails acceptance.
    """
    module = analysis()
    empty = sample("p", 0, None, metrics={})
    unread = sample("p", 1, None, metrics={"trace_observed": False, "local_class": "ready"})
    timeout = sample(
        "p", 2, None, state=None, error="timeout", timed_out=True, metrics={"trace_observed": False}
    )
    assert module.unobserved_reason(sample("p", 3, 5)) is None
    assert module.unobserved_reason(empty) == "missing_metrics"
    assert module.unobserved_reason(unread) == "missing_metrics"
    assert module.unobserved_reason(timeout) == "timeout"
    coverage = module.latency_metric_coverage(
        {"first_token_ms": {}, "samples": [sample("p", 3, 5), empty, unread, timeout]}
    )
    assert coverage["accepted"] is False
    assert coverage["missing"] == [1, 2]
    assert coverage["unobserved_reasons"] == {"timeout": 1}
    assert module.pair_latency([timeout], [timeout])["B"]["unobserved"] == {"timeout": 1}


def test_latency_arm_counts_split_unobserved_requests_by_reason():
    """Spec 12.2: requests without a trace stay counted per arm, split by reason."""
    module = analysis()
    left = [sample("p", 0, 5), sample("p", 1, 6)]
    right = [sample("p", 0, 4), sample("p", 1, None, state=None, error="timeout", timed_out=True)]
    del right[1]["metrics"]
    paired = module.pair_latency(left, right)
    assert paired["B"]["unobserved"] == {"timeout": 1}
    assert paired["B"]["unobserved_total"] == 1
    assert paired["A"]["unobserved_total"] == 0
    guard = module.latency_guard(paired)
    assert guard["candidate_worse"] == ["timeouts", "unobserved_total"]
    assert module.latency_guard(module.pair_latency(left, left))["candidate_worse"] == []


def test_expected_wait_success_rate_is_unknown_when_a_count_is_unknown():
    """RM27, spec 14.4.3/14.4.4: a missing or null outcome count is unknown, never zero."""
    module = analysis()
    report = [
        dict(skill_id="known", local_completed=3, local_escalated=1),
        dict(skill_id="none_yet", local_completed=0, local_escalated=0),
        dict(skill_id="null", local_completed=None, local_escalated=2),
        dict(skill_id="missing", local_escalated=2),
        dict(skill_id="no_escalated", local_completed=2),
    ]
    view = dict(rows=[], prerequisites=[report], learning=None)
    result = module.expected_wait({"placement-normal-expected_wait-router-buffered-corpus": view})
    rows = result["placement-normal-expected_wait-router-buffered-corpus"]["initial_prerequisites"]
    assert {r["skill_id"]: r["p_local"] for r in rows} == dict(
        known=0.75, none_yet=None, null=None, missing=None, no_escalated=None
    )


def test_recorded_export_is_unknown_when_out_has_no_value():
    """RM16: a truncated recorded command lists no export instead of crashing inventory."""
    module = analysis()
    assert module._recorded_export(["python", "run.py", "--out", "m8/c/a/export.json"]) == (
        "m8/c/a/export.json"
    )
    assert module._recorded_export(["python", "run.py", "--out"]) is None
    assert module._recorded_export(None) is None


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


def test_cli_refuses_existing_outputs_and_any_path_in_the_checkout(tmp_path):
    """RM16: no existing report, Markdown sibling or checkout file is ever replaced."""
    module = analysis()
    matrix = tmp_path / "m8"
    matrix.mkdir()
    existing = tmp_path / "old.json"
    existing.write_text("{}", encoding="utf-8")
    sibling = tmp_path / "notes.md"
    sibling.write_text("keep", encoding="utf-8")
    evidence = module.ROOT / "docs" / "EVIDENCE.md"
    before = evidence.read_bytes()
    targets = (
        existing,
        tmp_path / "notes.json",
        module.ROOT / "docs" / "EVIDENCE.json",
        module.ROOT / "m8-analysis.json",
    )
    for target in targets:
        for mode in ([], ["--inventory"]):
            with pytest.raises(ValueError, match="output"):
                module.main([*mode, "--matrix", str(matrix), "--out", str(target)])
    assert existing.read_text(encoding="utf-8") == "{}"
    assert sibling.read_text(encoding="utf-8") == "keep"
    assert evidence.read_bytes() == before
    assert not (module.ROOT / "m8-analysis.json").exists()
    assert not (module.ROOT / "m8-analysis.md").exists()


@pytest.mark.parametrize("recorded", ["/recorded/m8", "/recorded/checkout"])
def test_cli_refuses_outputs_under_the_recorded_roots(fixture_matrix, recorded):
    """RM16/RM17, spec 14.4.5: writing there would change measured source or evidence."""
    module = analysis()
    with pytest.raises(ValueError, match="recorded root"):
        module.main(["--matrix", str(fixture_matrix), "--out", recorded + "/analysis/m8.json"])
    with pytest.raises(ValueError, match="recorded root"):
        module.main(["--inventory", "--matrix", str(fixture_matrix), "--out", recorded + "/i.json"])


def test_cli_refuses_outputs_under_recorded_windows_roots(tmp_path, monkeypatch):
    """RM16/RM17, spec 14.4.5: a root recorded with backslashes is still guarded.

    On POSIX the Windows root resolves under the working directory, so an output in a
    directory with that literal name is the only place the guard can be seen.
    """
    module = analysis()
    matrix = tmp_path / "m8"
    matrix.mkdir()
    recorded = {
        "matrix": r"C:\recorded\m8",
        "checkout": r"C:\recorded\checkout",
    }
    index = dict(
        identity=dict(source=dict(package_path=recorded["checkout"] + r"\src\relaymind\x.py")),
        attempts={
            "cell": [dict(argv=["python", "run.py", "--out", recorded["matrix"] + r"\c\a\x.json"])]
        },
    )
    (matrix / "index.json").write_text(json.dumps(index), encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    for root in recorded.values():
        out = work / root / "analysis.json"
        with pytest.raises(ValueError, match="recorded root"):
            module.check_recorded_outputs(matrix, out, out.with_suffix(".md"))
    out = work / "elsewhere" / "analysis.json"
    module.check_recorded_outputs(matrix, out, out.with_suffix(".md"))


def test_exact_prior_identity_survives_relocation_with_the_real_loader(tmp_path):
    """Spec 14.4.5/RM17: load_prior provenance has no absolute path, so it survives a move."""
    module = analysis()
    recorded = tmp_path / "recorded"
    recorded.mkdir()
    _, prior, configuration, _, _ = fixture_source(recorded)
    provenance = module.load_prior(prior, configuration).provenance
    moved = tmp_path / "moved"
    moved.mkdir()
    for name in ("prior.json", "source.json"):
        shutil.copy2(recorded / name, moved / name)
    (recorded / "source.json").rename(recorded / "source.moved-away")
    assert module.load_prior(moved / "prior.json", configuration).provenance == provenance
    assert "application" in provenance["sources"][0]


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
# What load_prior names as each source export's application in this fixture.
CALIBRATION_APPLICATION = {"head": "fixture calibration export"}


def fixture_analysis(monkeypatch):
    """The analyzer with a checked stand-in for load_prior.

    Fixture calibration exports carry no traces, so the real loader would refuse them
    (its relocation behavior is tested with real traces above). The stand-in still
    reads each source relative to the actual prior file and checks its digest.
    """
    module = analysis()

    def load_prior(path, configuration):
        raw = Path(path).read_bytes()
        for partition in json.loads(raw)["partitions"]:
            if partition["configuration"] != configuration:
                continue
            for source in partition["sources"]:
                data = (Path(path).parent / source["file"]).read_bytes()
                if hashlib.sha256(data).hexdigest() != source["sha256"]:
                    raise ValueError("source byte digest mismatch")
            provenance = dict(
                bundle_sha256=hashlib.sha256(raw).hexdigest(),
                sources=[
                    dict(s, application=CALIBRATION_APPLICATION) for s in partition["sources"]
                ],
                timings=1,
            )
            return SimpleNamespace(provenance=provenance)
        raise ValueError("no exact prior configuration partition")

    monkeypatch.setattr(module, "load_prior", load_prior)
    return module


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


def recorded_style(root):
    return PureWindowsPath if "\\" in root else PurePosixPath


def recorded_command(driver, cell, name, roots):
    """The driver's command as the recording machine wrote it, in its own path style."""
    recorded, checkout = roots
    style = recorded_style(recorded)
    argv = driver.command(cell, style(recorded) / cell["id"] / name, style(recorded), PROVIDER)
    here, sep = str(driver.ROOT), "\\" if style is PureWindowsPath else "/"
    return [argv[0]] + [
        checkout + v[len(here) :].replace(os.sep, sep) if v.startswith(here) else v
        for v in argv[1:]
    ]


def write_attempt(out, cell, name, *, source, prior_conf, prior, prior_digest, modules, roots):
    driver, corpus, runner_corpus, runner_latency, config, controller = modules
    argv = recorded_command(driver, cell, name, roots)
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
        root=recorded_style(roots[1])(roots[1]),
        work=recorded_style(roots[0])(roots[0]) / cell["id"] / name / "checkpoint.work",
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
            traces={
                g["run_id"]: [dict(event_type="tool.started", payload=dict(tool_name=MUTATING))]
                if g["case_class"] == "mutation" and g["success"]
                else []
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


def build_matrix(out, roots=(RECORDED, CHECKOUT)):
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
        package_path=str(recorded_style(roots[1])(roots[1], "src", "relaymind", "__init__.py")),
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
            # Exactly what the loader stand-in reports for this frozen prior.
            prior = dict(
                bundle_sha256=prior_digest,
                sources=[
                    dict(s, application=CALIBRATION_APPLICATION)
                    for s in priors[cell["configuration"]]
                ],
                timings=1,
            )
        attempts = []
        name = "initial"
        if cell["id"] == INTERRUPTED:
            argv = recorded_command(driver, cell, "initial", roots)
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
            roots=roots,
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


@pytest.fixture(scope="module")
def windows_matrix(tmp_path_factory):
    roots = (r"C:\recorded\m8", r"C:\recorded\checkout")
    return build_matrix(tmp_path_factory.mktemp("windows") / "m8", roots)


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


def rewrite_attempt(matrix, cell_id, *, export=None, checkpoint=None):
    """Change a copied attempt and re-digest it, so only the analyzer's rules can object."""

    def change(index):
        attempt = index["attempts"][cell_id][-1]
        directory = matrix / cell_id / attempt["name"]
        export_path, checkpoint_path = directory / "export.json", directory / "checkpoint.json"
        if export:
            document = json.loads(export_path.read_text(encoding="utf-8"))
            export(document)
            export_path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
        state = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        if checkpoint:
            checkpoint(state)
        state["result"]["export_sha256"] = hashlib.sha256(export_path.read_bytes()).hexdigest()
        checkpoint_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        attempt["export_sha256"] = state["result"]["export_sha256"]
        attempt["checkpoint_sha256"] = hashlib.sha256(checkpoint_path.read_bytes()).hexdigest()

    rewrite_index(matrix, change)


def time_out_first_samples(document, count=10):
    """Turn measured requests into client timeouts without a run, as the runner records them."""
    for row in document["samples"][:count]:
        for key in [k for k in row if k.endswith("_ms")]:
            row[key] = None
        row.update(state=None, error="timeout", timed_out=True, run_id=None)
        row.pop("metrics")


def test_relocated_complete_matrix_produces_accepted_comparisons_and_retains_provenance(
    fixture_matrix, tmp_path, monkeypatch
):
    """RM16/RM25–RM28/RM32, AC19/AC21: relocation keeps recorded paths as provenance."""
    module = fixture_analysis(monkeypatch)
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
    # The fixture records its matrix paths in the host's own spelling.
    assert provenance["matrix_root_recorded"] == str(Path(RECORDED))
    assert provenance["matrix_root_actual"] == str(fixture_matrix.resolve())
    assert provenance["measurement_checkout_recorded"] == CHECKOUT
    assert provenance["measured_revision"] == "f" * 40
    cell = report["cells"]["placement-normal-local_first-router-buffered-corpus"]
    assert cell["attempts"][-1]["argv"][3] == str(
        Path(RECORDED) / "placement-normal-local_first-router-buffered-corpus/initial/export.json"
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
    # Latency rows count successful final answers, never a made-up zero.
    latency_cell = report["cells"]["placement-normal-local_first-router-buffered-latency"]
    assert latency_cell["cohort_external_cost"]["completed_tasks"] == 60
    assert latency_cell["placement"]["external_cost"]["completed_tasks"] == 60
    assert latency_cell["cohort_success_scope"].startswith("latency successful final answers")
    by_class = local["stream_failures"]["B"]["by_measured_class"]
    assert by_class["ready"]["effective_live"] == 156
    hypotheses = report["hypotheses"]
    confirmations = hypotheses["no_missed_confirmation"]["by_cell"][
        "placement-normal-local_first-router-buffered-corpus"
    ]
    assert confirmations["mutating_tool_starts"] > 0
    assert confirmations["mutation_case_runs_with_start"] == confirmations["mutating_tool_starts"]
    assert confirmations["runs_without_trace"] == 0
    costs = hypotheses["external_cost_next_to_success"]["by_cell"]
    assert len(costs) == 29
    assert costs["placement-normal-local_first-router-buffered-latency"]["completed_tasks"] == 60
    none_reliability = hypotheses["none_live_streaming"]["latency_reliability"]
    assert none_reliability["B"]["unobserved_total"] == 0
    assert none_reliability["candidate_worse"] == []
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
    assert expected_wait["prerequisite_variant_count"] == 1
    assert expected_wait["prerequisite_variants"] == [expected_wait["initial_prerequisites"]]
    assert report["defaults"]["changed"] is False
    assert "Reporting acceptance: passed" in markdown
    assert "not measured" in markdown


def test_a_matrix_recorded_on_windows_is_accepted_on_any_platform(
    windows_matrix, tmp_path, monkeypatch
):
    """RM16/RM25, spec 14.4.3/14.4.5: recorded commands and roots keep their own path
    style, so the expected command of a Windows matrix matches its recorded argv."""
    module = fixture_analysis(monkeypatch)
    argv = module._read_json(windows_matrix / "index.json")["attempts"][INTERRUPTED][-1]["argv"]
    assert argv[1] == r"C:\recorded\checkout\scripts\latency_benchmark.py"
    result = module.inventory(windows_matrix)
    assert result["invalid"] == [] and result["problems"] == []
    assert result["accepted"] is True
    report, _ = module.analyze(windows_matrix)
    assert report["reporting_accepted"] is True
    assert report["provenance"]["matrix_root_recorded"] == r"C:\recorded\m8"


def test_report_renders_a_corpus_comparison_without_pairs(fixture_matrix, monkeypatch):
    """RM25, spec 14.4.3: a zero denominator is null and renders as unknown, not a crash."""
    module = fixture_analysis(monkeypatch)
    report, _ = module.analyze(fixture_matrix)
    corpus = next(c for c in report["comparisons"] if c["runner"] == "corpus")
    corpus["paired"] = {**corpus["paired"], **module._paired([]), "class_transitions": {}}
    assert corpus["paired"]["success_delta"] is None
    row = next(
        line
        for line in module.render(report).splitlines()
        if line.startswith(f"| {corpus['question']} | {corpus['configuration']} |")
        and corpus["candidate"] in line
    )
    assert "| - | no pairs |" in row


def test_inventory_mode_reports_complete_and_missing_cells_without_comparisons(
    fixture_matrix, tmp_path, monkeypatch
):
    """RM16/RM25: partial evidence is listed honestly and never becomes an accepted report."""
    module = fixture_analysis(monkeypatch)
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


def test_a_recorded_command_the_parser_refuses_makes_its_cells_invalid(
    fixture_matrix, monkeypatch, capsys
):
    """RM16/RM25: validator drift that refuses a recorded command is invalid evidence;
    inventory lists the cells instead of exiting."""
    import argparse

    module = fixture_analysis(monkeypatch)
    monkeypatch.setattr(module.run_corpus, "argument_parser", argparse.ArgumentParser)
    result = module.inventory(fixture_matrix)
    corpus = [c["id"] for c in module.driver.matrix_cells() if c["runner"] == "corpus"]
    assert result["accepted"] is False
    # Seeded latency cells follow, because their calibration prior is now unverified.
    assert set(corpus) <= set(result["invalid"])
    for cell_id in corpus:
        reason = result["cells"][cell_id]["reason"]
        assert reason.startswith("recorded command does not parse: ")
        assert "unrecognized arguments" in reason
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("change", ["conflicting", "malformed"])
def test_inventory_lists_a_cell_whose_attempt_cost_cannot_be_counted(
    fixture_matrix, tmp_path, monkeypatch, change
):
    """RM16/RM25: a cost ledger that cannot be built marks the cell invalid; inventory
    still lists every cell instead of crashing."""
    module = fixture_analysis(monkeypatch)
    matrix = copy_matrix(fixture_matrix, tmp_path)
    target = "placement-normal-device_aware-router-buffered-corpus"

    def edit(index):
        if change == "conflicting":
            # Two attempts under one name: one physical identity with two costs.
            index["attempts"][INTERRUPTED][0]["name"] = "retry"
        else:
            index["attempts"][target][-1]["cost"] = {"calls": "many"}

    rewrite_index(matrix, edit)
    result = module.inventory(matrix)
    cell_id = INTERRUPTED if change == "conflicting" else target
    record = result["cells"][cell_id]
    assert result["accepted"] is False
    assert cell_id in result["invalid"] and record["status"] == "invalid"
    assert record["physical_cost"] is None
    assert "physical cost" in record["reason"]
    assert "requests_validated" not in record
    assert sum(result["counts"].values()) == 31


@pytest.mark.parametrize(
    "change", ["export_bytes", "prior_bytes", "prior_identity", "argv", "plan_cells"]
)
def test_changed_evidence_fails_acceptance_instead_of_becoming_a_finding(
    fixture_matrix, tmp_path, monkeypatch, change
):
    """RM16/RM17/RM25: identity, digest or linkage drift is invalid evidence, not zeros."""
    module = fixture_analysis(monkeypatch)
    matrix = copy_matrix(fixture_matrix, tmp_path)
    target = "placement-normal-device_aware-router-buffered-corpus"
    if change == "prior_identity":
        # Spec 14.4.5: exact retained prior identity, not only the bundle-bound fields.
        rewrite_attempt(
            matrix,
            target,
            checkpoint=lambda state: state["identity"]["prior"].__setitem__("timings", 2),
        )
    elif change == "export_bytes":
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


def test_latency_verdicts_fail_when_the_candidate_loses_requests_to_timeouts(
    fixture_matrix, tmp_path, monkeypatch
):
    """RM25, spec 12.2/12.3: dropped timeouts cannot make an observed-value p95 'met'."""
    module = fixture_analysis(monkeypatch)
    matrix = copy_matrix(fixture_matrix, tmp_path)
    for cell_id in (
        "streaming-none-local_first-example-live-latency",
        "placement-constrained-device_aware-example-router-buffered-latency",
    ):
        rewrite_attempt(matrix, cell_id, export=time_out_first_samples)
    report, markdown = module.analyze(matrix)
    hypotheses = report["hypotheses"]
    none = hypotheses["none_live_streaming"]
    # Observed values alone would still say the live arm is faster.
    assert none["first_useful_content_ms"]["lower_p95"] is True
    assert none["first_useful_content_ms"]["n_A_B"] == [120, 110]
    reliability = none["latency_reliability"]
    assert reliability["B"]["timeouts"] == 10
    assert reliability["B"]["unobserved"] == {"timeout": 10}
    assert reliability["A"]["timeouts"] == reliability["A"]["unobserved_total"] == 0
    assert reliability["candidate_worse"] == ["timeouts", "unobserved_total"]
    assert none["status"] == "not met"
    scope = hypotheses["unsuitable_device_aware"]["by_scope"]["constrained/example-router"]
    assert scope["latency_reliability"]["B"]["timeouts"] == 10
    assert scope["status"] == "not met"
    unsuitable = hypotheses["unsuitable_device_aware"]
    assert unsuitable["status"] == "not met"
    assert unsuitable["by_scope"]["constrained/router"]["status"] == "met"
    comparison = next(
        c
        for c in report["comparisons"]
        if c["candidate"] == "streaming-none-local_first-example-live-latency"
    )
    assert comparison["paired"]["B"]["unobserved_total"] == 10
    assert "0/10/10" in markdown


def test_scope_without_an_unsuitable_cohort_stays_not_measured_when_latency_is_worse(
    fixture_matrix, tmp_path, monkeypatch
):
    """RM25, spec 12.3: worse candidate reliability cannot turn a scope with no measured
    unsuitable cohort into 'not met'; the reliability counts are still reported."""
    module = fixture_analysis(monkeypatch)
    matrix = copy_matrix(fixture_matrix, tmp_path)
    rewrite_attempt(
        matrix,
        "placement-normal-device_aware-router-buffered-latency",
        export=time_out_first_samples,
    )
    report, _ = module.analyze(matrix)
    unsuitable = report["hypotheses"]["unsuitable_device_aware"]
    scope = unsuitable["by_scope"]["normal/router"]
    assert scope["class_transitions"] == {"ready/ready": 52}
    assert scope["latency_reliability"]["candidate_worse"] == ["timeouts", "unobserved_total"]
    assert scope["status"] == "not measured"
    assert unsuitable["by_scope"]["constrained/router"]["status"] == "met"


def test_cli_writes_utf8_reports_under_cp1252_default(fixture_matrix, tmp_path, monkeypatch):
    """RM16/RM25: a non-ASCII evidence root survives a Windows-style text default."""
    module = fixture_analysis(monkeypatch)
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
