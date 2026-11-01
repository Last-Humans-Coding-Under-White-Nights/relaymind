"""Operations and platform run kit (#242; RM38, RM39, RM44; AC29, AC37).

Development tooling only: real ``relaymind serve`` processes, a generated
catalog and a memory sampler feed the existing AC29 startup driver, and an
installed-plugin lifecycle record becomes an ``AssetPlatformReportV1``. Runs
on CI or a developer machine are never the macOS/native Windows acceptance.
"""

import asyncio
import contextlib
import importlib.util
import json
import os
import shutil
import sys
import time
import tomllib
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

import relaymind.evaluation.asset_operations_run as kit
from relaymind.evaluation.asset_operations import StartupSample
from relaymind.evaluation.asset_operations_run import (
    _config,
    _wait_ready,
    generate_catalog,
    platform_report,
    rss_bytes,
    serve,
)
from relaymind.evaluation.asset_platform_export import PLATFORM_STEPS
from relaymind.evaluation.asset_whole_task import validate_whole_task

ROOT = Path(__file__).resolve().parents[1]
ENVIRONMENT = json.loads((ROOT / "tests/fixtures/asset-benchmark/environment.json").read_text())
LIFECYCLE = [
    "version", "isolate-log", "isolate-gateway", "install", "configure", "enable", "inspect",
    "store-token", "discover", "text-exchange", "tool-exchange", "disable", "uninstall",
    "remove-config", "remove-credential",
]  # fmt: skip


