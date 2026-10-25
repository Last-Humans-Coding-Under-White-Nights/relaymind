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


def lifecycle_fixture(tmp_path, monkeypatch, runner_kind="corpus"):
    """Run the real driver and checkpoint writer; replace only the inference subprocess."""
    import json
    import sqlite3
    from pathlib import Path
    from types import SimpleNamespace

    from relaymind.evaluation.checkpoint import Checkpoint, atomic_json

    driver = load_script_module("run_m8_matrix")
    runner = load_script_module("run_corpus" if runner_kind == "corpus" else "latency_benchmark")
    session = load_script_module("evaluation_session")
    config_module = load_script_module("evaluation_config")
    cell = dict(
        id="offline-cell",
        phase="streaming",
        configuration="none",
        policy="local_first",
        chain="example",
        stream="buffered",
        runner=runner_kind,
        requests=2,
    )
    source = {"head": "reviewed-fixture", "dirty": False}
    monkeypatch.setattr(driver, "matrix_cells", lambda: [cell])
    monkeypatch.setattr(driver, "source_fingerprint", lambda root: source)
    env = {
        "RELAYMIND_EXTERNAL_BASE_URL": "https://example.invalid/v1",
        "RELAYMIND_EXTERNAL_MODEL": "fixture-remote",
        "RELAYMIND_EXTERNAL_API_KEY": "fixture-secret",
    }
    monkeypatch.setattr(driver, "provider_environment", lambda path: dict(env))
    runtime = tmp_path / "llama-server"
    runtime.write_bytes(b"declared offline fixture; never executed")
    out = tmp_path / "matrix"
    arguments = ["--out", str(out), "--runtime", str(runtime), "--execute"]
    state = {"calls": 0, "finish": True}

    def fake_process(argv, **kwargs):
        state["calls"] += 1
        captured = {}

        class Parsed(Exception):
            pass

        def capture(args):
            captured.update(vars(args))
            raise Parsed

        with monkeypatch.context() as patch:
            patch.setattr(
                runner, "run_corpus" if runner_kind == "corpus" else "run_benchmark", capture
            )
            with pytest.raises(Parsed):
                runner.main(argv[2:])
        args = SimpleNamespace(**captured)
        config = config_module.configuration_from_args(args)
        external = SimpleNamespace(
            base_url=env["RELAYMIND_EXTERNAL_BASE_URL"],
            model=env["RELAYMIND_EXTERNAL_MODEL"],
            context_limit=128000,
        )
        partition = session.prior_configuration(None, external, config)
        identity = dict(
            application=source,
            configuration=config.to_json_object(),
            prior_configuration=partition,
            prior=None,
            arguments=captured,
        )
        checkpoint = Checkpoint(Path(args.checkpoint), identity)
        work = Path(args.checkpoint).with_suffix(".work")
        work.mkdir()
        with sqlite3.connect(work / "runs.sqlite3") as db:
            db.execute("CREATE TABLE events(run_id TEXT,event_type TEXT,actor TEXT,payload TEXT)")
            db.execute(
                "INSERT INTO events VALUES(?,?,?,?)",
                (
                    "r",
                    "model.started",
                    "external",
                    json.dumps({"call_id": "c", "model": "fixture-remote", "purpose": "final"}),
                ),
            )
        checkpoint.start("case/auto")
        if state["finish"]:
            checkpoint.finish("case/auto", {"state": "completed"})
            checkpoint.start("case/external_only")
            checkpoint.finish("case/external_only", {"state": "failed"})
            doc = dict(
                kind="relaymind.correctness_corpus",
                application=source,
                configuration=config.to_json_object(),
                prior_configuration=partition,
                timing_prior=None,
                grades=[{}, {}],
            )
            if runner_kind == "latency":
                # Match the real runner's effective export, not its raw checkpoint settings.
                doc["configuration"]["placement"]["warmup"] = (
                    config.local_model_configured and not args.no_startup_warmup
                )
                doc.pop("grades")
                doc["environment"] = {"application": doc.pop("application")}
                doc.update(kind="relaymind.latency_benchmark", samples=[{}], warmups=[])
            atomic_json(Path(args.out), doc)
            checkpoint.complete({"export_sha256": driver.byte_digest(Path(args.out)), "grades": 2})
        if state.get("interrupt"):
            raise KeyboardInterrupt("offline injected interruption")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(driver.subprocess, "run", fake_process)
    return driver, out, arguments, state


