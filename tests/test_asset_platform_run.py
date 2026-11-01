"""Platform runs on GitHub-hosted runners (#241, #235; RM38, RM39, RM44; AC29, AC37).

The ``platform-runs`` workflow runs the AC29 operations workload and the
packaged installation lifecycle on ``macos-latest``, ``windows-latest`` and
``ubuntu-latest``. Spec 0.65 section 12.6.5: CI runners never satisfy the
Apple Silicon macOS / native x86-64 Windows acceptance, so every report stays
``fixture`` and its gate ``not_applicable``. These tests check the harness
pieces deterministically: the gateway control channel, the predicted import
identities, the workload plan, the operations report round trip through the
existing grader, the platform report and the workflow file.
"""

import asyncio
import hashlib
import json
import math
import sys
from pathlib import Path

import pytest
import yaml

from relaymind.evaluation.asset_operations import (
    BatchSample,
    MemoryObservation,
    RequestSample,
    StartupSample,
    WorkloadSamples,
    _job_interval,
    _validate_plan,
    summarize_operations,
)
from relaymind.evaluation.asset_operations_run import (
    IMPORT_OFFSETS,
    IMPORT_SOURCE_ID,
    MemorySampler,
    development_base_config,
    generate_catalog,
    import_batch_packages,
    load_operations_report,
    memory_observation,
    operations_report,
    ordinary_product_failure,
    platform_report,
    rss_bytes,
    serve_baseline,
    serve_controlled,
    workload_bodies,
    workload_plan,
    write_import,
)
from relaymind.evaluation.asset_platform_export import PLATFORM_STEPS

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/platform-runs.yml"


def _script():
    from helpers import load_script_module

    return load_script_module("asset_platform_run")


# --- import batches and the workload plan ------------------------------------------


def test_import_batches_are_25_distinct_records_with_predicted_identities():
    """AC29: four 25-record batches, 100 distinct identities, fixed bytes."""
    seen = set()
    for batch in range(4):
        packages = import_batch_packages(batch)
        assert len(packages) == 25
        identities = {p.identity for p in packages}
        assert len(identities) == 25
        assert all(p.identity[0].startswith("skill:") and p.identity[2] is None for p in packages)
        assert all(len(p.identity[1]) == 64 for p in packages)
        assert {p.language for p in packages} == {"en", "zh"}
        seen |= identities
        # Deterministic: the same batch has the same bytes and identities.
        assert [p.payload for p in import_batch_packages(batch)] == [p.payload for p in packages]
    assert len(seen) == 100


def test_the_workload_plan_is_the_spec_protocol(tmp_path):
    """Spec 12.6: 120 slots, four batches at fixed offsets below 60 seconds."""
    bodies = workload_bodies()
    assert len(bodies) == 120
    assert {b["selection_scope"] for b in bodies} == {"discovery"}
    assert any(any("一" <= c <= "鿿" for c in b["task_text"]) for b in bodies)
    assert len(IMPORT_OFFSETS) == 4 and all(0 <= o < 60 for o in IMPORT_OFFSETS)

    async def control(op, **fields):
        raise AssertionError("not called while planning")

    plan = workload_plan(tmp_path / "imports", control)
    _validate_plan(bodies, plan.batches)  # The driver's own plan check.
    assert plan.identities() == {"rebuild_job_id": None, "batch_job_ids": {}}
    assert plan.job_evidence() == [] and plan.channel_errors == []
    for batch in plan.batches:
        # Each probe asks for at most the candidate limit.
        assert all(len(p.expected) <= 10 for p in batch.probes)


def test_receiving_a_batch_writes_one_incremental_metadata_import(tmp_path):
    """The import file is schema-valid, adds 25 records and names their originals."""
    from relaymind.assets.sources import _IMPORT_VALIDATOR

    async def control(op, **fields):
        raise AssertionError("not called by receive")

    plan = workload_plan(tmp_path / "imports", control)

    async def go():
        return await plan.batches[1].receive()

    received = asyncio.run(go())
    assert math.isfinite(received)
    data = json.loads((tmp_path / "imports" / "import.json").read_text(encoding="utf-8"))
    assert _IMPORT_VALIDATOR.is_valid(data)
    assert data["complete"] is False and len(data["records"]) == 25
    expected = {p.identity for p in import_batch_packages(1)}
    for entry in data["records"]:
        original = (tmp_path / "imports" / entry["original_path"]).read_bytes()
        record = entry["record"]
        assert (record["asset_id"], record["content_hash"], record["source_version"]) in expected
        assert hashlib.sha256(original).hexdigest() == record["content_hash"]
    assert not (tmp_path / "imports" / "import.json.tmp").exists()


# --- the real gateway with its control channel -------------------------------------


def _env():
    return {"RELAYMIND_OPERATIONS_UNUSED_KEY": "unused"}


