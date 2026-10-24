"""RM25/RM27/RM32: fixed matrix counts and restart semantics are reviewable offline."""

import pytest

from helpers import load_script_module


def test_matrix_counts_and_paired_control_identity():
    driver = load_script_module("run_m8_matrix")
    cells = driver.matrix_cells()
    assert len(cells) == 31
    assert sum(c["requests"] for c in cells if c["runner"] == "corpus") == 2548
    assert sum(c["requests"] for c in cells if c["runner"] == "latency") == 1736
    placement = [c for c in cells if c["phase"] == "placement"]
    assert len(placement) == 24
    assert {c["policy"] for c in placement} == {"local_first", "device_aware", "expected_wait"}
    assert all(c["stream"] == "buffered" for c in placement)
    assert len([c for c in cells if c["configuration"] == "none"]) == 4


def test_restart_requires_declared_unfinished_attempt_and_retains_original(tmp_path):
    driver = load_script_module("run_m8_matrix")
    from relaymind.evaluation.checkpoint import atomic_json

    cell = {"id": "test"}
    index = {"attempts": {"test": [{"name": "a", "status": "interrupted", "cost": {"calls": 2}}]}}
    atomic_json(tmp_path / "index.json", index)
    assert driver.declare_restart(index, cell, "b")["name"] == "b"
    assert index["attempts"]["test"][0]["cost"]["calls"] == 2
    with pytest.raises(ValueError):
        driver.declare_restart(index, cell, "b")
    index["attempts"]["test"][-1]["status"] = "complete"
    with pytest.raises(ValueError):
        driver.declare_restart(index, cell, "c")


def test_attempt_cost_recovers_unfinished_generation_without_zero_tokens(tmp_path):
    """RM16/RM25: interrupted attempts still cost observed calls; missing usage is unknown."""
    import json
    import sqlite3

    driver = load_script_module("run_m8_matrix")
    with sqlite3.connect(tmp_path / "runs.sqlite3") as db:
        db.execute("CREATE TABLE events(run_id TEXT,event_type TEXT,actor TEXT,payload TEXT)")
        db.execute(
            "INSERT INTO events VALUES(?,?,?,?)",
            (
                "r",
                "model.started",
                "external",
                json.dumps({"call_id": "c", "model": "remote", "purpose": "final"}),
            ),
        )
    cost = driver.attempt_cost(tmp_path)
    assert cost["calls"] == 1
    assert cost["input_tokens_missing_calls"] == 1
    assert cost["input_tokens_known_total"] == 0


def test_provider_configuration_is_literal_and_never_embeds_credentials_in_url(
    tmp_path, monkeypatch
):
    """RM12/RM25: exported endpoint identity cannot contain the credential."""
    driver = load_script_module("run_m8_matrix")
    for name in (
        "RELAYMIND_EXTERNAL_API_KEY",
        "RELAYMIND_EXTERNAL_BASE_URL",
        "RELAYMIND_EXTERNAL_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)
    env = tmp_path / "provider.env"
    env.write_text(
        'export RELAYMIND_EXTERNAL_API_KEY="fixture-secret"\nRELAYMIND_EXTERNAL_BASE_URL=https://example.invalid/v1\nRELAYMIND_EXTERNAL_MODEL=fixture-model\n'
    )
    result = driver.provider_environment(env)
    assert result["RELAYMIND_EXTERNAL_API_KEY"] == "fixture-secret"
    env.write_text(
        env.read_text().replace(
            "https://example.invalid/v1", "https://user:fixture-secret@example.invalid/v1"
        )
    )
    with pytest.raises(ValueError, match="endpoint"):
        driver.provider_environment(env)
