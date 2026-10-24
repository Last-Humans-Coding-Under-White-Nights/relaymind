"""AC21 / RM27: predictions are measured, purpose-specific and never budgets."""

import dataclasses

import pytest

from helpers import FakeClock
from relaymind.adapters import GenerationFailed
from relaymind.orchestrator.placement import Placement, PlacementSettings
from relaymind.store import ModelTiming, RunStore


def seed(store, **changes):
    value = ModelTiming(
        model="local",
        purpose="routing",
        kind="run",
        input_tokens=100,
        cached_tokens=0,
        first_token_ms=20,
        duration_ms=100,
        output_tokens=40,
        succeeded=True,
        run_id=None,
        recorded_at=store.clock.utc_now(),
    )
    store.add_model_timing(dataclasses.replace(value, **changes))


def test_prediction_uses_run_output_but_latest_prefix_throughput(tmp_path):
    store = RunStore(tmp_path / "samples", clock=FakeClock())
    seed(store)
    seed(store, kind="probe", cached_tokens=50, output_tokens=10, first_token_ms=10, duration_ms=30)
    predicted = Placement(store).predict("local", "routing", 200, output_cap=128)
    assert predicted.predicted_ms == 100  # 200/10 + 40/.5
    assert predicted.inputs["output_source"] == "run_samples"
    assert predicted.inputs["cache_class"] == "warm"
    assert store.timing_estimate("local", "routing").median_output_tokens == 25


def test_unknown_latest_prefix_does_not_fall_back(tmp_path):
    store = RunStore(tmp_path / "samples", clock=FakeClock())
    seed(store)
    seed(store, cached_tokens=None)
    p = Placement(store).predict("local", "routing", 100, output_cap=128)
    assert p.predicted_ms is None
    assert p.inputs["sample_count"] == 0
    assert p.inputs["prefill_tokens_per_ms"] is None


def test_no_production_output_uses_actual_cap_only(tmp_path):
    store = RunStore(tmp_path / "samples", clock=FakeClock())
    seed(store, kind="warm_up")
    estimator = Placement(store)
    assert estimator.predict("local", "routing", 100).predicted_ms is None
    p = estimator.predict("local", "routing", 100, output_cap=10)
    assert p.predicted_ms == 40
    assert p.inputs["output_source"] == "purpose_cap"
    assert estimator.predict("local", "routing", None, output_cap=10).predicted_ms is None


def test_measured_zero_output_and_explicit_cap(tmp_path):
    store = RunStore(tmp_path / "samples", clock=FakeClock())
    seed(store, kind="warm_up")
    seed(store, output_tokens=0)
    p = Placement(store).predict("local", "routing", 100)
    assert p.predicted_ms == 20
    assert p.inputs["expected_output_tokens"] == 0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"margin": True},
        {"margin": 0},
        {"margin": float("nan")},
        {"probe_after_seconds": False},
        {"probe_after_seconds": float("inf")},
        {"policy": "fast"},
    ],
)
def test_settings_reject_invalid_values(kwargs):
    with pytest.raises(ValueError):
        PlacementSettings(**kwargs)


def test_hard_cap_declines_router_without_charging_generation(tmp_path):
    """AC21/RM28: the chain records not_placed, with no fabricated model call."""
    from gateway_helpers import LOCAL_MODEL_ID, build_gateway, run
    from helpers import of_type

    gateway = build_gateway(
        tmp_path,
        [],
        external_turns=[[GenerationFailed(code="unavailable", message="test")]],
        placement=PlacementSettings(policy="device_aware"),
    )
    seed(gateway.store, model=LOCAL_MODEL_ID, duration_ms=10000)
    outcome, events = run(gateway)
    route = of_type(events, "route.selected")[0].payload
    assert route["decision"]["reason_code"] == "local_slow"
    assert route["selector"]["attempts"][0]["outcome"] == "not_placed"
    assert route["placement"]["reason"] == "local_slow"
    assert gateway.adapter.requests == []
    assert not [e for e in of_type(events, "model.started") if e.actor == "local"]
    accepted = of_type(events, "run.accepted")[0].payload
    assert accepted["placement_policy"] == "device_aware"
    assert accepted["budgets"]["placement.margin"] == 2


