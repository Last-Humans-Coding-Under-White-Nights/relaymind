"""The adapter comparison runner CLI (RM25, spec section 12.1; issue #37).

Validates configuration errors deterministically; the comparison core
is covered with scripted doubles in test_timing_stats.py, and running
against live servers is an operator action, not a CI test.
"""

import asyncio
import json

import pytest

from helpers import PINNED_MANIFEST_PATH, ScriptedAdapter, load_script_module, run_script
from relaymind.adapters import Completed
from relaymind.stats import AdapterIdentity

MANIFEST_PATH = PINNED_MANIFEST_PATH


def run_compare(config_path, out_dir):
    return run_script("compare_adapters", config_path, "--out-dir", out_dir)


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
                        "manifest": str(MANIFEST_PATH),
                    },
                    "weird": {"kind": "carrier-pigeon", "model_id": "x", "context_limit": 1},
                },
            }
        ),
        encoding="utf-8",
    )
    result = run_compare(config, tmp_path / "out")
    assert result.returncode != 0
    assert "carrier-pigeon" in result.stderr
    assert "Traceback" not in result.stderr


def test_compare_script_rejects_a_config_without_prompts(tmp_path):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"prompts": [], "adapters": {}}), encoding="utf-8")
    result = run_compare(config, tmp_path / "out")
    assert result.returncode != 0
    assert "prompts" in result.stderr
    assert "Traceback" not in result.stderr


LOCAL_SPEC = {
    "kind": "llamacpp",
    "base_url": "http://127.0.0.1:8033",
    "context_limit": 8192,
    "manifest": str(MANIFEST_PATH),
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
        (
            {
                "prompts": ["hi"],
                "adapters": {"local": LOCAL_SPEC, "b": {**LOCAL_SPEC, "context_limit": 8192.9}},
            },
            "context_limit",
        ),
        (
            {
                "prompts": ["hi"],
                "adapters": {"local": LOCAL_SPEC, "b": {**LOCAL_SPEC, "manifest": "missing.json"}},
            },
            "missing.json",
        ),
    ],
)
def test_compare_script_rejects_malformed_configs_without_a_traceback(tmp_path, config, expected):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    result = run_compare(config_path, tmp_path / "out")
    assert result.returncode != 0
    assert expected in result.stderr
    assert "Traceback" not in result.stderr


def test_a_null_api_key_env_falls_back_to_the_default():
    module = load_script_module("compare_adapters")
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


def test_run_closes_every_adapter_even_when_one_close_fails(tmp_path, capsys):
    # A close failure after every export is written must not turn a
    # successful run into a crash that hides the export paths.
    module = load_script_module("compare_adapters")
    tracked = TrackingAdapter([Completed(finish_reason="stop")])
    identity = AdapterIdentity(adapter="x", model_id="x")
    adapters = {
        "plain": (ScriptedAdapter([Completed(finish_reason="stop")]), identity),
        "failing": (FailingCloseAdapter([Completed(finish_reason="stop")]), identity),
        "tracked": (tracked, identity),
    }
    paths = asyncio.run(module._run(adapters, ["p"], tmp_path))
    assert set(paths) == {"plain", "failing", "tracked"}
    assert all(path.exists() for path in paths.values())
    assert tracked.closed
    assert "failing" in capsys.readouterr().err


def test_a_local_adapter_identity_comes_from_its_model_manifest(tmp_path):
    # Honest evidence (RM16, spec 12.2): the export names what was
    # measured from the pinned manifest, never from hand-typed fields.
    module = load_script_module("compare_adapters")
    adapter, identity = module._build_adapter("local", LOCAL_SPEC)
    try:
        assert identity == AdapterIdentity(
            adapter="llamacpp",
            model_id="qwen3-0.6b",
            quantization="Q8_0",
            runtime_version="0.7.0 (build 11669, commit 620d2225c)",
        )
    finally:
        asyncio.run(adapter.aclose())


def test_a_relative_manifest_path_resolves_against_the_config_directory():
    module = load_script_module("compare_adapters")
    spec = {**LOCAL_SPEC, "manifest": MANIFEST_PATH.name}
    adapter, identity = module._build_adapter("local", spec, base_dir=MANIFEST_PATH.parent)
    try:
        assert identity.model_id == "qwen3-0.6b"
    finally:
        asyncio.run(adapter.aclose())


@pytest.mark.parametrize(
    "spec",
    [
        {key: value for key, value in LOCAL_SPEC.items() if key != "manifest"},
        {**LOCAL_SPEC, "quantization": "Q4_K_M"},
        {**LOCAL_SPEC, "model_id": "qwen3-0.6b"},
    ],
)
def test_a_local_adapter_needs_a_manifest_and_no_hand_typed_identity(spec):
    module = load_script_module("compare_adapters")
    with pytest.raises(ValueError):
        module._build_adapter("local", spec)