def test_a_controlled_gateway_rescans_and_the_predicted_identities_become_visible(tmp_path):
    """RM38/AC29: a received metadata batch through the gateway's own rescan owner."""
    generate_catalog(tmp_path / "catalog", 3)
    base = development_base_config(tmp_path / "base", ROOT)
    imports = tmp_path / "imports"
    write_import(imports, ())

    async def go():
        async with serve_controlled(
            tmp_path / "work", tmp_path / "catalog", base_config=base, imports=imports, env=_env()
        ) as gateway:
            plan = workload_plan(imports, gateway.control)
            batch = plan.batches[0]
            await batch.receive()
            await batch.rescan()
            assert set(plan.batch_job_ids) == {"batch-0"}
            (job,) = [j for j in plan.jobs if j["job_id"] == plan.batch_job_ids["batch-0"]]
            assert job["source_id"] == IMPORT_SOURCE_ID
            assert _job_interval(job, kind="refresh") is not None
            for probe in batch.probes:
                response = await gateway.client.post(
                    "/relay/v1/recommendations",
                    json=dict(probe.body),
                    headers={"Idempotency-Key": probe.body["task_text"]},
                )
                assert response.status_code == 200, response.text
                found = {
                    (c["asset_id"], c["content_hash"], c["source_version"])
                    for c in response.json()["result"]["final_candidates"]
                }
                assert set(probe.expected) <= found
            await plan.rebuild()
            rebuilt = [j for j in plan.jobs if j["job_id"] == plan.identities()["rebuild_job_id"]]
            assert [job["source_id"] for job in rebuilt] == [None]
            assert _job_interval(rebuilt[0], kind="lexical") is not None
            refused = await gateway.control("rescan", source_id="missing")
            assert refused["ok"] is False and refused["error"] == "ValueError"
            return gateway.pid

    pid = asyncio.run(go())
    assert rss_bytes(pid) is None  # stopped and reaped


def test_the_baseline_gateway_runs_without_recommendations(tmp_path):
    """AC29: the memory baseline is a gateway without recommendations."""
    base = development_base_config(tmp_path / "base", ROOT)

    async def go():
        async with serve_baseline(tmp_path / "work", base_config=base, env=_env()) as (
            pid,
            client,
        ):
            ready = await client.get("/relay/v1/recommendations/readiness")
            assert ready.status_code != 200 or ready.json().get("state") != "ready"
            return rss_bytes(pid)

    assert asyncio.run(go()) > 0


def test_the_development_base_config_uses_absolute_paths_only(tmp_path):
    import tomllib

    from relaymind.evaluation.asset_operations_run import _relative_paths

    path = development_base_config(tmp_path / "base", ROOT)
    parsed = tomllib.loads(path.read_text(encoding="utf-8"))
    assert _relative_paths(parsed) == []
    assert not {"server", "recommendations"} & set(parsed)
    assert parsed["external_model"]["api_key_env"] == "RELAYMIND_OPERATIONS_UNUSED_KEY"


def test_the_memory_sampler_keeps_unknown_samples_unknown(monkeypatch):
    import relaymind.evaluation.asset_operations_run as kit

    values = iter([100, None, 300])
    monkeypatch.setattr(kit, "rss_bytes", lambda pid: next(values, 200))

    async def go():
        sampler = MemorySampler(1234, interval=0.001)
        sampler.sample()
        sampler.sample()
        sampler.sample()
        return sampler

    sampler = asyncio.run(go())
    assert sampler.samples == [100, None, 300]
    assert sampler.peak == 300 and sampler.complete is False


# --- the operations report and its grading ------------------------------------------


def _request(ordinal, category="scheduled", outcome="missed"):
    return RequestSample(category, ordinal, ordinal / 2, None, None, None, outcome, None, (), "x")


def _fixture_samples():
    startups = [
        StartupSample(i, 1.0, None, None, (), "failed", "deadline_exceeded") for i in range(10)
    ]
    workload = WorkloadSamples(provenance="fixture", environment={"os": "Linux"})
    workload.origin_at = 0.0
    from datetime import UTC, datetime

    workload.origin_utc = datetime(2026, 11, 1, tzinfo=UTC)
    workload.requests = [_request(i) for i in range(120)]
    workload.batches = [
        BatchSample(
            f"b{i}", 15.0 * i, None, None, "failed", "x", (_request(0, "visibility"),), 25, 0
        )
        for i in range(4)
    ]
    workload.rebuild = {"started_at": 0.0, "finished_at": 0.5, "outcome": "returned"}
    workload.errors = ["visibility_deadline"]
    memory = MemoryObservation(100, 300, "ps_rss", ("gateway",), ("query",), True)
    return startups, workload, memory


def test_an_operations_report_round_trips_through_the_existing_grader():
    """Spec 12.6.5: reuse asset_operations' summary; never a competing calculation."""
    startups, workload, memory = _fixture_samples()
    report = operations_report(
        startups,
        workload,
        memory,
        environment={"os": "Linux"},
        runner={"label": "test"},
        catalog={"assets": 300},
        memory_samples={"baseline_bytes": 100, "samples": [100, 300]},
        harness_errors=[],
    )
    assert report["qualification"] == "fixture" and report["acceptance"] == "not_applicable"
    stored = json.loads(json.dumps(report, default=str))
    loaded = load_operations_report(stored)
    recomputed = json.loads(json.dumps(summarize_operations(*loaded), default=str))
    assert recomputed == stored["summary"]
    assert stored["summary"]["startup"]["passes"] is False


