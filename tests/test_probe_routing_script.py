"""The live routing probe spike (issue #48; RM02, RM05, spec sections 6, 8, 12.1).

A spike gets minimal tests: the checked-in probe definition loads, each
mechanism builds the request the spec allows (native function calling
with one function per skill, or a JSON envelope validated by the
gateway, optionally grammar-constrained), model output is judged by the
production RouteDecision validator, and a pass records one row per
prompt. Model doubles only; the live run is an operator action recorded
in docs/EVIDENCE.md.
"""

import asyncio
import json
import typing

import httpx
import pytest

from helpers import (
    LLAMACPP_CONFIG,
    SCRIPTS_DIR,
    ScriptedAdapter,
    collect_events,
    delta_chunk,
    load_script_module,
    sse_response,
)
from relaymind.adapters import (
    Completed,
    GenerationFailed,
    GenerationRequest,
    TextDelta,
    ToolCall,
    Usage,
)
from relaymind.adapters.llamacpp import build_chat_request
from relaymind.orchestrator.route_decision import Route
from relaymind.stats import GenerationStats

CHECKED_IN_PROBE = SCRIPTS_DIR / "probes" / "m2_routing.json"


@pytest.fixture()
def probe():
    return load_script_module("probes/probe_routing")


@pytest.fixture()
def config(probe):
    return probe.load_routing_config(CHECKED_IN_PROBE)


class OutputDouble(ScriptedAdapter):
    """Replays one scripted output per request, by user prompt."""

    def __init__(self, outputs):
        super().__init__([])
        self.outputs = outputs
        self.requests: list[GenerationRequest] = []

    async def generate(self, request: GenerationRequest):
        self.requests.append(request)
        yield Usage(input_tokens=100, output_tokens=10)
        output = self.outputs(request)
        if isinstance(output, ToolCall):
            yield output
            yield Completed(finish_reason="tool_calls")
        else:
            yield TextDelta(text=output)
            yield Completed(finish_reason="stop")

    async def count_tokens(self, messages, tool_schemas=()):
        return 123


def test_the_checked_in_probe_has_six_skills_and_six_prompts_per_language(probe, config):
    assert len(config.skills) == 6
    languages = [prompt.language for prompt in config.prompts]
    assert languages.count("en") == 6 and languages.count("zh") == 6
    # Every expectation names an advertised skill or none (clarify).
    skill_ids = {skill.id for skill in config.skills}
    assert all(p.expected_skill in skill_ids | {None} for p in config.prompts)


def test_native_mechanism_offers_one_function_per_skill_plus_clarify(probe, config):
    request = probe.build_request("native_function_calling", config.prompts[0], config, "g")
    names = [tool.name for tool in request.tool_schemas]
    assert names == [skill.id for skill in config.skills] + [probe.CLARIFY_FUNCTION]


def test_a_native_call_maps_to_a_validated_decision(probe, config):
    call = ToolCall(call_id="c", tool_name="investigate-performance", arguments={"language": "zh"})
    decision, error = probe.decide("native_function_calling", [call], config)
    assert error is None
    # Route and reason come from the skill's configuration, not the model.
    assert decision.route is Route.EXTERNAL
    assert decision.reason_code == "complex_skill"
    assert decision.language.value == "zh"


@pytest.mark.parametrize(
    "events",
    [
        [TextDelta(text="staging-api is healthy")],
        [ToolCall(call_id="c", tool_name="drop_tables", arguments={"language": "en"})],
    ],
    ids=["no-call", "unknown-function"],
)
def test_native_output_without_a_known_call_is_invalid(probe, config, events):
    decision, error = probe.decide("native_function_calling", events, config)
    assert decision is None and error


def test_a_clarify_call_without_its_required_question_is_invalid(probe, config):
    # RM09: a call missing a required argument never passes validation,
    # even though the gateway shape allows clarification to be null.
    call = ToolCall(call_id="c", tool_name=probe.CLARIFY_FUNCTION, arguments={"language": "en"})
    decision, error = probe.decide("native_function_calling", [call], config)
    assert decision is None
    assert error == "missing required argument: question"


