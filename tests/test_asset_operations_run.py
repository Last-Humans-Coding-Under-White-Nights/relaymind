"""Operations and platform run kit (#242; RM38, RM39, RM44; AC29, AC37).

Development tooling only: real ``relaymind serve`` processes, a generated
catalog and an RSS sampler feed the existing AC29 drivers and grader, and an
installed-plugin lifecycle record becomes an ``AssetPlatformReportV1``. Runs
on CI or a developer machine are never the macOS/native Windows acceptance.
"""

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from relaymind.evaluation.asset_operations_run import (
    generate_catalog,
    platform_report,
    rss_bytes,
    serve,
)
from relaymind.evaluation.asset_platform_export import PLATFORM_STEPS
from relaymind.evaluation.asset_whole_task import validate_whole_task

ROOT = Path(__file__).resolve().parents[1]


def test_generated_catalog_has_distinct_bilingual_skills(tmp_path):
    paths = generate_catalog(tmp_path / "catalog", 6)
    assert len(paths) == 6 and len({p.parent.name for p in paths}) == 6
    texts = [p.read_text(encoding="utf-8") for p in paths]
    assert all(t.startswith("---\nname: ") for t in texts)
    assert any(any("一" <= c <= "鿿" for c in t) for t in texts)


def test_generated_catalog_can_grow_without_renaming(tmp_path):
    first = generate_catalog(tmp_path / "catalog", 3)
    more = generate_catalog(tmp_path / "catalog", 2, start=3)
    assert not {p.parent.name for p in first} & {p.parent.name for p in more}


@pytest.mark.skipif(sys.platform == "win32", reason="ps-based sampler is POSIX only")
def test_rss_of_this_process_is_positive():
    assert rss_bytes(os.getpid()) > 0


def test_rss_of_a_missing_process_is_unknown():
    assert rss_bytes(2**22 + 12345) is None


def test_a_real_gateway_serves_lexical_recommendations(tmp_path):
    from launcher_helpers import write_config
    from test_launcher import EXTERNAL

    generate_catalog(tmp_path / "catalog", 3)
    base = write_config(tmp_path, EXTERNAL, local=False).read_text()

    async def go():
        async with serve(
            tmp_path / "work",
            tmp_path / "catalog",
            base_config=base,
            env={"TEST_EXTERNAL_KEY": "unused"},
        ) as (pid, client):
            assert pid > 0
            ready = await client.get("/relay/v1/recommendations/readiness")
            assert ready.status_code == 200
            created = await client.post(
                "/relay/v1/recommendations",
                json=dict(
                    schema_version=1,
                    task_text="greeting",
                    requested_kinds=["skill"],
                    selection_scope="discovery",
                ),
                headers={"Idempotency-Key": "k"},
            )
            assert created.status_code == 200, created.text
            return pid

    pid = asyncio.run(go())
    assert rss_bytes(pid) is None  # the process is stopped on exit


def install_check(statuses=None):
    statuses = statuses or {}
    names = [
        "version",
        "isolate-log",
        "isolate-gateway",
        "install",
        "configure",
        "enable",
        "inspect",
        "store-token",
        "discover",
        "text-exchange",
        "tool-exchange",
        "disable",
        "uninstall",
        "remove-config",
        "remove-credential",
    ]
    return dict(
        kind="openclaw-install-check",
        passed=True,
        leftovers=[],
        steps=[dict(name=n, status=statuses.get(n, "passed"), exit_code=0) for n in names],
    )


def ordinary():
    return dict(
        checks=dict(ordinary_actions=True, ordinary_skill=True),
        ordinary_actions=dict(
            ui=dict(
                snapshots={k: {} for k in ("recommend", "choice", "correction", "delete", "reset")}
            )
        ),
        ordinary_skill=dict(receipt=dict(ok=True)),
    )