@pytest.mark.parametrize(
    "policy,mode,external,reason",
    [
        ("local_first", "auto", True, "local_first"),
        ("device_aware", "local_only", True, "local_only"),
        ("device_aware", "auto", False, "no_external_path"),
    ],
)
def test_exempt_paths_still_generate_and_label(tmp_path, policy, mode, external, reason):
    from gateway_helpers import LOCAL_MODEL_ID, build_gateway, route_turn, run
    from helpers import of_type

    gateway = build_gateway(
        tmp_path,
        [
            route_turn("check-service-health"),
            [GenerationFailed(code="unavailable", message="test")],
        ],
        external_turns=[[GenerationFailed(code="unavailable", message="test")]]
        if external
        else None,
        placement=PlacementSettings(policy=policy),
    )
    seed(gateway.store, model=LOCAL_MODEL_ID, duration_ms=10000)
    # The skill's unscripted call fails but the routing generation must start.
    _, events = run(gateway, execution_mode=mode)
    p = of_type(events, "model.started")[0].payload["placement"]
    assert p["reason"] == reason
    assert p["predicted_ms"] > 4000
    assert p["deadline_ms"] is None


def test_readiness_is_representative_and_never_tokenizes(tmp_path):
    import asyncio

    from gateway_helpers import LOCAL_MODEL_ID, build_gateway
    from relaymind.api.native import readiness
    from relaymind.orchestrator.route_decision import ExecutionMode

    gateway = build_gateway(
        tmp_path,
        [],
        external_turns=[[GenerationFailed(code="unavailable", message="test")]],
        placement=PlacementSettings(policy="device_aware"),
    )
    seed(gateway.store, model=LOCAL_MODEL_ID, duration_ms=10000)
    _, result = asyncio.run(readiness(gateway.orchestrator, [ExecutionMode.AUTO]))
    assert result["placement"]["representative"] is True
    assert result["placement"]["routing_fits"] is False
    assert result["placement"]["routing"]["inputs"]["input_source"] == "routing_sample"
    assert result["placement"]["auto_model"] == gateway.orchestrator.external.model_id
    assert gateway.adapter.requests == []
    assert gateway.adapter.counted == []


@pytest.mark.parametrize(
    "change",
    [
        {"predicted_ms": 0},
        {"predicted_ms": True},
        {"deadline_ms": -1},
        {"unexpected": 1},
        {"inputs": {}},
        {"role": "fast"},
    ],
)
def test_closed_placement_trace_contract_rejects_bad_fields(change):
    """RM22/AC21: malformed optional fields fail both published/runtime boundaries."""
    from relaymind.orchestrator.placement import marker
    from relaymind.store.trace_event import TraceEventError, validate_trace_event

    value = marker(True).to_json_object() | change
    with pytest.raises(TraceEventError):
        validate_trace_event(
            {
                "schema_version": 1,
                "run_id": "r",
                "seq": 2,
                "timestamp": "2026-10-24T00:00:00Z",
                "elapsed_ms": 0,
                "event_type": "model.started",
                "actor": "external",
                "payload": {"call_id": "c", "model": "m", "placement": value},
            }
        )


def test_deadline_miss_keeps_prediction_and_failed_sample_discards_route(tmp_path):
    """AC21/RM04: a late iterator cannot publish its cancelled routing result."""
    from gateway_helpers import LOCAL_MODEL_ID, build_gateway, route_turn, run
    from helpers import of_type

    clock = FakeClock()
    gateway = build_gateway(
        tmp_path,
        [route_turn("check-service-health")],
        clock=clock,
        input_tokens=100,
        on_generate=lambda _: clock.advance(0.3),
        external_turns=[[GenerationFailed(code="unavailable", message="test")]],
        placement=PlacementSettings(policy="device_aware"),
    )
    seed(gateway.store, model=LOCAL_MODEL_ID)
    _, events = run(gateway)
    finished = [e for e in of_type(events, "model.finished") if e.actor == "local"][0].payload
    assert finished["predicted_ms"] == 100
    assert finished["deadline_ms"] == 200
    assert finished["deadline_missed"] is True
    assert not of_type(events, "route.selected")
    assert of_type(events, "handoff.started")[0].payload["reason_code"] == "local_slow"
    assert not gateway.store.model_timings(model=LOCAL_MODEL_ID)[-1].succeeded
    assert len(gateway.adapter.cancelled) == 1