@pytest.mark.parametrize("mechanism", ["native_function_calling", "json_envelope"])
def test_a_failed_generation_is_reported_as_a_failure_not_a_syntax_error(probe, config, mechanism):
    # RM16: a transport or runtime failure is named as such, never
    # misreported as the model writing bad output.
    events = [GenerationFailed(code="provider_timeout", message="no response in 30 s")]
    decision, error = probe.decide(mechanism, events, config)
    assert decision is None
    assert error == "generation failed (provider_timeout): no response in 30 s"


def test_run_mechanism_declares_its_return_type(probe):
    # AGENTS.md style: public functions carry type hints.
    hints = typing.get_type_hints(probe.run_mechanism)
    assert hints["return"] == tuple[list[dict[str, object]], list[GenerationStats]]


def test_envelope_output_goes_through_the_production_validator(probe, config):
    good = {
        "schema_version": 1,
        "route": "clarify",
        "skill_id": None,
        "language": "en",
        "reason_code": "missing_input",
        "clarification": "Which service?",
    }
    decision, error = probe.decide("json_envelope", [TextDelta(text=json.dumps(good))], config)
    assert error is None and decision.route is Route.CLARIFY
    bad = [TextDelta(text=json.dumps({**good, "skill_id": "made-up"}))]
    decision, error = probe.decide("json_envelope", bad, config)
    assert decision is None and "skill_id" in error


def test_grammar_schema_is_the_published_schema_closed_over_the_catalog(probe, config):
    schema = probe.grammar_schema(config)
    assert schema["additionalProperties"] is False
    assert schema["properties"]["skill_id"]["enum"] == [s.id for s in config.skills] + [None]


def test_the_grammar_schema_leaves_the_cross_field_rules_to_the_validator(probe, config):
    # The runtime's grammar converter constrains shape; the if/then rule
    # and the non-blank patterns are judged by the validator afterwards.
    schema = probe.grammar_schema(config)
    assert "allOf" not in schema
    assert "pattern" not in schema["properties"]["clarification"]


def test_the_probe_adapter_records_its_cache_setting_in_every_generation_body(probe, config):
    # Spec 10 and 12.2: runtime prefix-cache behavior is recorded, so the
    # probe sets cache_prompt explicitly instead of taking the default.
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return sse_response([delta_chunk({}, finish_reason="stop")])

    adapter = probe.ProbeLlamaCppAdapter(
        LLAMACPP_CONFIG, cache_prompt=False, transport=httpx.MockTransport(handler)
    )
    request = probe.build_request("json_envelope", config.prompts[0], config, "g")
    collect_events(adapter, request)

    # Written so it holds with the adapter's own cache_prompt true (spec 0.31 section 6):
    # the probe's arm replaces it.
    assert seen["body"] == {
        **build_chat_request(request, chat_template_kwargs=LLAMACPP_CONFIG.chat_template_kwargs),
        "cache_prompt": False,
    }


def test_the_checked_in_probe_states_its_cache_setting(config):
    assert config.cache_prompt is True


def test_the_grammar_adapter_adds_response_format_to_the_generation_body(probe, config):
    # Only the chat body changes; the request is otherwise what the
    # plain adapter sends.
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return sse_response([delta_chunk({}, finish_reason="stop")])

    schema = probe.grammar_schema(config)
    adapter = probe.ProbeLlamaCppAdapter(
        LLAMACPP_CONFIG, cache_prompt=True, schema=schema, transport=httpx.MockTransport(handler)
    )
    request = probe.build_request("json_envelope_grammar", config.prompts[0], config, "g")
    collect_events(adapter, request)

    body = dict(seen["body"])
    assert body.pop("response_format") == {
        "type": "json_schema",
        "json_schema": {"name": "route_decision", "schema": schema},
    }
    assert body == {
        **build_chat_request(request, chat_template_kwargs=LLAMACPP_CONFIG.chat_template_kwargs),
        "cache_prompt": True,
    }


