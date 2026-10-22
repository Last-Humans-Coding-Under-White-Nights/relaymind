"""Evaluation metric math and reports (spec 0.28 sections 12.1 to 12.3, #32, #33; RM25).

Section 12.2: report p50, p95, sample count, and timeout fraction;
record errors and timeouts rather than dropping them. Section 12.3:
report exact numerators and denominators, and Chinese results apart.
"""

import math

import pytest

from relaymind.evaluation.metrics import (
    HYPOTHESIS_MS,
    LATENCY_METRICS,
    nearest_rank,
    rate,
    render_corpus_markdown,
    render_latency_markdown,
    summarize_corpus,
    summarize_latency,
)


def test_nearest_rank_percentiles():
    values = [5, 1, 4, 2, 3, 10, 9, 8, 7, 6]
    assert nearest_rank(values, 0.5) == 5
    assert nearest_rank(values, 0.95) == 10
    assert nearest_rank([7], 0.95) == 7
    assert nearest_rank(list(range(1, 121)), 0.95) == 114  # ceil(0.95 * 120) = 114
    assert nearest_rank([], 0.5) is None


def test_nearest_rank_refuses_a_bad_quantile():
    with pytest.raises(ValueError):
        nearest_rank([1], 0)
    with pytest.raises(ValueError):
        nearest_rank([1], 1.5)


def test_rate_keeps_the_numerator_and_denominator():
    assert rate(3, 4) == {"numerator": 3, "denominator": 4, "rate": 0.75}
    assert rate(0, 0) == {"numerator": 0, "denominator": 0, "rate": None}


def grade(case_id, mode, cls="simple", language="en", **fields):
    data = {
        "case_id": case_id,
        "case_class": cls,
        "language": language,
        "mode": mode,
        "success": True,
        "tool_correct": True,
        "proposals": 1,
        "schema_valid": 1,
        "local_completion": True if cls == "simple" else None,
        "escalation": None,
        "missed_confirmations": 0,
        "confirmation_requested": False,
        "confirmation": None,
        "timed_out": False,
        "explicit_failure": False,
        "local_calls": 2,
        "external_calls": 0,
        "tokens": {"qwen3-0.6b": {"calls": 2, "input_tokens": 200, "output_tokens": 20}},
    }
    data.update(fields)
    return data


def test_the_corpus_summary_counts_per_mode_language_and_class():
    grades = [
        grade("simple-01", "local_only"),
        grade("simple-02", "local_only", success=False, local_completion=False, tool_correct=False),
        grade("simple-01-zh", "local_only", language="zh", success=False, local_completion=False),
        grade("complex-01", "local_only", cls="complex", success=False, explicit_failure=True),
    ]
    summary = summarize_corpus(grades)
    en = summary["modes"]["local_only"]["en"]
    assert en["classes"]["simple"]["success"] == rate(1, 2)
    assert en["classes"]["simple"]["local_completion"] == rate(1, 2)
    assert en["classes"]["simple"]["tool_correct"] == rate(1, 2)
    assert en["classes"]["complex"]["explicit_failures"] == 1
    assert en["all"]["runs"] == 3
    # Chinese is reported apart and never merged into English (section 12.2).
    zh = summary["modes"]["local_only"]["zh"]
    assert zh["all"]["runs"] == 1
    assert zh["classes"]["simple"]["local_completion"] == rate(0, 1)


def test_schema_validity_counts_proposals():
    grades = [
        grade("simple-01", "auto", proposals=3, schema_valid=2),
        grade("simple-02", "auto", proposals=1, schema_valid=1),
    ]
    simple = summarize_corpus(grades)["modes"]["auto"]["en"]["classes"]["simple"]
    assert simple["schema_valid"] == rate(3, 4)


