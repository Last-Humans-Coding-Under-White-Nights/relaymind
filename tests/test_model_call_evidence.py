"""First-token time, cached and reasoning tokens, and request options in the trace.

Covers #120: RM16 (an unreported count is null, never 0), RM18, RM22
(model.started and model.finished payloads), RM25 (the monotonic clock),
and RM27 (the figures the placement policy will read). Spec 0.31
sections 6 and 8. Deterministic: model doubles and a fake clock.
"""

import asyncio
import json

import httpx
import jsonschema

from gateway_helpers import (
    EXTERNAL_MODEL_ID,
    LOCAL_MODEL_ID,
    STAGING_OK,
    build_gateway,
    health_tail,
    route_turn,
    run,
    text_turn,
)
from helpers import (
    FakeClock,
    ScriptedAdapter,
    delta_chunk,
    make_llamacpp_adapter,
    of_type,
    sse_response,
)
from relaymind.adapters import Completed, FirstToken, TextDelta, ToolCall, Usage
from relaymind.adapters.chat_completions import ChatCompletionsAdapter, ChatCompletionsConfig
from relaymind.orchestrator.local_loop import ConfiguredModel
from relaymind.store.trace_event import TRACE_EVENT_SCHEMA


class SteppedAdapter(ScriptedAdapter):
    """A double whose turns hold numbers: the fake clock advances that many seconds."""

    def __init__(self, clock, turns, **kwargs):
        super().__init__(turns=turns, **kwargs)
        self.clock = clock

    async def generate(self, request):
        self.requests.append(request)
        for item in self._turns.pop(0):
            if isinstance(item, int | float):
                self.clock.advance(item)
            else:
                yield item


def _local(clock, turns):
    return ConfiguredModel(adapter=SteppedAdapter(clock, turns), model_id=LOCAL_MODEL_ID)


def validate(events):
    for event in events:
        jsonschema.Draft202012Validator(TRACE_EVENT_SCHEMA).validate(event.to_json_object())


def test_model_finished_records_first_token_from_the_marker_not_the_tool_call(tmp_path):
    """Spec 0.31 section 8: a tool-call generation's first token is its first fragment (RM09)."""
    clock = FakeClock()
    turns = [
        [
            0.25,
            FirstToken(),
            1.0,
            ToolCall(call_id="call_route", tool_name="check-service-health", arguments={}),
            Usage(input_tokens=800, output_tokens=12),
            Completed(finish_reason="tool_calls"),
        ]
    ]
    gateway = build_gateway(tmp_path, None, clock=clock, local_model=_local(clock, turns))
    _, events = run(gateway, "Which service is it?")
    (finished, *_) = of_type(events, "model.finished")
    assert finished.payload["first_token_ms"] == 250
    assert finished.payload["duration_ms"] == 1250


def test_a_double_without_the_marker_falls_back_to_the_first_tool_call(tmp_path):
    """Spec 0.31 section 6: an adapter that sends no marker is stamped at its first tool call."""
    clock = FakeClock()
    turns = [
        [
            0.5,
            ToolCall(call_id="call_route", tool_name="request_clarification", arguments={}),
            Completed(finish_reason="tool_calls"),
        ]
    ]
    gateway = build_gateway(tmp_path, None, clock=clock, local_model=_local(clock, turns))
    _, events = run(gateway, "Which service is it?")
    (finished, *_) = of_type(events, "model.finished")
    assert finished.payload["first_token_ms"] == 500


def test_text_fallback_ignores_empty_deltas_and_the_marker_wins(tmp_path):
    clock = FakeClock()
    turns = [
        [
            TextDelta(text=""),
            0.1,
            TextDelta(text="hello"),
            0.4,
            Completed(finish_reason="stop"),
        ]
    ]
    gateway = build_gateway(tmp_path, None, clock=clock, local_model=_local(clock, turns))
    _, events = run(gateway, "hi")
    (finished, *_) = of_type(events, "model.finished")
    assert finished.payload["first_token_ms"] == 100


def test_first_token_is_null_when_no_token_arrived(tmp_path):
    """Spec 0.31 section 8: a generation that produced nothing has no first token."""
    gateway = build_gateway(tmp_path, [[Completed(finish_reason="stop")]])
    _, events = run(gateway, "hi")
    (finished, *_) = of_type(events, "model.finished")
    assert finished.payload["first_token_ms"] is None
    validate(events)


def test_counts_the_source_did_not_report_are_null_in_the_trace(tmp_path):
    """RM16, finding F9: the trace never shows a defaulted 0 for cached or reasoning tokens."""
    gateway = build_gateway(tmp_path, [route_turn("check-service-health"), *health_tail()])
    outcome, events = run(gateway)
    assert outcome.state == "completed"
    finished = of_type(events, "model.finished")
    assert finished
    reported = [e.payload["usage"] for e in finished if e.payload["usage"] is not None]
    assert reported
    for usage in reported:
        assert usage["cached_tokens"] is None
        assert usage["reasoning_tokens"] is None
    validate(events)


