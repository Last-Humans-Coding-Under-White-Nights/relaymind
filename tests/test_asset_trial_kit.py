"""Early installed-skill trial kit for the customer's Apple M1 8 GiB machine (#234).

Spec 0.65 section 12.6.5 (RM39, RM44; AC30, AC36). The kit checks the target
machine, the pinned OpenClaw host and the packaged plugin release checksum,
installs the plugin with the pinned host CLI, runs the frozen early-trial plan
through the existing conserving driver over operator-captured row
observations, bundles every file with its SHA-256 and grades the bundle
offline with the existing grader. Everything here is deterministic: the host
command is a local double and the companion bundle is fixture data, so no
result is measured and no gate can pass.
"""

import hashlib
import json
import re
import sys
from pathlib import Path

import pytest

from relaymind.canonical import canonical_digest
from relaymind.evaluation import asset_trial_kit as kit
from relaymind.evaluation.asset_whole_task import plan_host_trial
from relaymind.evaluation.asset_whole_task_cli import main
from test_asset_whole_task_inputs import Bundle, _bytes
from whole_task_helpers import ENVIRONMENT, row

REPO = Path(__file__).resolve().parents[1]
M1 = dict(
    ENVIRONMENT,
    os="Darwin",
    os_version="macOS 27.0.1",
    architecture="arm64",
    cpu="Apple M1",
    ram_bytes=8 * 1024**3,
)
# Like the pinned 2026.10.7 host (#269): a fresh install is disabled until it is
# configured and enabled; inspect then reports status "disabled".
FAKE_HOST = r"""
import json, os, sys
args = sys.argv[1:]
mode = open(sys.argv[0] + ".mode").read().strip() if os.path.exists(
    sys.argv[0] + ".mode") else "ok"
with open(sys.argv[0] + ".log", "a") as log:
    log.write(json.dumps(args) + "\n")
path = sys.argv[0] + ".state"
state = json.load(open(path)) if os.path.exists(path) else {}
def save():
    json.dump(state, open(path, "w"))
if args == ["--version"]:
    print("OpenClaw 2026.10.7" if mode != "old" else "OpenClaw 2026.9.1")
elif args[:2] == ["plugins", "install"]:
    print("installed relaymind")
    if mode == "install-fails":
        sys.exit(1)
    state.update(installed=True, enabled=False)
    save()
elif args[:3] == ["config", "set", "plugins.entries.relaymind.config.baseUrl"] and len(args) == 4:
    if mode == "config-fails":
        sys.exit(1)
    if state.get("installed") and not state.get("enabled"):
        print("plugin disabled (relaymind) but config is present")
    state["base_url"] = args[3]
    save()
elif args == ["plugins", "enable", "relaymind"]:
    if not state.get("installed") or "base_url" not in state:
        sys.exit(1)
    state["enabled"] = mode != "stays-disabled"
    save()
elif args[:2] == ["plugins", "inspect"]:
    if mode == "stderr-noise":
        print("(node) ExperimentalWarning: update available", file=sys.stderr)
    enabled = bool(state.get("enabled"))
    print(json.dumps({"plugin": {"status": "loaded" if enabled else "disabled",
                                 "enabled": enabled, "version": "1.0.0"},
                      "diagnostics": []}))
else:
    sys.exit(9)
"""


def fake_host(tmp_path: Path, mode: str = "ok") -> list[str]:
    script = tmp_path / "fake_openclaw.py"
    script.write_text(FAKE_HOST, encoding="utf-8")
    if mode != "ok":
        (tmp_path / "fake_openclaw.py.mode").write_text(mode, encoding="utf-8")
    return [sys.executable, str(script)]


def release(tmp_path: Path, payload: bytes = b"ev-package", *, sums: str | None = None) -> Path:
    """A release directory shaped like ``npm run pack:release`` output."""
    directory = tmp_path / "release"
    directory.mkdir(exist_ok=True)
    name = "relaymind-openclaw-provider-1.0.0.tgz"
    (directory / name).write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    (directory / "SHA256SUMS").write_text(f"{sums or digest}  {name}\n", encoding="utf-8")
    record = dict(
        package="@relaymind/openclaw-provider",
        version="1.0.0",
        filename=name,
        sha256=digest,
        openclaw=dict(peer="2026.10.7", pluginApi="2026.10.7", built_with="2026.10.7"),
    )
    (directory / "release.json").write_text(json.dumps(record), encoding="utf-8")
    return directory / name


