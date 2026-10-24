"""Measured timing storage and placement inputs (RM17, RM25, RM27; #129)."""

import asyncio
import dataclasses
import sqlite3
from datetime import timedelta

import pytest

from gateway_helpers import (
    LOCAL_MODEL_ID,
    STAGING_OK,
    build_gateway,
    route_turn,
    run,
    text_turn,
    tool_turn,
)
from helpers import ACCEPTED_PAYLOAD, TERMINAL_EVIDENCE, FakeClock, ScriptedAdapter
from native_helpers import gated_gateway
from relaymind.adapters import Completed, GenerationFailed, Message, Role, TextDelta, Usage
from relaymind.orchestrator.client_loop import ToolOffer, run_client_request
from relaymind.orchestrator.local_loop import ConfiguredModel
from relaymind.orchestrator.warmup import warm_up
from relaymind.store import MIGRATIONS, ModelTiming, RunStore
from relaymind.store.run_store import split_sql_statements
from relaymind.store.trace_event import EventType


def sample(clock, **changes):
    return dataclasses.replace(
        ModelTiming(
            model="local",
            purpose="skill",
            kind="run",
            input_tokens=100,
            cached_tokens=0,
            first_token_ms=20,
            duration_ms=100,
            output_tokens=40,
            succeeded=True,
            run_id=None,
            recorded_at=clock.utc_now(),
        ),
        **changes,
    )


def test_upgrade_previous_schema_preserves_runs_and_adds_timings(tmp_path):
    """RM17: append a migration; never change a previously applied script."""
    path = tmp_path / "old.sqlite"
    with sqlite3.connect(path) as connection:
        for migration in MIGRATIONS[:-1]:
            for statement in split_sql_statements(migration):
                connection.execute(statement)
        connection.execute(f"PRAGMA user_version={len(MIGRATIONS) - 1}")
        connection.execute(
            "INSERT INTO runs (run_id,state,version,created_at,updated_at)"
            " VALUES ('old','accepted',1,'2026-10-24T00:00:00Z','2026-10-24T00:00:00Z')"
        )
    with RunStore(path) as store:
        assert store.schema_version == len(MIGRATIONS)
        assert store.get_run("old").state == "accepted"
        assert store.model_timings() == []
        store.add_model_timing(sample(store.clock))
        assert len(store.model_timings()) == 1


def test_roundtrip_filters_and_run_deletion(tmp_path):
    """RM17/RM27: run-owned samples leave with a deleted run; warmups do not."""
    clock = FakeClock()
    path = tmp_path / "timings.sqlite"
    with RunStore(path, clock=clock) as store:
        run_id = store.create_run("accepted").run_id
        values = [sample(clock, run_id=run_id), sample(clock, kind="warm_up", model="other")]
        for value in values:
            store.add_model_timing(value)
        assert store.model_timings(model="local", purpose="skill") == values[:1]
    with RunStore(path, clock=clock) as store:
        assert store.model_timings() == values
        store.delete_run(run_id)
        assert store.model_timings() == values[1:]


@pytest.mark.parametrize(
    "changes",
    [
        {"model": " "},
        {"purpose": ""},
        {"kind": "seed"},
        {"input_tokens": -1},
        {"cached_tokens": True},
        {"cached_tokens": 101},
        {"duration_ms": -1},
        {"duration_ms": float("inf")},
        {"first_token_ms": 101},
        {"output_tokens": -1},
        {"succeeded": 1},
        {"recorded_at": FakeClock().now.replace(tzinfo=None)},
        {"run_id": ""},
        {"run_id": "missing", "kind": "warm_up"},
    ],
)
def test_invalid_samples_are_rejected_without_writing(changes):
    """RM25/RM27: invalid numeric data cannot poison predictions."""
    clock = FakeClock()
    with RunStore(clock=clock) as store:
        with pytest.raises(ValueError):
            store.add_model_timing(sample(clock, **changes))
        assert store.model_timings() == []