def test_client_call_decline_preserves_client_contract(tmp_path):
    """AC21/RM19: client calls have no managed skill or routing call."""
    import asyncio

    from gateway_helpers import LOCAL_MODEL_ID, build_gateway, text_turn
    from helpers import of_type
    from relaymind.adapters import GenerationOptions, Message, Role
    from relaymind.orchestrator.client_loop import ToolOffer, run_client_request

    gateway = build_gateway(
        tmp_path,
        [],
        external_turns=[text_turn("external answer")],
        placement=PlacementSettings(policy="device_aware"),
    )
    seed(gateway.store, model=LOCAL_MODEL_ID, purpose="skill", duration_ms=10000)
    result = asyncio.run(
        run_client_request(
            gateway.orchestrator,
            [Message(role=Role.USER, content="hello")],
            ToolOffer(()),
            final_options=GenerationOptions(max_output_tokens=100),
        )
    )
    events = gateway.store.events(result.run_id)
    assert result.answer == "external answer"
    assert gateway.adapter.requests == []
    handoff = of_type(events, "handoff.started")[0].payload
    assert handoff["placement"]["reason"] == "local_slow"
    assert handoff["placement"]["inputs"]["local_success_rate"] is None
    assert not of_type(events, "skill.loaded")


def test_placement_launch_values_and_defaults(tmp_path):
    """RM03/AC21: configure policy explicitly; exact defaults remain unchanged."""
    from launcher_helpers import write_config
    from relaymind.launcher import load_config

    config = load_config(
        write_config(
            tmp_path, '\n[placement]\npolicy="expected_wait"\nmargin=3\nprobe_after_seconds=80\n'
        )
    )
    assert config.placement.policy == "expected_wait"
    assert config.placement.margin == 3
    assert config.placement.probe_after_seconds == 80


def test_expected_wait_uses_observed_skill_and_configured_external_run_cost(tmp_path):
    """AC21: hard cap precedes expected-wait; probes never become external cost."""
    from test_model_timings import terminal_skill_run

    store = RunStore(tmp_path / "store", clock=FakeClock())
    seed(store)
    terminal_skill_run(store)  # one local success
    terminal_skill_run(store, external_model="external", calls=2)  # one local escalation
    seed(store, model="external", actor="external", purpose="external", duration_ms=1000)
    seed(store, model="other", actor="external", duration_ms=1, first_token_ms=0)
    seed(store, model="external", actor="external", kind="probe", duration_ms=1, first_token_ms=0)
    placement = Placement(store, PlacementSettings(policy="expected_wait"))
    p = placement.predict("local", "routing", 100)
    admitted = placement.admit(
        p,
        mode="auto",
        external_model="external",
        stage_seconds=4,
        run_seconds=60,
        skill_id="health",
    )
    assert admitted.reason == "within_budget"
    assert admitted.inputs["local_success_rate"] == 0.5
    assert admitted.inputs["expected_external_ms"] == 2000
    declined = placement.admit(
        dataclasses.replace(p, predicted_ms=1000),
        mode="auto",
        external_model="external",
        stage_seconds=4,
        run_seconds=60,
        skill_id="health",
    )
    assert declined.reason == "expected_wait"  # strict inequality
    hard = placement.admit(
        dataclasses.replace(p, predicted_ms=5000),
        mode="auto",
        external_model="external",
        stage_seconds=4,
        run_seconds=60,
        skill_id="health",
    )
    assert hard.reason == "local_slow" and hard.inputs["local_success_rate"] is None
    unavailable = placement.admit(
        p, mode="auto", external_model="other", stage_seconds=4, run_seconds=60, skill_id="health"
    )
    assert unavailable.reason == "within_budget"
    assert unavailable.inputs["expected_external_ms"] is None


def test_not_placed_cannot_be_forged_or_swallowed_by_selector():
    """RM28: only the callback's placement signal permits not_placed fallthrough."""
    from relaymind.adapters import GenerationOptions
    from relaymind.orchestrator.placement import PlacementDeclined, marker
    from relaymind.selection.contract import SelectionContext
    from test_selection_interface import ModelBased, chain_run, pick, result

    class Swallow(ModelBased):
        async def select(self, request, context):
            try:
                await context.generate((), (), GenerationOptions())
            except Exception:
                pass
            return result(self.name)

    async def decline(*args):
        raise PlacementDeclined(marker())

    chain = chain_run(
        [Swallow("swallow"), pick("next")], context=SelectionContext(generate=decline)
    )
    assert [a.outcome.value for a in chain.attempts] == ["not_placed", "selected"]

    class Forge(ModelBased):
        async def select(self, request, context):
            raise PlacementDeclined(marker())

    chain = chain_run([Forge("forge"), pick("next")], context=SelectionContext(generate=decline))
    assert chain.attempts[0].outcome.value == "error"


