"""Evidence helper for the #259 owner-run early installed-skill trial (spec 0.66 12.6.5/12.6.6).

RM16, RM39, RM40, RM44; AC30, AC36. This is operator evidence tooling for one
trial, not product code: it only arranges files for the existing kit
(``scripts/asset_whole_task.py trial-*``, ``label-*``, ``grade-host``) and the
existing record checker. It never runs a host turn, judges a task or writes a
label.

Commands (run from the repository root with the project's Python):

``draft --root R``
    Copy the eight admissible OpenClaw 2026.10.7 bundled skill originals and
    the pinned host's captures from the private operator root ``R`` into
    ``inputs/`` and write the draft catalog, the DRAFT case, the drafted
    statements and the arm configurations (the external base URL is omitted;
    no credential is ever present in these files).
``freeze --root R --cases C --labels L``
    After the owner has authored the case(s) and the labels exist
    (``label-template`` / ``label-worksheet`` / ``label-freeze``), write the
    frozen corpus lock, the AssetHostTasksV1 / HostFreezeV1 documents, the
    companion manifest ``frozen/inputs.json`` and a private resolver for the
    inventory preimages under ``R/resolver``.
``record --bundle B --record-id ID``
    Write a pending QualificationRecordV1 (scope host_trial, owner_machine)
    binding the bundle's report, freeze, tasks, corpus lock, labels and
    environment lock, and check it with ``load_record``.
"""

import argparse
import hashlib
import json
import os
import shutil
import sys
import tomllib
import unicodedata
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = Path(os.environ.get("RELAYMIND_REPO") or HERE.parents[3])
sys.path.insert(0, str(REPO / "src"))

from relaymind.canonical import canonical_digest, canonical_json  # noqa: E402

DATASET = "m10-early-trial-m1-openclaw-bundled"
SOURCE_ID = "openclaw-bundled-trial"
HOST_VERSION = "2026.10.7"
MODEL = "glm-5.3-flash"
PROVIDER = "owner-external-openai-compatible"
SKILLS = (
    "apple-notes",
    "camsnap",
    "eightctl",
    "gifgrep",
    "nano-pdf",
    "openai-whisper",
    "songsee",
    "weather",
)
ARMS = ("native", "provider_only", "recommendations")
OWNER = "owner"
DRAFTER, ADJUDICATOR = "machine-drafter", "machine-adjudicator"
MACHINE_A, MACHINE_B = "machine-annotator-a", "machine-annotator-b"
SEED = 259


def _bytes(value) -> bytes:
    return (canonical_json(value) + "\n").encode("utf-8")


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


# --- draft -----------------------------------------------------------------------

STATEMENTS = {
    "source_provenance": (
        "The eight originals are the unmodified skills/<name>/SKILL.md files of the npm\n"
        "package openclaw@2026.10.7 (integrity in host-distribution.json), installed with\n"
        "npm ci from integrations/openclaw/package-lock.json on the owner's MacBook.\n"
    ),
    "permission": (
        "openclaw@2026.10.7 is MIT licensed (license.txt, the package LICENSE file). MIT\n"
        "permits acquisition, evaluation, retention and redistribution with the notice.\n"
    ),
    "language_review": (
        "All eight originals are written in English only; no translation or generated\n"
        "text was added. Prompts are machine drafted in English (spec 0.66 12.6.6); the\n"
        "owner reviews them in the pull request that commits the lock.\n"
    ),
    "authorship": (
        "Spec 0.66 section 12.6.6 machine_drafted_owner_approved. Every prompt was\n"
        "written by the recorded drafter machine-drafter (exact model ID in the drafting\n"
        "response, temperature 0, template machine/drafter.md) from the offered catalog\n"
        "projections and the case plan only. The drafting request and response bytes are\n"
        "evidence drafting-request and drafting-response. No draft was rejected. The\n"
        "owner approves the frozen cases in the pull request; until then they are pending.\n"
    ),
    "independence": (
        "Spec 0.66 section 12.6.6 machine_dual_owner_approved, same_family: annotators\n"
        "machine-annotator-a and machine-annotator-b (one provider family, glm) each judge\n"
        "only their own blind label template and the catalog projections, with two\n"
        "independently written templates (machine/annotator-a.md, annotator-b.md), at\n"
        "temperature 0, before the worksheet exists. Neither sees the other's judgment,\n"
        "tuning results, arm outputs or the drafter's plan.\n"
    ),
    "adjudication": (
        "The distinct machine adjudicator machine-adjudicator (template\n"
        "machine/adjudicator.md, temperature 0) completes the worksheet from the worksheet\n"
        "and the catalog projections only. The owner approves the frozen labels in the\n"
        "pull request that commits the label lock.\n"
    ),
    "split_review": ("The early-trial case is held_out; no selector or setting was tuned on it.\n"),
    "access_control": (
        "The operator tooling keeps the labels outside the gateway and host runtime\n"
        "directories until the trial inputs are frozen and committed. The owner is the\n"
        "approver (role reviewer); no person authored, annotated or adjudicated.\n"
    ),
    "derivation": (
        "Derived by docs/evidence/m10/early-trial-m1-20261101/build_trial_inputs.py from\n"
        "the private operator root; records are the gateway's own local_directory capture.\n"
    ),
}


