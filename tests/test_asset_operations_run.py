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
    script.write_text("import os\nprint('Gateway-Pid: ' + str(os.getpid()), flush=True)\n" + body)
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
    """RM38/AC29: missing process samples keep their ordinal across repeated runs.

    A launch that raises is a harness error (exit 2, review 264 H1); the report
    with every collected sample is still written.
    """
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
        assert module.main(_startup_argv(base, tmp_path / "work", 5, out)) == 2
        report = json.loads(out.read_text())
        assert report["harness_errors"] == ["startup_0_OSError", "startup_4_OSError"]
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


def test_loopback_gateway_ignores_operator_proxy(tmp_path, monkeypatch):
    """RM38/AC29: proxy settings must not divert local measurement traffic."""
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    generate_catalog(tmp_path / "catalog", 3)
    base = _base(tmp_path, monkeypatch)

    async def go():
        async with serve(tmp_path / "work", tmp_path / "catalog", base_config=base, timeout=10) as (
            _,
            client,
        ):
            response = await client.get("/relay/v1/recommendations/readiness")
            assert response.status_code == 200
            assert response.json()["state"] == "ready"

    asyncio.run(go())


@pytest.mark.parametrize("controlled", [False, True])
def test_gateway_pid_and_allocations_are_measured(tmp_path, monkeypatch, controlled):
    """RM38/AC29: sample the interpreter, including a native Windows venv child."""
    import relaymind.evaluation.asset_operations_run as kit

    signal = tmp_path / "allocate"
    identity = tmp_path / "pid"
    name = "_controlled_argv" if controlled else "_child_argv"
    original = getattr(kit, name)
    setup = (
        "import os, pathlib, threading, time\n"
        f"pathlib.Path({str(identity)!r}).write_text(str(os.getpid()))\n"
        "def allocate():\n"
        f" while not pathlib.Path({str(signal)!r}).exists(): time.sleep(.01)\n"
        " global held\n held = bytearray(64 * 1024 * 1024)\n"
        "threading.Thread(target=allocate, daemon=True).start()\n"
    )

    def argv(config):
        result = original(config)
        result[2] = setup + result[2]
        return result

    monkeypatch.setattr(kit, name, argv)
    base = _base(tmp_path, monkeypatch)
    generate_catalog(tmp_path / "catalog", 3)

    async def go():
        async with kit._gateway(
            tmp_path / "work",
            tmp_path / "catalog",
            base_config=base,
            env=None,
            timeout=20,
            controlled=controlled,
        ) as gateway:
            assert gateway.pid == int(identity.read_text())
            before = rss_bytes(gateway.pid)
            assert before is not None
            signal.touch()
            for _ in range(50):
                after = rss_bytes(gateway.pid)
                if after is not None and after - before >= 48 * 1024 * 1024:
                    return
                await asyncio.sleep(0.1)
            pytest.fail("gateway allocation did not reach the sampled process")

    asyncio.run(go())


# --- review 264: job evidence collection is not a product refusal ----------------------


class _JobStore:
    """A job store whose reads fail on request (RM38/AC29 evidence collection)."""

    def __init__(self, outcomes_fail_after=None, events_fail=False):
        self.calls = 0
        self.outcomes_fail_after = outcomes_fail_after
        self.events_fail = events_fail

    def job_outcomes(self):
        self.calls += 1
        if self.outcomes_fail_after is not None and self.calls > self.outcomes_fail_after:
            raise OSError("database is locked")
        if self.calls == 1:
            return ()
        return ({"job_id": "j1", "source_id": "s"},)

    def job_events(self, job_id):
        if self.events_fail:
            raise OSError("database is locked")
        return ()


class _JobCatalog:
    def __init__(self, store, refusal=None):
        self.store = store
        self.refusal = refusal

    async def rescan(self, source_id):
        from relaymind.store.asset_catalog import Publication

        if self.refusal is not None:
            raise self.refusal
        return Publication(1, False, (), ())


def _job_app(store, refusal=None):
    return SimpleNamespace(state=SimpleNamespace(asset_catalog=_JobCatalog(store, refusal)))