def test_a_report_labelled_measured_by_its_caller_is_refused():
    startups, workload, memory = _fixture_samples()
    workload.provenance = "measured"
    with pytest.raises(ValueError, match="measured_requires_acceptance_platform"):
        operations_report(
            startups,
            workload,
            memory,
            environment={},
            runner={},
            catalog={},
            memory_samples={},
            harness_errors=[],
        )


def _bundle_dir(tmp_path, startups=None):
    startups_, workload, memory = _fixture_samples()
    report = operations_report(
        startups or startups_,
        workload,
        memory,
        environment={"os": "Linux"},
        runner={"label": "test"},
        catalog={"assets": 300},
        memory_samples={},
        harness_errors=[],
    )
    out = tmp_path / "run"
    out.mkdir()
    (out / "operations.json").write_text(json.dumps(report, default=str), encoding="utf-8")
    return out


def test_verify_rejects_a_summary_that_differs_from_its_samples(tmp_path):
    out = _bundle_dir(tmp_path)
    report = json.loads((out / "operations.json").read_text())
    report["summary"]["startup"]["useful"] = 10
    (out / "operations.json").write_text(json.dumps(report))
    assert _script().verify_operations(out / "operations.json") == "summary_mismatch"


def test_verify_accepts_an_unchanged_operations_report(tmp_path):
    out = _bundle_dir(tmp_path)
    assert _script().verify_operations(out / "operations.json") is None


def test_verify_rejects_a_measured_claim(tmp_path):
    out = _bundle_dir(tmp_path)
    report = json.loads((out / "operations.json").read_text())
    report["qualification"] = "measured"
    (out / "operations.json").write_text(json.dumps(report))
    assert _script().verify_operations(out / "operations.json") == "qualification"


# --- the platform report ----------------------------------------------------------------

LIFECYCLE = [
    "version", "isolate-log", "isolate-gateway", "install", "configure", "enable", "inspect",
    "store-token", "discover", "text-exchange", "tool-exchange", "disable", "uninstall",
    "remove-config", "remove-credential",
]  # fmt: skip


def _install_check(status="passed"):
    return {"steps": [{"name": n, "status": status} for n in LIFECYCLE], "passed": True}