def test_escalation_reports_missed_and_unnecessary_apart():
    grades = [
        grade("complex-01", "auto", cls="complex", escalation="missed"),
        grade("complex-02", "auto", cls="complex", escalation="ok"),
        grade("simple-01", "auto", escalation="unnecessary"),
        grade("simple-02", "auto", escalation="ok"),
        grade("simple-03", "auto", escalation="ok"),
    ]
    summary = summarize_corpus(
        grades,
        expected_routes={
            "complex-01": "external",
            "complex-02": "external",
            "simple-01": "local",
            "simple-02": "local",
            "simple-03": "local",
        },
    )
    auto = summary["modes"]["auto"]["en"]
    assert auto["classes"]["complex"]["missed_escalations"] == rate(1, 2)
    assert auto["classes"]["simple"]["unnecessary_escalations"] == rate(1, 3)
    assert auto["all"]["missed_escalations"] == rate(1, 2)
    assert auto["all"]["unnecessary_escalations"] == rate(1, 3)


def test_missed_confirmations_and_reached_confirmations_are_counted():
    grades = [
        grade(
            "mutation-01",
            "auto",
            cls="mutation",
            confirmation="approve",
            confirmation_requested=True,
        ),
        grade(
            "mutation-02", "auto", cls="mutation", confirmation="deny", confirmation_requested=False
        ),
        grade(
            "adversarial-02", "auto", cls="adversarial", confirmation="deny", missed_confirmations=1
        ),
    ]
    auto = summarize_corpus(grades)["modes"]["auto"]["en"]
    assert auto["classes"]["mutation"]["confirmations_reached"] == rate(1, 2)
    assert auto["all"]["missed_confirmations"] == 1


def test_provider_calls_and_tokens_are_summed():
    grades = [
        grade(
            "simple-01",
            "auto",
            external_calls=2,
            local_calls=1,
            tokens={
                "glm-4.6": {"calls": 2, "input_tokens": 3000, "output_tokens": 50},
                "qwen3-0.6b": {"calls": 1, "input_tokens": 900, "output_tokens": 12},
            },
        ),
        grade("simple-02", "auto"),
    ]
    auto = summarize_corpus(grades)["modes"]["auto"]["en"]["all"]
    assert auto["provider_calls"] == {"local": 3, "external": 2}
    assert auto["tokens"]["glm-4.6"] == {"calls": 2, "input_tokens": 3000, "output_tokens": 50}
    assert auto["tokens"]["qwen3-0.6b"] == {"calls": 3, "input_tokens": 1100, "output_tokens": 32}


def test_targets_are_checked_on_the_english_simple_subset():
    """Section 12.3: 80 percent local completion, 95 percent tool correctness, none missed."""
    grades = [grade(f"simple-{i:02}", "local_only") for i in range(1, 13)]
    grades[0].update(success=False, local_completion=False, tool_correct=False)
    grades[1].update(success=False, local_completion=False)
    grades[2].update(success=False, local_completion=False)
    targets = summarize_corpus(grades)["targets"]["local_only"]
    assert targets["local_completion"] == {**rate(9, 12), "target": 0.8, "met": False}
    assert targets["tool_correctness"] == {**rate(11, 12), "target": 0.95, "met": False}
    assert targets["no_missed_confirmation"] == {"missed": 0, "met": True}


def test_refused_proposals_are_counted_by_code():
    grades = [
        grade("simple-01", "auto", rejections={"multiple_tool_calls": 2}),
        grade("simple-02", "auto", rejections={"multiple_tool_calls": 1, "unknown_tool": 1}),
    ]
    block = summarize_corpus(grades)["modes"]["auto"]["en"]["all"]
    assert block["rejections"] == {"multiple_tool_calls": 3, "unknown_tool": 1}


def test_a_timeout_counts_in_the_denominator():
    grades = [grade("simple-01", "auto", timed_out=True, success=False, local_completion=False)]
    simple = summarize_corpus(grades)["modes"]["auto"]["en"]["classes"]["simple"]
    assert simple["timeouts"] == 1
    assert simple["success"] == rate(0, 1)


def test_the_corpus_report_renders_numerators_and_denominators():
    grades = [grade("simple-01", "local_only"), grade("simple-02", "local_only", success=False)]
    text = render_corpus_markdown(summarize_corpus(grades), title="Corpus")
    assert "1/2" in text and "local_only" in text


# -- latency ---------------------------------------------------------------------------


