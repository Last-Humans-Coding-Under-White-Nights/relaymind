"""RM03/RM16/RM17/RM25: offline M7 plan and exact shared evidence identity."""

import json
from copy import deepcopy
from pathlib import Path

import pytest

from helpers import load_script_module


def driver():
    return load_script_module("run_m7_matrix")


def test_offline_plan_counts_and_no_provider_access(tmp_path, monkeypatch):
    d = driver()
    monkeypatch.setattr(
        d, "provider_environment", lambda _: pytest.fail("planning read credentials")
    )
    monkeypatch.setattr(
        d.subprocess, "run", lambda *a, **k: pytest.fail("planning launched process")
    )
    out = tmp_path / "plan"
    d.main(["--out", str(out), "--revision", "a" * 40, "--criteria-revision", "b" * 40])
    plan = json.loads((out / "plan.json").read_text(encoding="utf-8"))
    assert len(plan["cells"]) == 9
    assert plan["corpus_runs"] == 832
    assert plan["latency_requests"] == 248
    assert plan["external_inference_ceiling"] == 3120
    assert plan["conditional_reuse"]["incremental_corpus_runs"] == 572
    assert plan["conditional_reuse"]["external_inference_ceiling"] == 1872
    assert plan["cells"][6]["revision"] == "b" * 40
    d.main(["--out", str(tmp_path / "narrow"), "--revision", "a" * 40, "--omit-criteria"])
    narrow = json.loads((tmp_path / "narrow/plan.json").read_text(encoding="utf-8"))
    assert narrow["corpus_runs"] == 728
    assert narrow["external_inference_ceiling"] == 2808
    assert narrow["criteria_hypothesis"] == "not measured"


def test_fixed_commands_preserve_single_levers_and_local_latency(tmp_path):
    d = driver()
    env = {
        "RELAYMIND_EXTERNAL_BASE_URL": "https://example.invalid/v1",
        "RELAYMIND_EXTERNAL_MODEL": "fixture",
    }
    cells = d.matrix_cells("a" * 40, "b" * 40)
    configs = {}
    for cell in cells:
        argv = d.command(cell, tmp_path / cell["id"], env, root=tmp_path)
        runner = load_script_module(
            "run_corpus" if cell["runner"] == "corpus" else "latency_benchmark"
        )
        args = runner.argument_parser().parse_args(argv[2:])
        configs[cell["id"]] = d.configuration_from_args(args).to_json_object()
        assert args.timing_samples is None
        assert "--completion-criteria" not in argv
        if cell["runner"] == "latency":
            assert args.external_base_url is None
            assert args.runs == 10 and args.seed == 20261023
        else:
            assert len(args.modes) * 52 == cell["requests"]
    base = configs["M7-C0"]
    assert configs["M7-C1"]["performance"]["answer_pass"] == "draft"
    assert configs["M7-C2"]["performance"]["calls_per_generation"] == "many"
    assert configs["M7-C3"]["performance"]["prefetch"] is True
    assert configs["M7-C4"]["performance"] == {
        **base["performance"],
        "answer_pass": "draft",
        "calls_per_generation": "many",
        "prefetch": True,
    }
    assert configs["M7-C5"]["selection"]["chain"] == ["example_match"]
    assert configs["M7-C6"] == base
    assert configs["M7-L1"]["performance"] == {
        **configs["M7-L0"]["performance"],
        "early_headers": True,
    }


def test_reuse_is_limited_to_c0_c5_and_every_identity_dimension():
    d = driver()
    identity = {
        "source": {"head": "a" * 40},
        "configuration": {"prefetch": False},
        "timing_prior": None,
        "runtime_sha256": "r",
        "model_sha256": "m",
        "provider": {"context": 128000},
        "corpus_sha256": "c",
        "sidecar_sha256": "s",
        "fixtures_sha256": "f",
        "modes": ["auto", "external_only", "local_only"],
    }
    assert d.validate_reuse_identity("M7-C0", identity, deepcopy(identity)) is None
    for key in identity:
        changed = deepcopy(identity)
        changed[key] = "different"
        with pytest.raises(ValueError, match="identity"):
            d.validate_reuse_identity("M7-C0", identity, changed)
    seeded = deepcopy(identity)
    seeded["timing_prior"] = {"sha256": "prior"}
    with pytest.raises(ValueError, match="prior"):
        d.validate_reuse_identity("M7-C0", seeded, seeded)
    for name in ("M7-C1", "M7-L0", "M7-C6"):
        with pytest.raises(ValueError, match="C0.*C5"):
            d.validate_reuse_identity(name, identity, identity)


def test_ablation_requires_distinct_explicit_revision():
    d = driver()
    with pytest.raises(ValueError, match="distinct"):
        d.matrix_cells("a" * 40, "a" * 40)
    plan = d.make_plan("a" * 40, None)
    assert plan["cells"][6]["revision"] is None
    assert plan["criteria_hypothesis"] == "requires experimental revision"