def set_lock(bundle: Bundle, environment: dict) -> None:
    entry = next(d for d in bundle.documents if d["kind"] == "environment_lock")
    payload = _bytes(environment)
    (bundle.root / entry["path"]).write_bytes(payload)
    entry.update(digest=canonical_digest(environment), bytes=len(payload))


def frozen_manifest(bundle: Bundle) -> Path:
    bundle.write_manifest()
    return bundle.root / "inputs.json"


def observations(tmp_path: Path, bundle: Bundle, *, skip=(), provenance="fixture") -> Path:
    """One observation per planned row; evidence bytes equal the frozen registry."""
    directory = tmp_path / "observations"
    directory.mkdir()
    (directory / "files").mkdir()
    for planned in plan_host_trial(bundle.tasks):
        if planned["ordinal"] in skip:
            continue
        value = row(planned["task_id"], planned["arm"])
        ids = set(value["evidence_ids"])
        for attempt in value["attempts"]:
            ids.update(attempt["evidence_ids"])
        evidence = []
        for i in sorted(ids):
            (directory / "files" / i).write_bytes(i.encode())
            evidence.append(dict(id=i, file="files/" + i, provenance=provenance))
        document = dict(
            schema_version=1,
            ordinal=planned["ordinal"],
            task_id=planned["task_id"],
            arm=planned["arm"],
            row=value,
            evidence=evidence,
        )
        name = kit.observation_name(planned["ordinal"])
        (directory / name).write_text(json.dumps(document), encoding="utf-8")
    return directory


def preflight_record(tmp_path, bundle, environment=M1, mode="ok") -> dict:
    tarball = release(tmp_path)
    return kit.preflight(
        environment=environment,
        lock=json.loads(
            (
                bundle.root
                / next(d for d in bundle.documents if d["kind"] == "environment_lock")["path"]
            ).read_bytes()
        ),
        host_command=fake_host(tmp_path, mode),
        tarball=tarball,
        freeze=bundle.freeze,
    )


@pytest.fixture
def bundle(tmp_path):
    return Bundle(tmp_path)


def checks(record: dict) -> dict:
    return {c["name"]: c["status"] for c in record["checks"]}


# --- preflight ----------------------------------------------------------------


def test_pinned_host_equals_the_plugin_compatibility_pin():
    """RM39/AC30: the kit and the packaged plugin name the same pinned host."""
    source = (REPO / "integrations/openclaw/src/compat.ts").read_text(encoding="utf-8")
    pinned = re.search(r'PINNED_OPENCLAW = "([^"]+)"', source).group(1)
    assert kit.PINNED_OPENCLAW == pinned


def test_preflight_passes_on_apple_m1_8gib_with_pinned_host_and_checksum(tmp_path, bundle):
    set_lock(bundle, M1)
    record = preflight_record(tmp_path, bundle)
    assert record["passed"] is True
    assert set(checks(record).values()) == {"passed"}
    assert list(checks(record)) == list(kit.PREFLIGHT_CHECKS)
    assert record["observed"]["host_version"] == "2026.10.7"
    assert record["observed"]["release"]["sha256"] == hashlib.sha256(b"ev-package").hexdigest()


def test_preflight_on_another_machine_fails_the_target_checks(tmp_path, bundle):
    record = preflight_record(tmp_path, bundle, environment=ENVIRONMENT)
    status = checks(record)
    assert record["passed"] is False
    assert status["macos_arm64"] == status["apple_m1"] == status["memory_8gib"] == "failed"
    # The fixture lock is Linux, so the observed Linux machine matches it.
    assert status["environment_lock"] == "passed"


def test_preflight_unknown_memory_is_not_a_pass(tmp_path, bundle):
    record = preflight_record(tmp_path, bundle, environment=dict(M1, ram_bytes=None))
    assert checks(record)["memory_8gib"] == "failed" and record["passed"] is False


def test_preflight_rejects_another_host_version(tmp_path, bundle):
    set_lock(bundle, M1)
    record = preflight_record(tmp_path, bundle, mode="old")
    assert checks(record)["host_version"] == "failed"
    assert record["observed"]["host_version"] is None


