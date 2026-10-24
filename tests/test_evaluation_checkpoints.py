"""RM16/RM25: durable same-identity resume never silently repeats inference."""

import json

import pytest


def test_checkpoint_refuses_interrupted_and_changed_identity(tmp_path):
    from relaymind.evaluation.checkpoint import Checkpoint

    path = tmp_path / "cell.json"
    c = Checkpoint(path, {"source": "a", "seed": "b"})
    c.start("case/auto")
    with pytest.raises(ValueError, match="interrupted"):
        Checkpoint(path, {"source": "a", "seed": "b"})
    with pytest.raises(ValueError, match="identity"):
        Checkpoint(path, {"source": "z", "seed": "b"})
    c.finish("case/auto", {"state": "failed", "calls": 2})
    c.complete({"grades": 1})
    resumed = Checkpoint(path, {"source": "a", "seed": "b"})
    assert resumed.document["completed"] is True
    assert resumed.document["steps"][0]["result"]["calls"] == 2
    assert json.loads(path.read_text()) == resumed.document


def test_partial_sequence_is_not_a_finalized_cell(tmp_path):
    from relaymind.evaluation.checkpoint import Checkpoint

    path = tmp_path / "cell.json"
    c = Checkpoint(path, {"source": "a"})
    c.start("case")
    c.finish("case", {"state": "completed"})
    with pytest.raises(ValueError, match="interrupted"):
        Checkpoint(path, {"source": "a"})


def test_source_environment_overrides_wrong_editable_install(tmp_path):
    from relaymind.evaluation.environment import checkout_environment

    env = checkout_environment(tmp_path, {"PYTHONPATH": "/other/src", "OTHER": "retained"})
    assert env["PYTHONPATH"].split(__import__("os").pathsep)[0] == str(tmp_path / "src")
    assert env["OTHER"] == "retained"


def test_latency_checkpoints_every_request_including_cold_warmups(tmp_path):
    """RM25: each scheduled request is durable and uniquely identified before dispatch."""
    import asyncio

    from helpers import load_script_module
    from relaymind.evaluation.checkpoint import Checkpoint

    bench = load_script_module("latency_benchmark")
    checkpoint = Checkpoint(tmp_path / "latency.json", {"source": "fixture"})

    async def measured(api, prompt, **options):
        assert checkpoint.document["steps"][-1]["result"] is None
        return {"error": None, "prompt_id": prompt["id"]}

    result = asyncio.run(
        bench.run_samples(
            None, bench.load_prompts(), seed=7, runs=1, measure_one=measured, checkpoint=checkpoint
        )
    )
    assert len(checkpoint.document["steps"]) == 16
    assert all(s["result"] is not None for s in checkpoint.document["steps"])
    assert len(result["measured"]) == 12
