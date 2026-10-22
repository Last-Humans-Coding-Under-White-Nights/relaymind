"""Large trace results retain redacted evidence by reference (RM12, RM22)."""

import dataclasses
import json

import pytest

from gateway_helpers import build_gateway, health_tail, route_turn, run
from helpers import ACCEPTED_PAYLOAD, of_type
from relaymind.skills.predicates import COMPLETION_PREDICATES
from relaymind.store import RunStore
from relaymind.store.trace_event import EventType, TraceEventError
from test_completion_predicates import COUNTS, DEPLOYED, UNAVAILABLE


def test_large_tool_result_is_referenced_and_survives_reopen(tmp_path):
    """RM22: removing artifact persistence breaks retrieval after a store restart."""
    gateway = build_gateway(tmp_path, [route_turn("check-service-health"), *health_tail()])
    # Force a normal sandbox result over the configured inline threshold.
    gateway.store.max_inline_result_bytes = 1
    outcome, events = run(gateway)
    (finished,) = of_type(events, "tool.finished")
    assert "result" not in finished.payload
    ref = finished.payload["result_ref"]
    assert ref["size_bytes"] > 1
    assert ref["truncated"] is False
    assert outcome.state == "completed"  # reference storage does not truncate model evidence
    operations = gateway.store.tool_operations(outcome.run_id)
    gateway.store.close()
    with RunStore(tmp_path / "runs.sqlite3") as reopened:
        result = reopened.read_artifact(ref["artifact_id"])
        assert result == operations[0]["result"]
        assert result["data"]["status"] == "healthy"
        assert result["operation_id"] == finished.payload["operation_id"]
        assert ref["size_bytes"] == len(
            json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        )
        assert reopened.artifact_refs(outcome.run_id)[0]["artifact_id"] == ref["artifact_id"]
        reopened.delete_run(outcome.run_id)
        assert reopened.read_artifact(ref["artifact_id"]) is None


def test_artifact_and_event_roll_back_together(tmp_path):
    """RM22: a bad event later in a batch leaves no artifact or partial trace."""
    with RunStore(tmp_path / "runs.db", max_inline_result_bytes=10) as store:
        run_id = store.create_run(
            "accepted", accepted=(EventType.RUN_ACCEPTED, "gateway", ACCEPTED_PAYLOAD)
        ).run_id
        result = {"data": "证据" * 100, "truncated": True}
        draft = (
            EventType.TOOL_FINISHED,
            "gateway",
            {"operation_id": "op", "model": "local", "result": result},
        )
        with pytest.raises(TraceEventError):
            store.append_events(run_id, [draft, (EventType.MODEL_STARTED, "local", {})])
        assert store.artifact_refs(run_id) == []
        assert len(store.events(run_id)) == 1
        event = store.append_events(run_id, [draft])[0]
        ref = event.payload["result_ref"]
        assert ref["truncated"] is True
        assert store.read_artifact(ref["artifact_id"]) == result
        # Replaying identical evidence reuses its reference.
        replay = store.append_events(run_id, [draft])[0]
        assert replay.payload["result_ref"] == ref
        assert len(store.artifact_refs(run_id)) == 1


def test_the_store_reads_a_tool_result_inline_or_by_reference(tmp_path):
    """RM22, spec 0.30 section 9: one accessor for an inline, kept, or deleted result."""
    with RunStore(tmp_path / "runs.db", max_inline_result_bytes=40) as store:
        run_id = store.create_run(
            "accepted", accepted=(EventType.RUN_ACCEPTED, "gateway", ACCEPTED_PAYLOAD)
        ).run_id

        def finished(result):
            payload = {"operation_id": "op", "model": "local", "result": result}
            draft = (EventType.TOOL_FINISHED, "gateway", payload)
            return store.append_events(run_id, [draft])[0].payload

        small = {"data": "ok", "truncated": False}
        inline = store.tool_result(finished(small))
        assert (inline.result, inline.result_ref, inline.retained) == (small, None, True)
        large = {"data": "证据" * 100, "truncated": False}
        payload = finished(large)
        ref = payload["result_ref"]
        kept = store.tool_result(payload)
        assert (kept.result, kept.result_ref, kept.retained) == (large, ref, True)
        store.delete_run(run_id)
        gone = store.tool_result(payload)
        assert (gone.result, gone.result_ref, gone.retained) == (None, ref, False)