def test_preflight_rejects_a_tarball_that_differs_from_its_checksum(tmp_path, bundle):
    set_lock(bundle, M1)
    release(tmp_path, sums="0" * 64)
    record = kit.preflight(
        environment=M1,
        lock=M1,
        host_command=fake_host(tmp_path),
        tarball=tmp_path / "release" / "relaymind-openclaw-provider-1.0.0.tgz",
        freeze=bundle.freeze,
    )
    assert checks(record)["release_checksum"] == "failed" and not record["passed"]


def test_preflight_requires_the_frozen_package_bytes(tmp_path, bundle):
    set_lock(bundle, M1)
    tarball = release(tmp_path, payload=b"another build")
    record = kit.preflight(
        environment=M1,
        lock=M1,
        host_command=fake_host(tmp_path),
        tarball=tarball,
        freeze=bundle.freeze,
    )
    status = checks(record)
    assert status["release_checksum"] == "passed"
    assert status["frozen_package"] == "failed" and not record["passed"]


# --- install ------------------------------------------------------------------


def test_install_uses_the_pinned_cli_and_records_digests(tmp_path):
    """#269: install, configure, enable, inspect, the operator guide's lifecycle."""
    tarball = release(tmp_path)
    work = tmp_path / "install"
    record = kit.install_plugin(fake_host(tmp_path), tarball, work)
    assert record["passed"] is True
    names = [s["name"] for s in record["steps"]]
    assert names == ["install", "configure", "enable", "inspect"]
    assert all(s["status"] == "passed" for s in record["steps"])
    log = [
        json.loads(line) for line in (tmp_path / "fake_openclaw.py.log").read_text().splitlines()
    ]
    assert log == [
        ["plugins", "install", "npm-pack:" + str(tarball), "--force", "--accept-capabilities"],
        ["config", "set", "plugins.entries.relaymind.config.baseUrl", kit.DEFAULT_BASE_URL],
        ["plugins", "enable", "relaymind"],
        ["plugins", "inspect", "relaymind", "--runtime", "--json"],
    ]
    assert record["base_url"] == kit.DEFAULT_BASE_URL == "http://127.0.0.1:8080/v1"
    assert record["steps"][-1]["inspect"]["enabled"] is True
    assert record["steps"][-1]["inspect"]["status"] == "loaded"
    # The record never carries the private path, only placeholders and digests.
    assert str(tarball) not in json.dumps(record)
    assert record["archive"]["sha256"] == hashlib.sha256(b"ev-package").hexdigest()
    for step in record["steps"]:
        output = (work / step["log"]).read_bytes()
        assert step["output_sha256"] == hashlib.sha256(output).hexdigest()


def test_failed_install_is_recorded_and_not_passed(tmp_path):
    tarball = release(tmp_path)
    record = kit.install_plugin(fake_host(tmp_path, "install-fails"), tarball, tmp_path / "i")
    assert record["passed"] is False
    assert [s["status"] for s in record["steps"]] == ["failed", "not_run", "not_run", "not_run"]


def test_install_configures_the_given_loopback_url(tmp_path):
    """#269: the configure step sets the operator's gateway URL before enable."""
    tarball = release(tmp_path)
    url = "http://127.0.0.1:18080/v1"
    record = kit.install_plugin(fake_host(tmp_path), tarball, tmp_path / "i", base_url=url)
    assert record["passed"] is True and record["base_url"] == url
    configure = next(s for s in record["steps"] if s["name"] == "configure")
    assert configure["argv"] == [
        "<openclaw>",
        "config",
        "set",
        "plugins.entries.relaymind.config.baseUrl",
        url,
    ]
    assert json.loads((tmp_path / "fake_openclaw.py.state").read_text())["base_url"] == url


@pytest.mark.parametrize(
    ("mode", "statuses"),
    [
        ("config-fails", ["passed", "failed", "not_run", "not_run"]),
        ("stays-disabled", ["passed", "passed", "passed", "failed"]),
    ],
)
def test_a_real_lifecycle_failure_is_recorded_and_not_passed(tmp_path, mode, statuses):
    """#269: a refused configure, or a plugin still disabled after enable, fails."""
    tarball = release(tmp_path)
    record = kit.install_plugin(fake_host(tmp_path, mode), tarball, tmp_path / "i")
    assert record["passed"] is False
    assert [s["status"] for s in record["steps"]] == statuses


