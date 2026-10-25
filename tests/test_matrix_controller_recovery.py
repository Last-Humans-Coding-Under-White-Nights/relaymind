"""RM16/RM17/RM25: finalized evidence survives a separate controller repair."""

import json
from pathlib import Path

import pytest

from helpers import load_script_module


def fixture_key(path, separator=None):
    """A source fingerprint key: native, or as recorded on a host with this separator."""
    return str(path) if separator is None else path.as_posix().replace("/", separator)


def real_corpus(tmp_path, monkeypatch, *, full=False, local=False, separator=None):
    """Real corpus checkpoint, grading and export writer; only inference is replaced."""
    driver = load_script_module("run_m8_matrix")
    runner = load_script_module("run_corpus")
    source = {
        "head": "a" * 40,
        "dirty": False,
        "package_path": str(driver.ROOT / "src/relaymind/__init__.py"),
        "source_sha256": "fixture-source",
        "files": {
            fixture_key(p.relative_to(driver.ROOT), separator): driver.byte_digest(p)
            for p in (driver.ROOT / "tests/fixtures/sandbox").rglob("*")
            if p.is_file()
        },
    }
    monkeypatch.setattr(runner.session, "source_fingerprint", lambda root: source)
    monkeypatch.setattr(runner, "machine_manifest", lambda: {})
    monkeypatch.setattr(runner, "runtime_devices", lambda: None)
    monkeypatch.setenv(runner.harness.EXTERNAL_KEY_ENV, "offline-fixture")
    cell = next(
        c
        for c in driver.matrix_cells()
        if (
            c["phase"] == "calibration" and c["configuration"] == "normal"
            if local
            else c["configuration"] == "none" and c["runner"] == "corpus"
        )
    )
    cell = {**cell, "requests": cell["requests"] if full else 2}
    if local:
        manifest = runner.harness.DEFAULT_MANIFEST
        runtime = runner.harness.Runtime(
            manifest,
            runner.harness.load_manifest(manifest),
            tmp_path / "fixture.gguf",
            "offline runtime",
        )
        monkeypatch.setattr(runner.harness, "check_runtime", lambda *a, **kw: runtime)
        monkeypatch.setattr(runner.harness.Runtime, "launch", lambda *a: (None, None, 0))
    directory = tmp_path / cell["id"] / "initial"
    env = {
        "RELAYMIND_EXTERNAL_BASE_URL": "https://example.invalid/v1",
        "RELAYMIND_EXTERNAL_MODEL": "fixture",
    }
    argv = driver.command(cell, directory, tmp_path, env, tmp_path / "llama-server")
    if not full:
        argv += ["--cases", runner.load_frozen_corpus().cases[0].case_id]
    args = runner.argument_parser().parse_args(argv[2:])

    def run_one(case, mode, **options):
        record = dict(
            run_id=None, state="error", error_code=None, reason_code=None, timed_out=False
        )
        return (
            runner.grade_record(case, mode, record, [], None, local_model_configured=False),
            record,
            [],
        )

    monkeypatch.setattr(runner, "_run_one", run_one)
    runner.run_corpus(args)
    attempt = dict(name="initial", status="interrupted", argv=argv)
    return driver, runner, cell, directory, source, attempt


def test_actual_corpus_export_recovers_without_rewriting_evidence(tmp_path, monkeypatch):
    driver, runner, cell, directory, source, attempt = real_corpus(tmp_path, monkeypatch)
    before = {name: (directory / name).read_bytes() for name in ("checkpoint.json", "export.json")}
    export = json.loads(before["export.json"])
    assert export["application"]["package_path"] == str(
        Path("<repo>") / "src/relaymind/__init__.py"
    )
    driver.reconcile_attempt(attempt, cell, directory, attempt["argv"], source)
    assert attempt["status"] == "complete" and attempt["requests"] == 2
    assert before == {name: (directory / name).read_bytes() for name in before}


