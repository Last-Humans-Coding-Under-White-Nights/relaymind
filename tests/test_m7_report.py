"""RM16/RM25: reports validate immutable coverage and do not infer unmeasured effects."""

from copy import deepcopy

import pytest

from helpers import load_script_module


def test_report_rejects_missing_metric_blocks():
    report = load_script_module("analyze_m7")
    assert report.metric_coverage({"grades": [{"metrics": {}}]})["accepted"] is False
    missing = report.metric_coverage({"grades": [{"metrics": {}}]})["missing"]
    assert "prefetch_proposals" in str(missing)
    assert "model_calls" in str(missing)


def test_failed_attempt_cost_is_retained_and_physical_reuse_deduplicated():
    report = load_script_module("analyze_m7")
    cost = {
        "calls": 2,
        "input_tokens_known_total": 10,
        "input_tokens_missing_calls": 1,
        "output_tokens_known_total": 5,
        "output_tokens_missing_calls": 0,
        "unreadable_databases": [],
    }
    attempts = [
        {"identity": "failed", "cost": cost},
        {"identity": "complete", "cost": cost},
        {"identity": "complete", "cost": cost},
    ]
    value = report.physical_cost(attempts, successes=1)
    assert value["calls"] == 4 and value["calls_per_successful_task"] == 4
    assert value["input_tokens_per_successful_task"] is None
    assert value["input_tokens_known_total"] == 20
    assert report.physical_cost(attempts, successes=0)["calls_per_successful_task"] is None
    changed = deepcopy(attempts)
    changed[-1]["cost"] = {**changed[-1]["cost"], "calls": 3}
    with pytest.raises(ValueError, match="physical"):
        report.physical_cost(changed, successes=1)


def test_unmeasured_hypotheses_are_explicit_and_missed_targets_are_valid():
    report = load_script_module("analyze_m7")
    result = report.hypotheses({})
    assert len(result) == 9
    assert {v["status"] for v in result.values()} == {"not measured"}
    assert report.verdict(False) == "not met"
    assert report.verdict(True) == "met"


def test_export_requires_full_case_mode_identity():
    report = load_script_module("analyze_m7")
    runner = load_script_module("run_corpus")
    corpus = runner.load_frozen_corpus()
    cell = {"id": "M7-C0", "runner": "corpus", "modes": ["auto"], "requests": 52}
    document = {
        "corpus": {"sha256": corpus.digest},
        "grades": [{"case_id": case.case_id, "mode": "auto"} for case in corpus.cases],
    }
    report.validate_case_coverage(cell, document, corpus)
    document["grades"][-1] = document["grades"][0]
    with pytest.raises(ValueError, match="case/mode"):
        report.validate_case_coverage(cell, document, corpus)


def test_report_retains_cost_of_cell_without_any_completed_attempt(tmp_path):
    """RM16/RM25: a failed matrix remains reportable and its paid calls cannot disappear."""
    import json
    import sqlite3

    from relaymind.canonical import canonical_digest
    from relaymind.evaluation.checkpoint import atomic_json

    report = load_script_module("analyze_m7")
    cell = {
        "id": "M7-C0",
        "runner": "corpus",
        "revision": "a" * 40,
        "requests": 156,
        "modes": ["auto", "external_only", "local_only"],
    }
    plan = {"cells": [cell]}
    directory = tmp_path / "M7-C0/initial"
    directory.mkdir(parents=True)
    with sqlite3.connect(directory / "runs.sqlite3") as db:
        db.execute("CREATE TABLE events(run_id TEXT,event_type TEXT,actor TEXT,payload TEXT)")
        db.execute(
            "INSERT INTO events VALUES(?,?,?,?)",
            ("r", "model.started", "external", json.dumps(dict(call_id="c", model="fixture"))),
        )
    atomic_json(tmp_path / "plan.json", plan)
    atomic_json(
        tmp_path / "index.json",
        dict(
            identity=dict(plan_sha256=canonical_digest(plan), sources={"root": {"head": "a" * 40}}),
            attempts={
                "M7-C0": [dict(name="initial", status="interrupted", argv=["python", "runner"])]
            },
        ),
    )
    result, markdown = report.analyze(tmp_path)
    assert result["physical_cost"]["calls"] == 1
    assert result["physical_cost"]["input_tokens_missing_calls"] == 1
    assert result["reporting_accepted"] is False
    assert "not measured" in markdown


def test_report_validates_configuration_against_cell_not_only_self_consistent_export():
    """RM16/RM25: a seeded or changed-config export cannot masquerade as baseline."""
    report = load_script_module("analyze_m7")
    driver = load_script_module("run_m7_matrix")
    runner = load_script_module("run_corpus")
    cell = driver.matrix_cells("a" * 40, omit_criteria=True)[0]
    env = {
        "RELAYMIND_EXTERNAL_BASE_URL": "https://example.invalid/v1",
        "RELAYMIND_EXTERNAL_MODEL": "fixture",
    }
    from pathlib import Path

    args = runner.argument_parser().parse_args(driver.command(cell, Path("/unused"), env)[2:])
    config = driver.configuration_from_args(args).to_json_object()
    document = {
        "configuration": config,
        "timing_prior": None,
        "application": {"head": "a" * 40},
        "external_endpoint": {
            "base_url": env["RELAYMIND_EXTERNAL_BASE_URL"],
            "model": "fixture",
            "context_limit": 128000,
        },
    }
    report.validate_cell_configuration(cell, document, env)
    document["configuration"]["performance"]["prefetch"] = True
    with pytest.raises(ValueError, match="configuration"):
        report.validate_cell_configuration(cell, document, env)


def test_complex_generation_hypothesis_uses_only_languages_with_complex_cases():
    """RM25, section 12.2: the frozen corpus has no Chinese complex cases, so the
    prefetch/many-generations comparison is English only and never asks for zh/complex."""
    report = load_script_module("analyze_m7")

    def document(rate):
        complex_block = {"placement_metrics": {"generations": {"per_completed_run": rate}}}
        return {
            "summary": {
                "modes": {
                    "auto": {
                        "en": {"all": {}, "classes": {"simple": {}, "complex": complex_block}},
                        "zh": {"all": {}, "classes": {"simple": {}}},
                    }
                }
            }
        }

    documents = {"M7-C0": document(4.0), "M7-C2": document(3.0), "M7-C3": document(2.5)}
    result = report.hypotheses(documents)["prefetch_and_many_generations"]
    assert result["status"] == "met"
    assert result["values"] == {"en": [4.0, 3.0, 2.5]}
    assert result["not_applicable"] == {"zh": "the frozen corpus has no zh complex cases"}