def _write(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _platform(tmp_path, *, ordinary=None, operations=None):
    check = _write(tmp_path / "install.json", _install_check())
    paths = {"install-check": "evidence/install"}
    if ordinary is not None:
        ordinary = _write(tmp_path / "ordinary.json", ordinary)
        paths["ordinary-capture"] = "evidence/ordinary"
    if operations is not None:
        operations = _write(tmp_path / "operations.json", operations)
        paths["operations-report"] = "evidence/operations"
    return platform_report(
        install_check=check,
        ordinary=ordinary,
        ordinary_check=(lambda capture: False) if ordinary is not None else None,
        operations=operations,
        evidence_paths=paths,
        input_digest="0" * 64,
        input_manifest=dict(path="inputs.json", digest="1" * 64, bytes=1),
        platform={"os": "Darwin"},
    )


def _steps(report):
    return {s["name"]: s for s in report["steps"]}


def test_skill_use_mcp_use_and_restart_stay_not_run_with_their_reasons(tmp_path):
    """Spec 7.6.1/7.6.2: loaded-use and MCP activation remain disabled."""
    steps = _steps(_platform(tmp_path))
    assert steps["skill_use"]["status"] == "not_run"
    assert steps["skill_use"]["reason"] == "skill_use_not_observed"
    assert (steps["mcp_use"]["status"], steps["mcp_use"]["reason"]) == (
        "not_run",
        "mcp_use_disabled",
    )
    assert (steps["restart"]["status"], steps["restart"]["reason"]) == (
        "not_run",
        "restart_not_observed",
    )


def test_a_linux_only_ordinary_harness_is_recorded_as_such(tmp_path):
    """#235: the person-ingress harness runs only on Linux x86-64 runners."""
    capture = {"status": "failed", "reason": "linux_runner_required", "checks": {}}
    steps = _steps(_platform(tmp_path, ordinary=capture))
    for name in ("recommend", "choose", "feedback", "delete", "reset"):
        assert steps[name]["status"] == "not_run"
        assert steps[name]["reason"] == "ordinary_harness_linux_only"
        assert steps[name]["evidence_ids"] == []


def test_the_operations_report_is_the_dedicated_operations_evidence(tmp_path):
    report = _platform(tmp_path, operations={"kind": "asset-operations-run"})
    entry = report["operations_evidence"]
    assert entry["id"] == "operations-report" and entry["path"] == "evidence/operations"
    assert entry in report["evidence"]
    assert report["qualification"] == "fixture" and report["gate"] == "not_applicable"
    assert [s["name"] for s in report["steps"]] == list(PLATFORM_STEPS)


def test_the_platform_command_writes_a_bundle_the_grader_accepts(tmp_path):
    """Round trip: fixture companion inputs bound to the observed runner (12.6.5)."""
    from relaymind.evaluation.asset_evidence import collect_benchmark_environment

    out = tmp_path / "platform"
    check = _write(tmp_path / "install.json", _install_check())
    operations = _bundle_dir(tmp_path) / "operations.json"
    capture = _write(
        tmp_path / "ordinary.json",
        {"status": "failed", "reason": "linux_runner_required", "checks": {}},
    )
    code = _script().main(
        [
            "platform",
            "--install-check", str(check),
            "--ordinary", str(capture),
            "--operations", str(operations),
            "--out-dir", str(out),
        ]
    )  # fmt: skip
    assert code == 0
    report = json.loads((out / "platform.json").read_text())
    assert report["platform"] == collect_benchmark_environment()
    assert report["operations_evidence"]["id"] == "operations-report"
    assert _script().verify_platform(out) is None


def test_verify_rejects_a_platform_report_whose_evidence_changed(tmp_path):
    out = tmp_path / "platform"
    check = _write(tmp_path / "install.json", _install_check())
    assert _script().main(["platform", "--install-check", str(check), "--out-dir", str(out)]) == 0
    report = json.loads((out / "platform.json").read_text())
    report["steps"][0]["status"] = "failed"
    (out / "platform.json").write_text(json.dumps(report))
    # The grader accepts the shape; re-deriving the steps from the original does not.
    assert _script().verify_platform(out) == "steps_mismatch"


def test_verify_rejects_a_changed_evidence_original(tmp_path):
    out = tmp_path / "platform"
    check = _write(tmp_path / "install.json", _install_check())
    assert _script().main(["platform", "--install-check", str(check), "--out-dir", str(out)]) == 0
    manifest = json.loads((out / "bundle" / "inputs.json").read_text())
    (entry,) = [e for e in manifest["evidence"] if e["id"] == "install-check"]
    (out / "bundle" / entry["path"]).write_text(json.dumps(_install_check("failed")))
    assert _script().verify_platform(out).startswith("grade_exit_")


def test_verify_command_grades_a_downloaded_run_directory(tmp_path, capsys):
    run = tmp_path / "platform-run-ubuntu-latest"
    check = _write(tmp_path / "install.json", _install_check())
    operations = _bundle_dir(tmp_path) / "operations.json"
    run.mkdir()
    (run / "operations.json").write_bytes(operations.read_bytes())
    assert (
        _script().main(
            [
                "platform",
                "--install-check",
                str(check),
                "--operations",
                str(run / "operations.json"),
                "--out-dir",
                str(run / "platform"),
            ]
        )  # fmt: skip
        == 0
    )
    assert _script().main(["verify", str(tmp_path)]) == 0
    assert "platform-run-ubuntu-latest" in capsys.readouterr().out
    (run / "operations.json").write_text("{}")
    assert _script().main(["verify", str(tmp_path)]) == 1


# --- the ordinary installed-host step -----------------------------------------------------


@pytest.mark.parametrize(
    ("code", "reason", "system", "machine", "expected"),
    [
        (0, None, "Linux", "x86_64", 0),
        (2, "person_composition_unverified", "Linux", "x86_64", 2),
        (2, "linux_runner_required", "Darwin", "arm64", 0),
        (2, "linux_runner_required", "Windows", "AMD64", 0),
        # On a Linux x86-64 runner the harness must actually run.
        (2, "linux_runner_required", "Linux", "x86_64", 2),
        (2, "qualification_failed", "Darwin", "arm64", 2),
    ],
)
def test_the_ordinary_step_fails_only_on_harness_errors(code, reason, system, machine, expected):
    capture = {"reason": reason} if reason else {"status": "passed"}
    assert _script().ordinary_exit(code, capture, system=system, machine=machine) == expected


def test_a_missing_ordinary_capture_is_a_harness_error():
    assert _script().ordinary_exit(2, None, system="Darwin", machine="arm64") == 2


# --- the workflow -----------------------------------------------------------------------------


def _workflow():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_the_workflow_runs_on_the_three_hosted_platforms():
    workflow = _workflow()
    triggers = workflow[True]  # YAML 1.1 reads the bare key "on" as true.
    assert "workflow_dispatch" in triggers
    paths = triggers["pull_request"]["paths"]
    for path in (
        ".github/workflows/platform-runs.yml",
        "scripts/asset_platform_run.py",
        "scripts/asset_operations_run.py",
        "src/relaymind/evaluation/asset_operations_run.py",
        "src/relaymind/evaluation/asset_operations_child.py",
    ):
        assert _covered(path, paths), path
    (job,) = workflow["jobs"].values()
    assert job["strategy"]["fail-fast"] is False
    assert set(job["strategy"]["matrix"]["os"]) == {
        "macos-latest",
        "windows-latest",
        "ubuntu-latest",
    }
    assert job["timeout-minutes"] <= 130
    assert workflow["permissions"] == {"contents": "read"}


def test_the_workflow_never_hides_a_failure_and_always_uploads():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "continue-on-error" not in text
    (job,) = _workflow()["jobs"].values()
    steps = job["steps"]
    names = [s.get("name", "") for s in steps]
    for needed in ("Operations workload (AC29)", "Pinned host lifecycle", "Verify"):
        assert any(n.startswith(needed) for n in names), needed
    upload = [s for s in steps if s.get("uses", "").startswith("actions/upload-artifact")]
    assert upload and upload[-1]["if"] == "always()"
    # The report and its grading still run after a failed earlier step.
    for step in steps:
        if step.get("name", "").startswith(("Platform report", "Verify")):
            assert step["if"] == "always()"
    for step in steps:
        if "run" in step and "Install" not in step.get("name", ""):
            assert "timeout-minutes" in step, step.get("name")


def test_the_scripts_run_with_the_interpreter_on_path():
    # The workflow calls the scripts with python; nothing depends on a shebang.
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "python scripts/asset_operations_run.py operations" in text
    assert "python scripts/asset_platform_run.py platform" in text
    assert "python scripts/asset_platform_run.py verify" in text
    assert sys.executable  # the tests themselves run under one


# --- review findings (PR #264) --------------------------------------------------------


def _exiting_gateway(tmp_path, monkeypatch):
    """A child that reports its PID and token, then exits before readiness."""
    import relaymind.evaluation.asset_operations_run as kit

    script = tmp_path / "exiting_gateway.py"
    script.write_text(
        "import os\n"
        "print('Gateway-Pid: ' + str(os.getpid()), flush=True)\n"
        "print('Application token: t', flush=True)\n"
    )
    for name in ("_child_argv", "_controlled_argv"):
        monkeypatch.setattr(kit, name, lambda config: [sys.executable, str(script)])


def test_a_gateway_exit_after_the_token_still_writes_the_operations_report(tmp_path, monkeypatch):
    """RM38/AC29: a harness failure keeps the collected evidence and exits 2."""
    from helpers import load_script_module

    _exiting_gateway(tmp_path, monkeypatch)
    out = tmp_path / "out" / "operations.json"
    code = load_script_module("asset_operations_run").main(
        ["operations", "--work", str(tmp_path / "w"), "--assets", "2", "--output", str(out)]
    )
    assert code == 2
    report = json.loads(out.read_text(encoding="utf-8"))
    assert "baseline_gateway_exited" in report["harness_errors"]
    assert "workload_gateway_exited" in report["harness_errors"]
    assert len(report["startups"]) == 10
    assert all(s["outcome"] == "failed" for s in report["startups"])


def test_memory_phases_exclude_startup_without_its_samples():
    """RM38/AC29 (spec 12.6): phase coverage names only what was sampled."""
    memory = memory_observation(100, [None] * 10, [150, 300])
    assert memory.phases == ("rebuild", "import", "query")
    assert memory.complete is False and memory.peak_bytes == 300


def test_memory_phases_include_startup_and_its_peak_when_all_ten_are_known():
    memory = memory_observation(100, [500] + [120] * 9, [150, 300])
    assert memory.phases == ("startup", "rebuild", "import", "query")
    assert memory.complete is True and memory.peak_bytes == 500
    assert memory.coverage == ("gateway",)


def test_memory_is_incomplete_without_a_baseline_or_workload_samples():
    assert memory_observation(None, [120] * 10, [150]).complete is False
    assert memory_observation(100, [120] * 10, [150, None]).complete is False
    empty = memory_observation(100, [120] * 10, [])
    assert empty.complete is False and empty.phases == ("startup",)


_PRODUCT_FAILURE = {
    "status": "failed",
    "reason": "ordinary_ui_timeout",
    "ordinary_failure_step": "ui_actions",
    "ordinary_ui_action": "recommend",
    "ordinary_error_class": "value_error",
    "checks": {},
}


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({}, "recommend"),
        ({"ordinary_ui_action": "reset"}, "reset"),
        ({"ordinary_error_class": "playwright_timeout"}, "recommend"),
        # A browser crash or another setup step is not a product observation.
        ({"ordinary_error_class": "playwright_target_closed"}, None),
        ({"ordinary_failure_step": "install"}, None),
        ({"ordinary_ui_action": "open"}, None),
        ({"status": "passed"}, None),
    ],
)
def test_an_ordinary_product_failure_is_classified_by_its_action(changes, expected):
    assert ordinary_product_failure(dict(_PRODUCT_FAILURE, **changes)) == expected