def test_retention_includes_unowned_samples_and_reads_hide_expired():
    """RM17/RM27: default seven-day retention also covers warm-ups/probes."""
    clock = FakeClock()
    with RunStore(clock=clock) as store:
        old = sample(clock, kind="probe")
        store.add_model_timing(old)
        clock.advance(7 * 86400)
        assert store.model_timings() == []
        store.add_model_timing(sample(clock))
        store.purge_expired()
        assert len(store.model_timings()) == 1
        assert store._conn.execute("SELECT COUNT(*) FROM model_timings").fetchone()[0] == 1


def test_bound_and_deterministic_timestamp_insertion_order():
    """RM27: keep latest 1,000 per model/purpose, including timestamp ties."""
    clock = FakeClock()
    with RunStore(clock=clock) as store:
        for value in range(1003):
            store.add_model_timing(sample(clock, output_tokens=value))
        store.add_model_timing(sample(clock, purpose="routing"))
        values = store.model_timings(model="local", purpose="skill")
        assert len(values) == 1000
        assert [s.output_tokens for s in values] == list(range(3, 1003))
        # A backdated insertion cannot evict a newer sample.
        store.add_model_timing(sample(clock, recorded_at=clock.now - timedelta(seconds=1)))
        assert store.model_timings(model="local", purpose="skill") == values
        assert len(store.model_timings()) == 1001


def test_success_estimates_keep_warm_cold_and_unknown_separate():
    """RM25/RM27: medians of measured rates only; failed calls stay as evidence."""
    clock = FakeClock()
    with RunStore(clock=clock) as store:
        for changes in [
            {},
            {"input_tokens": 200, "first_token_ms": 40, "duration_ms": 200, "output_tokens": 160},
            {"cached_tokens": 80},
            {
                "input_tokens": None,
                "cached_tokens": None,
                "output_tokens": None,
                "first_token_ms": None,
            },
            {"duration_ms": 10000, "succeeded": False},
        ]:
            store.add_model_timing(sample(clock, **changes))
        estimate = store.timing_estimate("local", "skill")
        assert estimate.sample_count == 4
        assert estimate.median_duration_ms == 100
        assert estimate.median_output_tokens == 40
        assert estimate.cold.sample_count == 2
        assert estimate.cold.prefill_tokens_per_ms == 5
        assert estimate.cold.decode_tokens_per_ms == 0.75
        assert estimate.warm.sample_count == 1
        assert estimate.warm.prefill_tokens_per_ms == 5
        assert estimate.warm.decode_tokens_per_ms == 0.5
        assert estimate.latest_cache_class == "unknown"
        assert len(store.model_timings()) == 5


def test_unavailable_zero_and_missing_measurements_never_become_zero_rates():
    """RM16/RM27: missing/zero timing intervals cannot establish throughput."""
    clock = FakeClock()
    with RunStore(clock=clock) as store:
        empty = store.timing_estimate("local", "skill")
        assert empty.sample_count == 0
        assert empty.recorded_at is None
        assert empty.median_duration_ms is None
        assert empty.cold.prefill_tokens_per_ms is None
        store.add_model_timing(sample(clock, duration_ms=0, first_token_ms=0))
        estimate = store.timing_estimate("local", "skill")
        assert estimate.median_duration_ms == 0
        assert estimate.cold.prefill_tokens_per_ms is None
        assert estimate.cold.decode_tokens_per_ms is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", True),
        ("schema_version", 2),
        ("extra", 0),
        ("actor", "gateway"),
        ("recorded_at", "yesterday"),
    ],
)
def test_sample_json_boundary_rejects_invalid_seed_data(field, value):
    """RM27/section 8: seeded samples use the same closed versioned boundary."""
    data = sample(FakeClock()).to_json_object()
    assert ModelTiming.from_json_object(data) == sample(FakeClock())
    data[field] = value
    with pytest.raises(ValueError):
        ModelTiming.from_json_object(data)


