"""RM16/RM40/RM44, AC31/AC36: future trial evidence, spec 0.66 section 12.6.6.

Synthetic executions only; the historical harness and bound evidence stay intact.
"""

import hashlib
import importlib.util
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from relaymind.canonical import canonical_digest

REPO = Path(__file__).resolve().parents[1]
TRIAL = REPO / "docs/evidence/m10/early-trial-m1-20261101"


def module(path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


@pytest.fixture
def builder():
    return module(REPO / "scripts/m1_trial_inputs.py")


@pytest.fixture(params=["archived", "maintained"])
def observer(request):
    path = (
        TRIAL / "observe_row.py"
        if request.param == "archived"
        else REPO / "scripts/m1_trial_observe.py"
    )
    return module(path)


def test_unknown_start_is_never_replaced_by_an_end(builder):
    report = dict(
        rows=[
            dict(
                status="completed",
                attempts=[dict(started_at=None, ended_at="2026-11-01T10:00:10Z")],
            )
        ]
    )
    assert builder._first_dispatch(report) is None
    # A later known dispatch does not repair an unknown first row.
    report["rows"].append(
        dict(status="completed", attempts=[dict(started_at="2026-11-01T10:00:20Z", ended_at=None)])
    )
    assert builder._first_dispatch(report) is None


@pytest.mark.parametrize("rerun", [False, True])
def test_superseded_drafts_are_bound_through_freeze(builder, tmp_path, monkeypatch, rerun):
    passes = module(TRIAL / "machine_passes.py")
    trial, root = tmp_path / "trial", tmp_path / "operator"
    shutil.copytree(TRIAL / "inputs", trial / "inputs")
    shutil.copytree(TRIAL / "machine", trial / "machine")
    monkeypatch.setattr(builder, "HERE", trial, raising=False)
    monkeypatch.setattr(passes, "CALLS", trial / "machine/calls")
    # Start with no calls, then reject the first pair.
    shutil.rmtree(passes.CALLS)
    passes.keep_call("drafting", b"rejected request", b"rejected response")
    passes.keep_call("drafting", b"accepted request", b"accepted response")
    original = json.loads((trial / "inputs/catalog.json").read_text())
    records = {a["record"]["identity"]["package_id"]: a["record"] for a in original["assets"]}
    monkeypatch.setattr(builder, "_records", lambda root: records)
    monkeypatch.setattr(builder, "_inventory", lambda kind: dict(schema_version=1, files=[]))
    for skill in builder.SKILLS:
        payload = (trial / "inputs/originals" / skill / "SKILL.md").read_bytes()
        for dest in (
            root / "catalog" / skill / "SKILL.md",
            root / "cli/node_modules/openclaw/skills" / skill / "SKILL.md",
        ):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(payload)
    (root / "cli/node_modules/openclaw/LICENSE").write_bytes(b"fixture license")
    for arm in builder.ARMS:
        dest = root / "host" / f"arm-{arm}.json"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(trial / "inputs/configs" / f"host-{arm}.json", dest)
    for arm in ("provider_only", "recommendations"):
        dest = root / f"gateway-{arm}" / "relaymind.toml"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(trial / "inputs/configs" / f"gateway-{arm}.toml", dest)
    (root / "release").mkdir()
    (root / "release/relaymind-openclaw-provider-0.1.0.tgz").write_bytes(b"fixture package")
    builder.draft(root)
    catalog = json.loads((trial / "inputs/catalog.json").read_text())
    rejected = {e["id"]: e for e in catalog["evidence"] if "superseded" in e["id"]}
    assert set(rejected) == {"drafting-superseded-1-request", "drafting-superseded-1-response"}
    assert b"No draft was rejected" not in (trial / "inputs/evidence/authorship.txt").read_bytes()
    cases_path = trial / "inputs/cases.json"
    cases = json.loads(cases_path.read_text())
    for case in cases["cases"]:
        assert set(rejected) <= set(case["authorship_evidence_ids"])
    # Labels are produced only after the draft evidence is finalized.
    labels_path = trial / "inputs/labels.json"
    labels = json.loads(labels_path.read_text())
    labels["cases_digest"] = canonical_digest(cases)
    labels_path.write_text(json.dumps(labels))
    if rerun:
        passes.keep_call("drafting", b"late request", b"late response")
        with pytest.raises(SystemExit, match="drafting_evidence_stale"):
            builder.freeze(root, cases_path, labels_path)
        assert not (trial / "frozen").exists()
        return
    builder.freeze(root, cases_path, labels_path)
    manifest = json.loads((trial / "frozen/inputs.json").read_text())
    lock_ref = next(d for d in manifest["documents"] if d["kind"] == "lock")
    lock = json.loads((trial / "frozen" / lock_ref["path"]).read_text())
    for evidence_id, ref in rejected.items():
        item = next(e for e in manifest["evidence"] if e["id"] == evidence_id)
        payload = (trial / "frozen" / item["path"]).read_bytes()
        bound = next(e for e in lock["evidence"] if e["id"] == evidence_id)
        assert bound["sha256"] == ref["sha256"] == hashlib.sha256(payload).hexdigest()
        assert bound["bytes"] == len(payload)


def prepare_observation(observer, tmp_path, monkeypatch, arm):
    root, obs = tmp_path / "root", tmp_path / "observations"
    config = {}
    config_path = root / "host" / f"arm-{arm}.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(json.dumps(config))
    task = dict(id="task-1", prompt_case_id="case-1")
    freeze = dict(
        configurations=[
            dict(arm=arm, host_config_digest=canonical_digest(config), provider="fixture")
        ]
    )
    monkeypatch.setattr(
        observer,
        "_frozen",
        lambda path: (dict(tasks=[task]), freeze, {"case-1": dict(task_text="test")}),
    )
    monkeypatch.setattr(observer, "HERE", TRIAL, raising=False)
    monkeypatch.setattr(observer, "_machine_names", lambda: [b"fixture-machine"])
    monkeypatch.setattr(
        observer,
        "_transcript",
        lambda *a: [
            dict(
                event=dict(
                    message=dict(
                        role="assistant",
                        model="fixture",
                        timestamp=20000,
                        responseId="r",
                        text="fixture-machine",
                    )
                )
            )
        ],
    )
    monkeypatch.setattr(observer, "_run_ids", lambda *a: set())
    monkeypatch.setattr(observer, "_gateway_events", lambda *a: [dict(text="fixture-machine")])
    import relaymind.evaluation.asset_host_runner as runner

    monkeypatch.setattr(runner, "attempts_from_trace", lambda *a, **kw: [])
    gateway = root / f"gateway-{arm}"
    gateway.mkdir()
    (gateway / "stdout.log").write_text("Application token: fixture-token\n")
    for key in observer.SECRETS:
        monkeypatch.setenv(key, "fixture-credential")
    clock = iter([10.0, 21.0])
    monkeypatch.setattr(observer.time, "time", lambda: next(clock))
    seen = []

    def run(argv, **kwargs):
        if "exec" in argv:
            path = obs / "evidence/0001/host-dispatch.json"
            seen.append(json.loads(path.read_text()) if path.exists() else None)
        return subprocess.CompletedProcess(
            argv, 0, stdout=b'{"sessionId":"session-1","text":"fixture-machine"}'
        )

    monkeypatch.setattr(observer.subprocess, "run", run)
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps(dict(planned=[dict(ordinal=1, task_id="task-1", arm=arm)])))
    receipt = tmp_path / "receipt.json"
    receipt.write_text('{"recommendation_id":"rec-1","text":"fixture-machine"}')
    args = SimpleNamespace(
        root=root,
        observations=obs,
        inputs=tmp_path / "inputs.json",
        plan=plan,
        ordinal=1,
        not_run=None,
        timeout=10,
        recommendation_receipt=receipt,
        interactions=0,
        human_wait_ms=0,
    )
    return args, seen