@pytest.mark.parametrize(
    "change",
    ["package_path", "head", "configuration", "digest"],
    ids=["path", "source", "config", "digest"],
)
def test_scrubbed_export_does_not_accept_changed_identity(tmp_path, monkeypatch, change):
    driver, runner, cell, directory, source, attempt = real_corpus(tmp_path, monkeypatch)
    export_path, checkpoint_path = directory / "export.json", directory / "checkpoint.json"
    export = json.loads(export_path.read_text(encoding="utf-8"))
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    if change in ("package_path", "head"):
        export["application"][change] = "foreign"
    elif change == "configuration":
        export["configuration"]["performance"]["prefetch"] = True
    else:
        checkpoint["identity"]["application"]["source_sha256"] = "foreign"
    driver.atomic_json(export_path, export)
    checkpoint["result"]["export_sha256"] = driver.byte_digest(export_path)
    driver.atomic_json(checkpoint_path, checkpoint)
    with pytest.raises(ValueError, match="identity"):
        driver.reconcile_attempt(attempt, cell, directory, attempt["argv"], source)


def test_reconcile_only_does_not_read_credentials_or_launch_inference(tmp_path, monkeypatch):
    from test_m8_matrix_driver import lifecycle_fixture

    driver, out, args, state = lifecycle_fixture(tmp_path, monkeypatch)
    state["interrupt"] = True
    with pytest.raises(KeyboardInterrupt):
        driver.main(args)
    before = {p.name: p.read_bytes() for p in (out / "offline-cell/initial").glob("*.json")}
    monkeypatch.setattr(driver, "provider_environment", lambda _: pytest.fail("credentials read"))
    monkeypatch.setattr(
        driver.subprocess, "run", lambda *a, **kw: pytest.fail("inference repeated")
    )
    driver.main([a for a in args if a != "--execute"] + ["--reconcile-only"])
    index = json.loads((out / "index.json").read_text(encoding="utf-8"))
    assert index["attempts"]["offline-cell"][0]["status"] == "complete"
    assert len(index["attempts"]["offline-cell"]) == 1
    assert index["controller_history"]
    assert before == {p.name: p.read_bytes() for p in (out / "offline-cell/initial").glob("*.json")}


def test_reconcile_only_refusal_leaves_index_unchanged(tmp_path, monkeypatch):
    from test_m8_matrix_driver import lifecycle_fixture

    driver, out, args, state = lifecycle_fixture(tmp_path, monkeypatch)
    state.update(finish=False, interrupt=True)
    with pytest.raises(KeyboardInterrupt):
        driver.main(args)
    before = (out / "index.json").read_bytes()
    with pytest.raises(ValueError, match="unfinished|partial"):
        driver.main([a for a in args if a != "--execute"] + ["--reconcile-only"])
    assert (out / "index.json").read_bytes() == before


def test_m7_reconciliation_only_preserves_finalized_attempt(tmp_path, monkeypatch):
    from test_m7_matrix_driver import lifecycle

    driver, out, args, state = lifecycle(tmp_path, monkeypatch)
    state.update(finish=True, interrupt_after_finish=True)
    with pytest.raises(KeyboardInterrupt):
        driver.main(args)
    monkeypatch.setattr(driver, "provider_environment", lambda _: pytest.fail("credentials read"))
    monkeypatch.setattr(
        driver.subprocess, "run", lambda *a, **kw: pytest.fail("inference repeated")
    )
    driver.main([a for a in args if a != "--execute"] + ["--reconcile-only"])
    index = json.loads((out / "index.json").read_text(encoding="utf-8"))
    assert index["attempts"]["M7-C5"][0]["status"] == "complete"
    assert index["controller_history"]