def _script():
    spec = importlib.util.spec_from_file_location(
        "asset_operations_run_script", ROOT / "scripts/asset_operations_run.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _base(tmp_path, monkeypatch):
    from launcher_helpers import write_config
    from test_launcher import EXTERNAL

    monkeypatch.setenv("TEST_EXTERNAL_KEY", "unused")
    return write_config(tmp_path, EXTERNAL, local=False)


# --- catalog and memory ----------------------------------------------------------


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


def test_memory_of_this_process_is_positive():
    assert rss_bytes(os.getpid()) > 0


def test_memory_of_a_missing_process_is_unknown():
    assert rss_bytes(2**22 + 12345) is None


def test_memory_of_undecodable_process_output_is_unknown(monkeypatch):
    """RM38/AC29: unreadable process output yields no sample instead of a crash."""

    def undecodable(*args, **kwargs):
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    monkeypatch.setattr(kit.subprocess, "run", undecodable)
    assert rss_bytes(123) is None


# --- the real gateway --------------------------------------------------------------


def test_a_real_gateway_is_ready_and_serves_lexical_recommendations(tmp_path, monkeypatch):
    generate_catalog(tmp_path / "catalog", 3)
    base = _base(tmp_path, monkeypatch)

    async def go():
        async with serve(tmp_path / "work", tmp_path / "catalog", base_config=base) as (
            pid,
            client,
        ):
            ready = await client.get("/relay/v1/recommendations/readiness")
            assert ready.json()["state"] == "ready"
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
    assert rss_bytes(pid) is None  # stopped and reaped on exit
    assert (tmp_path / "work" / "stderr.log").exists()


def test_a_relative_work_directory_and_pythonpath_still_start(tmp_path, monkeypatch):
    generate_catalog(tmp_path / "catalog", 2)
    base = _base(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)
    # Keep the relative import root on the temp drive even when checkout is elsewhere.
    shutil.copytree(ROOT / "src", tmp_path / "src", ignore=shutil.ignore_patterns("__pycache__"))
    monkeypatch.setenv("PYTHONPATH", "src")

    async def go():
        async with serve(Path("rel-work"), Path("catalog"), base_config=base):
            pass

    asyncio.run(go())


def test_a_relative_path_in_the_base_config_is_refused(tmp_path):
    base = tmp_path / "base.toml"
    base.write_text('[sandbox]\nseed = "baseline.json"\n')

    async def go():
        async with serve(tmp_path / "w", tmp_path, base_config=base):
            pass

    with pytest.raises(ValueError, match="base_config_relative_path"):
        asyncio.run(go())


def test_the_kit_owns_the_server_and_source_tables(tmp_path):
    base = tmp_path / "base.toml"
    base.write_text("[server]\nport = 1\n")

    async def go():
        async with serve(tmp_path / "w", tmp_path, base_config=base):
            pass

    with pytest.raises(ValueError, match="base_config_owns_kit_tables"):
        asyncio.run(go())


def _empty_base(tmp_path) -> Path:
    base = tmp_path / "base.toml"
    base.write_text("")
    return base


def _skip_readiness(monkeypatch):
    async def ready(client, deadline, process):
        return None

    monkeypatch.setattr(kit, "_wait_ready", ready)


def _fake_python(tmp_path, body: str) -> Path:
    script = tmp_path / "fake_gateway.py"
    script.write_text(body)
    return script


def _patch_child(monkeypatch, script: Path):
    monkeypatch.setattr(kit, "_child_argv", lambda config: [sys.executable, str(script)])


class _ReadinessResponse:
    status_code = 200

    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


class _ReadinessClient:
    """A readiness client that replays bodies, or raises them, in order."""

    def __init__(self, bodies):
        self.bodies = list(bodies)
        self.calls = 0

    async def get(self, path):
        self.calls += 1
        body = self.bodies.pop(0)
        if isinstance(body, BaseException):
            raise body
        return _ReadinessResponse(body)


def test_readiness_tolerates_non_mapping_and_undecodable_bodies():
    """RM38/AC29: a non-dict or undecodable readiness body is retried, not fatal."""
    client = _ReadinessClient(
        [
            [],
            True,
            "ready",
            httpx.DecodingError("bad body"),
            {"state": "starting"},
            {"state": "ready"},
        ]
    )
    asyncio.run(_wait_ready(client, time.monotonic() + 5, SimpleNamespace(returncode=None)))
    assert client.calls == 6


def test_readiness_keeps_retrying_a_non_mapping_body_until_the_deadline():
    """RM38/AC29: a non-mapping body never escapes as AttributeError."""
    client = _ReadinessClient([[]] * 100)
    with pytest.raises(TimeoutError, match="gateway_not_ready"):
        asyncio.run(_wait_ready(client, time.monotonic() + 0.1, SimpleNamespace(returncode=None)))
    assert client.calls > 1


def test_a_process_that_ignores_kill_is_not_awaited_forever(monkeypatch, capsys):
    """RM38/AC29: a hung gateway cannot block shutdown after the grace period."""

    class HungProcess:
        returncode = None

        def terminate(self):
            pass

        def kill(self):
            pass

        async def wait(self):
            await asyncio.Event().wait()

    monkeypatch.setattr(kit, "_GRACE_SECONDS", 0.05)
    began = time.monotonic()
    reaped = asyncio.run(kit._stop(HungProcess()))
    assert reaped is False
    assert time.monotonic() - began < 5
    assert "could not be reaped" in capsys.readouterr().err


def test_a_silent_child_cannot_outlast_the_token_deadline(tmp_path, monkeypatch):
    script = _fake_python(tmp_path, "import time\ntime.sleep(30)\n")
    _patch_child(monkeypatch, script)
    base = _empty_base(tmp_path)

    async def go():
        async with serve(tmp_path / "w", tmp_path, base_config=base, timeout=0.5):
            pass

    began = time.monotonic()
    with pytest.raises(TimeoutError, match="gateway_token_unavailable"):
        asyncio.run(go())
    assert time.monotonic() - began < 10


def test_a_gateway_that_exits_after_the_token_fails_before_the_deadline(tmp_path, monkeypatch):
    """RM38/AC29: a crashed child is reported at once, not after the full timeout."""
    script = _fake_python(tmp_path, "print('Application token: t', flush=True)\n")
    _patch_child(monkeypatch, script)
    base = _empty_base(tmp_path)

    async def go():
        async with serve(tmp_path / "w", tmp_path, base_config=base, timeout=60):
            pass

    began = time.monotonic()
    with pytest.raises(RuntimeError, match="gateway_exited"):
        asyncio.run(go())
    assert time.monotonic() - began < 10


@pytest.mark.skipif(sys.platform == "win32", reason="SIGTERM handling is POSIX only")
def test_cancellation_during_shutdown_still_kills_and_reaps(tmp_path, monkeypatch):
    pid_file = tmp_path / "pid"
    script = _fake_python(
        tmp_path,
        "import os, signal, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
        "print('Application token: t', flush=True)\n"
        "time.sleep(60)\n",
    )
    _patch_child(monkeypatch, script)
    monkeypatch.setattr(kit, "_GRACE_SECONDS", 1.0)

    _skip_readiness(monkeypatch)
    base = _empty_base(tmp_path)

    async def go():
        async def body():
            async with serve(tmp_path / "w", tmp_path, base_config=base, timeout=5):
                pass

        task = asyncio.create_task(body())
        while not pid_file.exists() or not pid_file.read_text():
            await asyncio.sleep(0.01)
        # Let the body exit and cleanup begin, then cancel during the grace wait.
        await asyncio.sleep(0.5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())
    pid = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


# --- platform report -------------------------------------------------------------


def install_check(statuses=None, default="passed"):
    statuses = statuses or {}
    return dict(
        kind="openclaw-install-check",
        status="completed",
        steps=[dict(name=n, status=statuses.get(n, default), exit_code=0) for n in LIFECYCLE],
        leftovers=dict(
            after_uninstall=dict(plugin_dir=False), after_removal=dict(plugin_dir=False)
        ),
    )


def write(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _report(tmp_path, *, check=None, ordinary=None, ordinary_check=None):
    install = write(tmp_path / "install.json", check or install_check())
    capture = None if ordinary is None else write(tmp_path / "ordinary.json", ordinary)
    references = {"install-check": "evidence/install.json"}
    if capture is not None:
        references["ordinary-capture"] = "evidence/ordinary.json"
    return platform_report(
        install_check=install,
        ordinary=capture,
        ordinary_check=ordinary_check,
        evidence_paths=references,
        input_digest="a" * 64,
        input_manifest=dict(path="inputs.json", digest="b" * 64, bytes=1),
        platform=ENVIRONMENT,
    )


def _steps(report):
    return {s["name"]: s["status"] for s in report["steps"]}


def test_a_clean_lifecycle_record_passes_install_configure_and_uninstall(tmp_path):
    report = _report(tmp_path)
    validate_whole_task("AssetPlatformReportV1", report)
    steps = _steps(report)
    assert [s["name"] for s in report["steps"]] == list(PLATFORM_STEPS)
    assert steps["install"] == steps["configure"] == steps["uninstall"] == "passed"
    assert report["qualification"] == "fixture" and report["gate"] == "not_applicable"


def test_selection_only_skill_receipts_never_pass_skill_use(tmp_path):
    report = _report(tmp_path, ordinary={"any": 1}, ordinary_check=lambda capture: True)
    step = next(s for s in report["steps"] if s["name"] == "skill_use")
    assert step["status"] == "not_run" and step["reason"] == "skill_use_not_observed"


def test_a_failed_or_timed_out_cleanup_step_fails_uninstall(tmp_path):
    for status in ("failed", "timed_out"):
        steps = _steps(_report(tmp_path, check=install_check({"remove-config": status})))
        assert steps["uninstall"] == "failed"


def test_an_unrun_lifecycle_record_stays_not_run(tmp_path):
    steps = _steps(_report(tmp_path, check=install_check(default="not_run")))
    assert steps["install"] == steps["configure"] == steps["uninstall"] == "not_run"


def test_ordinary_actions_pass_only_when_the_ordinary_check_passes(tmp_path):
    stale = {"checks": {"ordinary_actions": True}}
    refused = _steps(_report(tmp_path, ordinary=stale, ordinary_check=lambda capture: False))
    accepted = _steps(_report(tmp_path, ordinary=stale, ordinary_check=lambda capture: True))
    for name in ("recommend", "choose", "feedback", "delete", "reset"):
        assert refused[name] == "not_run"
        assert accepted[name] == "passed"


def test_an_ordinary_capture_needs_its_check(tmp_path):
    with pytest.raises(ValueError, match="ordinary_check_required"):
        _report(tmp_path, ordinary={})


def test_the_real_ordinary_check_refuses_a_stale_capture():
    check = _script().ordinary_check()
    assert check({"checks": {"ordinary_actions": True}, "ordinary_actions": {}}) is False


def test_a_measured_claim_is_refused_by_the_kit(tmp_path):
    with pytest.raises(ValueError, match="measured"):
        platform_report(
            install_check=write(tmp_path / "install.json", install_check()),
            ordinary=None,
            evidence_paths={"install-check": "evidence/install.json"},
            input_digest="a" * 64,
            input_manifest=dict(path="inputs.json", digest="b" * 64, bytes=1),
            platform={},
            qualification="measured",
        )


def _platform_bundle(tmp_path, environment=None):
    from test_asset_whole_task_inputs import Bundle
    from whole_task_helpers import ENVIRONMENT as LOCKED

    bundle = Bundle(tmp_path)
    install = json.dumps(install_check()).encode()
    bundle.extra_evidence["install-check"] = install
    bundle.write_manifest()
    (tmp_path / "install.json").write_bytes(install)
    (tmp_path / "environment.json").write_text(json.dumps(environment or LOCKED))
    return bundle


def _platform_main(tmp_path, bundle, out) -> int:
    return _script().main(
        [
            "platform",
            "--install-check", str(tmp_path / "install.json"),
            "--inputs", str(bundle.root / "inputs.json"),
            "--platform", str(tmp_path / "environment.json"),
            "--output", str(out),
        ]
    )  # fmt: skip


def test_the_platform_command_refuses_a_platform_outside_the_environment_lock(tmp_path):
    bundle = _platform_bundle(tmp_path, dict(ENVIRONMENT, os="elsewhere"))
    code = _platform_main(tmp_path, bundle, tmp_path / "out.json")
    assert code == 2 and not (tmp_path / "out.json").exists()


def test_the_platform_command_produces_a_report_the_grader_accepts(tmp_path):
    """Round trip: kit output graded through the companion manifest (spec 12.6.5)."""
    from test_asset_whole_task_inputs import _grade

    bundle = _platform_bundle(tmp_path)
    out = tmp_path / "platform.json"
    code = _platform_main(tmp_path, bundle, out)
    assert code == 0
    report = json.loads(out.read_text())
    code, result = _grade(bundle, "grade-platform", report)
    assert (code, result.get("gate")) == (0, "not_applicable"), result


def test_the_platform_command_refuses_an_unregistered_record(tmp_path):
    from test_asset_whole_task_inputs import Bundle
    from whole_task_helpers import ENVIRONMENT as LOCKED

    bundle = Bundle(tmp_path)
    bundle.write_manifest()
    write(tmp_path / "install.json", install_check())
    (tmp_path / "environment.json").write_text(json.dumps(LOCKED))
    code = _platform_main(tmp_path, bundle, tmp_path / "out.json")
    assert code == 2 and not (tmp_path / "out.json").exists()


def test_the_platform_command_refuses_a_non_object_platform_document(tmp_path, capsys):
    """RM39/AC37: a JSON array is not a platform mapping and exits 2, not a traceback."""
    bundle = _platform_bundle(tmp_path)
    (tmp_path / "environment.json").write_text(json.dumps([]))
    code = _platform_main(tmp_path, bundle, tmp_path / "out.json")
    assert code == 2 and not (tmp_path / "out.json").exists()
    assert "platform_invalid" in capsys.readouterr().err


def test_the_platform_command_creates_the_output_directory(tmp_path):
    """RM39/AC37: an output path in a new directory is written, not crashed on."""
    bundle = _platform_bundle(tmp_path)
    out = tmp_path / "reports" / "platform.json"
    code = _platform_main(tmp_path, bundle, out)
    assert code == 0 and out.is_file()


def test_a_failed_output_write_exits_two(tmp_path, capsys):
    """RM39/AC37: an unwritable report path is reported with exit code 2."""
    bundle = _platform_bundle(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    code = _platform_main(tmp_path, bundle, out)
    assert code == 2
    assert "asset_operations_run" in capsys.readouterr().err


# --- startup command --------------------------------------------------------------


def _startup_argv(base, work, assets, out) -> list[str]:
    return [
        "startup", "--base-config", str(base), "--work", str(work),
        "--assets", str(assets), "--output", str(out),
    ]  # fmt: skip


def test_startup_refuses_a_missing_base_config_before_any_work(tmp_path, monkeypatch, capsys):
    """RM38/AC29: a missing base config fails before catalog generation and launches."""
    module = _script()
    generated = []
    launched = []

    @contextlib.asynccontextmanager
    async def launch(*args, **kwargs):
        launched.append(args)
        raise AssertionError("launch must not run")
        yield

    monkeypatch.setattr(module, "generate_catalog", lambda *a, **k: generated.append(a))
    monkeypatch.setattr(module, "serve", launch)
    out = tmp_path / "startup.json"
    code = module.main(_startup_argv(tmp_path / "missing.toml", tmp_path / "work", 5, out))
    assert code == 2 and not generated and not launched and not out.exists()
    assert "base_config_not_found" in capsys.readouterr().err


@pytest.mark.parametrize("assets", ["0", "-1"])
def test_startup_refuses_a_non_positive_asset_count_before_any_work(
    tmp_path, monkeypatch, capsys, assets
):
    """RM38/AC29: the AC29 catalog size must be a positive count."""
    module = _script()
    generated = []
    launched = []
    base = _empty_base(tmp_path)

    monkeypatch.setattr(module, "generate_catalog", lambda *a, **k: generated.append(a))
    monkeypatch.setattr(module, "serve", lambda *a, **k: launched.append(a))
    out = tmp_path / "startup.json"
    code = module.main(_startup_argv(base, tmp_path / "work", assets, out))
    assert code == 2 and not generated and not launched and not out.exists()
    assert "assets_must_be_positive" in capsys.readouterr().err


def test_startup_command_records_ten_real_startups(tmp_path, monkeypatch):
    base = _base(tmp_path, monkeypatch)
    out = tmp_path / "startup.json"
    code = _script().main(_startup_argv(base, tmp_path / "work", 5, out))
    report = json.loads(out.read_text())
    assert code == 0 and report["qualification"] == "fixture"
    assert len(report["samples"]) == 10 and len(report["memory_bytes"]) == 10
    # RM38/AC29: CI verifies honest records, not the acceptance machine's timing.
    assert [s["ordinal"] for s in report["samples"]] == list(range(10))
    for sample in report["samples"]:
        assert sample["outcome"] in {"useful", "failed"}
        if sample["outcome"] == "useful":
            assert sample["pid"] and sample["error"] is None
            assert 0 <= sample["first_useful_at"] - sample["process_started_at"] <= 1
        else:
            assert sample["error"]
    useful = sum(s["outcome"] == "useful" for s in report["samples"])
    assert report["useful_startups"] == useful
    assert report["complete"] == (
        useful == 10 and len(report["memory_bytes"]) == 10 and None not in report["memory_bytes"]
    )


def test_startup_report_is_incomplete_without_useful_startups():
    module = _script()
    samples = [StartupSample(i, 0.0, None, 100 + i, (), "timeout", "timeout") for i in range(10)]
    report = module.startup_report(
        samples, [1] * 10, assets=300, base_config_digest="d", counter="x"
    )
    assert report["useful_startups"] == 0 and report["complete"] is False


@pytest.mark.parametrize(
    "count,memory",
    [(10, []), (10, [1] * 9), (9, [1] * 9)],
    ids=["no-memory", "short-memory", "short-run"],
)
def test_startup_report_is_incomplete_without_every_memory_sample(count, memory):
    """RM38/AC29: an empty or partial memory list never completes the report."""
    samples = [StartupSample(i, 0.0, 0.5, 100 + i, (), "useful", None) for i in range(count)]
    report = _script().startup_report(
        samples, memory, assets=300, base_config_digest="d", counter="x"
    )
    assert report["useful_startups"] == count
    assert report["complete"] is False


def test_startup_report_is_complete_with_ten_useful_startups_and_memory_samples():
    """RM38/AC29: completeness needs ten useful startups and ten known samples."""
    samples = [StartupSample(i, 0.0, 0.5, 100 + i, (), "useful", None) for i in range(10)]
    report = _script().startup_report(
        samples, [1] * 10, assets=300, base_config_digest="d", counter="x"
    )
    assert report["complete"] is True
    assert report["base_config_digest"] == "d"


@pytest.mark.parametrize("first_useful_at", [None, 1.5])
def test_deadline_failures_never_count_as_useful_startups(first_useful_at):
    """RM38/AC29: a late useful response still fails the startup deadline."""
    samples = [
        StartupSample(i, 0.0, first_useful_at, 100 + i, (), "failed", "deadline_exceeded")
        for i in range(10)
    ]
    report = _script().startup_report(
        samples, [1] * 10, assets=300, base_config_digest="d", counter="x"
    )
    assert report["useful_startups"] == 0
    assert report["complete"] is False


def test_startup_memory_stays_with_its_launch_and_work_can_be_reused(tmp_path, monkeypatch):
    """RM38/AC29: missing process samples keep their ordinal across repeated runs."""
    module = _script()
    base = _empty_base(tmp_path)

    @contextlib.asynccontextmanager
    async def launch(work, catalog, *, base_config):
        ordinal = int(work.name.removeprefix("run-"))
        if ordinal in (0, 4):
            raise OSError("launch failed")
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(503)),
            base_url="http://test",
        ) as client:
            yield 100 + ordinal, client

    monkeypatch.setattr(module, "serve", launch)
    monkeypatch.setattr(module, "rss_bytes", lambda pid: pid * 1024)
    for _ in range(2):
        out = tmp_path / "startup.json"
        assert module.main(_startup_argv(base, tmp_path / "work", 5, out)) == 0
        report = json.loads(out.read_text())
        pids = [None, 101, 102, 103, None, 105, 106, 107, 108, 109]
        assert [s["pid"] for s in report["samples"]] == pids
        assert report["memory_bytes"] == [None if p is None else p * 1024 for p in pids]


def test_generated_catalog_can_be_regenerated(tmp_path):
    """RM38/AC29: reusing a work directory recreates the same catalog."""
    first = generate_catalog(tmp_path, 3)
    contents = [path.read_bytes() for path in first]
    assert generate_catalog(tmp_path, 3) == first
    assert [path.read_bytes() for path in first] == contents


def _tasklist(monkeypatch, stdout: str):
    monkeypatch.setattr(kit.sys, "platform", "win32")
    monkeypatch.setattr(
        kit.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=stdout)
    )


