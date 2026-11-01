"""Owner-run qualification records (spec 0.66 section 12.6.6; #251).

RM16, RM39, RM40, RM42, RM44; AC30, AC31, AC33, AC35, AC36, AC37. A
QualificationRecordV1 is a closed JSON file committed under
``docs/evidence/m10/``. It binds by SHA-256 the exact bytes of the reports,
freeze, task or episode documents, corpus lock and labels, captures, machine
environment and media it covers, records the machine identity and the harness
and adapter revisions, and is approved in a pull request by at least one
organization member other than the owner. The pull request number, reviewer
logins and approval time are written into the record after the merge.

``load_record`` checks the closed shape, the cross-field rules and every bound
digest; any mismatch rejects. A record only lifts the
``qualified_evidence_review`` reason of a report it binds, and only once its
review is complete; it never lifts a fixture input or another unverified
input. A ``negative`` record closes a host-operation gate with the operation
disabled and listed as a known host limitation (``known_host_limitations``).

Software cannot authenticate the reviewer, the machine or the annotators from
strings and hashes; the record is the reviewed assertion the specification
names, and the git history and pull request review are its audit trail.
"""

import hashlib
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from relaymind.canonical import canonical_digest
from relaymind.evaluation.asset_benchmark_io import (
    REPORT_BYTES,
    read_json_path,
    validate_closed_document,
)
from relaymind.evaluation.asset_corpus_io import DOCUMENT_BYTES, parse_json, read_local
from relaymind.evaluation.asset_judgments import METHODS
from relaymind.evaluation.asset_qualification import CustodianReview
from relaymind.evaluation.asset_whole_task import WholeTaskRejected
from relaymind.schema_util import load_packaged_schema

_SCHEMA = load_packaged_schema("relaymind.evaluation", "asset_qualification_record.v1.json")
GIB8 = 8 * 1024**3
# Which assessment kind each report scope qualifies.
REPORT_SCOPES = {
    "host_trial": "host",
    "host_acceptance": "host",
    "adaptation": "adaptation",
    "platform": "platform",
}
_REQUIRED_SUBJECTS = {
    "host_trial": ("report", "freeze", "tasks"),
    "host_acceptance": ("report", "freeze", "tasks"),
    "adaptation": ("report", "freeze", "episodes"),
    "platform": ("report",),
    "walkthrough": ("media",),
    "host_operation": ("capture",),
}
_LABELLED = ("host_trial", "host_acceptance", "adaptation")
_PREREGISTERED = ("host_acceptance", "adaptation")
_MACHINE_SCOPES = ("platform", "walkthrough", "host_operation")
_NEGATIVE_FINDINGS = {"skill_loaded_use": "stop_point_absent", "mcp_use": "exact_route_absent"}
_KAPPA_TOLERANCE = 1e-9
# JSON input documents whose canonical digest must equal the resolved input.
_INPUT_ROLES = ("freeze", "tasks", "episodes", "labels")
_HOST_SCOPES = {"early_trial": "host_trial", "acceptance": "host_acceptance"}


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _reject(reason: str):
    raise WholeTaskRejected(reason)


def check_machine(machine: Mapping) -> None:
    """Spec 12.6.6 machine roles: the M1 8 GiB Mac, native x86-64 Windows, Linux host."""
    role, os_name, arch = machine["role"], machine["os"], machine["architecture"]
    if role == "target_macos_m1_8gib":
        ok = (
            os_name == "Darwin"
            and arch == "arm64"
            and machine["cpu_model"] == "Apple M1"
            and machine["ram_bytes"] == GIB8
        )
    elif role == "target_windows_x64":
        ok = os_name == "Windows" and arch in ("AMD64", "x86_64")
    else:
        ok = os_name == "Linux"
    if not ok:
        _reject("machine_identity")