def _records(root: Path) -> dict:
    """The product records of the trial catalog, from the gateway's own capture."""
    import sqlite3

    store = root / "gateway-recommendations" / "relaymind.sqlite3"
    connection = sqlite3.connect(store.as_uri() + "?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT relative_package_path, record_json FROM asset_catalog_assets"
        ).fetchall()
    finally:
        connection.close()
    return {path: json.loads(record) for path, record in rows}


def draft(root: Path) -> None:
    inputs = HERE / "inputs"
    skills_root = root / "cli/node_modules/openclaw/skills"
    evidence = []

    def add(evidence_id: str, kind: str, path: str, payload: bytes) -> None:
        _write(inputs / path, payload)
        evidence.append(
            dict(
                id=evidence_id,
                kind=kind,
                access="redistributable",
                path=path,
                sha256=_sha(payload),
                bytes=len(payload),
            )
        )

    license_bytes = (root / "cli/node_modules/openclaw/LICENSE").read_bytes()
    add("license", "license", "evidence/license.txt", license_bytes)
    add("notice", "notice", "evidence/notice.txt", license_bytes)
    for name, text in STATEMENTS.items():
        add(name, name, f"evidence/{name}.txt", text.encode("utf-8"))
    # The drafter's original request and response bytes (spec 12.6.6).
    for part in ("request", "response"):
        drafted = HERE / "machine/calls" / f"drafting.{part}.json"
        if drafted.exists():
            add(
                "drafting-" + part,
                "authorship",
                f"evidence/drafting.{part}.json",
                drafted.read_bytes(),
            )
    records = _records(root)
    assets = []
    for skill in SKILLS:
        original = (root / "catalog" / skill / "SKILL.md").read_bytes()
        if original != (skills_root / skill / "SKILL.md").read_bytes():
            raise SystemExit(f"{skill}: catalog copy differs from the pinned host original")
        record = records[skill]
        if record["content_hash"] != _sha(original):
            raise SystemExit(f"{skill}: product record does not bind the original bytes")
        add("original:" + skill, "original", f"originals/{skill}/SKILL.md", original)
        assets.append(
            dict(
                record=record,
                origin="other_original",
                original_languages=["en"],
                asset_family_ids=["family:" + skill],
                original_evidence_id="original:" + skill,
                source_url=f"npm:openclaw@{HOST_VERSION}/skills/{skill}/SKILL.md",
                # The record has no source_version; the host version is in source_url.
                source_revision=record["source_version"],
                rights=dict(
                    acquisition="permitted",
                    evaluation="permitted",
                    retention="permitted",
                    redistribution="permitted",
                    evidence_ids=["license", "permission"],
                    notice_evidence_ids=["notice"],
                ),
                language_review_evidence_ids=["language_review"],
                provenance_evidence_ids=["source_provenance"],
            )
        )
    assets.sort(key=lambda a: a["record"]["asset_id"].encode())
    exclusions = _read_json(HERE / "inputs/captures/projection-exclusions.json")
    catalog = dict(
        schema_version=1,
        dataset_id=DATASET,
        release=1,
        purpose="development",
        assets=assets,
        people=sorted(
            [
                dict(
                    id=MACHINE_A,
                    roles=["annotator"],
                    competencies=[],
                    evidence_ids=["independence"],
                ),
                dict(
                    id=MACHINE_B,
                    roles=["annotator"],
                    competencies=[],
                    evidence_ids=["independence"],
                ),
                dict(
                    id=ADJUDICATOR,
                    roles=["adjudicator"],
                    competencies=[],
                    evidence_ids=["adjudication"],
                ),
                dict(id=DRAFTER, roles=["author"], competencies=[], evidence_ids=["authorship"]),
                # The owner approves by pull request review only.
                dict(
                    id=OWNER, roles=["reviewer"], competencies=[], evidence_ids=["access_control"]
                ),
            ],
            key=lambda p: p["id"].encode(),
        ),
        equivalent_pairs=[],
        exclusions=[
            dict(
                source_id=SOURCE_ID,
                original_id=f"npm:openclaw@{HOST_VERSION}/skills/{row['skill']}/SKILL.md",
                reason="projection_" + row["reason"],
                evidence_ids=["source_provenance"],
            )
            for row in exclusions["excluded"]
        ],
        evidence=sorted(evidence, key=lambda e: e["id"].encode()),
    )
    _write(inputs / "catalog.json", _bytes(catalog))
    configs = inputs / "configs"
    for arm in ARMS:
        payload = (root / "host" / f"arm-{arm}.json").read_bytes()
        _write(configs / f"host-{arm}.json", _bytes(json.loads(payload)))
    for arm in ("provider_only", "recommendations"):
        text = (root / f"gateway-{arm}" / "relaymind.toml").read_text(encoding="utf-8")
        # The owner's external base URL is confidential: omitted, never hashed.
        kept = [line for line in text.splitlines() if not line.startswith("base_url")]
        tomllib.loads("\n".join(kept))
        _write(configs / f"gateway-{arm}.toml", ("\n".join(kept) + "\n").encode("utf-8"))
    print(f"draft written to {inputs}")