def test_trial_install_command_takes_the_base_url(tmp_path):
    """#269: ``trial-install --base-url`` reaches the configure step."""
    tarball = release(tmp_path)
    output = tmp_path / "install.json"
    url = "http://localhost:9090/v1"
    argv = ["trial-install", "--tarball", str(tarball), "--work", str(tmp_path / "w")]
    argv += ["--output", str(output), "--base-url", url, "--", *fake_host(tmp_path)]
    assert main(argv) == 0
    record = json.loads(output.read_bytes())
    assert record["passed"] is True and record["base_url"] == url


# --- run, bundle and grade ----------------------------------------------------


def run(tmp_path, bundle, *, qualification="fixture", preflight=None, **kwargs):
    inputs = frozen_manifest(bundle)
    observed = kwargs.pop("observed", None) or observations(tmp_path, bundle)
    return kit.trial_run(
        inputs=inputs,
        observations=observed,
        bundle=tmp_path / "out",
        resolver=bundle.resolver,
        qualification=qualification,
        environment=kwargs.pop("environment", ENVIRONMENT),
        preflight_record=preflight or preflight_record(tmp_path, bundle, ENVIRONMENT),
        install_record=kwargs.pop("install_record", None),
        tarball=kwargs.pop("tarball", None),
        **kwargs,
    )


def sums(directory: Path) -> dict:
    listed = {}
    for line in (directory / "SHA256SUMS").read_text().splitlines():
        digest, name = line.split("  ", 1)
        listed[name] = digest
    return listed


def test_fixture_trial_bundles_every_file_and_grades_offline(tmp_path, bundle):
    """AC36: the existing driver and grader; a fixture trial is graded, never passed."""
    code, result = run(tmp_path, bundle)
    out = tmp_path / "out"
    assert code == 3 and result["status"] == "graded" and result["gate"] == "blocked"
    report = json.loads((out / "report.json").read_bytes())
    assert report["qualification"] == "fixture" and report["kind"] == "early_trial"
    assert [(r["task_id"], r["arm"]) for r in report["rows"]] == [
        (p["task_id"], p["arm"]) for p in plan_host_trial(bundle.tasks)
    ]
    grade = json.loads((out / "grade.json").read_bytes())
    assert grade["status"] == "graded" and grade["summary"]["success_gate"] == "pass"
    listed = sums(out)
    files = {
        p.relative_to(out).as_posix()
        for p in out.rglob("*")
        if p.is_file() and p.name != "SHA256SUMS"
    }
    assert set(listed) == files
    for name, digest in listed.items():
        assert hashlib.sha256((out / name).read_bytes()).hexdigest() == digest
    registry = {e["id"] for e in json.loads((out / "inputs.json").read_bytes())["evidence"]}
    assert {"trial-preflight"} <= registry
    # The bundle grades again on its own, from its own companion manifest.
    again = out / "regrade.json"
    argv = ["grade-host", "--report", str(out / "report.json"), "--inputs"]
    argv += [str(out / "inputs.json"), "--resolver", str(bundle.resolver), "--output", str(again)]
    assert main(argv) == 3
    assert json.loads(again.read_bytes())["summary"] == grade["summary"]


def test_missing_observation_is_exported_as_not_run(tmp_path, bundle):
    observed = observations(tmp_path, bundle, skip={2})
    code, result = run(tmp_path, bundle, observed=observed)
    report = json.loads((tmp_path / "out" / "report.json").read_bytes())
    second = report["rows"][1]
    assert second["status"] == "not_run" and second["reason"] == "observation_missing"
    assert second["attempts"] == [] and second["host_session_id"] is None
    assert code == 3 and result["status"] == "graded"


def test_operator_may_skip_a_row_interactively(tmp_path, bundle):
    observed = observations(tmp_path, bundle, skip={1})
    prompts = []

    def ask(text):
        prompts.append(text)
        return "skip"

    run(tmp_path, bundle, observed=observed, ask=ask)
    report = json.loads((tmp_path / "out" / "report.json").read_bytes())
    assert report["rows"][0]["reason"] == "operator_skipped"
    assert len(prompts) == 1 and kit.observation_name(1) in prompts[0]