@pytest.mark.parametrize("memory", ["12,345 K", "12.345 KB", "12\u00a0345 Ko", "12 345 КБ"])
@pytest.mark.parametrize("image", ['"python.exe"', '"python, worker.exe"', "python.exe"])
def test_windows_memory_parses_csv_and_localized_units(monkeypatch, memory, image):
    """RM38/AC29: working-set samples survive CSV quoting and localized units."""
    _tasklist(monkeypatch, f'{image},"123","Console","1","{memory}"\n')
    assert rss_bytes(123) == 12641280


def test_windows_memory_finds_the_matching_row_in_multiline_output(monkeypatch):
    """RM38/AC29: a banner or several rows must not hide the process's own sample."""
    _tasklist(
        monkeypatch,
        'INFO: tasks listed below\n"python","124","Console","1","1,024 K"\n'
        '"python","123","Console","1","12,345 K"\n',
    )
    assert rss_bytes(123) == 12345 * 1024


def test_windows_memory_that_is_malformed_stays_unknown(monkeypatch):
    """RM38/AC29: an unterminated CSV quote drops the sample instead of crashing."""
    _tasklist(monkeypatch, '"python","123","Console","1","12,345 K')
    assert rss_bytes(123) is None


@pytest.mark.parametrize(
    "row",
    [
        "INFO: no tasks",
        '"python","124","Console","1","123 K"',
        '"python","123","Console","1","N/A"',
    ],
)
def test_windows_missing_memory_stays_unknown(monkeypatch, row):
    """RM38/AC29: absent counters and other processes supply no measurement."""
    _tasklist(monkeypatch, row)
    assert rss_bytes(123) is None