@pytest.mark.parametrize("local", [True, False], ids=["C0", "C5"])
def test_m7_reuses_real_sanitized_unseeded_corpus_export(tmp_path, monkeypatch, local):
    driver, runner, cell, directory, source, attempt = real_corpus(
        tmp_path, monkeypatch, full=True, local=local
    )
    m7 = load_script_module("run_m7_matrix")
    m7_cell = m7.matrix_cells("a" * 40, omit_criteria=True)[0 if local else 5]
    export = json.loads((directory / "export.json").read_text(encoding="utf-8"))
    provider = {
        "RELAYMIND_EXTERNAL_BASE_URL": "https://example.invalid/v1",
        "RELAYMIND_EXTERNAL_MODEL": "fixture",
    }
    foreign = {
        "identity": {"source": source, "runtime_sha256": "runtime", "provider": provider},
        "attempts": {cell["id"]: [attempt]},
    }
    index_path = tmp_path / "index.json"
    driver.atomic_json(index_path, foreign)
    expected = m7.reuse_identity(
        m7_cell, m7.ROOT, source, export["configuration"], "runtime", provider
    )
    before = {
        p: p.read_bytes()
        for p in (index_path, directory / "checkpoint.json", directory / "export.json")
    }
    reused = m7.reuse_attempt(m7_cell, index_path, expected)
    assert reused["status"] == "complete" and reused["requests"] == cell["requests"]
    assert reused["reuse_identity"]["timing_prior"] is None
    assert before == {p: p.read_bytes() for p in before}
    # A matching raw checkpoint cannot excuse a foreign serialized source.
    export["application"]["package_path"] = "<repo>/foreign.py"
    driver.atomic_json(directory / "export.json", export)
    checkpoint = json.loads((directory / "checkpoint.json").read_text(encoding="utf-8"))
    checkpoint["result"]["export_sha256"] = driver.byte_digest(directory / "export.json")
    driver.atomic_json(directory / "checkpoint.json", checkpoint)
    with pytest.raises(ValueError, match="identity"):
        m7.reuse_attempt(m7_cell, index_path, expected)


@pytest.mark.parametrize("separator", ["/", "\\"], ids=["posix", "windows"])
@pytest.mark.parametrize("local", [True, False], ids=["C0", "C5"])
def test_m7_reuse_recognizes_fixture_keys_of_either_separator(
    tmp_path, monkeypatch, local, separator
):
    """RM16, RM17, RM25: a native fingerprint keeps its own key spelling on every host."""
    from relaymind.canonical import canonical_digest

    driver, runner, cell, directory, source, attempt = real_corpus(
        tmp_path, monkeypatch, full=True, local=local, separator=separator
    )
    m7 = load_script_module("run_m7_matrix")
    m7_cell = m7.matrix_cells("a" * 40, omit_criteria=True)[0 if local else 5]
    export = json.loads((directory / "export.json").read_text(encoding="utf-8"))
    provider = {
        "RELAYMIND_EXTERNAL_BASE_URL": "https://example.invalid/v1",
        "RELAYMIND_EXTERNAL_MODEL": "fixture",
    }
    index_path = tmp_path / "index.json"
    driver.atomic_json(
        index_path,
        {
            "identity": {"source": source, "runtime_sha256": "runtime", "provider": provider},
            "attempts": {cell["id"]: [attempt]},
        },
    )
    fixtures = source["files"]
    assert fixtures and all(separator in k for k in fixtures)
    expected = m7.reuse_identity(
        m7_cell, m7.ROOT, source, export["configuration"], "runtime", provider
    )
    # The fixture digest a host with this separator computes from its own checkout.
    expected["fixtures_sha256"] = canonical_digest(fixtures)
    before = {
        p: p.read_bytes()
        for p in (index_path, directory / "checkpoint.json", directory / "export.json")
    }
    reused = m7.reuse_attempt(m7_cell, index_path, expected)
    assert reused["status"] == "complete"
    assert reused["reuse_identity"]["fixtures_sha256"] == canonical_digest(fixtures)
    assert before == {p: p.read_bytes() for p in before}
    # A changed fixture is still refused, in either spelling.
    first = sorted(fixtures)[0]
    expected["fixtures_sha256"] = canonical_digest({**fixtures, first: "0" * 64})
    with pytest.raises(ValueError, match="identity"):
        m7.reuse_attempt(m7_cell, index_path, expected)
    assert before == {p: p.read_bytes() for p in before}


def test_recovery_never_replaces_retained_cost_with_missing_databases(tmp_path, monkeypatch):
    driver, runner, cell, directory, source, attempt = real_corpus(tmp_path, monkeypatch)
    cost = {
        "calls": 218,
        "input_tokens_known_total": 326684,
        "output_tokens_known_total": 27409,
        "input_tokens_missing_calls": 5,
        "output_tokens_missing_calls": 5,
        "unreadable_databases": [],
    }
    attempt["cost"] = cost.copy()
    with pytest.raises(ValueError, match="cost"):
        driver.reconcile_attempt(attempt, cell, directory, attempt["argv"], source)
    assert attempt["cost"] == cost