# --- freeze ----------------------------------------------------------------------


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
        files.append(dict(path=path, sha256=_sha(payload), bytes=len(payload)))
    return dict(schema_version=1, files=files)


class _Manifest:
    def __init__(self, out: Path):
        self.out, self.documents, self.evidence = out, [], {}

    def document(self, kind: str, value, *, payload: bytes | None = None, fmt="json") -> str:
        payload = _bytes(value) if payload is None else payload
        digest = canonical_digest(value)
        singleton = kind in ("environment_lock", "runner_inventory", "protocol_inventory")
        name = kind if singleton else kind + ":" + digest
        path = "docs/" + _sha(name.encode()) + (".toml" if fmt == "toml" else ".json")
        _write(self.out / path, payload)
        entry = dict(id=name, kind=kind, path=path, digest=digest, bytes=len(payload), format=fmt)
        if all(d["id"] != name for d in self.documents):
            self.documents.append(entry)
        return digest

    def add_evidence(self, evidence_id: str, payload: bytes, *, kind="original", access=None):
        path = "evidence/" + _sha(evidence_id.encode())
        _write(self.out / path, payload)
        self.evidence[evidence_id] = dict(
            id=evidence_id,
            kind=kind,
            access=access or "restricted",
            path=path,
            sha256=_sha(payload),
            bytes=len(payload),
        )

    def write(self) -> None:
        manifest = dict(
            schema_version=1,
            documents=sorted(self.documents, key=lambda d: d["id"].encode()),
            evidence=sorted(self.evidence.values(), key=lambda e: e["id"].encode()),
            source_proof_evidence_ids=[],
        )
        _write(self.out / "inputs.json", _bytes(manifest))


