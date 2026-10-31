"""Spec 0.60 section 12.6.5 companion input resolution and the export command.

RM16, RM40, RM44; AC31, AC36, AC37. Builds a real on-disk companion bundle
(frozen tasks, freeze, referenced configurations, environment lock and the
actual runner/protocol inventories with an explicit resolver) and checks that
missing inputs block with a nonzero exit while contradictions reject.
"""

import hashlib
import json
import shutil
from pathlib import Path

import pytest
from whole_task_helpers import ENVIRONMENT, freeze_doc, ordered_rows, report_for, row, tasks_doc

from relaymind.canonical import canonical_digest, canonical_json
from relaymind.evaluation.asset_platform_export import PLATFORM_STEPS, platform_gate
from relaymind.evaluation.asset_whole_task import ARMS, WholeTaskRejected, host_summary
from relaymind.evaluation.asset_whole_task_cli import main
from relaymind.evaluation.asset_whole_task_inputs import (
    WholeTaskBlocked,
    resolve_whole_task_inputs,
    verify_whole_task_inventory,
)

REPO = Path(__file__).resolve().parents[1]


def _bytes(value) -> bytes:
    return (canonical_json(value) + "\n").encode("utf-8")


def _inventory(kind: str) -> dict:
    if kind == "runner_inventory":
        paths = ["pyproject.toml", "scripts/asset_whole_task.py"]
        paths += [
            p.relative_to(REPO).as_posix()
            for p in (REPO / "src/relaymind").rglob("*.py")
            if "__pycache__" not in p.parts
        ]
    else:
        paths = ["docs/SPECIFICATION.md"]
        paths += [p.relative_to(REPO).as_posix() for p in (REPO / "src/relaymind").rglob("*.json")]
    files = []
    for path in sorted(set(paths)):
        payload = (REPO / path).read_bytes()
        files.append(
            dict(path=path, sha256=hashlib.sha256(payload).hexdigest(), bytes=len(payload))
        )
    return dict(schema_version=1, files=files)