@pytest.mark.parametrize("runner_kind", ["corpus", "latency"])
@pytest.mark.parametrize("boundary", ["runner_finalized", "before_index", "after_index"])
def test_driver_recovers_finalized_cell_without_repeating_inference(
    tmp_path, monkeypatch, boundary, runner_kind
):
    """RM16/RM25: final runner evidence survives interruption on either index-write side."""
    import json

    driver, out, args, state = lifecycle_fixture(tmp_path, monkeypatch, runner_kind)
    original_write = driver.atomic_json
    if boundary == "runner_finalized":
        state["interrupt"] = True
    else:

        def interrupt_index(path, value):
            if (
                path.name == "index.json"
                and value["attempts"]["offline-cell"][-1]["status"] == "complete"
            ):
                if boundary == "after_index":
                    original_write(path, value)
                raise KeyboardInterrupt("index publication")
            original_write(path, value)

        monkeypatch.setattr(driver, "atomic_json", interrupt_index)
    with pytest.raises(KeyboardInterrupt):
        driver.main(args)
    monkeypatch.setattr(driver, "atomic_json", original_write)
    state["interrupt"] = False
    driver.main(args + ["--restart", "offline-cell=explicit-recovery"])
    index = json.loads((out / "index.json").read_text())
    attempts = index["attempts"]["offline-cell"]
    assert state["calls"] == 1
    assert len(attempts) == 1
    assert attempts[0]["status"] == "complete"
    assert attempts[0]["cost"]["calls"] == 1
    assert attempts[0]["requests"] == 2


def test_driver_hard_exit_recovers_old_cost_before_explicit_restart(tmp_path, monkeypatch):
    """RM25: crash recovery reopens retained SQLite, even when no exception handler ran."""
    import json

    driver, out, args, state = lifecycle_fixture(tmp_path, monkeypatch)
    original_write = driver.atomic_json
    state.update(finish=False, interrupt=True)

    def hard_exit(path, value):
        if (
            path.name == "index.json"
            and value["attempts"]["offline-cell"][-1]["status"] == "interrupted"
        ):
            raise SystemExit("simulated process death before interruption could be recorded")
        original_write(path, value)

    monkeypatch.setattr(driver, "atomic_json", hard_exit)
    with pytest.raises(SystemExit):
        driver.main(args)
    index = json.loads((out / "index.json").read_text())
    assert "cost" not in index["attempts"]["offline-cell"][0]
    monkeypatch.setattr(driver, "atomic_json", original_write)
    # Conservative resume still refuses repeating a partial cell, but publishes recovered cost.
    with pytest.raises(ValueError, match="restart"):
        driver.main(args)
    index = json.loads((out / "index.json").read_text())
    assert index["attempts"]["offline-cell"][0]["cost"]["calls"] == 1
    state.update(finish=True, interrupt=False)
    driver.main(args + ["--restart", "offline-cell=recovery"])
    attempts = json.loads((out / "index.json").read_text())["attempts"]["offline-cell"]
    assert state["calls"] == 2
    assert [a["status"] for a in attempts] == ["interrupted", "complete"]
    assert [a["cost"]["calls"] for a in attempts] == [1, 1]
    assert attempts[0]["cost"]["input_tokens_missing_calls"] == 1