def freeze(root: Path, cases_path: Path, labels_path: Path) -> None:
    from relaymind.evaluation.asset_evidence import collect_benchmark_environment
    from relaymind.evaluation.asset_whole_task import verify_host_tasks

    inputs, out = HERE / "inputs", HERE / "frozen"
    if out.exists():
        raise SystemExit("frozen/ exists: a changed freeze is a new directory")
    catalog = _read_json(inputs / "catalog.json")
    cases, labels = _read_json(cases_path), _read_json(labels_path)
    if labels["cases_digest"] != canonical_digest(cases):
        raise SystemExit("labels do not bind these cases")
    manifest = _Manifest(out)
    for item in catalog["evidence"]:
        manifest.add_evidence(
            item["id"],
            (inputs / item["path"]).read_bytes(),
            kind=item["kind"],
            access=item["access"],
        )
        # The release verifier reads redistributable evidence at its declared path.
        manifest.evidence[item["id"]]["path"] = "release/" + item["path"]
        _write(out / "release" / item["path"], (inputs / item["path"]).read_bytes())
    frozen_at = _now()
    lock = dict(
        schema_version=1,
        dataset_id=DATASET,
        release=1,
        purpose="development",
        state="frozen",
        frozen_at=frozen_at,
        catalog=dict(path="catalog.json", digest=canonical_digest(catalog)),
        cases=dict(path="cases.json", digest=canonical_digest(cases)),
        labels=dict(path="labels.json", digest=canonical_digest(labels)),
        related_locks=[],
        evidence=[
            dict(id=e["id"], sha256=e["sha256"], bytes=e["bytes"])
            for e in sorted(catalog["evidence"], key=lambda e: e["id"].encode())
        ],
        derivation=dict(
            revision="early-trial-m1-20261101.v1",
            config_digest=canonical_digest(dict(source_id=SOURCE_ID, skills=list(SKILLS))),
            evidence_ids=["derivation"],
        ),
        unicode_version=unicodedata.unidata_version,
        qualification_evidence_ids=["access_control"],
    )
    lock["digest"] = canonical_digest(lock)
    lock_digest = manifest.document("lock", lock)
    for kind, value in (("catalog", catalog), ("cases", cases), ("labels", labels)):
        manifest.document(kind, value)
    configs = inputs / "configs"
    digests = {}
    for arm in ARMS:
        digests["host-" + arm] = manifest.document(
            "referenced_config", _read_json(configs / f"host-{arm}.json")
        )
    rec_toml = (configs / "gateway-recommendations.toml").read_bytes()
    digests["recommendations"] = manifest.document(
        "referenced_config", tomllib.loads(rec_toml.decode()), payload=rec_toml, fmt="toml"
    )
    provider = dict(
        schema_version=1,
        provider=PROVIDER,
        model=MODEL,
        api="openai-completions",
        base_url="omitted: owner-confidential",
        api_key_env="RELAYMIND_EXTERNAL_API_KEY",
        context_limit=128000,
        native_entry="host-native.json models.providers.external-direct",
        relaymind_entry="gateway-*.toml [external_model]",
    )
    sampling = dict(
        schema_version=1,
        temperature="provider default (not set by host or gateway)",
        top_p="provider default (not set by host or gateway)",
        max_tokens="provider default (not set by host or gateway)",
        thinking="provider default (agent exec --thinking not given)",
    )
    digests["provider"] = manifest.document("referenced_config", provider)
    digests["sampling"] = manifest.document("referenced_config", sampling)
    inventory = _read_json(inputs / "captures/skills-list-native.json")
    digests["inventory"] = manifest.document("referenced_config", inventory)
    tarball = root / "release/relaymind-openclaw-provider-0.1.0.tgz"
    package = tarball.read_bytes()
    manifest.add_evidence("package:relaymind-openclaw-provider-0.1.0", package)
    manifest.add_evidence(
        "host:openclaw-2026.10.7", (inputs / "captures/host-distribution.json").read_bytes()
    )
    task_policies, tasks = [], []
    bindings = sorted(
        (
            dict(
                asset_id=a["record"]["asset_id"],
                content_hash=a["record"]["content_hash"],
                schema_hash=None,
            )
            for a in catalog["assets"]
        ),
        key=lambda b: b["asset_id"].encode(),
    )
    for case in sorted(cases["cases"], key=lambda c: c["id"].encode()):
        task_id = "task:" + case["id"]
        permissions = ["openclaw.agent_exec.default_tools"]
        budgets = dict(
            schema_version=1,
            task_id=task_id,
            agent_exec_timeout_seconds=300,
            plugin_max_retries=0,
            relaymind_budgets="gateway defaults (no [budgets] table)",
            host_retries_outside_budget=0,
        )
        budgets_digest = manifest.document("referenced_config", budgets)
        rubric = (inputs / f"rubric-{case['id']}.md").read_bytes()
        setup = (inputs / "setup.md").read_bytes()
        manifest.add_evidence("rubric:" + task_id, rubric)
        manifest.add_evidence("setup:" + task_id, setup)
        task_policies.append(
            dict(task_id=task_id, permissions=permissions, budgets_digest=budgets_digest)
        )
        tasks.append(
            dict(
                id=task_id,
                lane=case["lane"],
                family_ids=list(case["task_family_ids"]),
                prompt_case_id=case["id"],
                setup_evidence_ids=["setup:" + task_id],
                success_rubric_evidence_id="rubric:" + task_id,
                permissions_digest=canonical_digest(permissions),
                budgets_digest=budgets_digest,
                asset_bindings=bindings,
                mutation=False,
            )
        )
    host_freeze = dict(
        schema_version=1,
        corpus_lock_digest=lock_digest,
        configurations=[
            dict(
                arm=arm,
                provider=PROVIDER,
                model=MODEL,
                provider_config_digest=digests["provider"],
                sampling_config_digest=digests["sampling"],
                host_config_digest=digests["host-" + arm],
                recommendation_config_digest=digests["recommendations"]
                if arm == "recommendations"
                else None,
            )
            for arm in sorted(ARMS)
        ],
        task_policies=task_policies,
        native_inventory_digest=digests["inventory"],
        packages=[
            dict(
                name="@relaymind/openclaw-provider",
                version="0.1.0",
                sha256=_sha(package),
                bytes=len(package),
                evidence_id="package:relaymind-openclaw-provider-0.1.0",
            )
        ],
        host=dict(
            name="openclaw",
            version=HOST_VERSION,
            sdk_version=HOST_VERSION,
            distribution_evidence_ids=["host:openclaw-2026.10.7"],
        ),
        asset_bindings=bindings,
        rubric_evidence_ids=sorted(t["success_rubric_evidence_id"] for t in tasks),
    )
    freeze_digest = manifest.document("host_freeze", host_freeze)
    host_tasks = dict(
        schema_version=1,
        kind="early_trial",
        corpus_lock_digest=lock_digest,
        seed=SEED,
        freeze_digest=freeze_digest,
        tasks=tasks,
    )
    verify_host_tasks(host_tasks, host_freeze)
    manifest.document("host_tasks", host_tasks)
    manifest.document("environment_lock", collect_benchmark_environment())
    entries = []
    resolver_root = root / "resolver"
    for kind in ("runner_inventory", "protocol_inventory"):
        inventory_doc = _inventory(kind)
        manifest.document(kind, inventory_doc)
        for item in inventory_doc["files"]:
            target = resolver_root / "originals" / item["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(REPO / item["path"], target)
            entries.append(
                dict(
                    id="file:" + _sha(item["path"].encode()),
                    hash_kind="original",
                    hash=item["sha256"],
                    bytes=item["bytes"],
                    path="originals/" + item["path"],
                )
            )
    entries.sort(key=lambda e: (e["id"], e["hash_kind"], e["hash"]))
    _write(resolver_root / "resolver.json", _bytes(dict(schema_version=1, entries=entries)))
    manifest.write()
    print(f"frozen inputs: {out / 'inputs.json'}; resolver: {resolver_root}")


# --- record ----------------------------------------------------------------------


def _model(call: str) -> str:
    """The exact model ID the provider returned for one recorded call."""
    return _read_json(HERE / "machine/calls" / f"{call}.response.json")["model"]


def _machine(annotator_id: str, call: str, template: str) -> dict:
    return dict(
        annotator_id=annotator_id,
        model_id=_model(call),
        prompt_sha256=_sha((HERE / "machine" / template).read_bytes()),
        temperature=0,
        saw_tuning_results=False,
        saw_arm_outputs=False,
    )


def record(bundle: Path, record_id: str) -> None:
    import socket
    import subprocess

    from relaymind.evaluation.asset_qualification_record import class_kappa, load_record

    target = REPO / "docs/evidence/m10" / f"{record_id}.json"
    manifest = _read_json(bundle / "inputs.json")
    by_kind = {d["kind"]: d for d in manifest["documents"] if d["kind"] != "referenced_config"}
    labels = _read_json(bundle / by_kind["labels"]["path"])
    group = next(g for g in labels["agreement"]["groups"] if g["lane"] == "all")
    report = _read_json(bundle / "report.json")
    # The first dispatch: native attempts carry only the host's message time.
    started = sorted(
        a["started_at"] or a["ended_at"]
        for row in report["rows"]
        for a in row["attempts"]
        if (a["started_at"] or a["ended_at"]) is not None
    )

    def subject(role: str, path: Path) -> dict:
        payload = path.read_bytes()
        rel = path.resolve().relative_to(target.parent.resolve()).as_posix()
        return dict(role=role, path=rel, sha256=_sha(payload), bytes=len(payload))

    subjects = [
        subject("report", bundle / "report.json"),
        subject("freeze", bundle / by_kind["host_freeze"]["path"]),
        subject("tasks", bundle / by_kind["host_tasks"]["path"]),
        subject("corpus_lock", bundle / by_kind["lock"]["path"]),
        subject("labels", bundle / by_kind["labels"]["path"]),
        subject("environment", bundle / by_kind["environment_lock"]["path"]),
    ]
    environment = _read_json(bundle / by_kind["environment_lock"]["path"])
    build = subprocess.run(
        ["sw_vers", "-buildVersion"], capture_output=True, text=True, check=True
    ).stdout.strip()
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    value = dict(
        schema_version=1,
        record_id=record_id,
        profile="owner_run",
        outcome="positive",
        scope="host_trial",
        labelling="machine_dual_owner_approved",
        annotators=dict(
            machine_a=_machine(MACHINE_A, "labels-annotator_a", "annotator-a.md"),
            machine_b=_machine(MACHINE_B, "labels-annotator_b", "annotator-b.md"),
            adjudicator=_machine(ADJUDICATOR, "adjudication", "adjudicator.md"),
            family_relation="same_family",
        ),
        prompt_authorship="machine_drafted_owner_approved",
        drafter=dict(
            author_id=DRAFTER,
            model_id=_model("drafting"),
            prompt_sha256=_sha((HERE / "machine/drafter.md").read_bytes()),
            temperature=0,
        ),
        agreement=dict(
            paired_cases=group["paired_cases"],
            class_agreements=group["class_agreements"],
            cohen_kappa=class_kappa(labels),
        ),
        corpus_review=None,
        preregistration=None,
        runs_started_at=started[0] if started else None,
        machine=dict(
            role="target_macos_m1_8gib",
            os=environment["os"],
            os_build=f"{environment['os_version']} {build}",
            architecture=environment["architecture"],
            cpu_model=environment["cpu"],
            ram_bytes=environment["ram_bytes"],
            hostname_sha256=_sha(socket.gethostname().encode("utf-8")),
        ),
        revisions=dict(
            repository_commit=commit,
            adapter_revision=None,
            harness_sha256=_sha(Path(__file__).read_bytes()),
        ),
        operation=None,
        subjects=sorted(subjects, key=lambda s: s["path"].encode()),
        review=dict(
            owner_login="devnullexile", pull_request=None, reviewer_logins=[], approved_at=None
        ),
        recorded_at=_now(),
    )
    _write(target, json.dumps(value, indent=1, ensure_ascii=False).encode("utf-8") + b"\n")
    checked = load_record(target)
    checked.check_labels(labels)
    checked.check_cases(_read_json(bundle / by_kind["cases"]["path"]))
    print(f"record {target} loads; approved={checked.approved}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    d = sub.add_parser("draft")
    d.add_argument("--root", type=Path, required=True)
    f = sub.add_parser("freeze")
    f.add_argument("--root", type=Path, required=True)
    f.add_argument("--cases", type=Path, required=True)
    f.add_argument("--labels", type=Path, required=True)
    r = sub.add_parser("record")
    r.add_argument("--bundle", type=Path, required=True)
    r.add_argument("--record-id", required=True)
    args = parser.parse_args(argv)
    if args.command == "draft":
        draft(args.root.absolute())
    elif args.command == "freeze":
        freeze(args.root.absolute(), args.cases.absolute(), args.labels.absolute())
    else:
        record(args.bundle.absolute(), args.record_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