def test_selected_skill_decline_retains_skill_body_and_predicate(tmp_path):
    """AC21/RM13: declining a skill keeps the selection and sends its contract."""
    from gateway_helpers import LOCAL_MODEL_ID, build_gateway, health_tail, route_turn, run
    from helpers import of_type

    gateway = build_gateway(
        tmp_path,
        [route_turn("check-service-health")],
        external_turns=health_tail(),
        placement=PlacementSettings(policy="device_aware"),
    )
    seed(gateway.store, model=LOCAL_MODEL_ID, purpose="skill", duration_ms=10000)
    result, events = run(gateway)
    assert result.state.value == "completed"
    handoff = of_type(events, "handoff.started")[0].payload
    assert handoff["skill"]["name"] == "check-service-health"
    assert handoff["placement"]["inputs"]["purpose"] == "skill"
    assert handoff["reason_code"] == "local_slow"
    assert len(gateway.adapter.requests) == 1
    assert "Completion" in str(gateway.external.requests[0].messages)


@pytest.mark.parametrize("run_limit,reason", [(0.2, "local_budget"), (60, "local_slow")])
def test_deadline_ties_prefer_run_budget_then_placement(tmp_path, run_limit, reason):
    """RM03/AC21: the entire-run limit wins ties; placement wins the stage tie."""
    from gateway_helpers import LOCAL_MODEL_ID, build_gateway, route_turn, run
    from helpers import of_type
    from relaymind.orchestrator.budgets import LocalBudgets

    clock = FakeClock()
    gateway = build_gateway(
        tmp_path,
        [route_turn("check-service-health")],
        clock=clock,
        input_tokens=100,
        on_generate=lambda _: clock.advance(0.21),
        budgets=LocalBudgets(active_seconds=0.2, run_active_seconds=run_limit),
        external_turns=[[GenerationFailed(code="unavailable", message="test")]],
        placement=PlacementSettings(policy="device_aware"),
    )
    seed(gateway.store, model=LOCAL_MODEL_ID)
    _, events = run(gateway)
    finished = of_type(events, "model.finished")[0].payload
    assert finished["deadline_missed"] is True
    transition = of_type(events, "handoff.started") or of_type(events, "run.failed")
    assert transition[0].payload["reason_code"] == reason
    if run_limit == 0.2:
        assert gateway.external.requests == []


def test_actual_tokenizer_request_is_reused_only_when_identical(tmp_path):
    """AC21: routing reuses its exact measurement; every skill request is measured."""
    from gateway_helpers import build_gateway, run
    from helpers import of_type
    from test_local_loop import HEALTH_RUN

    gateway = build_gateway(tmp_path, HEALTH_RUN)
    _, events = run(gateway)
    assert len(gateway.adapter.counted) == len(gateway.adapter.requests)
    for (messages, tools), request in zip(
        gateway.adapter.counted, gateway.adapter.requests, strict=True
    ):
        assert (messages, tools) == (request.messages, request.tool_schemas)
    for event in of_type(events, "model.started"):
        assert event.payload["placement"]["inputs"]["input_source"] == "tokenizer"


def test_only_placement_tokenizer_failure_does_not_fail_the_generation(tmp_path):
    """RM27: optional measurement failures remain null, with no capability failure."""
    from gateway_helpers import build_gateway, run
    from helpers import of_type
    from test_local_loop import HEALTH_RUN

    gateway = build_gateway(tmp_path, HEALTH_RUN)
    original = gateway.adapter.count_tokens
    calls = 0

    async def count(messages, schemas=()):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise RuntimeError("tokenizer unavailable")
        return await original(messages, schemas)

    gateway.adapter.count_tokens = count
    result, events = run(gateway)
    assert result.state.value == "completed"
    skill = of_type(events, "model.started")[1].payload["placement"]
    assert skill["predicted_ms"] is None and skill["inputs"]["input_tokens"] is None


def test_cancellation_resistant_local_timeout_is_not_admitted(tmp_path):
    """AC21/RM14: late completed output after a real timeout remains unsuccessful."""
    import asyncio

    from gateway_helpers import LOCAL_MODEL_ID, build_gateway
    from relaymind.adapters import Completed, Message, Role, TextDelta
    from relaymind.orchestrator.client_loop import ToolOffer, run_client_request

    gateway = build_gateway(
        tmp_path,
        [],
        external_turns=[[GenerationFailed(code="unavailable", message="test")]],
        placement=PlacementSettings(policy="device_aware", margin=0.1),
    )
    seed(gateway.store, model=LOCAL_MODEL_ID, purpose="skill")

    async def resistant(request):
        gateway.adapter.requests.append(request)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            yield TextDelta("late answer")
            yield Completed("stop")

    gateway.adapter.generate = resistant
    result = asyncio.run(
        run_client_request(
            gateway.orchestrator, [Message(role=Role.USER, content="hello")], ToolOffer(())
        )
    )
    assert result.answer is None
    assert gateway.store.model_timings(model=LOCAL_MODEL_ID)[-1].succeeded is False
    assert len(gateway.adapter.cancelled) == 1