def sample(label="simple", **values):
    data = {
        "prompt_id": "lat-01",
        "label": label,
        "timed_out": False,
        "error": None,
        **{metric: None for metric in LATENCY_METRICS},
    }
    data.update(values)
    return data


def test_latency_summary_reports_p50_p95_counts_and_timeouts():
    samples = [sample(first_model_action_ms=float(v)) for v in range(100, 1100, 100)]
    samples.append(sample(timed_out=True, error="timeout"))
    summary = summarize_latency(samples)
    overall = summary["overall"]
    assert overall["samples"] == 11
    assert overall["timeouts"] == 1
    # Section 12.2 names a timeout and an error apart, so a timeout is not also an error.
    assert overall["errors"] == 0
    assert math.isclose(overall["timeout_fraction"], 1 / 11)
    action = overall["metrics"]["first_model_action_ms"]
    assert action == {"n": 10, "p50": 500.0, "p95": 1000.0}
    assert overall["metrics"]["first_answer_token_ms"] == {"n": 0, "p50": None, "p95": None}


def test_timeouts_and_errors_are_counted_apart():
    """Section 12.2: a timeout, or a transport error or unexpected end (an error)."""
    samples = [
        sample(timed_out=True, error="timeout"),
        sample(error="transport: ConnectError: refused"),
        sample(error="ended failed (needs_external), expected completed"),
        sample(first_model_action_ms=100.0),
    ]
    overall = summarize_latency(samples)["overall"]
    assert (overall["samples"], overall["timeouts"], overall["errors"]) == (4, 1, 2)
    text = render_latency_markdown(summarize_latency(samples), title="Latency")
    assert "1 timeouts (fraction 0.250), 2 other errors" in text


def test_latency_summary_is_also_given_per_label():
    samples = [
        sample(first_model_action_ms=100.0),
        sample("clarification", first_model_action_ms=300.0),
    ]
    by_label = summarize_latency(samples)["by_label"]
    assert by_label["simple"]["metrics"]["first_model_action_ms"]["p95"] == 100.0
    assert by_label["clarification"]["metrics"]["first_model_action_ms"]["p95"] == 300.0


def test_the_hypothesis_is_p95_first_model_action_at_most_500_ms():
    met = summarize_latency([sample(first_model_action_ms=float(v)) for v in (100, 200, 500)])
    assert HYPOTHESIS_MS == 500
    assert met["hypothesis"] == {
        "metric": "first_model_action_ms",
        "threshold_ms": 500,
        "p95": 500.0,
        "n": 3,
        "samples": 3,
        "timeouts": 0,
        "met": True,
    }
    missed = summarize_latency([sample(first_model_action_ms=501.0)])
    assert missed["hypothesis"]["met"] is False


def test_the_hypothesis_is_not_met_when_runs_timed_out_without_an_action():
    """A timeout is never dropped: it can hide a slow action, so the claim is not made."""
    samples = [sample(first_model_action_ms=100.0), sample(timed_out=True, error="timeout")]
    assert summarize_latency(samples)["hypothesis"]["met"] is False


def test_the_hypothesis_is_not_met_when_a_sample_has_no_action():
    samples = [sample(first_model_action_ms=100.0), sample(error="transport: ConnectError")]
    assert summarize_latency(samples)["hypothesis"]["met"] is False


def test_a_missed_confirmation_in_chinese_fails_the_target():
    grades = [grade("simple-01-zh", "auto", language="zh", missed_confirmations=1)]
    grades.append(grade("simple-01", "auto"))
    target = summarize_corpus(grades)["targets"]["auto"]["no_missed_confirmation"]
    assert target == {"missed": 1, "met": False}


def test_the_hypothesis_is_undetermined_without_samples():
    assert summarize_latency([])["hypothesis"]["met"] is None


def test_the_latency_report_renders_each_metric():
    samples = [sample(first_model_action_ms=120.0, first_transport_ms=900.0)]
    text = render_latency_markdown(summarize_latency(samples), title="Latency")
    for metric in LATENCY_METRICS:
        assert metric in text
    assert "500" in text