def _check_rules(record: Mapping) -> None:
    scope, outcome, labelling = record["scope"], record["outcome"], record["labelling"]
    roles = Counter(subject["role"] for subject in record["subjects"])
    paths = [subject["path"] for subject in record["subjects"]]
    if len(set(paths)) != len(paths) or paths != sorted(paths, key=str.encode):
        _reject("record_subjects")
    if any(roles[role] < 1 for role in _REQUIRED_SUBJECTS[scope]):
        _reject("record_subjects")
    # One unambiguous subject per bound input document.
    if any(roles[role] > 1 for role in ("report", *_INPUT_ROLES)):
        _reject("record_subjects")
    # Labelling: owner_machine names both annotators and the raw agreement.
    annotators, agreement = record["annotators"], record["agreement"]
    if scope in _LABELLED and labelling == "not_applicable":
        _reject("labelling_profile")
    if scope not in _LABELLED and labelling != "not_applicable":
        _reject("labelling_profile")
    if labelling == "owner_machine" and (
        annotators is None
        or agreement is None
        or annotators["owner_annotator_id"] == annotators["machine"]["annotator_id"]
    ):
        _reject("labelling_profile")
    if labelling != "owner_machine" and annotators is not None:
        _reject("labelling_profile")
    if labelling == "not_applicable" and (agreement or record["corpus_review"]):
        _reject("labelling_profile")
    # Preregistration: a commit and its time before any run.
    prereg, started = record["preregistration"], record["runs_started_at"]
    if scope in _PREREGISTERED and (prereg is None or started is None):
        _reject("preregistration_missing")
    if prereg is not None and started is not None:
        if _time(prereg["committed_at"]) > _time(started):
            _reject("preregistration_late")
    # Machine identity.
    machine = record["machine"]
    if scope in _MACHINE_SCOPES and machine is None:
        _reject("machine_identity")
    if machine is not None:
        check_machine(machine)
    # Host operation outcome: positive qualifies, negative disables.
    operation = record["operation"]
    if (scope == "host_operation") != (operation is not None):
        _reject("record_operation")
    if operation is None:
        if outcome != "positive":
            _reject("record_outcome")
    elif outcome == "positive":
        if operation["finding"] != "qualified" or operation["disabled"]:
            _reject("record_outcome")
    elif operation["finding"] != _NEGATIVE_FINDINGS[operation["name"]] or not operation["disabled"]:
        _reject("record_outcome")
    # Review: a pull request approved by someone other than the owner.
    review = record["review"]
    if review["owner_login"] in review["reviewer_logins"]:
        _reject("self_review")
    if review["approved_at"] is not None and (
        review["pull_request"] is None or not review["reviewer_logins"]
    ):
        _reject("review_incomplete")
    corpus_review = record["corpus_review"]
    if corpus_review is not None:
        ids = [row["evidence_id"] for row in corpus_review["reviewed_evidence"]]
        if len(set(ids)) != len(ids) or ids != sorted(ids, key=str.encode):
            _reject("invalid_qualification_record")


@dataclass(frozen=True)
class QualificationRecord:
    """A structurally valid record whose bound digests all matched."""

    document: Mapping[str, Any]
    path: Path
    # Canonical digest of each JSON input subject, by role.
    canonical: Mapping[str, str] = field(default_factory=dict)

    @property
    def record_id(self) -> str:
        return self.document["record_id"]

    @property
    def scope(self) -> str:
        return self.document["scope"]

    @property
    def outcome(self) -> str:
        return self.document["outcome"]

    @property
    def labelling(self) -> str:
        return self.document["labelling"]

    @property
    def machine(self) -> Mapping | None:
        return self.document["machine"]

    @property
    def approved(self) -> bool:
        review = self.document["review"]
        return (
            review["pull_request"] is not None
            and bool(review["reviewer_logins"])
            and review["approved_at"] is not None
        )

    def binds(self, role: str, sha256: str) -> bool:
        return any(s["role"] == role and s["sha256"] == sha256 for s in self.document["subjects"])

    def custodian_review(self) -> CustodianReview | None:
        """The section 12.6.1 review this approved record carries, if any."""
        review = self.document["corpus_review"]
        if review is None or not self.approved:
            return None
        return CustodianReview(
            lock_digest=review["lock_digest"],
            custodian_id=review["custodian_id"],
            review_record_evidence_ids=tuple(review["review_record_evidence_ids"]),
            reviewed_evidence={
                row["evidence_id"]: frozenset(row["domains"]) for row in review["reviewed_evidence"]
            },
        )

    def check_inputs(self, kind: str, inputs) -> None:
        """Spec 12.6.6: the bound freeze/tasks/episodes are the resolved inputs.

        The host scope must also match the task document's kind, so a trial
        record cannot review an acceptance report without preregistration.
        """
        root = inputs.root
        if kind == "host":
            if _HOST_SCOPES.get(root.get("kind")) != self.scope:
                _reject("qualification_record_scope")
            bound = {"tasks": root, "freeze": inputs.freeze}
        elif kind == "adaptation":
            bound = {"episodes": root, "freeze": inputs.freeze}
        else:
            return
        for role, document in bound.items():
            if self.canonical.get(role) != canonical_digest(document):
                _reject("record_input_mismatch")

    def check_labels(self, labels: Mapping, people: Mapping) -> None:
        """The record's labelling, people and agreement must match the frozen labels.

        ``people`` maps catalog Person IDs to their entries. Under
        ``owner_machine`` the named machine holds the annotator role only and
        the named owner adjudicates every case.
        """
        method = labels["agreement"]["method"]
        if self.labelling == "not_applicable":
            return
        if method != METHODS[self.labelling]:
            _reject("labelling_mismatch")
        if "labels" in self.canonical and self.canonical["labels"] != canonical_digest(labels):
            _reject("record_input_mismatch")
        if self.labelling == "owner_machine":
            annotators = self.document["annotators"]
            owner, machine = annotators["owner_annotator_id"], annotators["machine"]["annotator_id"]
            if {row["annotator_id"] for row in labels["judgments"]} != {owner, machine}:
                _reject("labelling_mismatch")
            if (
                machine not in people
                or list(people[machine]["roles"]) != ["annotator"]
                or any(row["adjudicator_id"] != owner for row in labels["adjudications"])
            ):
                _reject("labelling_roles")
        agreement = self.document["agreement"]
        if agreement is None:
            return
        group = next((g for g in labels["agreement"]["groups"] if g["lane"] == "all"), None)
        kappa = class_kappa(labels)
        stored = agreement["cohen_kappa"]
        if (
            group is None
            or group["paired_cases"] != agreement["paired_cases"]
            or group["class_agreements"] != agreement["class_agreements"]
            or (kappa is None) != (stored is None)
            or (kappa is not None and abs(kappa - stored) > _KAPPA_TOLERANCE)
        ):
            _reject("record_agreement_mismatch")