def test_model_identity_does_not_mix_local_and_external_measurements():
    """RM27: the same model name at two endpoints retains its measured actor."""
    clock = FakeClock()
    with RunStore(clock=clock) as store:
        store.add_model_timing(sample(clock))
        store.add_model_timing(sample(clock, actor="external", duration_ms=1000))
        assert store.timing_estimate("local", "skill").median_duration_ms == 100
        assert store.timing_estimate("local", "skill", actor="external").median_duration_ms == 1000
        assert len(store.model_timings(actor="external")) == 1


@pytest.mark.parametrize(
    "mode",
    [
        "auto",
        "external_only",
        "client",
        "client_external",
        "parallel",
        "failure",
        "external_failure",
    ],
)
def test_every_actual_generation_has_exactly_one_sample(tmp_path, mode):
    """RM27: shared boundary covers local, external, client and fixed workflow calls."""
    local = [
        route_turn("check-service-health"),
        tool_turn("get_service_health", service="staging-api"),
        text_turn(STAGING_OK),
        text_turn(STAGING_OK),
    ]
    external = [tool_turn("get_service_health", service="staging-api"), text_turn(STAGING_OK)]
    options = {}
    if mode.startswith("client"):
        local = [text_turn("Hello")]
        external = [text_turn("Hello")]
    elif mode == "failure":
        local = [[GenerationFailed(code="offline", message="offline")]]
    elif mode == "external_failure":
        external = [[GenerationFailed(code="offline", message="offline")]]
    elif mode == "parallel":
        workflow = "health-and-diagnostic-checklist"
        local = [route_turn(workflow), tool_turn("get_service_health", service="staging-api")]
        external = [
            text_turn("Check dependencies."),
            text_turn(
                "Observation: "
                + STAGING_OK
                + "\nDiagnostic checklist (draft, not log-backed):\n- Check dependencies."
            ),
        ]
        options["enabled_workflows"] = {workflow}
    gateway = build_gateway(tmp_path, local, external_turns=external, **options)
    if mode.startswith("client"):
        outcome = asyncio.run(
            run_client_request(
                gateway.orchestrator,
                (Message(role=Role.USER, content="Hello"),),
                ToolOffer(),
                execution_mode="external_only" if mode == "client_external" else "auto",
            )
        )
    else:
        outcome, _ = run(
            gateway,
            execution_mode={
                "external_only": "external_only",
                "external_failure": "external_only",
                "parallel": "parallel_demo",
            }.get(mode),
        )
    finished = [
        e for e in gateway.store.events(outcome.run_id) if e.event_type.value == "model.finished"
    ]
    samples = gateway.store.model_timings()
    assert len(samples) == len(finished) > 0
    for value, event in zip(samples, finished, strict=True):
        assert value.model == event.payload["model"]
        assert value.actor == event.actor
        assert value.purpose == event.payload["purpose"]
        assert value.run_id == outcome.run_id
        assert value.duration_ms == event.payload["duration_ms"]
        assert value.succeeded == (event.payload["error"] is None)
        usage = event.payload["usage"]
        assert value.input_tokens == (None if usage is None else usage["input_tokens"])