def test_restart_and_cost_use_existing_m8_framework():
    d = driver()
    shared = __import__("run_m8_matrix")
    assert d.reconcile_attempt is shared.reconcile_attempt
    assert d.attempt_cost is shared.attempt_cost
    assert d.declare_restart is shared.declare_restart
    index = {
        "attempts": {
            "M7-C0": [
                {
                    "name": "initial",
                    "status": "interrupted",
                    "cost": {"calls": 3, "input_tokens_missing_calls": 1},
                }
            ]
        }
    }
    d.declare_restart(index, {"id": "M7-C0"}, "recovery")
    assert index["attempts"]["M7-C0"][0]["cost"]["calls"] == 3


def lifecycle(tmp_path, monkeypatch):
    """Real M7 orchestration/checkpoints, with inference replaced by a deterministic writer."""
    import sqlite3
    from types import SimpleNamespace

    from relaymind.evaluation.checkpoint import Checkpoint, atomic_json

    d = driver()
    runner = load_script_module("run_corpus")
    session = load_script_module("evaluation_session")
    cell = d.matrix_cells("a" * 40, omit_criteria=True)[5]
    cell = {**cell, "requests": 2}
    plan = {**d.make_plan("a" * 40, omit_criteria=True), "cells": [cell], "run_order": [cell["id"]]}
    monkeypatch.setattr(d, "make_plan", lambda *a, **kw: plan)
    source = {"head": "a" * 40, "dirty": False}
    monkeypatch.setattr(d, "measured_source", lambda _: source)
    env = {
        "RELAYMIND_EXTERNAL_BASE_URL": "https://example.invalid/v1",
        "RELAYMIND_EXTERNAL_MODEL": "fixture",
        "RELAYMIND_EXTERNAL_API_KEY": "fixture",
    }
    monkeypatch.setattr(d, "provider_environment", lambda _: dict(env))
    runtime = tmp_path / "llama-server"
    runtime.write_bytes(b"fixture")
    out = tmp_path / "matrix"
    args = [
        "--out",
        str(out),
        "--revision",
        "a" * 40,
        "--omit-criteria",
        "--runtime",
        str(runtime),
        "--execute",
    ]
    state = {"calls": 0, "finish": False, "interrupt_after_finish": False}

    def run(argv, **kwargs):
        state["calls"] += 1
        parsed = runner.argument_parser().parse_args(argv[2:])
        config = d.configuration_from_args(parsed)
        partition = session.prior_configuration(
            None,
            SimpleNamespace(
                base_url=env["RELAYMIND_EXTERNAL_BASE_URL"], model="fixture", context_limit=128000
            ),
            config,
        )
        checkpoint = Checkpoint(
            Path(parsed.checkpoint),
            dict(
                application=source,
                configuration=config.to_json_object(),
                prior_configuration=partition,
                prior=None,
                arguments=vars(parsed),
            ),
        )
        dbpath = Path(parsed.checkpoint).with_suffix(".sqlite3")
        with sqlite3.connect(dbpath) as db:
            db.execute("CREATE TABLE events(run_id TEXT,event_type TEXT,actor TEXT,payload TEXT)")
            db.execute(
                "INSERT INTO events VALUES(?,?,?,?)",
                ("r", "model.started", "external", json.dumps(dict(call_id="c", model="fixture"))),
            )
        checkpoint.start("case/auto")
        if state["finish"]:
            checkpoint.finish("case/auto", {})
            checkpoint.start("case/external_only")
            checkpoint.finish("case/external_only", {})
            atomic_json(
                Path(parsed.out),
                dict(
                    application=source,
                    configuration=config.to_json_object(),
                    prior_configuration=partition,
                    timing_prior=None,
                    grades=[{}, {}],
                ),
            )
            checkpoint.complete(dict(export_sha256=d.byte_digest(Path(parsed.out))))
            if state["interrupt_after_finish"]:
                raise KeyboardInterrupt
        return SimpleNamespace(returncode=0 if state["finish"] else 1)

    monkeypatch.setattr(d.subprocess, "run", run)
    return d, out, args, state


def test_m7_restart_retains_failed_usage_and_requires_new_attempt(tmp_path, monkeypatch):
    d, out, args, state = lifecycle(tmp_path, monkeypatch)
    with pytest.raises(RuntimeError):
        d.main(args)
    with pytest.raises(ValueError, match="explicit"):
        d.main(args)
    state["finish"] = True
    d.main(args + ["--restart", "M7-C5=recovery"])
    attempts = json.loads((out / "index.json").read_text(encoding="utf-8"))["attempts"]["M7-C5"]
    assert state["calls"] == 2
    assert [a["cost"]["calls"] for a in attempts] == [1, 1]
    assert attempts[0]["cost"]["input_tokens_missing_calls"] == 1
    assert attempts[1]["status"] == "complete"
    d.main(args)
    assert state["calls"] == 2


def test_m7_finalized_checkpoint_reconciles_without_repeat(tmp_path, monkeypatch):
    d, out, args, state = lifecycle(tmp_path, monkeypatch)
    state.update(finish=True, interrupt_after_finish=True)
    with pytest.raises(KeyboardInterrupt):
        d.main(args)
    state["interrupt_after_finish"] = False
    d.main(args)
    assert state["calls"] == 1
    assert (
        json.loads((out / "index.json").read_text(encoding="utf-8"))["attempts"]["M7-C5"][0][
            "status"
        ]
        == "complete"
    )