def write(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def test_lifecycle_records_become_a_closed_platform_report(tmp_path):
    report = platform_report(
        install_check=write(tmp_path / "install.json", install_check()),
        ordinary=write(tmp_path / "ordinary.json", ordinary()),
        input_digest="a" * 64,
        input_manifest=dict(path="inputs.json", digest="b" * 64, bytes=1),
        platform=json.loads((ROOT / "tests/fixtures/asset-benchmark/environment.json").read_text()),
    )
    validate_whole_task("AssetPlatformReportV1", report)
    steps = {s["name"]: s["status"] for s in report["steps"]}
    assert [s["name"] for s in report["steps"]] == list(PLATFORM_STEPS)
    assert steps["install"] == steps["uninstall"] == steps["skill_use"] == "passed"
    assert steps["mcp_use"] == steps["restart"] == "not_run"
    assert report["qualification"] == "fixture" and report["gate"] == "not_applicable"
    ids = {e["id"] for e in report["evidence"]}
    assert all(set(s["evidence_ids"]) <= ids for s in report["steps"])


def test_a_failed_cleanup_step_fails_uninstall(tmp_path):
    report = platform_report(
        install_check=write(tmp_path / "install.json", install_check({"remove-config": "failed"})),
        ordinary=None,
        input_digest="a" * 64,
        input_manifest=dict(path="inputs.json", digest="b" * 64, bytes=1),
        platform=json.loads((ROOT / "tests/fixtures/asset-benchmark/environment.json").read_text()),
    )
    steps = {s["name"]: s["status"] for s in report["steps"]}
    assert steps["uninstall"] == "failed"
    assert steps["recommend"] == steps["skill_use"] == "not_run"


def test_a_measured_claim_is_refused_by_the_kit(tmp_path):
    with pytest.raises(ValueError, match="measured"):
        platform_report(
            install_check=write(tmp_path / "install.json", install_check()),
            ordinary=None,
            input_digest="a" * 64,
            input_manifest=dict(path="inputs.json", digest="b" * 64, bytes=1),
            platform={},
            qualification="measured",
        )


def test_the_kit_owns_the_server_and_source_tables(tmp_path):
    async def go():
        async with serve(tmp_path / "w", tmp_path, base_config="[server]\nport = 1\n"):
            pass

    with pytest.raises(ValueError, match="base_config_owns_kit_tables"):
        asyncio.run(go())


def _script():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "asset_operations_run_script", ROOT / "scripts/asset_operations_run.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_platform_command_writes_a_valid_report(tmp_path):
    out = tmp_path / "platform.json"
    code = _script().main(
        [
            "platform",
            "--install-check",
            str(write(tmp_path / "install.json", install_check())),
            "--ordinary",
            str(write(tmp_path / "ordinary.json", ordinary())),
            "--input-manifest",
            str(write(tmp_path / "inputs.json", {"documents": []})),
            "--platform",
            str(ROOT / "tests/fixtures/asset-benchmark/environment.json"),
            "--output",
            str(out),
        ]
    )
    assert code == 0
    validate_whole_task("AssetPlatformReportV1", json.loads(out.read_text()))


def test_platform_command_refuses_a_malformed_record(tmp_path):
    code = _script().main(
        [
            "platform",
            "--install-check",
            str(write(tmp_path / "install.json", {"steps": "x"})),
            "--input-manifest",
            str(write(tmp_path / "inputs.json", {})),
            "--platform",
            str(ROOT / "tests/fixtures/asset-benchmark/environment.json"),
            "--output",
            str(tmp_path / "out.json"),
        ]
    )
    assert code == 2 and not (tmp_path / "out.json").exists()


def test_startup_command_measures_ten_real_startups(tmp_path, monkeypatch):
    from launcher_helpers import write_config
    from test_launcher import EXTERNAL

    monkeypatch.setenv("TEST_EXTERNAL_KEY", "unused")
    base = write_config(tmp_path, EXTERNAL, local=False)
    out = tmp_path / "startup.json"
    code = _script().main(
        [
            "startup",
            "--base-config",
            str(base),
            "--work",
            str(tmp_path / "work"),
            "--assets",
            "5",
            "--output",
            str(out),
        ]
    )
    report = json.loads(out.read_text())
    assert code == 0 and report["qualification"] == "fixture"
    assert len(report["samples"]) == 10
    assert all(s["pid"] for s in report["samples"])