def test_reported_counts_reach_model_finished_inside_usage(tmp_path):
    turn = [
        TextDelta(text="hi"),
        Usage(input_tokens=100, output_tokens=9, cached_tokens=64, reasoning_tokens=0),
        Completed(finish_reason="stop"),
    ]
    gateway = build_gateway(tmp_path, [turn])
    _, events = run(gateway, "hi")
    (finished, *_) = of_type(events, "model.finished")
    assert finished.payload["usage"] == {
        "input_tokens": 100,
        "output_tokens": 9,
        "cached_tokens": 64,
        "reasoning_tokens": 0,
    }
    validate(events)


def external_run(tmp_path, adapter):
    gateway = build_gateway(
        tmp_path,
        [],
        external_model=ConfiguredModel(adapter=adapter, model_id=EXTERNAL_MODEL_ID),
    )
    return run(gateway, "Is staging-api healthy right now?", execution_mode="external_only")


def test_model_started_records_the_declared_request_options_of_an_external_call(tmp_path):
    """Spec 0.31 section 8: model.started names the provider options sent."""
    adapter = ScriptedAdapter(turns=[text_turn(STAGING_OK)], locality="external")
    adapter.request_options = {"thinking": {"type": "disabled"}}
    _, events = external_run(tmp_path, adapter)
    (started,) = of_type(events, "model.started")
    assert started.actor == "external"
    assert started.payload["request_options"] == {"thinking": {"type": "disabled"}}
    validate(events)


def test_an_external_call_with_no_declared_options_records_no_field(tmp_path):
    """Spec 0.33 section 8: request_options is recorded only when options were declared."""
    adapter = ScriptedAdapter(turns=[text_turn(STAGING_OK)], locality="external")
    adapter.request_options = {}
    _, events = external_run(tmp_path, adapter)
    (started,) = of_type(events, "model.started")
    assert "request_options" not in started.payload
    validate(events)


def test_an_adapter_that_declares_no_options_records_none(tmp_path):
    """Local calls and doubles have request_options None: the field is left out."""
    adapter = ScriptedAdapter(turns=[text_turn(STAGING_OK)], locality="external")
    _, events = external_run(tmp_path, adapter)
    assert of_type(events, "model.started")
    assert all("request_options" not in e.payload for e in of_type(events, "model.started"))
    (tmp_path / "local").mkdir()
    gateway = build_gateway(
        tmp_path / "local", [route_turn("check-service-health"), *health_tail()]
    )
    _, events = run(gateway)
    assert all("request_options" not in e.payload for e in of_type(events, "model.started"))


def test_real_adapters_without_declared_options_record_no_field(tmp_path, monkeypatch):
    """Spec 0.33 sections 6 and 8: real adapters record request_options like the doubles.

    The external ChatCompletionsAdapter without declared options and the
    local LlamaCppAdapter both leave the field out of model.started.
    """
    monkeypatch.setenv("TEST_EXTERNAL_KEY", "sk-test-123")
    external = ChatCompletionsAdapter(
        ChatCompletionsConfig(
            base_url="https://x.example/v1",
            model=EXTERNAL_MODEL_ID,
            context_limit=8192,
            api_key_env="TEST_EXTERNAL_KEY",
        ),
        transport=httpx.MockTransport(
            lambda request: sse_response(
                [delta_chunk({"content": STAGING_OK}), delta_chunk({}, finish_reason="stop")]
            )
        ),
    )
    route = {
        "tool_calls": [
            {
                "index": 0,
                "id": "call_route",
                "type": "function",
                "function": {
                    "name": "request_clarification",
                    "arguments": json.dumps(
                        {"language": "en", "question": "Which service do you mean?"}
                    ),
                },
            }
        ]
    }

    def llama_server(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/apply-template":
            return httpx.Response(200, json={"prompt": "templated"})
        if request.url.path == "/tokenize":
            return httpx.Response(200, json={"tokens": list(range(100))})
        return sse_response([delta_chunk(route), delta_chunk({}, "tool_calls")])

    local = make_llamacpp_adapter(llama_server)
    (tmp_path / "external").mkdir()
    (tmp_path / "local").mkdir()
    gateways = [
        (
            build_gateway(
                tmp_path / "external",
                [],
                external_model=ConfiguredModel(adapter=external, model_id=EXTERNAL_MODEL_ID),
            ),
            "Is staging-api healthy right now?",
            "external_only",
        ),
        (
            build_gateway(
                tmp_path / "local",
                None,
                local_model=ConfiguredModel(adapter=local, model_id=LOCAL_MODEL_ID),
            ),
            "Which service is it?",
            "local_only",
        ),
    ]

    async def go():
        async with external, local:
            return [
                await gateway.orchestrator.run_text(prompt, execution_mode=mode)
                for gateway, prompt, mode in gateways
            ]

    outcomes = asyncio.run(go())
    for (gateway, _, _), outcome in zip(gateways, outcomes, strict=True):
        events = gateway.store.events(outcome.run_id)
        started = of_type(events, "model.started")
        assert started
        assert all("request_options" not in e.payload for e in started)
        validate(events)