class Bundle:
    """One companion bundle written below tmp_path, with no implicit lookups."""

    def __init__(self, tmp_path: Path):
        self.root = tmp_path / "bundle"
        self.root.mkdir()
        configs = {name: {"name": name, "value": 1} for name in ("provider", "sampling")}
        for arm in ARMS:
            configs["host-" + arm] = {"host": arm}
        configs["recommendations"] = {"enabled": True}
        configs["native-inventory"] = {"tools": ["a", "b"]}
        for task_id in ("t1", "t2"):
            configs["budget-" + task_id] = {"tool_calls": 3, "task": task_id}
        self.configs = configs
        digest = {k: canonical_digest(v) for k, v in configs.items()}
        freeze = freeze_doc()
        for config in freeze["configurations"]:
            config.update(
                provider_config_digest=digest["provider"],
                sampling_config_digest=digest["sampling"],
                host_config_digest=digest["host-" + config["arm"]],
            )
            if config["arm"] == "recommendations":
                config["recommendation_config_digest"] = digest["recommendations"]
        freeze["native_inventory_digest"] = digest["native-inventory"]
        for policy in freeze["task_policies"]:
            policy["budgets_digest"] = digest["budget-" + policy["task_id"]]
        self.freeze = freeze
        self.tasks = tasks_doc(freeze)
        self.documents = []
        self.evidence = []
        self.add("host_tasks", self.tasks)
        self.add("host_freeze", freeze)
        for value in configs.values():
            self.add("referenced_config", value)
        self.add("environment_lock", ENVIRONMENT)
        resolver_entries = []
        for kind in ("runner_inventory", "protocol_inventory"):
            inventory = _inventory(kind)
            self.add(kind, inventory)
            for item in inventory["files"]:
                target = self.root / "originals" / item["path"]
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(REPO / item["path"], target)
                resolver_entries.append(
                    dict(
                        id="file:" + hashlib.sha256(item["path"].encode()).hexdigest(),
                        hash_kind="original",
                        hash=item["sha256"],
                        bytes=item["bytes"],
                        path="originals/" + item["path"],
                    )
                )
        resolver_entries.sort(key=lambda e: (e["id"], e["hash_kind"], e["hash"]))
        self.resolver = self.root / "resolver.json"
        self.resolver.write_bytes(_bytes(dict(schema_version=1, entries=resolver_entries)))

    def add(self, kind: str, value: dict) -> None:
        payload = _bytes(value)
        digest = canonical_digest(value)
        name = (
            kind
            if kind in ("environment_lock", "runner_inventory", "protocol_inventory")
            else (kind + ":" + digest)
        )
        path = "docs/" + hashlib.sha256(name.encode()).hexdigest() + ".json"
        (self.root / "docs").mkdir(exist_ok=True)
        (self.root / path).write_bytes(payload)
        self.documents.append(
            dict(id=name, kind=kind, path=path, digest=digest, bytes=len(payload), format="json")
        )

    def write_manifest(self, *, drop: str | None = None) -> dict:
        documents = sorted(
            (d for d in self.documents if drop is None or not d["id"].startswith(drop)),
            key=lambda d: d["id"],
        )
        evidence = []
        for i in sorted(self.evidence_ids()):
            payload = i.encode()
            (self.root / "evidence").mkdir(exist_ok=True)
            (self.root / "evidence" / i).write_bytes(payload)
            evidence.append(
                dict(
                    id=i,
                    kind="original",
                    access="restricted",
                    path="evidence/" + i,
                    sha256=hashlib.sha256(payload).hexdigest(),
                    bytes=len(payload),
                )
            )
        manifest = dict(
            schema_version=1, documents=documents, evidence=evidence, source_proof_evidence_ids=[]
        )
        payload = _bytes(manifest)
        (self.root / "inputs.json").write_bytes(payload)
        return dict(path="inputs.json", digest=canonical_digest(manifest), bytes=len(payload))

    def evidence_ids(self) -> set[str]:
        ids = {"ev-package", "ev-host", *self.freeze["rubric_evidence_ids"]}
        for task in self.tasks["tasks"]:
            ids.update(task["setup_evidence_ids"])
        for r in ordered_rows(self.tasks, row):
            ids.update(r["evidence_ids"])
            for a in r["attempts"]:
                ids.update(a["evidence_ids"])
        return ids | {"ops", "approval"} | {"receipt-" + s for s in PLATFORM_STEPS}

    def host_report(self, **manifest_kwargs) -> dict:
        reference = self.write_manifest(**manifest_kwargs)
        report = report_for(self.freeze, self.tasks, ordered_rows(self.tasks, row))
        report["input_manifest"] = reference
        report["evidence"] = [
            dict(
                id=e["id"],
                path=e["path"],
                sha256=e["sha256"],
                bytes=e["bytes"],
                provenance="fixture",
            )
            for e in json.loads((self.root / "inputs.json").read_text())["evidence"]
            if e["id"] in {e2["id"] for e2 in report["evidence"]}
        ]
        report["summary"] = host_summary(report, self.tasks, self.freeze)
        return report

    def save(self, name: str, value: dict) -> Path:
        path = self.root / name
        path.write_bytes(_bytes(value))
        return path


@pytest.fixture
def bundle(tmp_path):
    return Bundle(tmp_path)


def _grade(bundle: Bundle, command: str, report: dict, *, resolver=True) -> tuple[int, dict]:
    output = bundle.root / (command + ".out.json")
    argv = [
        command,
        "--report",
        str(bundle.save(command + ".json", report)),
        "--inputs",
        str(bundle.root / "inputs.json"),
        "--output",
        str(output),
    ]
    if resolver:
        argv += ["--resolver", str(bundle.resolver)]
    code = main(argv)
    return code, json.loads(output.read_text())


def test_complete_bundle_resolves_and_binds_every_frozen_input(bundle):
    report = bundle.host_report()
    from relaymind.evaluation.asset_evidence import OfflineResolver

    inputs = resolve_whole_task_inputs(
        "host", report, bundle.root / "inputs.json", resolver=OfflineResolver(bundle.resolver)
    )
    assert inputs.root == bundle.tasks and inputs.freeze == bundle.freeze
    assert set(inputs.configs) == {canonical_digest(v) for v in bundle.configs.values()}
    assert inputs.registry["ev-package"]["path"] == "evidence/ev-package"


def test_fixture_host_report_grades_but_exits_blocked(bundle):
    code, result = _grade(bundle, "grade-host", bundle.host_report())
    assert code == 3
    assert result["status"] == "graded" and result["gate"] == "blocked"
    assert result["summary"]["success_gate"] == "pass"


def test_missing_referenced_configuration_blocks(bundle):
    digest = canonical_digest(bundle.configs["sampling"])
    code, result = _grade(
        bundle, "grade-host", bundle.host_report(drop="referenced_config:" + digest)
    )
    assert (code, result["status"], result["reason"]) == (
        3,
        "blocked",
        "referenced_config_unresolved",
    )


def test_missing_root_task_document_blocks(bundle):
    code, result = _grade(bundle, "grade-host", bundle.host_report(drop="host_tasks:"))
    assert (code, result["reason"]) == (3, "root_input_unresolved")