def test_base_config_preserves_root_dotted_keys_and_non_path_values(tmp_path):
    """RM38/AC29: relocation preserves TOML meaning and opaque provider options."""
    base = tmp_path / "base.toml"
    base.write_text(
        f"store.path = {json.dumps(str(tmp_path / 'runs.sqlite'))}\n"
        '[external_model]\nmodel = "versioned-3.5"\n'
        '[external_model.request_options.headers]\nContent-Type = "application/json"\n'
    )
    parsed = tomllib.loads(_config(tmp_path, base, tmp_path, 1234).read_text())
    assert parsed["store"]["path"] == str(tmp_path / "runs.sqlite")
    assert parsed["server"] == {"host": "127.0.0.1", "port": 1234}
    assert parsed["external_model"]["model"] == "versioned-3.5"
    assert (
        parsed["external_model"]["request_options"]["headers"]["Content-Type"] == "application/json"
    )


@pytest.mark.parametrize(
    "key",
    [
        "store.path",
        "sandbox.seed",
        "sandbox.work_dir",
        "skills.roots",
        "local_model.manifest",
        "local_model.artifact",
        "user_actions.registrations_path",
        "asset_hybrid.manifest",
        "asset_hybrid.config",
        "asset_adaptation.config",
    ],
)
@pytest.mark.parametrize("value", ["plain", "../relative/file"])
def test_known_config_paths_must_be_absolute(tmp_path, key, value):
    """RM38/AC29: relocating even extensionless paths must never change their meaning."""
    base = tmp_path / "base.toml"
    encoded = json.dumps([value] if key == "skills.roots" else value)
    base.write_text(f"{key} = {encoded}\n")
    with pytest.raises(ValueError, match="base_config_relative_path: " + key):
        _config(tmp_path, base, tmp_path, 1234)