def test_a_recorded_ordinary_product_failure_does_not_fail_the_harness():
    """The workflow fails on harness errors only; the failed action is evidence."""
    assert _script().ordinary_exit(2, _PRODUCT_FAILURE, system="Linux", machine="x86_64") == 0
    setup = dict(_PRODUCT_FAILURE, ordinary_failure_step="install")
    assert _script().ordinary_exit(2, setup, system="Linux", machine="x86_64") == 2


def test_the_failed_ordinary_action_is_a_failed_platform_step(tmp_path):
    steps = _steps(
        _platform(tmp_path, ordinary=dict(_PRODUCT_FAILURE, ordinary_ui_action="correct"))
    )
    assert steps["feedback"]["status"] == "failed"
    assert steps["feedback"]["evidence_ids"] == ["ordinary-capture"]
    assert steps["feedback"]["reason"] == "ordinary_correct_failed"
    for name in ("recommend", "choose", "delete", "reset"):
        assert steps[name]["status"] == "not_run"
        assert steps[name]["reason"] == "ordinary_capture_unverified"


# --- review 264 (error boundaries) ------------------------------------------------------


def test_a_missing_install_check_is_recorded_not_built_and_fails_verify(tmp_path, capsys):
    """Review 4435649815: the always-run report step records the missing input."""
    out = tmp_path / "run" / "platform"
    code = _script().main(
        ["platform", "--install-check", str(tmp_path / "missing.json"), "--out-dir", str(out)]
    )
    assert code == 2
    record = json.loads((out / "platform-not-built.json").read_text(encoding="utf-8"))
    assert record == {"status": "not_built", "reason": "install_check_missing"}
    assert not (out / "platform.json").exists()
    assert _script().main(["verify", str(tmp_path / "run")]) == 1
    assert "FAIL (install_check_missing)" in capsys.readouterr().out