def test_interrupt_exports_the_remaining_rows_as_not_run(tmp_path, bundle):
    calls = iter(range(100))
    code, _ = run(tmp_path, bundle, interrupted=lambda: next(calls) >= 3)
    rows = json.loads((tmp_path / "out" / "report.json").read_bytes())["rows"]
    assert [r["status"] for r in rows[3:]] == ["not_run"] * 3
    assert {r["reason"] for r in rows[3:]} == {"interrupted"}


def test_observation_for_another_row_rejects(tmp_path, bundle):
    observed = observations(tmp_path, bundle)
    first = observed / kit.observation_name(1)
    document = json.loads(first.read_bytes())
    document["arm"] = "native" if document["arm"] != "native" else "provider_only"
    first.write_text(json.dumps(document), encoding="utf-8")
    code, result = run(tmp_path, bundle, observed=observed)
    assert (code, result["status"], result["reason"]) == (2, "rejected", "observation_row")


def test_observation_evidence_cannot_escape_its_directory(tmp_path, bundle):
    observed = observations(tmp_path, bundle)
    first = observed / kit.observation_name(1)
    document = json.loads(first.read_bytes())
    document["evidence"][0]["file"] = "../inputs.json"
    first.write_text(json.dumps(document), encoding="utf-8")
    code, result = run(tmp_path, bundle, observed=observed)
    assert (code, result["reason"]) == (2, "observation_path")


def test_changed_bytes_for_a_registered_evidence_id_reject(tmp_path, bundle):
    observed = observations(tmp_path, bundle)
    document = json.loads((observed / kit.observation_name(1)).read_bytes())
    (observed / document["evidence"][0]["file"]).write_bytes(b"different bytes")
    code, result = run(tmp_path, bundle, observed=observed)
    assert (code, result["reason"]) == (2, "evidence_conflict")


def test_provisional_requires_a_passed_preflight(tmp_path, bundle):
    code, result = run(tmp_path, bundle, qualification="provisional")
    assert (code, result["status"], result["reason"]) == (3, "blocked", "preflight_failed")
    assert not (tmp_path / "out" / "report.json").exists()


def test_provisional_trial_on_the_target_machine_records_provisional(tmp_path, bundle):
    set_lock(bundle, M1)
    preflight = preflight_record(tmp_path, bundle)
    tarball = tmp_path / "release" / "relaymind-openclaw-provider-1.0.0.tgz"
    install = kit.install_plugin(fake_host(tmp_path), tarball, tmp_path / "install")
    code, result = run(
        tmp_path,
        bundle,
        qualification="provisional",
        preflight=preflight,
        environment=M1,
        install_record=install,
        tarball=tarball,
        observed=observations(tmp_path, bundle, provenance="observed"),
    )
    report = json.loads((tmp_path / "out" / "report.json").read_bytes())
    assert report["qualification"] == "provisional" and report["environment"] == M1
    # Early trial never passes the acceptance gate; it records provisional costs.
    assert report["summary"]["arms"][0]["external_tokens"] == 30
    # #240: the companion corpus here is a fixture, so the provisional label is
    # a qualification contradiction and the grade rejects it.
    assert (code, result["status"], result["reason"]) == (
        2,
        "rejected",
        "qualification_contradiction",
    )


def test_provisional_requires_a_passed_install(tmp_path, bundle):
    set_lock(bundle, M1)
    preflight = preflight_record(tmp_path, bundle)
    tarball = tmp_path / "release" / "relaymind-openclaw-provider-1.0.0.tgz"
    install = kit.install_plugin(fake_host(tmp_path, "install-fails"), tarball, tmp_path / "i")
    code, result = run(
        tmp_path,
        bundle,
        qualification="provisional",
        preflight=preflight,
        environment=M1,
        install_record=install,
    )
    assert (code, result["reason"]) == (3, "install_failed")


def test_fixture_evidence_cannot_back_a_provisional_report(tmp_path, bundle):
    set_lock(bundle, M1)
    preflight = preflight_record(tmp_path, bundle)
    tarball = tmp_path / "release" / "relaymind-openclaw-provider-1.0.0.tgz"
    install = kit.install_plugin(fake_host(tmp_path), tarball, tmp_path / "install")
    code, result = run(
        tmp_path,
        bundle,
        qualification="provisional",
        preflight=preflight,
        environment=M1,
        install_record=install,
    )
    assert (code, result["reason"]) == (2, "observation_provenance")


