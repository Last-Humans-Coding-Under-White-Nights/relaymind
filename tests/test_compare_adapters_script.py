"""The adapter comparison runner CLI (issue #37).

Validates configuration errors deterministically; the comparison core
is covered with scripted doubles in test_timing_stats.py, and running
against live servers is an operator action, not a CI test.
"""

import json
import subprocess
import sys
from pathlib import Path

# Resolved from this file, not the current working directory, so the
# suite passes no matter where pytest is invoked from.
SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "compare_adapters.py"


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