def test_a_pass_records_one_graded_row_per_prompt(probe, config):
    def outputs(request):
        return ToolCall(call_id="c", tool_name="check-service-health", arguments={"language": "en"})

    adapter = OutputDouble(outputs)
    rows, _records = asyncio.run(probe.run_mechanism(adapter, "native_function_calling", config))

    measured = [row for row in rows if not row["warmup"]]
    assert len(measured) == 12
    assert len(rows) == 12 + config.warmups
    first = measured[0]
    assert first["prompt"] == config.prompts[0].text
    assert first["valid"] is True
    assert first["skill_id"] == "check-service-health"
    assert first["route_correct"] is True and first["skill_correct"] is True
    assert first["router_input_tokens"] == 123
    assert first["first_model_action_seconds"] is not None
    # The clarify prompt got a skill: graded wrong, not dropped.
    clarify = next(row for row in measured if row["expected_route"] == "clarify")
    assert clarify["route_correct"] is False


def test_a_token_count_failure_is_recorded_and_the_pass_continues(probe, config):
    """A count_tokens error lands in that prompt's row; the pass continues.

    Spec section 12.2: errors are recorded rather than dropped, so one
    tokenizer failure (spec section 4.1) must not abort the pass and
    lose the rows already measured.
    """
    failing_prompt = config.prompts[1].text

    class CountFails(OutputDouble):
        async def count_tokens(self, messages, tool_schemas=()):
            if any(failing_prompt in str(message.content) for message in messages):
                raise RuntimeError("tokenize failed")
            return 123

    def outputs(request):
        return ToolCall(call_id="c", tool_name="check-service-health", arguments={"language": "en"})

    adapter = CountFails(outputs)
    rows, _records = asyncio.run(probe.run_mechanism(adapter, "native_function_calling", config))

    measured = [row for row in rows if not row["warmup"]]
    assert len(measured) == 12
    failed = measured[1]
    assert failed["router_input_tokens"] is None
    assert failed["valid"] is False
    # RM16: the error is recorded as an error, never as model text.
    assert "tokenize failed" in failed["error"]
    assert failed["raw_text"] is None
    assert failed["validation_error"] is None
    assert measured[2]["router_input_tokens"] == 123
    assert measured[2]["error"] is None


class FakeClock:
    """A monotonic clock the doubles advance by hand."""

    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_first_model_action_includes_the_tokenizer_round_trips(probe, config):
    # Spec 12.1: first model action is measured from request start. The
    # router counts its input before generating, so that count is part
    # of the measured time, not before it.
    clock = FakeClock()

    class Timed(OutputDouble):
        async def count_tokens(self, messages, tool_schemas=()):
            clock.now += 0.2
            return 123

        async def generate(self, request):
            clock.now += 0.1
            async for event in super().generate(request):
                yield event

    def outputs(request):
        return ToolCall(call_id="c", tool_name="check-service-health", arguments={"language": "en"})

    rows, _records = asyncio.run(
        probe.run_mechanism(Timed(outputs), "native_function_calling", config, clock=clock)
    )
    measured = [row for row in rows if not row["warmup"]]
    assert measured[0]["first_model_action_seconds"] == pytest.approx(0.3)


def test_an_over_cap_router_input_takes_the_bypass_and_is_marked(probe, config):
    # Spec 4.1: inputs over 2,048 local tokens bypass the fast router and
    # are never counted as short-context successes.
    assert probe.ROUTER_INPUT_TOKEN_CAP == 2048
    long_prompt = config.prompts[1].text

    class Oversized(OutputDouble):
        async def count_tokens(self, messages, tool_schemas=()):
            if any(long_prompt in str(message.content) for message in messages):
                return 2049
            return 123

    def outputs(request):
        return ToolCall(call_id="c", tool_name="check-service-health", arguments={"language": "en"})

    adapter = Oversized(outputs)
    rows, _records = asyncio.run(probe.run_mechanism(adapter, "native_function_calling", config))

    measured = [row for row in rows if not row["warmup"]]
    bypassed = measured[1]
    assert bypassed["bypass"] is True
    assert bypassed["router_input_tokens"] == 2049
    assert bypassed["valid"] is False
    assert bypassed["validation_error"] is None and bypassed["error"] is None
    assert bypassed["raw_text"] is None and bypassed["raw_tool_calls"] == []
    # No generation was sent for it.
    assert all(long_prompt not in str(r.messages[-1].content) for r in adapter.requests)
    assert measured[0]["bypass"] is False
    summary = probe.summarize(rows)
    assert summary["native_function_calling/en"]["bypass"] == "1/6"
