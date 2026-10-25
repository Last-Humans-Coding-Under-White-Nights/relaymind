"""RM16/RM25/RM27/RM28: costs retain failed attempts and missing measurements."""


def event(kind, actor="gateway", **payload):
    return {"event_type": kind, "actor": actor, "payload": payload}


def test_distribution_and_cost_denominators_keep_failed_usage():
    from relaymind.evaluation.placement_metrics import summarize_placement, trace_metrics

    completed = [
        event("run.accepted", local_model_status="ready"),
        event("model.started", "external", call_id="a", purpose="final", model="remote"),
        event(
            "model.finished",
            "external",
            call_id="a",
            duration_ms=30,
            usage={"input_tokens": 10, "output_tokens": None},
        ),
        event("answer.delta", "external", model="remote", text="answer"),
        event("run.completed", final_answer_model="remote", local_active_ms=12),
    ]
    failed = [
        event("model.started", "external", call_id="b", purpose="final", model="remote"),
        event(
            "model.finished",
            "external",
            call_id="b",
            duration_ms=30,
            usage={"input_tokens": 20, "output_tokens": 4},
        ),
        event("run.failed", error_code="external_failed"),
    ]
    rows = [
        dict(success=True, state="completed", metrics=trace_metrics(completed)),
        dict(success=False, state="failed", metrics=trace_metrics(failed)),
    ]
    result = summarize_placement(rows)
    assert result["external_cost"]["calls_per_completed_task"] == 2
    assert result["external_cost"]["input_tokens_per_completed_task"] == 30
    assert result["external_cost"]["output_tokens_per_completed_task"] is None
    assert result["external_cost"]["output_tokens_missing_calls"] == 1
    assert result["generations"]["completed_runs"] == 1
    assert result["generations"]["by_actor_purpose"] == {"external/final": 1}
    assert result["wasted_local_ms"]["n"] == 1
    assert result["wasted_local_ms"]["p50"] == 12


def test_prediction_and_wasted_time_never_reconstruct_unknowns():
    from relaymind.evaluation.placement_metrics import summarize_placement, trace_metrics

    events = [
        event("model.finished", "local", call_id="x", duration_ms=40, predicted_ms=30),
        event("model.finished", "local", call_id="y", duration_ms=20, predicted_ms=None),
        event("run.failed", error_code="needs_external", local_active_ms=None),
    ]
    value = summarize_placement(
        [dict(success=False, state="failed", metrics=trace_metrics(events))]
    )
    assert value["prediction_error_ms"]["values"] == [-10]
    assert value["prediction_error_ms"]["missing"] == 1
    assert value["wasted_local_ms"]["eligible"] == 1
    assert value["wasted_local_ms"]["missing"] == 1
    assert value["wasted_local_ms"]["p50"] is None
    assert value["external_cost"]["calls_per_completed_task"] is None


def test_latency_completion_excludes_waiting_errors_and_timeouts():
    from relaymind.evaluation.metrics import summarize_latency

    samples = [
        dict(label="simple", completion_ms=10, state="completed", error=None, timed_out=False),
        dict(label="simple", completion_ms=500, state="failed", error="failed", timed_out=False),
        dict(
            label="clarification",
            completion_ms=20,
            state="waiting_input",
            error=None,
            timed_out=False,
        ),
    ]
    result = summarize_latency(samples)
    assert result["overall"]["metrics"]["completion_ms"] == {"n": 1, "p50": 10, "p95": 10}


def test_unknown_trace_is_not_free_external_work():
    """RM16/RM25: a failed attempt without trace makes aggregate cost incomplete."""
    from relaymind.evaluation.placement_metrics import summarize_placement

    value = summarize_placement(
        [
            {"state": "completed", "success": True, "metrics": {"external_usage": []}},
            {"state": "error", "success": False},
        ]
    )
    assert value["external_cost"]["unobserved_runs"] == 1
    assert value["external_cost"]["calls_per_completed_task"] is None


def test_failed_streams_and_admitted_selector_are_distinct_from_attempts():
    """RM28/RM32: fallback attempts do not each get credit for the admitted choice."""
    from relaymind.evaluation.placement_metrics import summarize_placement, trace_metrics

    events = [
        event(
            "route.selected",
            decision={"skill_id": "s"},
            selector={
                "name": "model_router",
                "attempts": [
                    {"name": "example_match", "outcome": "none"},
                    {"name": "model_router", "outcome": "selected"},
                ],
            },
        ),
        event("answer.delta", "local", model="local", call_id="c", text="prefix"),
        event("run.failed", error_code="predicate_unmet"),
    ]
    row = trace_metrics(events, expected_skill="s")
    assert row["admitted_selector"] == "model_router"
    assert row["streamed_failure"] is True
    report = summarize_placement([{"success": False, "state": "failed", "metrics": row}])
    assert report["selection_by_admitted_selector"]["model_router"]["n"] == 1
    assert report["streamed_failures"] == 1


def test_latency_keeps_measured_classes_separate():
    """RM25/RM27: runtime labels cannot merge actual ready/unsuitable observations."""
    from relaymind.evaluation.metrics import summarize_latency

    samples = [
        dict(
            label="simple",
            state="completed",
            error=None,
            timed_out=False,
            completion_ms=10,
            metrics={"local_class": "ready"},
        ),
        dict(
            label="simple",
            state="completed",
            error=None,
            timed_out=False,
            completion_ms=100,
            metrics={"local_class": "unsuitable"},
        ),
    ]
    result = summarize_latency(samples)
    assert result["by_measured_class"]["ready"]["metrics"]["completion_ms"]["p95"] == 10
    assert result["by_measured_class"]["unsuitable"]["metrics"]["completion_ms"]["p95"] == 100


def test_missing_completed_traces_leave_generation_average_unknown():
    """RM16/RM25: missing observations cannot count as zero generations."""
    from relaymind.evaluation.placement_metrics import summarize_placement, trace_metrics

    known = {
        "state": "completed",
        "success": True,
        "metrics": trace_metrics(
            [
                event("model.started", "local", call_id="a", model="local", purpose="final"),
                event("run.completed"),
            ]
        ),
    }
    missing = {"state": "completed", "success": False}
    empty = {"state": "completed", "metrics": trace_metrics([])}
    for rows, measured, absent in [([missing], 0, 1), ([known, missing, empty], 1, 2)]:
        value = summarize_placement(rows)["generations"]
        assert value["per_completed_run"] is None
        assert value["completed_runs"] == measured + absent
        assert value["measured_completed_runs"] == measured
        assert value["unobserved_completed_runs"] == absent
        assert value["per_measured_completed_run"] == (1 if measured else None)
        assert value["by_actor_purpose"] == ({"local/final": 1} if measured else {})
    zero = {"state": "completed", "metrics": trace_metrics([event("run.completed")])}
    assert summarize_placement([zero])["generations"]["per_completed_run"] == 0
