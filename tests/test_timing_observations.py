"""Actual-call measurement rejection preserves execution (RM14/RM16/RM25/RM27)."""

import asyncio
import sqlite3

import pytest

from gateway_helpers import STAGING_OK, build_gateway, route_turn, text_turn, tool_turn
from helpers import FakeClock, ScriptedAdapter
from relaymind.adapters import Completed, FirstToken, GenerationFailed, Message, Role, Usage
from relaymind.orchestrator.budgets import LocalBudgets
from relaymind.orchestrator.client_loop import ToolOffer, run_client_request
from relaymind.orchestrator.local_loop import ConfiguredModel
from relaymind.orchestrator.local_status import LocalStatus
from relaymind.orchestrator.warmup import detect_local_model, warm_up
from relaymind.store.trace_event import EventType


@pytest.mark.parametrize("path", ["client", "managed", "warmup"])
@pytest.mark.parametrize("ending", ["success", "failure", "exception", "cancel", "timeout"])
def test_invalid_cached_observation_preserves_execution(tmp_path, caplog, path, ending):
    """RM14/RM16/RM25/RM27, spec 0.38.1: reject only cached count, never the call outcome."""
    clock = FakeClock()
    original = (
        asyncio.CancelledError("original interruption")
        if ending == "cancel"
        else RuntimeError("original interruption")
    )
    first = route_turn("check-service-health") if path == "managed" else text_turn("Hello")

    class ObservedAdapter(ScriptedAdapter):
        async def generate(self, request):
            if self.requests:
                async for event in super().generate(request):
                    yield event
                return
            self.requests.append(request)
            clock.advance(0.01)
            yield FirstToken()
            yield Usage(input_tokens=10, output_tokens=2, cached_tokens=11, reasoning_tokens=1)
            clock.advance(0.02)
            if ending in ("exception", "cancel"):
                raise original
            if ending == "timeout":
                await asyncio.Future()
            elif ending == "failure":
                yield GenerationFailed(code="offline", message="provider failure")
            else:
                for event in first:
                    if not isinstance(event, Usage):
                        yield event

    adapter = ObservedAdapter(
        turns=(
            [
                tool_turn("get_service_health", service="staging-api"),
                text_turn(STAGING_OK),
                text_turn(STAGING_OK),
            ]
            if path == "managed"
            else [[Completed(finish_reason="stop")]]
        )
    )
    gateway = build_gateway(
        tmp_path,
        None,
        clock=clock,
        local_model=ConfiguredModel(adapter, "private-provider-identifier"),
        budgets=LocalBudgets(active_seconds=0.05 if ending == "timeout" else 4),
    )

    async def exercise():
        if path == "warmup":
            action = warm_up(
                gateway.orchestrator, adapter, seconds=0.05 if ending == "timeout" else 30
            )
        elif path == "client":
            action = run_client_request(
                gateway.orchestrator, (Message(role=Role.USER, content="Hello"),), ToolOffer()
            )
        else:
            action = gateway.orchestrator.run_text("Is staging-api healthy right now?")
        if ending == "cancel" or (ending == "exception" and path != "warmup"):
            with pytest.raises(type(original)) as caught:
                await action
            assert caught.value is original
            return None
        return await action

    outcome = asyncio.run(exercise())
    samples = gateway.store.model_timings()
    expected_count = (
        4
        if path == "managed" and ending == "success"
        else 2
        if path == "warmup" and ending == "success"
        else 1
    )
    assert len(samples) == len(adapter.requests) == expected_count
    observed = samples[0]
    assert (observed.input_tokens, observed.output_tokens, observed.cached_tokens) == (10, 2, None)
    assert (observed.first_token_ms, observed.duration_ms) == (10, 30)
    assert observed.succeeded == (ending == "success")
    if path == "warmup":
        assert all(s.run_id is None and s.kind == "warm_up" for s in samples)
        if outcome is not None:
            assert outcome.ready == (ending == "success")
            assert outcome.timed_out == (ending == "timeout")
            if outcome.ready:
                assert outcome.samples[0].cached_tokens is None
                assert outcome.samples[0].input_tokens == 10
            elif ending == "exception":
                assert outcome.detail == "RuntimeError: original interruption"
            elif ending == "failure":
                assert "offline" in outcome.detail
    else:
        record = gateway.store.get_run(observed.run_id)
        assert record.state == (
            "completed" if ending == "success" else "cancelled" if ending == "cancel" else "failed"
        )
        finished = gateway.store.events(observed.run_id, event_type=EventType.MODEL_FINISHED)
        assert len(finished) == expected_count
        payload = finished[0].payload
        assert payload["usage"] == {
            "input_tokens": 10,
            "output_tokens": 2,
            "cached_tokens": None,
            "reasoning_tokens": 1,
        }
        error = payload["error"]
        assert (error["code"] if error else None) == {
            "success": None,
            "failure": "offline",
            "exception": "interrupted",
            "cancel": "cancelled",
            "timeout": "time_budget",
        }[ending]
        if ending == "timeout":
            assert len(adapter.cancelled) == 1
    warnings = [r for r in caplog.records if "invalid_cached_tokens" in r.getMessage()]
    assert len(warnings) == 1
    assert warnings[0].levelname == "WARNING"
    assert (
        warnings[0].getMessage()
        == "invalid_cached_tokens: cached_tokens exceeds input_tokens; recorded as unknown"
    )
    assert not warnings[0].args and warnings[0].exc_info is None


