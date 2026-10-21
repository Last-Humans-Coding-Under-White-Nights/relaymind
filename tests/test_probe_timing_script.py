"""The timing probe runner (RM25, spec section 12.1; RM16, section 12).

An EVIDENCE.md entry must name the exact command that produced its
numbers. The probe script therefore records cold start, warm-ups,
prompts, system prompt, tool schema, and per-generation outcomes in its
export. Deterministic parts are tested with doubles; launching a real
llama-server is an operator action.
"""

import asyncio
import json
from pathlib import Path

import pytest

from helpers import (
    HEALTH_TOOL,
    PINNED_MANIFEST_PATH,
    SCRIPTS_DIR,
    ScriptedAdapter,
    load_script_module,
    run_script,
)
from relaymind.adapters import Completed, GenerationRequest, TextDelta, ToolCall, Usage

CHECKED_IN_PROBE = SCRIPTS_DIR / "probes" / "m1_timing.json"

# The canonical demo tool in the JSON shape a probe config carries.
TOOL = {
    "name": HEALTH_TOOL.name,
    "description": HEALTH_TOOL.description,
    "input_schema": dict(HEALTH_TOOL.input_schema),
}

GOOD_CONFIG = {
    "manifest": str(PINNED_MANIFEST_PATH),
    "warmups": 2,
    "system_prompt": "You check service health.",
    "tool": TOOL,
    "text_prompts": ["Reply with exactly one word: pong", "只用一个词回答：pong"],
    "tool_prompts": ["Is staging-api healthy right now?", "staging-api 现在健康吗？"],
}


class RoutingDouble(ScriptedAdapter):
    """Proposes a tool call when tools are offered, answers text otherwise."""

    def __init__(self):
        super().__init__([])
        self.requests: list[GenerationRequest] = []

    async def generate(self, request: GenerationRequest):
        self.requests.append(request)
        yield Usage(input_tokens=10, output_tokens=2)
        if request.tool_schemas:
            yield ToolCall(
                call_id="call_1",
                tool_name="get_service_health",
                arguments={"service": "staging-api"},
            )
            yield Completed(finish_reason="tool_calls")
        else:
            yield TextDelta(text="pong")
            yield Completed(finish_reason="stop")


@pytest.fixture()
def probe():
    return load_script_module("probe_timing")


@pytest.fixture()
def good_config(probe, tmp_path):
    config_path = tmp_path / "probe.json"
    config_path.write_text(json.dumps(GOOD_CONFIG), encoding="utf-8")
    return probe.load_probe_config(config_path)


def test_the_checked_in_probe_config_loads(probe):
    # The config that produced the EVIDENCE.md entry stays loadable and
    # offers the canonical demo tool.
    config = probe.load_probe_config(CHECKED_IN_PROBE)
    assert config.warmups >= 1
    assert config.text_prompts and config.tool_prompts
    assert config.tool == HEALTH_TOOL


def test_warmups_are_split_from_the_measured_pass(probe, good_config):
    warmups, measured = probe.probe_requests(good_config)
    # Warm-ups cover both prompt kinds, so the first measured tool call
    # is not the first request that ever carried the tool schema.
    assert [r.generation_id for r in warmups] == [
        "warmup_text_0",
        "warmup_tool_0",
        "warmup_text_1",
        "warmup_tool_1",
    ]
    assert [r.generation_id for r in measured] == ["text_00", "text_01", "tool_00", "tool_01"]


def test_run_probe_sends_warmups_first_and_records_outcomes(probe, good_config):
    adapter = RoutingDouble()

    records, outcomes = asyncio.run(probe.run_probe(adapter, good_config))

    ids = [record.generation_id for record in records]
    assert ids == [
        "warmup_text_0",
        "warmup_tool_0",
        "warmup_text_1",
        "warmup_tool_1",
        "text_00",
        "text_01",
        "tool_00",
        "tool_01",
    ]
    # Tool prompts carry the system prompt and the one tool schema;
    # plain prompts carry neither.
    by_id = {request.generation_id: request for request in adapter.requests}
    assert by_id["tool_00"].messages[0].content == "You check service health."
    assert by_id["tool_00"].tool_schemas == (HEALTH_TOOL,)
    assert by_id["text_00"].tool_schemas == ()
    assert len(by_id["text_00"].messages) == 1
    # Outcomes back the Proposal and Answer columns of an entry.
    assert outcomes["tool_00"] == {
        "answer": None,
        "tool_calls": [
            {"tool_name": "get_service_health", "arguments": {"service": "staging-api"}}
        ],
    }
    assert outcomes["text_00"] == {"answer": "pong", "tool_calls": []}


def test_run_info_names_what_produced_the_numbers(probe, good_config):
    info = probe.build_run_info(
        good_config,
        command=["python", "scripts/probe_timing.py", "probe.json"],
        cold_start_seconds=1.25,
        server_argv=["--ctx-size", "8192"],
        model_path=Path("/models/Qwen3-0.6B-Q8_0.gguf"),
        warmup_generation_ids=["warmup_text_0"],
        outcomes={"text_00": {"answer": "pong", "tool_calls": []}},
        runtime_banner="version: 0.7.0",
    )

    assert info["command"] == "python scripts/probe_timing.py probe.json"
    assert info["cold_start_seconds"] == 1.25
    assert info["warmups"] == 2
    assert info["warmup_generation_ids"] == ["warmup_text_0"]
    assert info["system_prompt"] == "You check service health."
    assert info["tool"] == TOOL
    assert info["text_prompts"] == GOOD_CONFIG["text_prompts"]
    assert info["tool_prompts"] == GOOD_CONFIG["tool_prompts"]
    assert info["server_argv"] == ["--ctx-size", "8192"]
    assert info["model_file"] == "Qwen3-0.6B-Q8_0.gguf"
    assert info["runtime_banner"] == "version: 0.7.0"
    assert info["outcomes"] == {"text_00": {"answer": "pong", "tool_calls": []}}
    assert "platform" in info and "python" in info
    # Single pass by construction: nothing here is a percentile.
    assert info["passes"] == 1


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        ([1, 2], "JSON object"),
        ({**GOOD_CONFIG, "manifest": "missing.json"}, "missing.json"),
        ({k: v for k, v in GOOD_CONFIG.items() if k != "manifest"}, "manifest"),
        ({**GOOD_CONFIG, "warmups": -1}, "warmups"),
        ({**GOOD_CONFIG, "warmups": 1.5}, "warmups"),
        ({**GOOD_CONFIG, "system_prompt": ""}, "system_prompt"),
        ({**GOOD_CONFIG, "text_prompts": []}, "text_prompts"),
        ({**GOOD_CONFIG, "tool_prompts": "Is staging-api up?"}, "tool_prompts"),
        ({**GOOD_CONFIG, "tool_prompts": [42]}, "tool_prompts"),
        ({**GOOD_CONFIG, "tool": {"name": "x"}}, "tool"),
        ({**GOOD_CONFIG, "tool": {**TOOL, "input_schema": "object"}}, "tool"),
    ],
)
def test_malformed_configs_are_rejected_naming_the_field(probe, tmp_path, config, expected):
    config_path = tmp_path / "probe.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(ValueError, match=expected):
        probe.load_probe_config(config_path)


def test_probe_script_reports_a_config_error_without_a_traceback(tmp_path):
    # One subprocess case proves the CLI turns a ValueError into a clean
    # exit; the field-level checks above run in-process.
    result = run_script("probe_timing", tmp_path / "nope.json", "--out", tmp_path / "out.json")
    assert result.returncode != 0
    assert "cannot read config" in result.stderr
    assert "Traceback" not in result.stderr