def load_record(path: Path) -> QualificationRecord:
    """Read, validate and verify one record; subject paths are relative to it."""
    path = Path(path).absolute()
    try:
        record, _ = read_json_path(path, DOCUMENT_BYTES)
        validate_closed_document(_SCHEMA, record, "invalid_qualification_record")
    except ValueError as exc:
        if isinstance(exc, WholeTaskRejected):
            raise
        raise WholeTaskRejected("invalid_qualification_record") from exc
    _check_rules(record)
    canonical = {}
    for subject in record["subjects"]:
        try:
            payload = read_local(path.parent, subject["path"], REPORT_BYTES)
        except (OSError, ValueError) as exc:
            raise WholeTaskRejected("record_subject_unavailable") from exc
        if (
            hashlib.sha256(payload).hexdigest() != subject["sha256"]
            or len(payload) != subject["bytes"]
        ):
            _reject("record_digest_mismatch")
        if subject["role"] in _INPUT_ROLES:
            try:
                canonical[subject["role"]] = canonical_digest(parse_json(payload))
            except ValueError as exc:
                raise WholeTaskRejected("record_input_mismatch") from exc
    return QualificationRecord(record, path, canonical)


def class_kappa(labels: Mapping) -> float | None:
    """Cohen's kappa of the raw class judgments between the two annotators.

    Computed over every raw pair before adjudication; ``None`` when chance
    agreement is 1 (both annotators used one and the same class only).
    """
    annotators = sorted({row["annotator_id"] for row in labels["judgments"]})
    if len(annotators) != 2:
        _reject("labelling_mismatch")
    by_case: dict[str, dict[str, str]] = {}
    for row in labels["judgments"]:
        by_case.setdefault(row["case_id"], {})[row["annotator_id"]] = row["class"]
    pairs = [(c[annotators[0]], c[annotators[1]]) for c in by_case.values() if len(c) == 2]
    if len(pairs) != len(by_case) or not pairs:
        _reject("labelling_mismatch")
    total = len(pairs)
    observed = sum(a == b for a, b in pairs) / total
    left, right = Counter(a for a, _ in pairs), Counter(b for _, b in pairs)
    chance = sum(left[k] * right[k] for k in left.keys() | right.keys()) / total**2
    if chance == 1:
        return None
    return (observed - chance) / (1 - chance)


def known_host_limitations(records: Iterable[QualificationRecord]) -> list[dict]:
    """Approved negative records: disabled operations listed as host limitations."""
    rows = []
    for record in records:
        if record.outcome != "negative" or not record.approved:
            continue
        operation = record.document["operation"]
        rows.append(
            dict(
                operation=operation["name"],
                host=operation["host"],
                host_version=operation["host_version"],
                finding=operation["finding"],
                record_id=record.record_id,
            )
        )
    return sorted(rows, key=lambda r: (r["operation"], r["record_id"]))