@pytest.mark.parametrize(
    "store",
    [
        _JobStore(outcomes_fail_after=0),
        _JobStore(outcomes_fail_after=1),
        _JobStore(events_fail=True),
    ],
)
def test_a_job_store_read_failure_is_a_closed_evidence_error(store):
    """RM38/AC29: unavailable job evidence is not a refused rescan or rebuild."""
    from relaymind.evaluation.asset_operations_child import handle

    reply = asyncio.run(handle(_job_app(store), {"id": 7, "op": "rescan", "source_id": "s"}))
    assert reply == {
        "id": 7,
        "ok": False,
        "error": "job_evidence_unavailable",
        "evidence_error": "OSError",
    }


def test_a_product_refusal_carries_no_evidence_error():
    from relaymind.evaluation.asset_operations_child import handle

    refusal = ValueError("refused")
    reply = asyncio.run(
        handle(_job_app(_JobStore(), refusal), {"id": 8, "op": "rescan", "source_id": "s"})
    )
    assert reply == {"id": 8, "ok": False, "error": "ValueError", "reason": None}


def test_unavailable_job_evidence_is_a_harness_error_that_keeps_collected_jobs(tmp_path):
    """RM38/AC29: the evidence failure reaches channel_errors; earlier jobs stay."""
    job = {"job_id": "j0", "source_id": kit.IMPORT_SOURCE_ID, "events": []}
    replies = [
        {"id": 1, "ok": True, "revision": 1, "changed": True, "jobs": [job], "job_id": "j0"},
        {"id": 2, "ok": False, "error": "job_evidence_unavailable", "evidence_error": "OSError"},
        {"id": 3, "ok": False, "error": "ValueError", "reason": None},
    ]

    async def control(op, **fields):
        return replies.pop(0)

    plan = kit.workload_plan(tmp_path / "imports", control)

    async def go():
        await plan.batches[0].rescan()
        with pytest.raises(RuntimeError):
            await plan.batches[1].rescan()
        with pytest.raises(RuntimeError):
            await plan.rebuild()

    asyncio.run(go())
    assert plan.channel_errors == ["rescan_job_evidence_unavailable"]
    assert plan.job_evidence() == [job]
    assert plan.identities()["batch_job_ids"] == {"batch-0": "j0"}


# --- review 264 (second review): startup harness errors, input and work reuse ----------


def _healthy_baseline_and_workload(module, monkeypatch, launched=None):
    """Baseline and workload gateways that start; only ``serve`` is left to the test."""

    @contextlib.asynccontextmanager
    async def baseline(work, *, base_config, env=None):
        async with httpx.AsyncClient(base_url="http://test") as client:
            yield os.getpid(), client

    @contextlib.asynccontextmanager
    async def controlled(work, catalog, *, base_config, imports=None, env=None):
        if launched is not None:
            launched.append(base_config)
        yield SimpleNamespace(pid=os.getpid())

    async def workload(gateway, imports, samples):
        return SimpleNamespace(channel_errors=[])

    monkeypatch.setattr(module, "serve_baseline", baseline)
    monkeypatch.setattr(module, "serve_controlled", controlled)
    monkeypatch.setattr(module, "run_operations_workload", workload)


def _failing_serve(error):
    @contextlib.asynccontextmanager
    async def launch(work, catalog, *, base_config, env=None):
        raise error
        yield

    return launch


def _operations_argv(work, out, assets=2):
    return ["operations", "--work", str(work), "--assets", str(assets), "--output", str(out)]


def test_a_startup_only_launch_failure_is_a_harness_error(tmp_path, monkeypatch):
    """RM38/AC29 (review 264 H1): ten startup gateways that never start exit 2.

    Baseline and workload still start, so only the startup samples carry the
    failure; the report is still written and verify does not pass it.
    """
    module = _script()
    _healthy_baseline_and_workload(module, monkeypatch)
    monkeypatch.setattr(module, "serve", _failing_serve(RuntimeError("gateway_exited")))
    out = tmp_path / "operations.json"
    code = module.main(_operations_argv(tmp_path / "w", out))
    assert code == 2
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["harness_errors"] == [f"startup_{n}_gateway_exited" for n in range(10)]
    assert all(s["error"] == "startup_failed" for s in report["startups"])
    from helpers import load_script_module

    assert load_script_module("asset_platform_run").verify_operations(out) == "harness_errors"


