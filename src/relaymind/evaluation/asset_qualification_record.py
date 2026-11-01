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
input. Per the specification, an approved ``negative`` record closes a
host-operation gate with the operation disabled; ``known_host_limitations``
returns the approved ones for the roadmap, docs and reports to list. Loading a
record enables or disables nothing by itself.

Software cannot authenticate the reviewer, the machine or the annotators from
strings and hashes; the record is the reviewed assertion the specification
names, and the git history and pull request review are its audit trail.
"""

import hashlib
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from relaymind.canonical import canonical_digest
from relaymind.evaluation.asset_benchmark_io import (
    REPORT_BYTES,
    read_json_path,
    validate_closed_document,
)
from relaymind.evaluation.asset_corpus_io import DOCUMENT_BYTES, parse_json, read_local
from relaymind.evaluation.asset_judgments import DRAFTED, METHODS
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
MACHINE_DUAL_PROFILE = "machine_dual_owner_approved"
# JSON input documents whose canonical digest must equal the resolved input.
_INPUT_ROLES = ("freeze", "tasks", "episodes", "labels")
_HOST_SCOPES = {"early_trial": "host_trial", "acceptance": "host_acceptance"}
# Labelling profiles that record their annotators and Cohen's kappa agreement.
_AGREEMENT_PROFILES = frozenset({"owner_machine", MACHINE_DUAL_PROFILE})


_TIME = re.compile(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?Z")


def _time(value: str) -> tuple[datetime, Decimal]:
    """A spec Time (UTC RFC 3339 with Z) as a comparable instant.

    The schema checks only the shape; an impossible date or clock value
    (month 13, 30 February, hour 24, minute 60 or a leap second) rejects with
    the closed reason ``invalid_record_time``. Fractional seconds of any
    length are kept exactly.
    """
    match = _TIME.fullmatch(value)
    try:
        if match is None:
            raise ValueError(value)
        *parts, fraction = match.groups()
        instant = datetime(*(int(part) for part in parts), tzinfo=UTC)
    except ValueError:
        raise WholeTaskRejected("invalid_record_time") from None
    return instant, Decimal("0." + (fraction or "0"))


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
    # Labelling: owner_machine and machine_dual name their annotators and the
    # raw agreement; independent human labels carry no machine annotators.
    annotators, agreement = record["annotators"], record["agreement"]
    if scope in _LABELLED and labelling == "not_applicable":
        _reject("labelling_profile")
    if scope not in _LABELLED and labelling != "not_applicable":
        _reject("labelling_profile")
    # Only a profile with recorded annotators carries them and their agreement.
    if labelling in _AGREEMENT_PROFILES:
        if annotators is None or agreement is None:
            _reject("labelling_profile")
    elif annotators is not None or agreement is not None:
        _reject("labelling_profile")
    if labelling == "owner_machine" and (
        "owner_annotator_id" not in annotators
        or annotators["owner_annotator_id"] == annotators["machine"]["annotator_id"]
    ):
        _reject("labelling_profile")
    # Every Time field is a real instant, whether or not it is compared.
    for value in (
        record["recorded_at"],
        record["runs_started_at"],
        (record["preregistration"] or {}).get("committed_at"),
        record["review"]["approved_at"],
    ):
        if value is not None:
            _time(value)
    # The corpus review belongs to a record whose report resolves a corpus
    # (host, adaptation and platform scopes); others carry none.
    if scope not in REPORT_SCOPES and record["corpus_review"] is not None:
        _reject("record_corpus_review")
    if labelling == MACHINE_DUAL_PROFILE:
        _check_machine_dual(annotators)
    _check_prompt_authorship(record, scope)
    # Preregistration: a commit and its time before any run.
    prereg, started = record["preregistration"], record["runs_started_at"]
    if scope in _PREREGISTERED and prereg is None:
        _reject("preregistration_missing")
    # A recorded preregistration is always checked against the first dispatch.
    if prereg is not None:
        if started is None:
            _reject("preregistration_missing")
        if _time(prereg["committed_at"]) > _time(started):
            _reject("preregistration_late")
    # The owner approves drafted cases and machine_dual labels in the pull
    # request that commits their lock, before preregistration (12.6.6), so an
    # approved record of those profiles always names its preregistration.
    owner_approved = labelling == MACHINE_DUAL_PROFILE or record["prompt_authorship"] == DRAFTED
    if owner_approved and prereg is None and record["review"]["approved_at"] is not None:
        _reject("preregistration_missing")
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
    # GitHub logins are case-insensitive.
    owner_login = review["owner_login"].casefold()
    if any(login.casefold() == owner_login for login in review["reviewer_logins"]):
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


def _label_ids(annotators: Mapping | None) -> set[str]:
    if annotators is None:
        return set()
    if "owner_annotator_id" in annotators:
        return {annotators["owner_annotator_id"], annotators["machine"]["annotator_id"]}
    return {annotators[k]["annotator_id"] for k in ("machine_a", "machine_b", "adjudicator")}


def _check_machine_dual(annotators: Mapping) -> None:
    """Spec 12.6.6: three distinct machine IDs; one model is one family; two templates."""
    # The owner/machine shape does not describe machine_dual labels.
    if "machine_a" not in annotators or len(_label_ids(annotators)) != 3:
        _reject("labelling_profile")
    a, b = annotators["machine_a"], annotators["machine_b"]
    # One model ID is one family; the same_family fallback always needs two
    # independently written templates, whether or not the model IDs differ.
    if a["model_id"] == b["model_id"] and annotators["family_relation"] != "same_family":
        _reject("labelling_profile")
    if annotators["family_relation"] == "same_family" and a["prompt_sha256"] == b["prompt_sha256"]:
        _reject("labelling_profile")


def _check_prompt_authorship(record: Mapping, scope: str) -> None:
    """Spec 12.6.6: labelled scopes name their prompt authorship; drafted ones a drafter."""
    authorship, drafter = record["prompt_authorship"], record["drafter"]
    if (scope in _LABELLED) == (authorship == "not_applicable"):
        _reject("prompt_authorship")
    if (authorship == DRAFTED) != (drafter is not None):
        _reject("prompt_authorship")
    if drafter is not None and drafter["author_id"] in _label_ids(record["annotators"]):
        _reject("prompt_authorship")


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
    def prompt_authorship(self) -> str:
        return self.document["prompt_authorship"]

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

    def check_scope(self, kind: str) -> None:
        """Spec 12.6.6: only a positive record of the matching scope reviews a report."""
        if self.outcome != "positive" or REPORT_SCOPES.get(self.scope) != kind:
            _reject("qualification_record_scope")

    def check_report(self, kind: str, report_sha256: str | None) -> None:
        """The scope first, then the binding of the report's exact bytes."""
        self.check_scope(kind)
        if report_sha256 is None or not self.binds("report", report_sha256):
            _reject("qualification_record_unbound")

    def check_inputs(self, kind: str, inputs) -> None:
        """Spec 12.6.6: the bound freeze/tasks/episodes are the resolved inputs.

        The host scope must also match the task document's kind, so a trial
        record cannot review an acceptance report without preregistration.
        """
        # The scope rejects first, for every report kind.
        self.check_scope(kind)
        root = inputs.root
        if kind == "host" and _HOST_SCOPES.get(root.get("kind")) != self.scope:
            _reject("qualification_record_scope")
        # Platform reports resolve the host tasks and freeze.
        root_role = "episodes" if kind == "adaptation" else "tasks"
        resolved = {root_role: root, "freeze": inputs.freeze}
        # Host and adaptation scopes require their inputs (load_record); a
        # platform record may bind them. Labels are compared in check_labels.
        for role, digest in self.canonical.items():
            if role == "labels":
                continue
            if role not in resolved or digest != canonical_digest(resolved[role]):
                _reject("record_input_mismatch")

    def check_labels(self, labels: Mapping, people: Mapping) -> None:
        """The record's labelling, people and agreement must match the frozen labels.

        ``people`` maps catalog Person IDs to their entries. Under
        ``owner_machine`` the named machine holds the annotator role only and
        the named owner adjudicates every case.
        """
        method = labels["agreement"]["method"]
        # Bound labels are the resolved labels for every scope (12.6.6).
        if "labels" in self.canonical and self.canonical["labels"] != canonical_digest(labels):
            _reject("record_input_mismatch")
        if self.labelling == "not_applicable":
            return
        if method != METHODS[self.labelling]:
            _reject("labelling_mismatch")
        if self.labelling == "owner_machine":
            annotators = self.document["annotators"]
            owner, machine = annotators["owner_annotator_id"], annotators["machine"]["annotator_id"]
            if {row["annotator_id"] for row in labels["judgments"]} != {owner, machine}:
                _reject("labelling_mismatch")
            if (
                owner not in people
                or machine not in people
                or list(people[machine]["roles"]) != ["annotator"]
                or any(row["adjudicator_id"] != owner for row in labels["adjudications"])
            ):
                _reject("labelling_roles")
        if self.labelling == MACHINE_DUAL_PROFILE:
            annotators = self.document["annotators"]
            expected = {annotators[k]["annotator_id"] for k in ("machine_a", "machine_b")}
            adjudicator = annotators["adjudicator"]["annotator_id"]
            if {row["annotator_id"] for row in labels["judgments"]} != expected or any(
                row["adjudicator_id"] != adjudicator for row in labels["adjudications"]
            ):
                _reject("labelling_mismatch")
            # The catalog Persons hold exactly these roles (12.6.6).
            roles = {**{i: ["annotator"] for i in expected}, adjudicator: ["adjudicator"]}
            if any(
                person not in people or list(people[person]["roles"]) != wanted
                for person, wanted in roles.items()
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

    def check_grade(self, grade: Mapping) -> None:
        """A grade presented under this record carries its labelling (spec 12.6.6).

        Every report graded under the record says so: the qualification output
        carries the record's ``labelling`` and ``prompt_authorship``. A grade
        made without the record (both null) or under another profile rejects.
        """
        qualification = grade.get("qualification") if isinstance(grade, Mapping) else None
        if not isinstance(qualification, Mapping) or (
            qualification.get("labelling"),
            qualification.get("prompt_authorship"),
        ) != (self.labelling, self.prompt_authorship):
            _reject("record_grade_mismatch")

    def check_cases(self, cases: Mapping) -> None:
        """The frozen cases agree with the record's prompt authorship (spec 12.6.6).

        A drafted record: the drafter writes every prompt of the release, so
        every case is drafted and names the drafter alone. An original record:
        no case is drafted; translated or generated validation cases may occur
        and never count (section 12.6.1).
        """
        authorship = self.prompt_authorship
        if authorship == "not_applicable":
            return
        drafter = self.document["drafter"]
        for case in cases["cases"]:
            if authorship == DRAFTED:
                if case["authorship"] != DRAFTED or case["author_ids"] != [drafter["author_id"]]:
                    _reject("prompt_authorship_mismatch")
            elif case["authorship"] == DRAFTED:
                _reject("prompt_authorship_mismatch")


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