@pytest.mark.parametrize(
    "actor,event,change",
    [
        ("external", "model.started", {"reason": "local_first"}),
        ("external", "model.started", {"role": "none"}),
        ("external", "model.started", {"inputs": {"input_source": "routing_sample"}}),
    ],
)
def test_runtime_and_published_schema_enforce_placement_location(actor, event, change):
    """RM22: a trace's external marker cannot masquerade as local/readiness evidence."""
    import json
    from pathlib import Path

    from jsonschema import Draft202012Validator

    from relaymind.orchestrator.placement import marker
    from relaymind.store.trace_event import TraceEventError, validate_trace_event

    placement = marker(True).to_json_object()
    if "inputs" in change:
        placement["inputs"].update(change["inputs"])
    else:
        placement.update(change)
    obj = {
        "schema_version": 1,
        "run_id": "r",
        "seq": 2,
        "timestamp": "2026-10-24T00:00:00Z",
        "elapsed_ms": 0,
        "event_type": event,
        "actor": actor,
        "payload": {"call_id": "c", "model": "m", "placement": placement},
    }
    with pytest.raises(TraceEventError):
        validate_trace_event(obj)
    schema = json.loads(Path("src/relaymind/store/schemas/trace_event.v1.json").read_text())
    assert list(Draft202012Validator(schema).iter_errors(obj))


def test_fixed_parallel_local_branch_uses_same_admission(tmp_path):
    """AC21/RM15: the fixed workflow does not bypass per-generation placement."""
    from gateway_helpers import LOCAL_MODEL_ID
    from helpers import of_type
    from test_parallel_workflow import parallel_gateway, run_parallel

    gateway = parallel_gateway(tmp_path, placement=PlacementSettings(policy="device_aware"))
    seed(gateway.store, model=LOCAL_MODEL_ID, purpose="skill", duration_ms=10000)
    result, events = run_parallel(gateway)
    assert result.reason_code == "local_slow"
    assert len(gateway.adapter.requests) == 1  # routing only
    assert of_type(events, "run.failed")[0].payload["placement"]["inputs"]["purpose"] == "skill"
    assert not of_type(events, "tool.started")


def test_native_cancel_beats_expired_placement_deadline(tmp_path):
    """RM17/AC21: cancellation is never relabeled as a placement escalation."""
    import asyncio

    from gateway_helpers import LOCAL_MODEL_ID, build_gateway, route_turn
    from helpers import of_type
    from relaymind.adapters import Message, Role

    gateway = build_gateway(
        tmp_path,
        [],
        input_tokens=100,
        external_turns=[],
        placement=PlacementSettings(policy="device_aware"),
    )
    seed(gateway.store, model=LOCAL_MODEL_ID)

    async def go():
        entered = asyncio.Event()

        async def resistant(request):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                gateway.store.clock.advance(0.3)
                for event in route_turn("check-service-health"):
                    yield event

        gateway.adapter.generate = resistant
        started = gateway.orchestrator.start([Message(role=Role.USER, content="hi")])
        await entered.wait()
        version = gateway.store.get_run(started.run_id).version
        await gateway.orchestrator.cancel(started.run_id, version)
        return gateway.store.events(started.run_id)

    events = asyncio.run(go())
    assert of_type(events, "run.cancelled")[0].payload["reason"] == "cancel_requested"
    assert not of_type(events, "handoff.started")
    assert not of_type(events, "model.finished")[0].payload["deadline_missed"]
    assert len(gateway.adapter.cancelled) == 1


def test_prediction_is_immutable_and_overflow_is_unavailable(tmp_path):
    """RM27: prediction evidence is immutable and nonfinite arithmetic never invents time."""
    store = RunStore(tmp_path / "store", clock=FakeClock())
    seed(store)
    placement = Placement(store)
    p = placement.predict("local", "routing", 100)
    with pytest.raises(TypeError):
        p.inputs["input_tokens"] = 999
    assert placement.predict("local", "routing", 10**1000).predicted_ms is None