def test_absent_model_warmup_never_dispatches(tmp_path):
    """RM26/RM27: missing local configuration is detected before an actual call."""
    gateway = build_gateway(tmp_path, [], local_status=LocalStatus.absent(), external_turns=[])
    result = asyncio.run(warm_up(gateway.orchestrator, gateway.adapter, seconds=30))
    assert not result.ready and result.detail == "no local model is configured"
    assert gateway.adapter.requests == []
    assert gateway.store.model_timings() == []


def test_recording_database_errors_remain_visible(tmp_path, monkeypatch):
    """RM17/RM27: normalization never suppresses a failed evidence transaction."""
    gateway = build_gateway(tmp_path, [text_turn("Hello")])
    original = sqlite3.OperationalError("timing write unavailable")

    def fail_write(sample):
        raise original

    monkeypatch.setattr(gateway.store, "add_model_timing", fail_write)
    with pytest.raises(sqlite3.OperationalError) as caught:
        asyncio.run(
            run_client_request(
                gateway.orchestrator, (Message(role=Role.USER, content="Hello"),), ToolOffer()
            )
        )
    assert caught.value is original
    assert gateway.store.model_timings() == []


@pytest.mark.parametrize("path", ["client", "warmup"])
@pytest.mark.parametrize(
    "counts",
    [(None, 2, 11), (10, None, None), (10, 2, 0), (10, 2, 10)],
)
def test_valid_and_unknown_observations_are_preserved(tmp_path, caplog, path, counts):
    """RM16/RM27: normalize only an impossible known relation, never valid/missing counts."""
    usage = Usage(*counts)
    gateway = build_gateway(
        tmp_path,
        [[usage, Completed(finish_reason="stop")], [Completed(finish_reason="stop")]],
    )
    if path == "warmup":
        result = asyncio.run(warm_up(gateway.orchestrator, gateway.adapter, seconds=30))
        assert result.ready
        assert result.samples[0].cached_tokens == usage.cached_tokens
    else:
        asyncio.run(
            run_client_request(
                gateway.orchestrator, (Message(role=Role.USER, content="Hello"),), ToolOffer()
            )
        )
    sample = gateway.store.model_timings()[0]
    assert (sample.input_tokens, sample.output_tokens, sample.cached_tokens) == counts
    assert not any("invalid_cached_tokens" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("path", ["warmup", "detect"])
@pytest.mark.parametrize("failed_write", [1, 2])
@pytest.mark.parametrize("error_type", [sqlite3.OperationalError, ValueError, TimeoutError])
def test_warmup_evidence_errors_escape_without_readiness(
    tmp_path, monkeypatch, path, failed_write, error_type
):
    """RM17/RM27/spec 0.38.1: evidence errors cannot become a runtime health result."""
    gateway = build_gateway(tmp_path, [text_turn("Hello"), text_turn("Hello")])
    original = error_type("timing write unavailable")
    write_sample = gateway.store.add_model_timing
    writes = 0

    def persist_or_fail(sample):
        nonlocal writes
        writes += 1
        if writes == failed_write:
            raise original
        write_sample(sample)

    monkeypatch.setattr(gateway.store, "add_model_timing", persist_or_fail)
    action = (
        warm_up(gateway.orchestrator, gateway.adapter, seconds=30)
        if path == "warmup"
        else detect_local_model(gateway.orchestrator, gateway.adapter, warmup=True, seconds=30)
    )
    with pytest.raises(error_type) as caught:
        asyncio.run(action)
    assert caught.value is original
    assert writes == len(gateway.adapter.requests) == failed_write
    assert len(gateway.store.model_timings()) == failed_write - 1
