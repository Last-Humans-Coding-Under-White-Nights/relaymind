"""M7 observation coverage and initial selector attribution (RM16/RM25/RM28/RM29)."""

import json
from copy import deepcopy

import pytest

from helpers import load_script_module
from relaymind.evaluation.metrics import _corrections
from relaymind.evaluation.placement_metrics import trace_metrics
from test_evaluation_grading import make_case


def event(kind, actor="gateway", **payload):
    return {"event_type": kind, "actor": actor, "payload": payload}


def test_resolved_prefetch_and_model_proposal_denominators():
    """RM25/RM29/AC18: rejection is diagnostic, workflows are not prefetch."""
    from relaymind.evaluation.m7_metrics import enrich_metrics, summarize_m7

    proposals = [
        event(
            "tool.proposed",
            mechanism="prefetch",
            proposal_id="a",
            tool_name="get_service_health",
            arguments={"service": "payments-api"},
            admissible=True,
        ),
        event(
            "tool.proposed",
            mechanism="prefetch",
            proposal_id="b",
            tool_name="get_service_health",
            arguments={"service": "bad"},
            admissible=False,
            rejection={"code": "permission_denied"},
        ),
        event(
            "tool.proposed",
            mechanism="workflow",
            proposal_id="c",
            tool_name="get_service_health",
            arguments={"service": "bad"},
            admissible=True,
        ),
        event("tool.started", proposal_id="a"),
        event("run.completed", final_answer_model="gateway"),
    ]
    metrics = enrich_metrics(make_case(), proposals, expected_skill=None)
    result = summarize_m7([{"state": "completed", "metrics": metrics}])
    assert result["model_proposal_accuracy"] == {"numerator": 0, "denominator": 0, "rate": None}
    assert result["prefetch_accuracy"] == {"numerator": 1, "denominator": 2, "rate": 0.5}
    assert result["prefetch_diagnostics"] == {"admitted": 1, "rejected": 1, "executed": 1}
    assert result["gateway_proposals"] == 3
    assert result["owners"] == {
        "eligible": 1,
        "local": 0,
        "external": 0,
        "gateway": 1,
        "unknown": 0,
    }
    empty = summarize_m7(
        [
            {
                "state": "failed",
                "metrics": enrich_metrics(make_case(), [event("run.failed")], expected_skill=None),
            }
        ]
    )
    assert empty["prefetch_accuracy"]["rate"] is None


def test_first_token_joins_and_keeps_unfinished_zero_and_phase():
    """RM16/RM25: no reconstructed duration; interrupted calls remain eligible."""
    from relaymind.evaluation.m7_metrics import first_token_summary, model_calls

    events = [
        event("model.started", "local", call_id="a", model="q", purpose="route"),
        event("model.finished", "local", call_id="a", first_token_ms=0, duration_ms=50),
        event("model.started", "external", call_id="b", model="g", purpose="external"),
        event("model.started", "local", call_id="c", model="q", purpose="final"),
        event("model.finished", "local", call_id="c", duration_ms=100, error="cancelled"),
    ]
    calls = model_calls(events, phase="cold")
    assert [(c["model"], c["purpose"], c["first_token_ms"]) for c in calls] == [
        ("q", "route", 0),
        ("g", "external", None),
        ("q", "final", None),
    ]
    result = first_token_summary(calls + model_calls(events[:2], phase="measured"))
    assert result["cold/local/q/route"]["n"] == 1
    assert result["cold/local/q/route"]["p50"] == 0
    assert result["cold/external/g/external"]["missing"] == 1
    assert result["cold/local/q/final"]["missing"] == 1
    assert result["measured/local/q/route"]["eligible"] == 1


def test_mixed_corrections_never_shrink_completed_denominator():
    """RM16/RM25: unavailable trace evidence is not zero corrections."""
    result = _corrections(
        [
            {"state": "completed", "corrections": 2, "correction_ms": 20},
            {"state": "completed"},
            {"state": "failed", "corrections": 0, "correction_ms": 0},
        ]
    )
    assert result["completed_runs"] == 2
    assert result["measured_completed_runs"] == 1
    assert result["missing_completed_runs"] == 1
    assert result["per_completed_run"] is None
    assert result["duration_ms"] is None
    assert result["known_duration_ms"] == 20
    assert (
        _corrections([{"state": "completed", "corrections": 1, "correction_ms": None}])[
            "missing_duration_runs"
        ]
        == 1
    )


def test_initial_selection_deduplicates_progress_and_does_not_score_resume():
    """RM25/RM28/AC22: fallback winner and later clarification are not earlier predictions."""
    from relaymind.evaluation.m7_metrics import enrich_metrics, summarize_m7

    a = {
        "name": "example_match",
        "version": "1",
        "transport": "in_process",
        "outcome": "timeout",
        "elapsed_ms": 10,
    }
    b = {
        "name": "model_router",
        "version": "1",
        "transport": "in_process",
        "outcome": "selected",
        "elapsed_ms": 20,
    }
    events = [
        event("handoff.started", selector={"attempts": [a]}),
        event(
            "route.selected",
            selector={"name": "model_router", "attempts": [a, b]},
            decision={"skill_id": None},
        ),
        event("clarification.required"),
        event("route.selected", mechanism="clarification_resume", decision={"skill_id": "later"}),
    ]
    metrics = enrich_metrics(
        make_case(),
        events,
        expected_skill=None,
        selector_chain=["example_match", "model_router", "third"],
    )
    assert metrics["selection"]["predicted"] == []
    assert len(metrics["selector_attempts"]) == 2
    result = summarize_m7([{"state": "completed", "metrics": metrics}])
    groups = result["selection_by_attempted_selector"]
    assert groups["example_match/1/in_process"]["attempted"] == 1
    assert groups["example_match/1/in_process"]["failed"] == 1
    assert groups["example_match/1/in_process"]["jaccard"] == 0
    assert groups["model_router/1/in_process"]["jaccard"] == 0
    assert groups["third/unknown/unknown"]["skipped"] == 1
    assert trace_metrics(events, expected_skill=None)["selection"]["predicted"] == []