def test_platform_refuses_an_existing_bundle_directory(tmp_path, capsys):
    """Review 4435649819: a rerun into the same directory is a clear rejection."""
    out = tmp_path / "platform"
    check = _write(tmp_path / "install.json", _install_check())
    argv = ["platform", "--install-check", str(check), "--out-dir", str(out)]
    assert _script().main(argv) == 0
    assert _script().main(argv) == 2
    assert "platform_out_dir_exists" in capsys.readouterr().err


def test_an_ordinary_timeout_is_a_closed_harness_error(tmp_path, monkeypatch, capsys):
    """Review 4435649825: TimeoutExpired exits 2 with a fixed reason."""
    import subprocess

    module = _script()

    def expire(argv, timeout):
        raise subprocess.TimeoutExpired(argv, timeout)

    monkeypatch.setattr(module.subprocess, "run", expire)
    code = module.main(
        [
            "ordinary", "--tarball", "t.tgz", "--work-dir", str(tmp_path / "w"),
            "--out", str(tmp_path / "ordinary.json"), "--timeout", "1",
        ]
    )  # fmt: skip
    assert code == 2
    assert "ordinary_timeout" in capsys.readouterr().out


def _ordinary_argv(tmp_path, out):
    return [
        "ordinary", "--tarball", "t.tgz", "--work-dir", str(tmp_path / "w"),
        "--out", str(out), "--timeout", "5",
    ]  # fmt: skip


def test_a_rerun_never_reads_a_stale_ordinary_capture(tmp_path, monkeypatch, capsys):
    """Review 264: a setup failure before any write is a harness error (exit 2),
    even when an earlier attempt left a product-failure capture at --out."""
    import subprocess

    module = _script()
    out = tmp_path / "ordinary.json"
    out.write_text(json.dumps(_PRODUCT_FAILURE), encoding="utf-8")

    def setup_failure(argv, timeout):
        return subprocess.CompletedProcess(argv, 2)  # exits before writing a capture

    monkeypatch.setattr(module.subprocess, "run", setup_failure)
    assert module.main(_ordinary_argv(tmp_path, out)) == 2
    assert "capture_missing" in capsys.readouterr().out
    # The stale capture is gone, so the platform step cannot pick it up.
    assert not out.exists()


def test_the_current_attempt_capture_is_graded(tmp_path, monkeypatch):
    import subprocess

    module = _script()
    out = tmp_path / "ordinary.json"
    out.write_text(json.dumps({"reason": "stale"}), encoding="utf-8")

    def product_failure(argv, timeout):
        Path(argv[argv.index("--out") + 1]).write_text(json.dumps(_PRODUCT_FAILURE))
        return subprocess.CompletedProcess(argv, 2)

    monkeypatch.setattr(module.subprocess, "run", product_failure)
    monkeypatch.setattr(module.platform, "system", lambda: "Linux")
    monkeypatch.setattr(module.platform, "machine", lambda: "x86_64")
    assert module.main(_ordinary_argv(tmp_path, out)) == 0
    assert json.loads(out.read_text()) == _PRODUCT_FAILURE


def test_describe_operations_tolerates_null_memory_samples(tmp_path):
    """Review 4435649829."""
    out = _bundle_dir(tmp_path)
    report = json.loads((out / "operations.json").read_text())
    report["memory_samples"] = None
    (out / "operations.json").write_text(json.dumps(report))
    assert "memory increment None" in _script()._describe_operations(out / "operations.json")


def test_platform_input_errors_exit_2_without_a_traceback(tmp_path, capsys):
    """Review 4435649834: the same boundary as asset_operations_run.py."""
    check = _write(tmp_path / "install.json", _install_check())
    code = _script().main(
        [
            "platform", "--install-check", str(check),
            "--operations", str(tmp_path / "missing-operations.json"),
            "--out-dir", str(tmp_path / "platform"),
        ]
    )  # fmt: skip
    assert code == 2
    assert capsys.readouterr().err.startswith("asset_platform_run: ")


class _Store:
    def __init__(self, fail_after=None):
        self.calls = 0
        self.fail_after = fail_after

    def job_outcomes(self):
        self.calls += 1
        if self.fail_after is not None and self.calls > self.fail_after:
            raise OSError("database is locked")
        return ()

    def job_events(self, job_id):
        return ()