def test_qualified_is_never_produced(tmp_path, bundle):
    with pytest.raises(ValueError, match="qualification"):
        run(tmp_path, bundle, qualification="qualified")


def test_acceptance_tasks_are_not_an_early_trial(tmp_path, bundle):
    entry = next(d for d in bundle.documents if d["kind"] == "host_tasks")
    tasks = dict(bundle.tasks, kind="acceptance")
    payload = _bytes(tasks)
    (bundle.root / entry["path"]).write_bytes(payload)
    entry.update(
        id="host_tasks:" + canonical_digest(tasks),
        digest=canonical_digest(tasks),
        bytes=len(payload),
    )
    code, result = run(tmp_path, bundle)
    assert (code, result["reason"]) == (2, "early_trial_required")


def test_existing_bundle_directory_is_refused(tmp_path, bundle):
    (tmp_path / "out").mkdir()
    code, result = run(tmp_path, bundle)
    assert (code, result["reason"]) == (2, "bundle_exists")


# --- one command --------------------------------------------------------------


def test_one_command_trial_runs_every_stage(tmp_path, bundle, monkeypatch):
    monkeypatch.setattr(kit, "observed_environment", lambda: ENVIRONMENT)
    tarball = release(tmp_path)
    inputs = frozen_manifest(bundle)
    observed = observations(tmp_path, bundle)
    argv = [
        "trial",
        "--inputs",
        str(inputs),
        "--tarball",
        str(tarball),
        "--observations",
        str(observed),
        "--bundle",
        str(tmp_path / "out"),
        "--work",
        str(tmp_path / "work"),
        "--resolver",
        str(bundle.resolver),
        "--qualification",
        "fixture",
        "--",
        *fake_host(tmp_path),
    ]
    assert main(argv) == 3
    out = tmp_path / "out"
    for name in ("preflight.json", "install.json", "report.json", "grade.json", "plan.json"):
        assert (out / name).is_file()
    assert json.loads((out / "install.json").read_bytes())["passed"] is True
    registry = {e["id"] for e in json.loads((out / "inputs.json").read_bytes())["evidence"]}
    assert {"trial-preflight", "trial-install"} <= registry


def test_preflight_command_exits_nonzero_off_target(tmp_path, bundle, monkeypatch):
    monkeypatch.setattr(kit, "observed_environment", lambda: ENVIRONMENT)
    tarball = release(tmp_path)
    output = tmp_path / "preflight.json"
    argv = ["trial-preflight", "--inputs", str(frozen_manifest(bundle)), "--tarball"]
    argv += [str(tarball), "--output", str(output), "--", *fake_host(tmp_path)]
    assert main(argv) == 3
    assert json.loads(output.read_bytes())["passed"] is False


# --- review follow-ups (PR #262) ----------------------------------------------


def test_preflight_with_a_missing_tarball_fails_its_release_checks(tmp_path, bundle):
    """RM39/AC30: an unreadable release is a failed check, never a crash."""
    set_lock(bundle, M1)
    record = kit.preflight(
        environment=M1,
        lock=M1,
        host_command=fake_host(tmp_path),
        tarball=tmp_path / "absent" / "relaymind-openclaw-provider-1.0.0.tgz",
        freeze=bundle.freeze,
    )
    status = checks(record)
    assert status["release_checksum"] == status["frozen_package"] == "failed"
    assert status["host_version"] == "passed" and record["passed"] is False
    assert record["observed"]["release"]["sha256"] is None


def test_inspect_parses_stdout_only_and_keeps_stderr_in_a_private_log(tmp_path):
    tarball = release(tmp_path)
    work = tmp_path / "install"
    record = kit.install_plugin(fake_host(tmp_path, "stderr-noise"), tarball, work)
    assert record["passed"] is True
    # Every lifecycle step (#269) keeps its own private stdout and stderr logs.
    assert [s["name"] for s in record["steps"]] == ["install", "configure", "enable", "inspect"]
    for step in record["steps"]:
        assert step["stderr_log"] == step["name"] + ".stderr.log"
        assert step["stderr_bytes"] == len((work / step["stderr_log"]).read_bytes())
    inspect = record["steps"][-1]
    assert inspect["inspect"]["enabled"] is True
    assert json.loads((work / inspect["log"]).read_bytes())["plugin"]["status"] == "loaded"
    stderr = (work / inspect["stderr_log"]).read_bytes()
    assert b"ExperimentalWarning" in stderr
    assert inspect["stderr_sha256"] == hashlib.sha256(stderr).hexdigest()