@pytest.mark.parametrize("change", ["arguments", "application", "digest", "count"])
def test_driver_rejects_changed_finalized_evidence_before_restart(tmp_path, monkeypatch, change):
    """RM25: reconciliation does not trust a foreign checkpoint or altered export."""
    import json

    driver, out, args, state = lifecycle_fixture(tmp_path, monkeypatch)
    state["interrupt"] = True
    with pytest.raises(KeyboardInterrupt):
        driver.main(args)
    path = out / "offline-cell" / "initial" / "checkpoint.json"
    document = json.loads(path.read_text())
    if change == "arguments":
        document["identity"]["arguments"]["external_model"] = "wrong"
    elif change == "application":
        document["identity"]["application"] = {"head": "wrong", "dirty": False}
    elif change == "digest":
        document["result"]["export_sha256"] = "0" * 64
    else:
        document["steps"].pop()
    driver.atomic_json(path, document)
    state["interrupt"] = False
    with pytest.raises(ValueError):
        driver.main(args + ["--restart", "offline-cell=recovery"])
    assert state["calls"] == 1


def test_reconciliation_checks_latency_initial_prior_report(tmp_path, monkeypatch):
    """RM25/RM27: latency's retained initial prerequisites accompany the same prior."""
    import json

    from relaymind.evaluation.prior import load_prior
    from relaymind.store import RunStore

    session = load_script_module("evaluation_session")
    driver, out, args, state = lifecycle_fixture(tmp_path, monkeypatch, "latency")
    driver.main(args)
    cell = driver.matrix_cells()[0]
    directory = out / cell["id"] / "initial"
    checkpoint_path, export_path = directory / "checkpoint.json", directory / "export.json"
    checkpoint = json.loads(checkpoint_path.read_text())
    export = json.loads(export_path.read_text())
    prior_path = tmp_path / "prior.json"
    driver.atomic_json(
        prior_path,
        {
            "schema_version": 1,
            "kind": "relaymind.placement_prior",
            "partitions": [
                {
                    "configuration": checkpoint["identity"]["prior_configuration"],
                    "include_outcomes": False,
                    "sources": [],
                }
            ],
        },
    )
    prior = load_prior(prior_path, checkpoint["identity"]["prior_configuration"])
    with RunStore(":memory:") as store:
        prior.seed(store)
        export["timing_prior"] = {
            **prior.provenance,
            "initial_expected_wait_prerequisites": session.prerequisites(prior, store),
        }
    checkpoint["identity"]["prior"] = prior.provenance
    checkpoint["identity"]["arguments"]["timing_samples"] = str(prior_path)
    driver.atomic_json(export_path, export)
    checkpoint["result"]["export_sha256"] = driver.byte_digest(export_path)
    driver.atomic_json(checkpoint_path, checkpoint)
    attempt = json.loads((out / "index.json").read_text())["attempts"][cell["id"]][0]
    attempt.pop("export_sha256")
    attempt.pop("checkpoint_sha256", None)
    attempt["argv"] += ["--timing-samples", str(prior_path)]
    attempt["status"] = "started"
    driver.reconcile_attempt(
        attempt, cell, directory, attempt["argv"], checkpoint["identity"]["application"]
    )
    assert attempt["status"] == "complete"
    assert state["calls"] == 1
    export["timing_prior"]["retained_timings"] += 1
    driver.atomic_json(export_path, export)
    checkpoint["result"]["export_sha256"] = driver.byte_digest(export_path)
    driver.atomic_json(checkpoint_path, checkpoint)
    attempt.pop("export_sha256")
    attempt.pop("checkpoint_sha256")
    with pytest.raises(ValueError, match="identity"):
        driver.reconcile_attempt(
            attempt, cell, directory, attempt["argv"], checkpoint["identity"]["application"]
        )