@pytest.mark.parametrize("arm", ["native", "provider_only", "recommendations"])
def test_every_kept_payload_redacts_machine_names(observer, tmp_path, monkeypatch, arm):
    """RM16, AC36: test actual file writes, including the early envelope write."""
    args, _ = prepare_observation(observer, tmp_path, monkeypatch, arm)
    observer.observe(args)
    names = ["host-envelope.json", "host-transcript.jsonl", "recommendation-receipt.json"]
    if arm != "native":
        names.append("gateway-trace.json")
    for name in names:
        payload = (args.observations / "evidence/0001" / name).read_bytes()
        assert b"fixture-machine" not in payload
        assert b"<machine-name>" in payload


def test_dispatch_is_persisted_before_host_execution(tmp_path, monkeypatch):
    observer = module(REPO / "scripts/m1_trial_observe.py")
    args, seen = prepare_observation(observer, tmp_path, monkeypatch, "native")
    observer.observe(args)
    assert seen == [
        dict(
            schema_version=1,
            task_id="task-1",
            arm="native",
            started_at="1970-01-01T00:00:10.000000Z",
        )
    ]
    observation = json.loads((args.observations / "row-0001.json").read_text())
    assert "host-dispatch:0001" in observation["row"]["evidence_ids"]
    assert any(e["id"] == "host-dispatch:0001" for e in observation["evidence"])
    # Native inference start remains unknown; host dispatch is separate evidence.
    assert observation["row"]["attempts"][0]["started_at"] is None