def test_controller_provenance_binds_measurement_and_original_artifacts(tmp_path):
    controller = load_script_module("matrix_controller")
    from relaymind.canonical import canonical_digest
    from relaymind.evaluation.checkpoint import atomic_json
    from relaymind.evaluation.prior import byte_digest

    index = dict(identity={"source": {"head": "r"}}, attempts={})
    path = tmp_path / "index.json"
    atomic_json(path, index)
    original_digest = byte_digest(path)
    record = {"head": "controller", "files": {"driver": "digest"}}
    artifacts = [{"checkpoint_sha256": "checkpoint", "export_sha256": "export"}]
    controller.record_controller(index, record, path, artifacts)
    history = index["controller_history"]
    assert history[0]["previous_index_sha256"] == original_digest
    assert history[0]["measurement_identity_sha256"] == canonical_digest(index["identity"])
    assert history[0]["artifacts"] == artifacts
    controller.publish_index(path, index, atomic_json)
    controller.record_controller(index, record, path, artifacts)
    assert len(history) == 1
    with pytest.raises(ValueError, match="provenance"):
        controller.record_controller(index, {"head": "foreign"}, path, artifacts)
    index["identity"]["source"]["head"] = "changed"
    with pytest.raises(ValueError, match="identity"):
        controller.record_controller(index, record, path, artifacts)