def test_missing_selection_is_not_a_measured_empty_prediction():
    """RM16/RM25: missing traces expose coverage instead of fabricated zero scores."""
    from relaymind.evaluation.m7_metrics import enrich_metrics, summarize_m7

    metrics = enrich_metrics(make_case(), [], expected_skill="s", selector_chain=["model_router"])
    result = summarize_m7([{"state": "completed", "metrics": metrics}])
    assert result["owners"]["unknown"] == 1
    assert (
        result["selection_by_attempted_selector"]["model_router/unknown/unknown"]["unobserved"] == 1
    )
    assert (
        result["selection_by_attempted_selector"]["model_router/unknown/unknown"]["jaccard"] is None
    )


@pytest.mark.parametrize(
    "owner,actor,expected",
    [("q", "local", "local"), ("g", "external", "external"), ("unseen", "local", "unknown")],
)
def test_answer_owner_requires_observed_model(owner, actor, expected):
    """RM16/RM25: gateway, local, external and unknown are separate ownership classes."""
    from relaymind.evaluation.m7_metrics import enrich_metrics

    events = [
        event(
            "model.started",
            actor,
            call_id="a",
            model="g" if actor == "external" else "q",
            purpose="final",
        ),
        event("run.completed", final_answer_model=owner),
    ]
    assert enrich_metrics(make_case(), events, expected_skill=None)["answer_owner"] == expected


def test_regrade_preserves_source_and_prior_analysis_with_metric_enrichment(tmp_path):
    """RM16/RM25: analysis is separate from measurement provenance and immutable bytes."""
    runner = load_script_module("run_corpus")
    source = runner.REPO_ROOT / "docs/evidence/2026-10-22-corpus-apple-m1.json"
    document = json.loads(source.read_text(encoding="utf-8"))
    original = deepcopy(document)
    result = runner.regrade(
        document, runner.load_frozen_corpus(), source_sha256=runner.byte_digest(source)
    )
    assert document == original
    assert result["application"] == original["application"]
    assert result.get("configuration") == original.get("configuration")
    assert result["analysis_history"][-1] == original["regraded_with"]
    assert result["regraded_with"]["source_sha256"] == runner.byte_digest(source)
    assert all("metrics" in grade for grade in result["grades"])
    for language, n, count, duration in [("en", 27, 10, 85041), ("zh", 12, 7, 57420)]:
        block = result["summary"]["modes"]["auto"][language]["all"]
        assert block["corrections"]["completed_runs"] == n
        assert block["corrections"]["corrections"] == count
        assert block["corrections"]["duration_ms"] == duration
    report = runner.render_corpus_markdown(result["summary"], title="M7")
    for heading in (
        "model proposal accuracy",
        "prefetch accuracy",
        "answer owner",
        "first token",
        "generations",
        "attempted selector",
        "known token",
    ):
        assert heading in report


def test_regrade_cli_writes_distinct_output_and_records_byte_digest(tmp_path):
    """RM16/RM25: no in-place rewrite of measured evidence is permitted."""
    runner = load_script_module("run_corpus")
    source = runner.REPO_ROOT / "docs/evidence/2026-10-22-corpus-apple-m1.json"
    before = source.read_bytes()
    out = tmp_path / "analysis.json"
    runner.main(["--regrade", str(source), "--out", str(out)])
    assert source.read_bytes() == before
    assert json.loads(out.read_text(encoding="utf-8"))["regraded_with"][
        "source_sha256"
    ] == runner.byte_digest(source)
    with pytest.raises(SystemExit):
        runner.main(["--regrade", str(source), "--out", str(source)])


def test_latency_first_token_populations_remain_separate():
    """RM16/RM25: cold/startup/runner-warmup cannot inflate measured timing n."""
    from relaymind.evaluation.m7_metrics import latency_first_tokens

    call = {"actor": "local", "model": "q", "purpose": "route", "first_token_ms": 7}
    document = {
        "cold_start": {
            "cold_request": {"model_calls": [call]},
            "startup_warmup": {
                "enabled": True,
                "samples": [{"purpose": "warmup", "first_token_ms": 0}],
            },
        },
        "warmups": [{"model_calls": [call]}],
        "samples": [{"model_calls": [call]}],
    }
    result = latency_first_tokens(document)
    assert {k.split("/")[0] for k in result} == {
        "cold",
        "startup_warmup",
        "runner_warmup",
        "measured",
    }
    assert sum(v["eligible"] for v in result.values()) == 4


def test_source_fingerprint_includes_fixture_identity(tmp_path):
    """RM16/RM25: same source label with different sandbox bytes cannot authorize reuse."""
    from relaymind.evaluation.environment import source_fingerprint

    fixture = tmp_path / "tests/fixtures/sandbox/seed.json"
    fixture.parent.mkdir(parents=True)
    fixture.write_text("{}", encoding="utf-8")
    before = source_fingerprint(tmp_path)
    fixture.write_text('{"changed":true}', encoding="utf-8")
    after = source_fingerprint(tmp_path)
    assert before["source_sha256"] != after["source_sha256"]