def test_documents_without_a_path_are_left_to_the_resolver(tmp_path, bundle):
    """A null DocumentRef path is not copied and never read as a local path."""
    entry = next(d for d in bundle.documents if d["kind"] == "referenced_config")
    entry["path"] = None
    code, result = run(tmp_path, bundle)
    assert result["reason"] != "path_traversal" and result["status"] != "rejected"
    copied = json.loads((tmp_path / "out" / "inputs.json").read_bytes())["documents"]
    assert next(d for d in copied if d["id"] == entry["id"])["path"] is None
    assert code == 3


def test_a_null_path_root_document_blocks(tmp_path, bundle):
    next(d for d in bundle.documents if d["kind"] == "host_tasks")["path"] = None
    code, result = run(tmp_path, bundle)
    assert (code, result["status"], result["reason"]) == (3, "blocked", "input_unresolved")


def test_end_of_input_stops_asking_and_leaves_rows_unobserved(tmp_path, bundle):
    observed = observations(tmp_path, bundle, skip={1, 3})
    asked = []

    def ask(text):
        asked.append(text)
        raise EOFError

    code, result = run(tmp_path, bundle, observed=observed, ask=ask)
    rows = json.loads((tmp_path / "out" / "report.json").read_bytes())["rows"]
    assert [rows[0]["reason"], rows[2]["reason"]] == ["observation_missing"] * 2
    assert rows[1]["status"] == "completed"
    assert len(asked) == 1 and result["status"] == "graded"


def cli_records(tmp_path, bundle):
    tarball = release(tmp_path)
    preflight = kit.preflight(
        environment=ENVIRONMENT,
        lock=ENVIRONMENT,
        host_command=fake_host(tmp_path),
        tarball=tarball,
        freeze=bundle.freeze,
    )
    path = tmp_path / "preflight.json"
    path.write_text(json.dumps(preflight), encoding="utf-8")
    return tarball, path


def test_trial_run_command_uses_the_preflight_environment(tmp_path, bundle, monkeypatch):
    """Offline bundling on another machine keeps the target's recorded environment."""
    monkeypatch.setattr(kit, "observed_environment", lambda: M1)
    tarball, preflight = cli_records(tmp_path, bundle)
    argv = ["trial-run", "--inputs", str(frozen_manifest(bundle)), "--observations"]
    argv += [str(observations(tmp_path, bundle)), "--bundle", str(tmp_path / "out")]
    argv += ["--preflight", str(preflight), "--resolver", str(bundle.resolver)]
    argv += ["--qualification", "fixture"]
    assert main(argv) == 3
    report = json.loads((tmp_path / "out" / "report.json").read_bytes())
    assert report["environment"] == ENVIRONMENT
    assert json.loads((tmp_path / "out" / "grade.json").read_bytes())["status"] == "graded"


def test_trial_run_command_refuses_a_preflight_without_an_environment(tmp_path, bundle):
    _, preflight = cli_records(tmp_path, bundle)
    record = json.loads(preflight.read_bytes())
    del record["observed"]["environment"]
    preflight.write_text(json.dumps(record), encoding="utf-8")
    argv = ["trial-run", "--inputs", str(frozen_manifest(bundle)), "--observations"]
    argv += [str(observations(tmp_path, bundle)), "--bundle", str(tmp_path / "out")]
    argv += ["--preflight", str(preflight), "--qualification", "fixture"]
    assert main(argv) == 2
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize(("mode", "code"), [("ok", 0), ("install-fails", 3)])
def test_trial_install_command_writes_its_record(tmp_path, mode, code):
    tarball = release(tmp_path)
    output = tmp_path / "install.json"
    argv = ["trial-install", "--tarball", str(tarball), "--work", str(tmp_path / "work")]
    argv += ["--output", str(output), "--", *fake_host(tmp_path, mode)]
    assert main(argv) == code
    assert json.loads(output.read_bytes())["passed"] is (code == 0)