def test_a_slow_startup_stays_a_product_sample(tmp_path, monkeypatch):
    """RM38/AC29: a startup cancelled by its one-second deadline is not a harness error."""
    module = _script()
    _healthy_baseline_and_workload(module, monkeypatch)

    @contextlib.asynccontextmanager
    async def slow(work, catalog, *, base_config, env=None):
        await asyncio.Event().wait()  # Never ready; the driver's deadline cancels it.
        yield

    monkeypatch.setattr(module, "serve", slow)
    out = tmp_path / "operations.json"
    assert module.main(_operations_argv(tmp_path / "w", out)) == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["harness_errors"] == []
    assert {s["error"] for s in report["startups"]} == {"deadline_exceeded"}


def test_the_startup_command_exits_two_on_a_launch_failure(tmp_path, monkeypatch, capsys):
    """RM38/AC29 (review 264 H1): the startup command draws the same line."""
    module = _script()
    base = _empty_base(tmp_path)
    monkeypatch.setattr(module, "serve", _failing_serve(OSError("no port")))
    out = tmp_path / "startup.json"
    assert module.main(_startup_argv(base, tmp_path / "work", 2, out)) == 2
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["harness_errors"] == [f"startup_{n}_OSError" for n in range(10)]
    assert report["complete"] is False
    assert "harness errors" in capsys.readouterr().err


def test_a_non_object_environment_lock_exits_two(tmp_path, capsys):
    """RM39/AC37 (review 264 L1): a list lock is a closed input error, not a traceback."""
    bundle = _platform_bundle(tmp_path)
    bundle.documents = [d for d in bundle.documents if d["id"] != "environment_lock"]
    bundle.add("environment_lock", [])
    bundle.write_manifest()
    code = _platform_main(tmp_path, bundle, tmp_path / "out.json")
    assert code == 2 and not (tmp_path / "out.json").exists()
    assert "environment_lock_invalid" in capsys.readouterr().err


def test_reusing_work_gives_the_requested_catalog_and_fresh_stores(tmp_path, monkeypatch):
    """RM38/AC29 (review 264 L2): a second run never sees the first run's skills or stores."""
    module = _script()
    launched = []
    _healthy_baseline_and_workload(module, monkeypatch, launched)
    monkeypatch.setattr(module, "measure_startups", _no_startups)
    work = tmp_path / "w"
    assert module.main(_operations_argv(work, tmp_path / "one.json", assets=4)) == 0
    store = launched[-1].parent / "runs.sqlite3"
    store.write_bytes(b"stale")
    (work / "imports" / "originals" / "stale.md").write_text("stale")
    assert module.main(_operations_argv(work, tmp_path / "two.json", assets=2)) == 0
    assert len(list((work / "catalog").iterdir())) == 2
    assert not store.exists()
    assert not (work / "imports" / "originals" / "stale.md").exists()
    report = json.loads((tmp_path / "two.json").read_text(encoding="utf-8"))
    assert report["catalog"]["assets"] == 2


def test_reusing_work_for_startup_regenerates_only_the_requested_catalog(tmp_path, monkeypatch):
    """RM38/AC29 (review 264 L2): the startup command also starts from a fresh catalog."""
    module = _script()
    base = _empty_base(tmp_path)
    monkeypatch.setattr(module, "measure_startups", _no_startups)
    work = tmp_path / "work"
    assert module.main(_startup_argv(base, work, 4, tmp_path / "one.json")) == 0
    assert module.main(_startup_argv(base, work, 2, tmp_path / "two.json")) == 0
    assert len(list((work / "catalog").iterdir())) == 2


async def _no_startups(launch, body, *, samples):
    return None


def _job(job_id, source_id, state, started, finished=None):
    return dict(
        job_id=job_id, source_id=source_id, state=state, started_at=started, finished_at=finished
    )