def test_warmup_records_success_and_failure_without_creating_runs(tmp_path):
    """RM27: a failed startup call is evidence, with unknown usage left null."""
    gateway = build_gateway(
        tmp_path,
        [[Completed(finish_reason="stop")], [GenerationFailed(code="offline", message="offline")]],
    )
    result = asyncio.run(warm_up(gateway.orchestrator, gateway.adapter, seconds=30))
    assert not result.ready
    values = gateway.store.model_timings()
    assert [s.succeeded for s in values] == [True, False]
    assert all(s.kind == "warm_up" and s.run_id is None for s in values)
    assert all(s.input_tokens is None and s.cached_tokens is None for s in values)
    assert gateway.store._conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_terminal_outcomes_are_recorded_once_and_external_only_is_not_a_local_loss(tmp_path):
    """RM17/RM27: expected-wait inputs are materialized, terminal and attempt based."""
    gateway = build_gateway(
        tmp_path,
        [
            route_turn("check-service-health"),
            tool_turn("get_service_health", service="staging-api"),
            text_turn(STAGING_OK),
            text_turn(STAGING_OK),
        ],
        external_turns=[],
    )
    outcome, _ = run(gateway)
    estimate = gateway.store.outcome_estimate(LOCAL_MODEL_ID, "check-service-health")
    assert (estimate.local_completed, estimate.local_escalated, estimate.local_success_rate) == (
        1,
        0,
        1,
    )
    # Estimate reads never inspect the event history.
    queries = []
    gateway.store._conn.set_trace_callback(queries.append)
    assert gateway.store.outcome_estimate(LOCAL_MODEL_ID, "check-service-health") == estimate
    assert not any("FROM events" in query for query in queries)
    gateway.store.delete_run(outcome.run_id)
    assert (
        gateway.store.outcome_estimate(LOCAL_MODEL_ID, "check-service-health").local_completed == 0
    )


@pytest.mark.parametrize(
    "local,terminal,expected",
    [
        (True, "external", (0, 1, 1)),
        (False, "external", (0, 0, 1)),
        (True, "local", (1, 0, 0)),
        (True, "cancel", (0, 0, 0)),
        (True, "failed", (0, 0, 0)),
        (True, "needs_external", (0, 1, 0)),
        (True, "external_cancel", (0, 1, 0)),
        (True, "external_failed", (0, 1, 0)),
    ],
)
def test_materialized_outcomes_distinguish_attempts_and_terminal_states(local, terminal, expected):
    """RM17/RM27: external-only, cancellation and unrelated failures are no local loss."""
    with RunStore(clock=FakeClock()) as store:
        run_id = store.create_run("accepted").run_id
        store.append_event(
            run_id, EventType.RUN_ACCEPTED, "gateway", {**ACCEPTED_PAYLOAD, "local_model": "local"}
        )
        store.append_event(
            run_id,
            EventType.SKILL_LOADED,
            "gateway",
            {"name": "health", "version": "1", "content_hash": "hash"},
        )
        if local:
            store.append_event(
                run_id,
                EventType.MODEL_STARTED,
                "local",
                {"model": "local", "call_id": "l1", "purpose": "skill"},
            )
        if terminal.startswith("external"):
            store.append_event(
                run_id,
                EventType.HANDOFF_STARTED,
                "gateway",
                {
                    "model": "external",
                    "reason_code": "local_budget",
                    "skill": {"name": "health", "version": "1", "content_hash": "hash"},
                    "operation_ids": [],
                    "allowed_tools": [],
                    "request": {"size": 10, "limit": 100, "method": "tokenizer"},
                },
            )
            for call in ("e1", "e2"):
                store.append_event(
                    run_id,
                    EventType.MODEL_STARTED,
                    "external",
                    {"model": "external", "call_id": call, "purpose": "external"},
                )
        event, payload = {
            "external": (EventType.RUN_COMPLETED, {"final_answer_model": "external"}),
            "local": (EventType.RUN_COMPLETED, {"final_answer_model": "local"}),
            "cancel": (EventType.RUN_CANCELLED, {"reason": "cancel_requested"}),
            "failed": (EventType.RUN_FAILED, {"error_code": "internal_error"}),
            "needs_external": (EventType.RUN_FAILED, {"error_code": "needs_external"}),
            "external_cancel": (EventType.RUN_CANCELLED, {"reason": "cancel_requested"}),
            "external_failed": (EventType.RUN_FAILED, {"error_code": "external_failed"}),
        }[terminal]
        # A repeated terminal event must not count a second run.
        for _ in range(2):
            store.append_event(run_id, event, "gateway", {**TERMINAL_EVIDENCE, **payload})
        stats = store.outcome_estimate("local", "health", external_model="external")
        assert (stats.local_completed, stats.local_escalated, stats.external_completed) == expected
        assert stats.median_external_generations == (2 if terminal == "external" else None)
        assert stats.local_success_rate == (
            1
            if terminal == "local"
            else 0
            if local and (terminal.startswith("external") or terminal == "needs_external")
            else None
        )