class _Catalog:
    def __init__(self, store):
        self.store = store

    async def rescan(self, source_id):
        from relaymind.store.asset_catalog import Publication

        return Publication(1, False, (), ())


def _app(store):
    from types import SimpleNamespace

    return SimpleNamespace(state=SimpleNamespace(asset_catalog=_Catalog(store)))


@pytest.mark.parametrize("fail_after", [0, 1])
def test_the_child_answers_even_when_the_job_store_fails(fail_after):
    """Review 4435649841: every command gets a reply, never a 60-second wait."""
    from relaymind.evaluation.asset_operations_child import handle

    reply = asyncio.run(
        handle(_app(_Store(fail_after)), {"id": 7, "op": "rescan", "source_id": "s"})
    )
    assert reply == {
        "id": 7,
        "ok": False,
        "error": "job_evidence_unavailable",
        "evidence_error": "OSError",
    }


def test_the_stdin_reader_never_calls_a_closed_loop():
    """Review 4435649852: lines or EOF after shutdown are dropped quietly."""
    import io

    from relaymind.evaluation.asset_operations_child import forward_lines

    loop = asyncio.new_event_loop()
    queue = asyncio.Queue()
    loop.close()
    forward_lines(io.StringIO("one\ntwo\n"), loop, queue)  # must not raise
    assert queue.empty()


def test_the_stdin_reader_forwards_lines_then_eof():
    import io

    from relaymind.evaluation.asset_operations_child import forward_lines

    async def go():
        queue = asyncio.Queue()
        await asyncio.to_thread(
            forward_lines, io.StringIO("a\nb\n"), asyncio.get_running_loop(), queue
        )
        await asyncio.sleep(0)
        return [queue.get_nowait() for _ in range(queue.qsize())]

    assert asyncio.run(go()) == ["a\n", "b\n", None]


# --- review 264 (second review) ---------------------------------------------------------


def test_verify_fails_an_operations_report_with_harness_errors(tmp_path, capsys):
    """RM38/AC29 (review 264 H2): a run that exited 2 never verifies as ok."""
    out = _bundle_dir(tmp_path)
    report = json.loads((out / "operations.json").read_text())
    report["harness_errors"] = ["baseline_gateway_exited"]
    (out / "operations.json").write_text(json.dumps(report))
    assert _script().verify_operations(out / "operations.json") == "harness_errors"
    assert _script().main(["verify", str(out)]) == 1
    line = capsys.readouterr().out
    assert "FAIL" in line and "baseline_gateway_exited" in line


def test_verify_rejects_a_non_list_harness_error_field(tmp_path):
    out = _bundle_dir(tmp_path)
    report = json.loads((out / "operations.json").read_text())
    report["harness_errors"] = "none"
    (out / "operations.json").write_text(json.dumps(report))
    assert _script().verify_operations(out / "operations.json") == "invalid_report"


def _platform_run(tmp_path) -> Path:
    run = tmp_path / "run"
    check = _write(tmp_path / "install.json", _install_check())
    argv = ["platform", "--install-check", str(check), "--out-dir", str(run / "platform")]
    assert _script().main(argv) == 0
    return run


def test_verify_fails_a_platform_report_without_its_bundle(tmp_path, capsys):
    """RM39/AC37 (review 264 M2): a partial download is a failure, never skipped."""
    import shutil

    run = _platform_run(tmp_path)
    shutil.rmtree(run / "platform" / "bundle")
    assert _script().main(["verify", str(run)]) == 1
    assert "FAIL (platform_bundle_missing)" in capsys.readouterr().out


def test_verify_fails_a_bundle_without_its_platform_report(tmp_path, capsys):
    run = _platform_run(tmp_path)
    (run / "platform" / "platform.json").unlink()
    assert _script().main(["verify", str(run)]) == 1
    assert "FAIL (platform_report_missing)" in capsys.readouterr().out


def test_verify_passes_a_complete_platform_run(tmp_path, capsys):
    run = _platform_run(tmp_path)
    assert _script().main(["verify", str(run)]) == 0
    assert "FAIL" not in capsys.readouterr().out


def test_the_workflow_keeps_time_for_the_always_steps():
    """Review 264 M3: every step has its own limit and they fit inside the job's."""
    (job,) = _workflow()["jobs"].values()
    limits = [step.get("timeout-minutes") for step in job["steps"]]
    assert None not in limits, [s.get("name", s.get("uses")) for s in job["steps"]]
    assert sum(limits) < job["timeout-minutes"]


def _covered(path: str, patterns) -> bool:
    from fnmatch import fnmatchcase

    # GitHub's ``**`` matches any number of directories; fnmatch's ``*`` crosses ``/``.
    return any(fnmatchcase(path, p.replace("**", "*")) for p in patterns)


