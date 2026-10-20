"""The timing probe runner (RM25, spec section 12.1; RM16, section 12).

An EVIDENCE.md entry must name the exact command that produced its
numbers. The probe script therefore records cold start, warm-ups,
prompts, system prompt, tool schema, and per-generation outcomes in its
export. Deterministic parts are tested with doubles; launching a real
llama-server is an operator action.
"""

import asyncio
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from helpers import make_capabilities
from relaymind.adapters import (
    AdapterCapabilities,
    AdapterHealth,
    Completed,
    GenerationRequest,
    ModelAdapter,
    TextDelta,
    ToolCall,
    Usage,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "probe_timing.py"
MANIFEST_PATH = REPO_ROOT / "models" / "qwen3-0.6b-q8_0.manifest.json"
CHECKED_IN_PROBE = REPO_ROOT / "scripts" / "probes" / "m1_timing.json"


def load_script_module():
    spec = importlib.util.spec_from_file_location("probe_timing", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_script(*args):
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH), *map(str, args)],
        capture_output=True,
        text=True,
    )


TOOL = {
    "name": "get_service_health",
    "description": "Return current health for a service.",
    "input_schema": {
        "type": "object",
        "properties": {"service": {"enum": ["staging-api", "payments-api"]}},
        "required": ["service"],
        "additionalProperties": False,
    },
}

GOOD_CONFIG = {
    "manifest": str(MANIFEST_PATH),
    "warmups": 2,
    "system_prompt": "You check service health.",
    "tool": TOOL,
    "text_prompts": ["Reply with exactly one word: pong", "只用一个词回答：pong"],
    "tool_prompts": ["Is staging-api healthy right now?", "staging-api 现在健康吗？"],
}


class RoutingDouble(ModelAdapter):
    """Proposes a tool call when tools are offered, answers text otherwise."""

    def __init__(self):
        self.requests: list[GenerationRequest] = []

    async def capabilities(self) -> AdapterCapabilities:
        return make_capabilities()

    async def generate(self, request: GenerationRequest):
        self.requests.append(request)
        yield Usage(input_tokens=10, output_tokens=2)
        if request.tool_schemas:
            service = "staging-api"
            yield ToolCall(
                call_id="call_1", tool_name="get_service_health", arguments={"service": service}
            )
            yield Completed(finish_reason="tool_calls")
        else:
            yield TextDelta(text="pong")
            yield Completed(finish_reason="stop")

    async def cancel(self, generation_id: str) -> None:
        pass

    async def health(self) -> AdapterHealth:
        return AdapterHealth(available=True, warm=True, detail="double")


def test_the_checked_in_probe_config_loads():
    # The config that produced the EVIDENCE.md entry stays loadable.
    module = load_script_module()
    config = module.load_probe_config(CHECKED_IN_PROBE)
    assert config.warmups >= 1
    assert config.text_prompts and config.tool_prompts
    assert config.tool.name == "get_service_health"


def test_warmups_are_labelled_and_measured_generations_follow(tmp_path):
    module = load_script_module()
    config_path = tmp_path / "probe.json"
    config_path.write_text(json.dumps(GOOD_CONFIG), encoding="utf-8")
    config = module.load_probe_config(config_path)
    adapter = RoutingDouble()

    records, outcomes = asyncio.run(module.run_probe(adapter, config))

    ids = [record.generation_id for record in records]
    # Warm-ups cover both prompt kinds, so the first measured tool call
    # is not the first request that ever carried the tool schema.
    assert ids[:4] == ["warmup_text_0", "warmup_tool_0", "warmup_text_1", "warmup_tool_1"]
    assert ids[4:] == ["text_00", "text_01", "tool_00", "tool_01"]
    assert module.warmup_ids(records) == ids[:4]
    # Tool prompts carry the system prompt and the one tool schema;
    # plain prompts carry neither.
    by_id = {request.generation_id: request for request in adapter.requests}
    assert by_id["tool_00"].messages[0].content == "You check service health."
    assert [schema.name for schema in by_id["tool_00"].tool_schemas] == ["get_service_health"]
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


def test_run_info_names_what_produced_the_numbers(tmp_path):
    module = load_script_module()
    config_path = tmp_path / "probe.json"
    config_path.write_text(json.dumps(GOOD_CONFIG), encoding="utf-8")
    config = module.load_probe_config(config_path)

    info = module.build_run_info(
        config,
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
def test_probe_script_rejects_malformed_configs_without_a_traceback(tmp_path, config, expected):
    config_path = tmp_path / "probe.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    result = run_script(config_path, "--out", tmp_path / "out.json")
    assert result.returncode != 0
    assert expected in result.stderr
    assert "Traceback" not in result.stderr


def test_probe_script_reports_an_unreadable_config(tmp_path):
    result = run_script(tmp_path / "nope.json", "--out", tmp_path / "out.json")
    assert result.returncode != 0
    assert "cannot read config" in result.stderr
    assert "Traceback" not in result.stderr