def test_cancelled_generation_is_recorded_once(tmp_path):
    """RM14/RM27: cancellation reaches the generation's completion boundary."""

    async def exercise():
        gateway, adapter = gated_gateway(tmp_path)
        task = asyncio.create_task(gateway.orchestrator.run_text("Is staging-api healthy?"))
        await adapter.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        samples = gateway.store.model_timings()
        assert len(samples) == 1
        assert not samples[0].succeeded
        assert samples[0].input_tokens is None

    asyncio.run(exercise())


def test_partial_provider_usage_does_not_invent_input_or_output_counts():
    """RM16/RM27: a partial usage object must not poison persisted samples with zero."""
    from relaymind.adapters.openai_compat import StreamAccumulator

    usage = StreamAccumulator().feed({"usage": {"prompt_tokens_details": {"cached_tokens": 80}}})[0]
    assert usage.input_tokens is None
    assert usage.output_tokens is None


@pytest.mark.parametrize(
    "counts",
    [
        {"input_tokens": None, "output_tokens": 1},
        {"input_tokens": 5, "output_tokens": None},
        {"input_tokens": None, "output_tokens": None},
    ],
)
def test_partial_usage_completes_and_persists_one_actual_generation(tmp_path, counts):
    """RM25/RM27: nullable usage survives the shared sample/trace transaction."""
    gateway = build_gateway(
        tmp_path, [[TextDelta(text="Hello"), Usage(**counts), Completed(finish_reason="stop")]]
    )
    outcome = asyncio.run(
        run_client_request(
            gateway.orchestrator,
            (Message(role=Role.USER, content="Hello"),),
            ToolOffer(),
        )
    )
    assert outcome.state.value == "completed"
    assert outcome.answer == "Hello"
    values = gateway.store.model_timings()
    assert len(values) == 1
    assert values[0].succeeded
    finished = gateway.store.events(outcome.run_id, event_type=EventType.MODEL_FINISHED)
    assert len(finished) == 1
    for field, count in counts.items():
        assert getattr(values[0], field) == count
        assert finished[0].payload["usage"][field] == count


@pytest.mark.parametrize("field", ["input_tokens", "output_tokens"])
@pytest.mark.parametrize("interruption", [RuntimeError, asyncio.CancelledError])
def test_partial_usage_recording_preserves_original_interruption(tmp_path, field, interruption):
    """RM14/RM25/RM27: trace recording cannot replace a provider exception or cancellation."""
    counts = {"input_tokens": 5, "output_tokens": 1, field: None}
    original = interruption("original interruption")

    class InterruptedAdapter(ScriptedAdapter):
        async def generate(self, request):
            self.requests.append(request)
            yield Usage(**counts)
            raise original

    gateway = build_gateway(
        tmp_path,
        None,
        local_model=ConfiguredModel(
            adapter=InterruptedAdapter(),
            model_id="interrupted",
        ),
    )

    async def exercise():
        with pytest.raises(interruption) as caught:
            await run_client_request(
                gateway.orchestrator, (Message(role=Role.USER, content="Hello"),), ToolOffer()
            )
        assert caught.value is original

    asyncio.run(exercise())
    values = gateway.store.model_timings()
    assert len(values) == 1 and not values[0].succeeded
    assert getattr(values[0], field) is None
    finished = gateway.store.events(values[0].run_id, event_type=EventType.MODEL_FINISHED)
    assert len(finished) == 1
    assert finished[0].payload["usage"][field] is None
    assert finished[0].payload["error"]["code"] == (
        "cancelled" if interruption is asyncio.CancelledError else "interrupted"
    )
    assert gateway.store.get_run(values[0].run_id).state == (
        "cancelled" if interruption is asyncio.CancelledError else "failed"
    )