def test_split_clean_checkouts_use_controller_helpers_and_measurement_imports(tmp_path):
    """RM25: exercise actual CLI bootstrap in a fresh interpreter, never an editable import."""
    import os
    import shutil
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[1]
    controller = tmp_path / "controller"
    measured = tmp_path / "measured"
    for target in (controller, measured):
        for directory in ("src", "scripts", "evaluation", "skills", "tests/fixtures/sandbox"):
            shutil.copytree(
                root / directory,
                target / directory,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
        shutil.copytree(root / "models", target / "models", ignore=shutil.ignore_patterns("*.gguf"))
        (target / ".gitignore").write_text("__pycache__/\n*.pyc\n", encoding="utf-8")
        for command in (
            ["init", "-q"],
            ["add", "."],
            [
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "-qm",
                "Offline fixture",
            ],
        ):
            subprocess.run(["git", *command], cwd=target, check=True, capture_output=True)
    code = """
import importlib.util, json, sys
from pathlib import Path
controller, measured = map(Path, sys.argv[1:3])
sys.argv = ["run_m7_matrix.py", "--measurement-root", str(measured)]
spec = importlib.util.spec_from_file_location(
    "run_m7_matrix", controller / "scripts/run_m7_matrix.py")
d = importlib.util.module_from_spec(spec); spec.loader.exec_module(d)
source = d.measured_source(measured)
record = d.controller_record(controller, measured)
cell = d.matrix_cells(source["head"], "b" * 40)[6]
argv = d.command(cell, Path("/unused"), {
    "RELAYMIND_EXTERNAL_BASE_URL":"https://example.invalid",
    "RELAYMIND_EXTERNAL_MODEL":"fixture"}, root=Path("/criteria"))
print(json.dumps(dict(root=str(d.ROOT), source=source, controller=record,
                     helper=d.reconcile_attempt.__code__.co_filename,
                     argv=argv, c6_revision=cell["revision"],
                     child_path=d.checkout_environment(measured,{})["PYTHONPATH"])))
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(controller), str(measured)],
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        check=True,
    )
    observed = json.loads(result.stdout)
    assert observed["root"] == str(measured)
    assert observed["source"]["dirty"] is False
    assert observed["source"]["package_path"] == str(measured / "src/relaymind/__init__.py")
    assert Path(observed["helper"]) == controller / "scripts/run_m8_matrix.py"
    assert all(
        Path(p).is_relative_to(measured)
        for p in observed["controller"]["measurement_imports"].values()
    )
    assert observed["child_path"] == str(measured / "src")
    assert observed["argv"][0] == sys.executable
    assert observed["argv"][1] == str(Path("/criteria/scripts/run_corpus.py"))
    assert observed["c6_revision"] == "b" * 40


def test_reconciliation_rejects_foreign_runner_command(tmp_path, monkeypatch):
    driver, runner, cell, directory, source, attempt = real_corpus(tmp_path, monkeypatch)
    attempt["argv"][1] = str(driver.ROOT / "scripts/foreign.py")
    with pytest.raises(ValueError, match="command"):
        driver.reconcile_attempt(attempt, cell, directory, attempt["argv"], source)


def test_recovery_keeps_original_index_bytes_and_checks_snapshot(tmp_path, monkeypatch):
    from test_m8_matrix_driver import lifecycle_fixture

    driver, out, args, state = lifecycle_fixture(tmp_path, monkeypatch)
    state["interrupt"] = True
    with pytest.raises(KeyboardInterrupt):
        driver.main(args)
    path = out / "index.json"
    old = json.loads(path.read_text(encoding="utf-8"))
    old.pop("controller_history", None)  # Existing frozen controller wrote no history.
    # Nonstandard whitespace proves that preservation keeps exact bytes, not a re-encoding.
    path.write_text(json.dumps(old, separators=(",", ":")) + "\n\n", encoding="utf-8")
    original = path.read_bytes()
    offline = [a for a in args if a != "--execute"] + ["--reconcile-only"]
    driver.main(offline)
    index = json.loads(path.read_text(encoding="utf-8"))
    entry = index["controller_history"][0]
    snapshot = out / entry["previous_index_file"]
    assert snapshot.read_bytes() == original
    assert (
        json.loads(snapshot.read_bytes())["attempts"]["offline-cell"][0]["status"] == "interrupted"
    )
    snapshot.write_bytes(b"changed")
    with pytest.raises(ValueError, match="snapshot"):
        driver.main(offline)


@pytest.mark.parametrize(
    "name, expected",
    [
        ("run_m8_matrix", "a926679674e4db6323ae8f6534c0b19803dc3f51a299051dc996c7dd444b76d2"),
        ("run_m7_matrix", "3acf15f65333697c97d61db5562d760c86e641fd00f5678dbb019fa4e9a8677d"),
    ],
    ids=["M8-31-cells", "M7-9-cells"],
)
def test_recovery_preserves_reviewed_cell_and_command_contract(name, expected):
    """RM03/RM25: golden contract captured from reviewed29e04e6, paths made portable."""
    from relaymind.canonical import canonical_digest

    driver = load_script_module(name)
    env = {
        "RELAYMIND_EXTERNAL_BASE_URL": "https://example.invalid/v1",
        "RELAYMIND_EXTERNAL_MODEL": "fixture",
    }
    cells = (
        driver.matrix_cells()
        if name == "run_m8_matrix"
        else driver.matrix_cells("a" * 40, "b" * 40)
    )
    commands = []
    for cell in cells:
        directory = Path("/matrix") / cell["id"] / "initial"
        argv = (
            driver.command(cell, directory, Path("/matrix"), env, Path("/runtime/llama-server"))
            if name == "run_m8_matrix"
            else driver.command(cell, directory, env, root=driver.ROOT)
        )
        argv[0] = "<python>"
        commands.append(
            [value.replace(str(driver.ROOT), "<measurement>").replace("\\", "/") for value in argv]
        )
    assert canonical_digest(dict(cells=cells, commands=commands)) == expected


@pytest.mark.parametrize("kind", ["m8", "m7"])
def test_dirty_measurement_refused_before_provider_access(tmp_path, monkeypatch, kind):
    from test_m7_matrix_driver import lifecycle
    from test_m8_matrix_driver import lifecycle_fixture

    driver, out, args, state = (lifecycle_fixture if kind == "m8" else lifecycle)(
        tmp_path, monkeypatch
    )
    monkeypatch.setattr(
        driver,
        "source_fingerprint" if kind == "m8" else "measured_source",
        lambda _: dict(head="a" * 40, dirty=True),
    )
    monkeypatch.setattr(driver, "provider_environment", lambda _: pytest.fail("credentials read"))
    with pytest.raises(ValueError, match="clean committed"):
        driver.main(args)
    assert state["calls"] == 0
    assert not (out / "index.json").exists()