class _BindingStore:
    """``job_outcomes`` before and after one request (newest ``started_at`` first)."""

    def __init__(self, before, after):
        self.outcomes = [tuple(before), tuple(after)]

    def job_outcomes(self):
        return self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]

    def job_events(self, job_id):
        return ({"job_id": job_id},)


def _bind(store, op="rescan"):
    from relaymind.evaluation.asset_operations_child import handle
    from relaymind.store.asset_catalog import Publication

    class Catalog:
        def __init__(self):
            self.store = store

        async def rescan(self, source_id):
            return Publication(1, False, (), ())

        async def rebuild(self):
            return Publication(1, False, (), ())

    app = SimpleNamespace(state=SimpleNamespace(asset_catalog=Catalog()))
    command = {"id": 1, "op": op}
    if op == "rescan":
        command["source_id"] = "s"
    return asyncio.run(handle(app, command))


def test_the_child_binds_the_awaited_job_not_a_newer_one():
    """RM38/AC29 (review 264 M1): a job that started later is not the request's job.

    The catalog owner runs one job at a time and answers the request when its
    job is recorded finished, so the awaited job is the latest finished new job
    of that source; a periodic or repair job started after it is still running.
    """
    old = _job("old", "s", "succeeded", "2026-11-01T00:00:00.000Z", "2026-11-01T00:00:00.100Z")
    mine = _job("mine", "s", "succeeded", "2026-11-01T00:00:02.000Z", "2026-11-01T00:00:02.500Z")
    later = _job("later", "s", "running", "2026-11-01T00:00:03.000Z")
    reply = _bind(_BindingStore([old], [later, mine, old]))
    assert reply["ok"] is True and reply["job_id"] == "mine"
    assert [j["job_id"] for j in reply["jobs"]] == ["later", "mine"]


def test_the_child_binds_a_shared_job_that_was_already_running():
    """A request that joins a running job of its source binds that job."""
    shared_running = _job("shared", None, "running", "2026-11-01T00:00:01.000Z")
    shared_done = dict(shared_running, state="succeeded", finished_at="2026-11-01T00:00:02.000Z")
    repair = _job("repair", None, "running", "2026-11-01T00:00:03.000Z")
    reply = _bind(_BindingStore([shared_running], [repair, shared_done]), op="rebuild")
    assert reply["job_id"] == "shared"


@pytest.mark.parametrize(
    "after,binding",
    [
        ([], "job_missing"),
        (
            [
                _job("a", "s", "succeeded", "2026-11-01T00:00:02.000Z", "2026-11-01T00:00:04.000Z"),
                _job("b", "s", "failed", "2026-11-01T00:00:01.000Z", "2026-11-01T00:00:04.000Z"),
            ],
            "job_ambiguous",
        ),
    ],
)
def test_the_child_never_guesses_a_job(after, binding):
    reply = _bind(_BindingStore([], after))
    assert reply["ok"] is True and reply["job_id"] is None and reply["binding"] == binding


def test_an_unbound_job_is_a_harness_error_not_the_first_listed_job(tmp_path):
    """RM38/AC29 (review 264 M1): the plan binds ``job_id``, never ``jobs[0]``."""
    later = {"job_id": "later", "source_id": kit.IMPORT_SOURCE_ID, "events": []}
    mine = {"job_id": "mine", "source_id": kit.IMPORT_SOURCE_ID, "events": []}
    replies = [
        {"id": 1, "ok": True, "revision": 1, "changed": True, "jobs": [later, mine],
         "job_id": "mine"},
        {"id": 2, "ok": True, "revision": 2, "changed": True, "jobs": [mine], "job_id": None,
         "binding": "job_ambiguous"},
        {"id": 3, "ok": True, "revision": 2, "changed": False, "jobs": [later], "job_id": "later"},
    ]  # fmt: skip

    async def control(op, **fields):
        return replies.pop(0)

    plan = kit.workload_plan(tmp_path / "imports", control)

    async def go():
        await plan.batches[0].rescan()
        await plan.batches[1].rescan()
        await plan.rebuild()

    asyncio.run(go())
    assert plan.identities() == {"rebuild_job_id": "later", "batch_job_ids": {"batch-0": "mine"}}
    assert plan.channel_errors == ["rescan_job_ambiguous"]