def terminal_skill_run(
    store,
    *,
    local_model="local",
    external_model=None,
    calls=0,
    skill="health",
    purpose="skill",
    mode="auto",
    error=None,
):
    run_id = store.create_run("accepted").run_id
    drafts = [
        (
            EventType.RUN_ACCEPTED,
            "gateway",
            {**ACCEPTED_PAYLOAD, "local_model": local_model, "effective_execution_mode": mode},
        )
    ]
    if skill is not None:
        drafts.append(
            (
                EventType.SKILL_LOADED,
                "gateway",
                {"name": skill, "version": "1", "content_hash": "hash"},
            )
        )
    if purpose is not None:
        drafts.append(
            (
                EventType.MODEL_STARTED,
                "local",
                {"model": local_model, "purpose": purpose, "call_id": "l1"},
            )
        )
    if external_model is not None:
        drafts.append(
            (
                EventType.HANDOFF_STARTED,
                "gateway",
                {
                    "model": external_model,
                    "reason_code": "local_budget",
                    "skill": None,
                    "operation_ids": [],
                    "allowed_tools": [],
                    "request": {"size": 1, "limit": 10, "method": "tokenizer"},
                },
            )
        )
        drafts.extend(
            (
                EventType.MODEL_STARTED,
                "external",
                {"model": external_model, "purpose": "external", "call_id": f"e{index}"},
            )
            for index in range(calls)
        )
    drafts.append(
        (
            EventType.RUN_FAILED if error else EventType.RUN_COMPLETED,
            "gateway",
            {
                **TERMINAL_EVIDENCE,
                **(
                    {"error_code": error}
                    if error
                    else {"final_answer_model": external_model or local_model}
                ),
            },
        )
    )
    store.append_events(run_id, drafts)
    return run_id


def test_outcomes_group_external_models_and_skills_and_expire_without_event_scans():
    """RM27: provider swaps, missing skills and local model swaps never mix costs."""
    clock = FakeClock()
    with RunStore(clock=clock) as store:
        for local_model, external_model, calls in [
            ("a", "e", 1),
            ("a", "e", 5),
            ("b", "e", 3),
            ("a", "other", 10),
        ]:
            terminal_skill_run(
                store, local_model=local_model, external_model=external_model, calls=calls
            )
        terminal_skill_run(store, external_model="e", calls=8, skill=None)
        stats = store.outcome_estimate("a", "health", external_model="e")
        assert stats.local_escalated == 3
        assert stats.external_completed == 3
        assert stats.median_external_generations == 3
        assert (
            store.outcome_estimate(
                "a", "health", external_model="other"
            ).median_external_generations
            == 10
        )
        assert store.outcome_estimate("a", "health").median_external_generations is None
        clock.advance(7 * 86400)
        assert store.outcome_estimate("a", "health", external_model="e").local_success_rate is None
        assert store.outcome_estimate("a", "health", external_model="e").external_completed == 0
        store.purge_expired()
        assert store._conn.execute("SELECT COUNT(*) FROM skill_outcomes").fetchone()[0] == 0


def test_routing_only_and_local_only_failure_do_not_lower_skill_success():
    """RM27: a routed skill was not necessarily attempted locally."""
    with RunStore(clock=FakeClock()) as store:
        terminal_skill_run(store, purpose="routing", external_model="e", calls=1)
        terminal_skill_run(store, mode="local_only", error="needs_external")
        stats = store.outcome_estimate("local", "health", external_model="e")
        assert stats.local_completed == stats.local_escalated == 0
        assert stats.local_success_rate is None
        assert stats.external_completed == 1


def test_warmup_timeout_records_failed_sample_without_a_run(tmp_path):
    """RM27: a warm-up timeout is kept even when no generation completed."""
    gateway, adapter = gated_gateway(tmp_path)
    result = asyncio.run(warm_up(gateway.orchestrator, adapter, seconds=0.01))
    assert result.timed_out
    values = gateway.store.model_timings()
    assert len(values) == 1
    assert not values[0].succeeded and values[0].kind == "warm_up"
    assert values[0].run_id is None