@pytest.mark.parametrize(
    "predicate,answer,result",
    [
        (
            "health_status_preserved",
            "payments-api is unavailable at 2026-10-20T02:59:00Z",
            UNAVAILABLE,
        ),
        (
            "deployment_reported",
            "staging-api deployment dep-staging-0142 at 2026-10-19T22:10:00Z",
            DEPLOYED,
        ),
        (
            "incident_counts_reported",
            "staging-api: 7 total, 1 critical, 3 warning, 3 info in 1440 minutes",
            COUNTS,
        ),
    ],
)
def test_truncated_evidence_cannot_satisfy_complete_result_predicates(predicate, answer, result):
    """RM22: truncation must invalidate grounding even if the retained fields look complete."""
    check = COMPLETION_PREDICATES[predicate]
    assert check(answer, [result])
    assert not check(answer, [dataclasses.replace(result, truncated=True)])
    # A newer truncated result must not fall back to stale complete evidence.
    assert not check(answer, [result, dataclasses.replace(result, truncated=True)])


@pytest.mark.parametrize(
    "field,value",
    [
        ("artifact_id", ""),
        ("uri", " "),
        ("size_bytes", -1),
        ("size_bytes", True),
        ("truncated", "false"),
        ("extra", 1),
    ],
)
def test_invalid_result_reference_is_rejected_before_persistence(field, value):
    """RM22: artifact size and truncation metadata must be validated on write."""
    ref = {
        "artifact_id": "art_1",
        "uri": "sqlite:artifact/art_1",
        "size_bytes": 100,
        "truncated": False,
    }
    ref[field] = value
    with RunStore() as store:
        run_id = store.create_run(
            "accepted", accepted=(EventType.RUN_ACCEPTED, "gateway", ACCEPTED_PAYLOAD)
        ).run_id
        with pytest.raises(TraceEventError):
            store.append_event(
                run_id,
                EventType.TOOL_FINISHED,
                "gateway",
                {
                    "operation_id": "op",
                    "model": "local",
                    "result_ref": ref,
                },
            )
        assert len(store.events(run_id)) == 1


def test_artifact_content_is_redacted_before_storage(tmp_path):
    """RM12/RM22: moving a result out of line must not bypass executor redaction."""
    from relaymind.redaction import Redactor

    gateway = build_gateway(
        tmp_path,
        [route_turn("check-service-health"), *health_tail()],
        redactor=Redactor(secret_fields={"checked_at"}),
    )
    gateway.store.max_inline_result_bytes = 1
    _, events = run(gateway)
    (finished,) = of_type(events, "tool.finished")
    result = gateway.store.read_artifact(finished.payload["result_ref"]["artifact_id"])
    assert result["data"]["checked_at"] == "[redacted]"
    assert "2026-10-20T02:58:00Z" not in json.dumps(result)


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
def test_inline_limit_must_be_a_positive_integer(limit):
    """RM22: an invalid storage threshold cannot silently disable the size bound."""
    with pytest.raises(ValueError, match="max_inline_result_bytes"):
        RunStore(max_inline_result_bytes=limit)


def test_inline_limit_counts_utf8_bytes_and_keeps_the_boundary_inline():
    """RM22: non-ASCII evidence uses its byte size, not its character count."""
    result = {"data": "证据", "truncated": False}
    encoded = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    for limit, is_inline in [(len(encoded), True), (len(encoded) - 1, False)]:
        with RunStore(max_inline_result_bytes=limit) as store:
            run_id = store.create_run(
                "accepted", accepted=(EventType.RUN_ACCEPTED, "gateway", ACCEPTED_PAYLOAD)
            ).run_id
            event = store.append_event(
                run_id,
                EventType.TOOL_FINISHED,
                "gateway",
                {
                    "operation_id": "op",
                    "model": "local",
                    "result": result,
                },
            )
            assert ("result" in event.payload) is is_inline
            if not is_inline:
                assert event.payload["result_ref"]["size_bytes"] == len(encoded)


def test_diagnostic_predicate_accepts_explicitly_partial_log_evidence():
    """RM06/RM22: investigate-performance permits a truncated log list as an observation."""
    from test_completion_predicates import result

    logs = dataclasses.replace(
        result("get_recent_logs", {"service": "payments-api", "entries": [], "truncated": True}),
        truncated=True,
    )
    answer = (
        "Observations:\n- get_recent_logs returned a truncated log list for payments-api.\n"
        "Insufficient evidence to name a cause."
    )
    assert COMPLETION_PREDICATES["performance_evidence_separated"](answer, [logs])
