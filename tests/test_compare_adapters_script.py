"""The adapter comparison runner CLI (RM25, spec section 12.1; issue #37).

Validates configuration errors deterministically; the comparison core
is covered with scripted doubles in test_timing_stats.py, and running
against live servers is an operator action, not a CI test.
"""

import asyncio
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from helpers import ScriptedAdapter
from relaymind.adapters import Completed
from relaymind.stats import AdapterIdentity

# Resolved from this file, not the current working directory, so the
# suite passes no matter where pytest is invoked from.
SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "compare_adapters.py"


def load_script_module():
    spec = importlib.util.spec_from_file_location("compare_adapters", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_script(config_path, out_dir):
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH), str(config_path), "--out-dir", str(out_dir)],
        capture_output=True,
        text=True,
    )


def test_compare_script_rejects_an_unknown_adapter_kind(tmp_path):
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "prompts": ["hello"],
                "adapters": {
                    "local": {
                        "kind": "llamacpp",
                        "base_url": "http://127.0.0.1:8033",
                        "context_limit": 8192,
                        "model_id": "qwen3-0.6b",
                    },
                    "weird": {"kind": "carrier-pigeon", "model_id": "x", "context_limit": 1},
                },
            }
        ),
        encoding="utf-8",
    )
    result = run_script(config, tmp_path / "out")
    assert result.returncode != 0
    assert "carrier-pigeon" in result.stderr
    assert "Traceback" not in result.stderr


def test_compare_script_rejects_a_config_without_prompts(tmp_path):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"prompts": [], "adapters": {}}), encoding="utf-8")
    result = run_script(config, tmp_path / "out")
    assert result.returncode != 0
    assert "prompts" in result.stderr
    assert "Traceback" not in result.stderr


LOCAL_SPEC = {
    "kind": "llamacpp",
    "base_url": "http://127.0.0.1:8033",
    "context_limit": 8192,
    "model_id": "qwen3-0.6b",
}


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        ({"prompts": ["hi"]}, "adapters"),
        ({"prompts": ["hi"], "adapters": None}, "adapters"),
        ([1, 2], "JSON object"),
        ({"prompts": ["hi"], "adapters": ["a", "b"]}, "adapters"),
        ({"prompts": "Is staging-api healthy?", "adapters": {}}, "prompts"),
        ({"prompts": [42], "adapters": {}}, "prompts"),
        ({"prompts": ["hi"], "adapters": {"local": LOCAL_SPEC, "b": "llamacpp"}}, "'b'"),
        ({"prompts": ["hi"], "adapters": {"local": LOCAL_SPEC, "../x": LOCAL_SPEC}}, "../x"),
    ],
)
def test_compare_script_rejects_malformed_configs_without_a_traceback(tmp_path, config, expected):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    result = run_script(config_path, tmp_path / "out")
    assert result.returncode != 0
    assert expected in result.stderr
    assert "Traceback" not in result.stderr


def test_a_null_api_key_env_falls_back_to_the_default():
    module = load_script_module()
    adapter, _identity = module._build_adapter(
        "external",
        {
            "kind": "chat_completions",
            "base_url": "https://api.provider.example/v1",
            "model_id": "provider-large-1",
            "context_limit": 128000,
            "api_key_env": None,
        },
    )
    try:
        assert adapter._config.api_key_env == "RELAYMIND_EXTERNAL_API_KEY"
    finally:
        asyncio.run(adapter.aclose())


class FailingCloseAdapter(ScriptedAdapter):
    async def aclose(self) -> None:
        raise RuntimeError("close failed")


class TrackingAdapter(ScriptedAdapter):
    closed = False

    async def aclose(self) -> None:
        self.closed = True


def test_run_closes_every_adapter_even_when_one_close_fails(tmp_path):
    module = load_script_module()
    tracked = TrackingAdapter([Completed(finish_reason="stop")])
    identity = AdapterIdentity(adapter="x", model_id="x")
    adapters = {
        "plain": (ScriptedAdapter([Completed(finish_reason="stop")]), identity),
        "failing": (FailingCloseAdapter([Completed(finish_reason="stop")]), identity),
        "tracked": (tracked, identity),
    }
    with pytest.raises(RuntimeError, match="close failed"):
        asyncio.run(module._run(adapters, ["p"], tmp_path))
    assert tracked.closed