def test_the_path_filter_covers_the_platform_runner_inventory():
    """Review 264 M5/M6: a change to any bound runner file triggers the platform run."""
    module = _script()
    paths = _workflow()[True]["pull_request"]["paths"]
    files = module.platform_runner_files()
    for needed in (
        "scripts/asset_platform_run.py",
        "scripts/asset_operations_run.py",
        "scripts/asset_whole_task.py",
        ".github/workflows/platform-runs.yml",
        "pyproject.toml",
        "integrations/openclaw/test/host-ordinary-qualification/qualify.py",
        "integrations/openclaw/test/host-action-qualification/qualify.py",
        "integrations/openclaw/test/host-person-qualification/qualify.py",
        "tests/test_asset_whole_task_inputs.py",
        "tests/whole_task_helpers.py",
        "src/relaymind/evaluation/asset_operations_run.py",
    ):
        assert needed in files, needed
    for path in [*files, "docs/SPECIFICATION.md", "integrations/openclaw/package.json"]:
        assert _covered(path, paths), path
    # Still a filter: an unrelated change does not start the three-platform run.
    assert not _covered("docs/ROADMAP.md", paths)


def _runner_files(platform_dir: Path) -> tuple[dict, Path]:
    manifest = json.loads((platform_dir / "bundle" / "inputs.json").read_text())
    (entry,) = [e for e in manifest["evidence"] if e["id"] == "platform-runner-files"]
    path = platform_dir / "bundle" / entry["path"]
    payload = path.read_bytes()
    assert (entry["sha256"], entry["bytes"]) == (hashlib.sha256(payload).hexdigest(), len(payload))
    return json.loads(payload), path


def test_the_platform_bundle_binds_the_files_the_platform_run_executes(tmp_path):
    """Review 264 M5: registered evidence digests the actual driver and ordinary harness."""
    run = _platform_run(tmp_path)
    inventory, _path = _runner_files(run / "platform")
    bound = {f["path"]: f["sha256"] for f in inventory["files"]}
    assert sorted(bound) == _script().platform_runner_files()
    for path, digest in bound.items():
        assert digest == hashlib.sha256((ROOT / path).read_bytes()).hexdigest(), path
    report = json.loads((run / "platform" / "platform.json").read_text())
    manifest = json.loads((run / "platform" / "bundle" / "inputs.json").read_text())
    from relaymind.canonical import canonical_digest

    # The report binds the manifest, and the manifest binds the runner files.
    assert report["input_manifest"]["digest"] == canonical_digest(manifest)
    assert _script().verify_platform(run / "platform") is None


def test_verify_fails_a_platform_run_from_other_runner_files(tmp_path):
    """Review 264 M5: verify re-derives with this checkout's driver, so it must match."""
    run = _platform_run(tmp_path)
    inventory, path = _runner_files(run / "platform")
    inventory["files"][0]["sha256"] = "0" * 64
    path.write_text(json.dumps(inventory, indent=1) + "\n", encoding="utf-8")
    module = _script()
    assert module._check_runner_files(run / "platform") == "platform_runner_files_changed"
    path.unlink()
    assert module._check_runner_files(run / "platform") == "platform_runner_files_missing"
    assert module.verify_platform(run / "platform") is not None


# --- review 264 (third review): workflow ordering, timeouts and checkout bytes ---------


def _step(job, prefix: str) -> dict:
    (step,) = [s for s in job["steps"] if s.get("name", "").startswith(prefix)]
    return step


def test_an_operations_harness_error_never_skips_the_packaged_lifecycle():
    """RM38/RM39, AC29/AC37: the lifecycle evidence is built even after an
    operations harness error, and that error still fails the job at the end."""
    (job,) = _workflow()["jobs"].values()
    names = [s.get("name", s.get("uses", "")) for s in job["steps"]]
    operations = _step(job, "Operations workload")
    script = operations["run"]
    assert "set +e" in script and "operations.exit" in script
    # The exit code is kept, and a later always-run step fails on it.
    (final,) = [
        s for s in job["steps"] if "operations.exit" in s.get("run", "") and s is not operations
    ]
    assert final.get("if") == "always()"
    assert names.index(final["name"]) > names.index(_step(job, "Pinned host lifecycle")["name"])


def test_the_ordinary_timeout_fires_inside_its_step():
    """RM39/AC37: the script's own ordinary_timeout is reached before the step limit."""
    import re

    (job,) = _workflow()["jobs"].values()
    step = _step(job, "Ordinary installed-host actions")
    match = re.search(r"--timeout\s+(\d+)", step["run"])
    seconds = float(match.group(1)) if match else 900.0
    assert seconds + 60 <= step["timeout-minutes"] * 60


def test_every_platform_runner_file_checks_out_with_lf_line_endings():
    """RM44/AC37: a Windows checkout binds the same runner file bytes as any other,
    so a downloaded windows-latest report verifies on a macOS or Linux checkout."""
    import shutil
    import subprocess

    if shutil.which("git") is None or not (ROOT / ".git").exists():
        pytest.skip("needs a git checkout")
    files = _script().platform_runner_files()
    result = subprocess.run(
        ["git", "-C", str(ROOT), "check-attr", "eol", "--", *files],
        capture_output=True,
        text=True,
        check=True,
    )
    unset = [line for line in result.stdout.splitlines() if not line.endswith(": eol: lf")]
    assert not unset, unset