def test_inventory_preimages_without_a_resolver_block(bundle):
    code, result = _grade(bundle, "grade-host", bundle.host_report(), resolver=False)
    assert (code, result["reason"]) == (3, "inventory_preimage_unresolved")


def test_manifest_digest_mismatch_rejects(bundle):
    report = bundle.host_report()
    report["input_manifest"]["digest"] = "0" * 64
    code, result = _grade(bundle, "grade-host", report)
    assert (code, result["status"], result["reason"]) == (2, "rejected", "input_manifest_digest")


def test_changed_document_bytes_reject(bundle):
    report = bundle.host_report()
    entry = next(d for d in bundle.documents if d["kind"] == "host_freeze")
    path = bundle.root / entry["path"]
    path.write_bytes(path.read_bytes().replace(b"openclaw", b"openclax"))
    code, result = _grade(bundle, "grade-host", report)
    assert (code, result["reason"]) == (2, "input_digest")


def test_environment_must_match_the_lock(bundle):
    report = bundle.host_report()
    report["environment"] = dict(report["environment"], architecture="arm64")
    code, result = _grade(bundle, "grade-host", report)
    assert (code, result["reason"]) == (2, "environment_binding")


def test_inventory_of_another_implementation_rejects():
    inventory = _inventory("runner_inventory")
    inventory["files"] = [
        f for f in inventory["files"] if not f["path"].endswith("asset_whole_task.py")
    ]
    with pytest.raises(WholeTaskRejected, match="inventory_membership"):
        verify_whole_task_inventory("runner_inventory", inventory)
    inventory = _inventory("runner_inventory")
    inventory["files"][-1] = dict(inventory["files"][-1], sha256="0" * 64)
    with pytest.raises(WholeTaskRejected, match="unsupported_generator"):
        verify_whole_task_inventory("runner_inventory", inventory)


def test_actual_inventories_verify():
    verify_whole_task_inventory("runner_inventory", _inventory("runner_inventory"))
    verify_whole_task_inventory("protocol_inventory", _inventory("protocol_inventory"))


def test_fixture_platform_report_exits_not_applicable(bundle):
    reference = bundle.write_manifest()
    registry = {
        e["id"]: e for e in json.loads((bundle.root / "inputs.json").read_text())["evidence"]
    }

    def ref(i):
        e = registry[i]
        return dict(
            id=i, path=e["path"], sha256=e["sha256"], bytes=e["bytes"], provenance="fixture"
        )

    steps = [
        dict(name=s, status="passed", evidence_ids=["receipt-" + s], reason=None)
        for s in PLATFORM_STEPS
    ]
    evidence = [
        ref(i) for i in sorted({"ops", "approval", *("receipt-" + s for s in PLATFORM_STEPS)})
    ]
    report = dict(
        schema_version=1,
        qualification="fixture",
        input_digest=canonical_digest(bundle.tasks),
        input_manifest=reference,
        platform=ENVIRONMENT,
        artifacts=[],
        steps=steps,
        operations_evidence=ref("ops"),
        approvals_evidence=[ref("approval")],
        evidence=evidence,
        gate=None,
    )
    report["gate"] = platform_gate(report)
    code, result = _grade(bundle, "grade-platform", report)
    assert (code, result["gate"]) == (0, "not_applicable")


def test_plan_command_writes_the_frozen_plan(bundle):
    output = bundle.root / "plan.json"
    code = main(
        [
            "plan-host",
            "--tasks",
            str(bundle.save("tasks.json", bundle.tasks)),
            "--freeze",
            str(bundle.save("freeze.json", bundle.freeze)),
            "--output",
            str(output),
        ]
    )
    planned = json.loads(output.read_text())["planned"]
    assert code == 0 and len(planned) == 6 and planned[0]["ordinal"] == 1


def test_plan_command_rejects_an_unsorted_task_document(bundle, capsys):
    tasks = dict(bundle.tasks, tasks=list(reversed(bundle.tasks["tasks"])))
    code = main(
        [
            "plan-host",
            "--tasks",
            str(bundle.save("tasks.json", tasks)),
            "--freeze",
            str(bundle.save("freeze.json", bundle.freeze)),
            "--output",
            str(bundle.root / "plan.json"),
        ]
    )
    assert code == 2 and "task_order" in capsys.readouterr().err
    assert not (bundle.root / "plan.json").exists()


def test_blocked_error_carries_a_fixed_reason():
    assert WholeTaskBlocked("input_unresolved").reason == "input_unresolved"