def test_machine_names_include_all_system_names(observer, monkeypatch):
    """RM16/AC36: discovery includes full and short names, longest first."""
    import socket

    monkeypatch.setattr(socket, "gethostname", lambda: "fixture-host.invalid")
    names = dict(
        ComputerName="fixture computer",
        LocalHostName="fixture-local",
        HostName="fixture-host.invalid",
    )
    monkeypatch.setattr(
        observer.subprocess,
        "run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 0, stdout=names[argv[-1]] + "\n"),
    )
    result = observer._machine_names()
    assert set(result) == {
        b"fixture-host.invalid",
        b"fixture-host",
        b"fixture computer",
        b"fixture-local",
    }
    assert result == sorted(result, key=len, reverse=True)
    payload = b" / ".join(result)
    assert observer.redact(payload) == b" / ".join([b"<machine-name>"] * len(result))


def test_record_checks_repository_paths_with_an_external_trial(builder, tmp_path, monkeypatch):
    """RM16/AC36: committing the maintained harness does not require private files."""
    monkeypatch.setattr(builder, "HERE", tmp_path / "private-trial", raising=False)

    def run(argv, **kwargs):
        paths = argv[argv.index("--") + 1 :]
        assert all(Path(path).is_relative_to(builder.REPO) for path in paths)
        return subprocess.CompletedProcess(argv, 0, stdout="")

    monkeypatch.setattr(subprocess, "run", run)
    builder._check_committed()


def test_record_requires_published_bundle(builder, tmp_path, monkeypatch):
    """RM16/AC36: record subjects must have contained repository paths."""
    monkeypatch.setattr(builder, "REPO", tmp_path / "repo")
    monkeypatch.setattr(builder, "_check_committed", lambda: None)
    with pytest.raises(SystemExit, match="publish the bundle under docs/evidence/m10"):
        builder.record(tmp_path / "private-bundle", "new-trial", None, "fixture-owner")


def test_untracked_maintained_harness_is_not_a_committed_revision(builder, tmp_path, monkeypatch):
    """RM16/AC36: an untracked script cannot claim to belong to HEAD."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/m1_trial_inputs.py").write_text("# untracked fixture\n")
    monkeypatch.setattr(builder, "REPO", tmp_path)
    with pytest.raises(SystemExit, match="harness_uncommitted"):
        builder._check_committed()