@pytest.mark.parametrize(
    "cell",
    [c for c in load_script_module("run_m8_matrix").matrix_cells() if c["runner"] == "latency"],
    ids=lambda c: c["id"],
)
def test_every_latency_command_reconciles_the_real_runner_export(tmp_path, monkeypatch, cell):
    """RM25: all fourteen latency cells use the real export/launch normalization, offline."""
    import json
    import tomllib
    from types import SimpleNamespace

    from launcher_helpers import make_artifact

    driver = load_script_module("run_m8_matrix")
    runner = load_script_module("latency_benchmark")
    source = {"head": "declared-offline-test-fixture", "dirty": False}
    monkeypatch.setattr(runner.session, "source_fingerprint", lambda root: source)
    env = {
        "RELAYMIND_EXTERNAL_BASE_URL": "https://example.invalid/v1",
        "RELAYMIND_EXTERNAL_MODEL": "fixture-remote",
    }
    monkeypatch.setenv(runner.harness.EXTERNAL_KEY_ENV, "fixture-secret")
    attempt_dir = tmp_path / cell["id"] / "initial"
    argv = driver.command(cell, attempt_dir, tmp_path, env, tmp_path / "llama-server")
    args = runner.argument_parser().parse_args(argv[2:])
    raw = runner.configuration_from_args(args)
    runtime = None
    if raw.local_model_configured:
        manifest, artifact = make_artifact(tmp_path)
        runtime = runner.harness.Runtime(
            manifest,
            runner.harness.load_manifest(manifest),
            artifact,
            "fixture runtime",
            tuple(args.runtime_extra_args),
        )

    def checked_runtime(*unused, **options):
        assert runtime is not None
        assert tuple(options["extra_args"]) == runtime.extra_args
        return runtime

    def launch(self, log):
        log.write_text("")
        return None, "http://127.0.0.1:1", 0.0

    monkeypatch.setattr(runner.harness, "check_runtime", checked_runtime)
    monkeypatch.setattr(runner.harness.Runtime, "launch", launch)
    monkeypatch.setattr(runner.harness, "_free_port", lambda: 8099)
    monkeypatch.setattr(runner, "_props", lambda url: None)
    monkeypatch.setattr(runner, "runtime_devices", lambda: None)
    monkeypatch.setattr(runner, "machine_manifest", lambda: {})
    launch_warmup = []

    class Gateway:
        def __init__(self, config, *unused):
            launch_warmup.append(tomllib.loads(config.read_text())["placement"].get("warmup", True))

        def start(self):
            return SimpleNamespace(url="http://127.0.0.1:1", token="fixture", recorded_lines=[])

        def stop(self):
            pass

    monkeypatch.setattr(runner.harness, "GatewayProcess", Gateway)

    async def samples(api, prompts, *, checkpoint, **options):
        assert options["seed"] == 1342026
        rows = []
        for index in range(cell["requests"]):
            value = {
                "label": "simple",
                "state": "completed",
                "model_calls": [],
                "error": None,
                "timed_out": False,
            }
            checkpoint.start(str(index))
            checkpoint.finish(str(index), value)
            rows.append(value)
        return {"cold": rows[0], "warmups": rows[1:4], "measured": rows[4:]}

    monkeypatch.setattr(runner, "run_samples", samples)
    if args.timing_samples:
        external = runner.harness.ExternalEndpoint(
            args.external_base_url, args.external_model, args.external_context_limit
        )
        partition = runner.session.prior_configuration(runtime, external, raw)
        # Empty, explicitly declared offline fixture; never a measured calibration source.
        driver.atomic_json(
            tmp_path / ("prior-" + cell["configuration"] + ".json"),
            {
                "schema_version": 1,
                "kind": "relaymind.placement_prior",
                "partitions": [
                    {"configuration": partition, "include_outcomes": False, "sources": []}
                ],
            },
        )
    document = runner.run_benchmark(args)
    checkpoint_path = attempt_dir / "checkpoint.json"
    checkpoint = json.loads(checkpoint_path.read_text())
    assert checkpoint["identity"]["configuration"] == raw.to_json_object()
    assert "warmup" not in checkpoint["identity"]["configuration"]["placement"]
    assert document["configuration"]["placement"]["warmup"] is raw.local_model_configured
    assert launch_warmup == [raw.local_model_configured]
    attempt = {"name": "initial", "status": "started", "argv": argv}
    driver.reconcile_attempt(attempt, cell, attempt_dir, argv, source)
    assert attempt["status"] == "complete"
    assert attempt["requests"] == 124