def test_reusing_a_work_directory_starts_each_launch_with_clean_logs(tmp_path, monkeypatch):
    """RM38/AC29: a reused work directory never mixes separate launches' logs."""
    body = (
        "print('startup diagnostic', flush=True)\n"
        "print('Application token: t', flush=True)\n"
        "import time\ntime.sleep(30)\n"
    )
    _patch_child(monkeypatch, _fake_python(tmp_path, body))

    _skip_readiness(monkeypatch)
    base = _empty_base(tmp_path)

    async def go():
        for _ in range(2):
            async with serve(tmp_path / "w", tmp_path, base_config=base, timeout=10):
                pass
            assert (tmp_path / "w" / "stdout.log").read_text() == "startup diagnostic\n"

    asyncio.run(go())
    assert (tmp_path / "w" / "stderr.log").read_text() == ""


@pytest.mark.parametrize("token", [False, True])
def test_stdout_before_token_is_preserved_even_if_startup_fails(tmp_path, monkeypatch, token):
    """RM38/AC29: startup diagnostics survive token handover without leaking its secret."""
    body = "print('startup diagnostic', flush=True)\n"
    if token:
        body += "print('Application token: secret-value', flush=True)\n"
    _patch_child(monkeypatch, _fake_python(tmp_path, body))

    _skip_readiness(monkeypatch)
    base = _empty_base(tmp_path)

    async def go():
        async with serve(tmp_path / "w", tmp_path, base_config=base, timeout=10):
            pass

    if token:
        asyncio.run(go())
    else:
        with pytest.raises(TimeoutError, match="gateway_token_unavailable"):
            asyncio.run(go())
    assert (tmp_path / "w" / "stdout.log").read_text() == "startup diagnostic\n"
