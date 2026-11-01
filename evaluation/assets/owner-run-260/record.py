"""Pending QualificationRecordV1 drafts for the #260 reports (spec 0.66 section 12.6.6).

RM40, RM42, RM44; AC31, AC33, AC36. Writes one record per graded report into
``docs/evidence/m10/`` with every subject bound by SHA-256 and length, the
machine-dual labelling, the machine-drafted prompt authorship, the
preregistration commit and the machine identity. The review is left pending:
``pull_request``, ``reviewer_logins`` and ``approved_at`` are filled from the
pull request review after the merge, so until then the record lifts nothing.

The machine identity takes ``os_version`` from the report's measured
environment and must agree with it on every shared field; the finished record
is loaded with the packaged schema and subject checks before it is kept.
"""

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2] / "src"))


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# Report Environment field -> record machine field (spec 0.66 section 12.6.6).
_MACHINE_FIELDS = (
    ("os", "os"),
    ("architecture", "architecture"),
    ("os_version", "os_version"),
    ("cpu", "cpu_model"),
    ("ram_bytes", "ram_bytes"),
)


def machine_identity(machine: dict, environment: dict) -> dict:
    """The record machine, with ``os_version`` from the measured environment.

    A field the machine document gives must equal the environment's value.
    """
    machine = dict(machine)
    machine.setdefault("os_version", environment.get("os_version"))
    for env_field, field in _MACHINE_FIELDS:
        if field in machine and env_field in environment:
            if machine[field] != environment[env_field]:
                raise ValueError("record_machine_mismatch")
    return machine


def main() -> None:
    from relaymind.evaluation.asset_qualification_record import class_kappa, load_record

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--scope", choices=("adaptation", "host_acceptance"), required=True)
    p.add_argument("--record-id", required=True)
    p.add_argument("--record-dir", type=Path, required=True)
    p.add_argument("--subject", nargs=2, action="append", metavar=("ROLE", "PATH"), required=True)
    p.add_argument("--machine", type=Path, required=True, help="JSON machine identity")
    p.add_argument(
        "--environment", type=Path, required=True, help="the report's environment document"
    )
    p.add_argument("--prereg-commit", required=True)
    p.add_argument("--prereg-time", required=True)
    p.add_argument("--runs-started-at", required=True)
    p.add_argument("--repository-commit", required=True)
    p.add_argument("--adapter-revision")
    p.add_argument("--harness", type=Path, required=True)
    p.add_argument("--owner-login", required=True)
    args = p.parse_args()
    labels = json.loads((HERE / "labels.json").read_text(encoding="utf-8"))
    group = next(g for g in labels["agreement"]["groups"] if g["lane"] == "all")

    def machine(annotator_id: str, template: str) -> dict:
        return dict(
            annotator_id=annotator_id,
            model_id="glm-5.3",
            prompt_sha256=sha(HERE / "prompts" / template),
            temperature=0,
            saw_tuning_results=False,
            saw_arm_outputs=False,
        )

    root = args.record_dir.absolute()
    subjects = []
    for role, path in args.subject:
        path = Path(path).absolute()
        relative = path.relative_to(root).as_posix()
        subjects.append(dict(role=role, path=relative, sha256=sha(path), bytes=path.stat().st_size))
    subjects.sort(key=lambda s: s["path"].encode())
    record = dict(
        schema_version=1,
        record_id=args.record_id,
        profile="owner_run",
        outcome="positive",
        scope=args.scope,
        labelling="machine_dual_owner_approved",
        annotators=dict(
            machine_a=machine("machine-annotator-a", "annotator-a.txt"),
            machine_b=machine("machine-annotator-b", "annotator-b.txt"),
            adjudicator=machine("machine-adjudicator", "adjudicator.txt"),
            family_relation="same_family",
        ),
        agreement=dict(
            paired_cases=group["paired_cases"],
            class_agreements=group["class_agreements"],
            cohen_kappa=class_kappa(labels),
        ),
        prompt_authorship="machine_drafted_owner_approved",
        drafter=dict(
            author_id="machine-drafter",
            model_id="glm-5.3",
            prompt_sha256=sha(HERE / "prompts" / "drafter.txt"),
            temperature=0,
        ),
        corpus_review=None,
        preregistration=dict(commit=args.prereg_commit, committed_at=args.prereg_time),
        runs_started_at=args.runs_started_at,
        machine=machine_identity(
            json.loads(args.machine.read_text(encoding="utf-8")),
            json.loads(args.environment.read_text(encoding="utf-8")),
        ),
        revisions=dict(
            repository_commit=args.repository_commit,
            adapter_revision=args.adapter_revision,
            harness_sha256=sha(args.harness),
        ),
        operation=None,
        subjects=subjects,
        review=dict(
            owner_login=args.owner_login, pull_request=None, reviewer_logins=[], approved_at=None
        ),
        recorded_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    out = root / f"{args.record_id}.json"
    out.write_text(json.dumps(record, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    try:
        load_record(out)
    except Exception:
        out.unlink()
        raise
    print(out)


if __name__ == "__main__":
    main()
