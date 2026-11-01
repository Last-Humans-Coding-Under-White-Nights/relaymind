"""Qualified-evidence assessment for the section 12.6.5 exports (#240).

Spec 0.65 sections 12.6.2 and 12.6.5 (RM16, RM40, RM42, RM44; AC31, AC33,
AC36, AC37). Qualification is assessed from the resolved inputs and evidence,
independently of structural validity, and never taken from the report's own
label: fixture inputs yield ``fixture``, incomplete or unverified actual
evidence yields ``provisional``, and only reviewed freeze/evidence with
matched execution identities could yield ``qualified`` (``measured`` for a
platform report).

A report claiming more than a known fixture input allows is a contradiction
and is rejected. A claim the inputs cannot establish never passes a gate; a
known gate failure still takes precedence.

Spec 0.66 section 12.6.6 defines the reviewed record: an approved
QualificationRecordV1 whose digests bind this report's exact bytes lifts the
``qualified_evidence_review`` reason, and its labelling (``owner_machine`` or
``independent``) is reported beside the tier. Without such a record the review
stays unverified. A record never lifts a fixture input or another unverified
input, and software still cannot establish authenticity, human observation or
actual hardware from labels or hashes: the record is the reviewed assertion.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from relaymind.evaluation.asset_corpus import assess_asset_release
from relaymind.evaluation.asset_qualification import CustodianReview
from relaymind.evaluation.asset_qualification_record import REPORT_SCOPES, QualificationRecord
from relaymind.evaluation.asset_whole_task import WholeTaskRejected

REVIEW_UNDEFINED = "qualified_evidence_review"
# Spec 12.6.5: actual Apple Silicon macOS and named native x86-64 Windows.
# WSL, CI and Linux do not satisfy the platform gate.
TARGET_PLATFORMS = frozenset({("Darwin", "arm64"), ("Windows", "AMD64"), ("Windows", "x86_64")})
_STRONG_CLAIMS = ("provisional", "qualified", "measured")


@dataclass(frozen=True)
class WholeTaskQualification:
    """The claimed label, the assessed tier and its fixed safe reasons."""

    kind: str
    claimed: str
    assessed: str
    reasons: tuple[str, ...]
    labelling: str | None = None

    def as_output(self) -> dict:
        return dict(
            claimed=self.claimed,
            assessed=self.assessed,
            reasons=list(self.reasons),
            labelling=self.labelling,
        )


def assess_qualification(
    *,
    kind: str,
    claimed: str,
    corpus_purpose: str,
    corpus_status: str,
    evidence: Iterable[Mapping],
    artifacts: Iterable[Mapping],
    unverified: Iterable[str],
    platform: Mapping | None,
    record: QualificationRecord | None = None,
    report_sha256: str | None = None,
) -> WholeTaskQualification:
    """Assess one S3 report's qualification tier from verified facts.

    ``corpus_status`` is the section 12.6.1 release assessment
    (``fixture|unverified|qualified``); ``unverified`` lists what input
    resolution could not verify; ``platform`` is the platform report's
    Environment (``None`` for host and adaptation reports). ``record`` is a
    loaded section 12.6.6 record and ``report_sha256`` the SHA-256 of the
    report's exact bytes; a record for another report or scope rejects.
    """
    reasons = set(unverified)
    labelling = None
    if record is None:
        reasons.add(REVIEW_UNDEFINED)
    else:
        if record.outcome != "positive" or REPORT_SCOPES.get(record.scope) != kind:
            raise WholeTaskRejected("qualification_record_scope")
        if report_sha256 is None or not record.binds("report", report_sha256):
            raise WholeTaskRejected("qualification_record_unbound")
        if kind == "platform":
            _check_platform_machine(record.machine, platform, reasons)
        if record.approved:
            reasons.discard(REVIEW_UNDEFINED)
        else:
            reasons.add(REVIEW_UNDEFINED)
        labelling = record.labelling
    fixture = False
    if corpus_purpose == "fixture" or corpus_status == "fixture":
        reasons.add("fixture_corpus")
        fixture = True
    elif corpus_status != "qualified":
        reasons.add("corpus_unqualified")
    if any(_provenance(item) == "fixture" for item in evidence):
        reasons.add("fixture_evidence")
        fixture = True
    if any(_provenance(item) in {"unknown", None} for item in artifacts):
        reasons.add("artifact_provenance_unknown")
    if kind == "platform" and (
        not isinstance(platform, Mapping)
        or (platform.get("os"), platform.get("architecture")) not in TARGET_PLATFORMS
    ):
        reasons.add("platform_not_target")
    assessed = "fixture" if fixture else ("provisional" if reasons else "qualified")
    return WholeTaskQualification(kind, claimed, assessed, tuple(sorted(reasons)), labelling)


# Report Environment field -> record machine field (spec 0.66 section 12.6.6).
_MACHINE_FIELDS = (
    ("os", "os"),
    ("architecture", "architecture"),
    ("os_version", "os_version"),
    ("cpu", "cpu_model"),
    ("ram_bytes", "ram_bytes"),
)


def _check_platform_machine(machine: Mapping, platform: Mapping, reasons: set[str]) -> None:
    """The record's machine identity must be the one the report measured.

    A contradicting field rejects; a field the report did not record leaves
    the identity unverified, never matched.
    """
    for env_field, machine_field in _MACHINE_FIELDS:
        value = platform.get(env_field) if isinstance(platform, Mapping) else None
        if value is None:
            reasons.add("machine_identity_unverified")
        elif value != machine[machine_field]:
            raise WholeTaskRejected("record_machine_mismatch")
    if machine["role"] == "linux_host":
        reasons.add("platform_not_target")


def _provenance(item) -> str | None:
    """The entry's provenance, or None when the entry is malformed."""
    return item.get("provenance") if isinstance(item, Mapping) else None


def check_claim(assessment: WholeTaskQualification) -> None:
    """Reject a non-fixture label over known fixture inputs."""
    if assessment.claimed in _STRONG_CLAIMS and assessment.assessed == "fixture":
        raise WholeTaskRejected("qualification_contradiction")


def enforce_qualification(assessment: WholeTaskQualification, gate: str) -> str:
    """Apply the assessment to a recomputed gate: nothing unqualified passes."""
    check_claim(assessment)
    if gate == "pass" and assessment.assessed != "qualified":
        return "blocked"
    return gate


def assess_inputs(
    kind: str,
    report: Mapping,
    inputs,
    *,
    custodian_review: CustodianReview | None = None,
    record: QualificationRecord | None = None,
    report_sha256: str | None = None,
) -> WholeTaskQualification:
    """Assess a report over its resolved ``WholeTaskInputs``.

    ``custodian_review`` is the trusted section 12.6.1 corpus review assertion.
    An approved section 12.6.6 record may carry it instead; without either the
    corpus stays unqualified. The record's labelling and agreement must match
    the frozen labels.
    """
    release = inputs.release
    if record is not None:
        record.check_inputs(kind, inputs)
        record.check_labels(release.labels, {p["id"]: p for p in release.catalog["people"]})
        if custodian_review is None:
            custodian_review = record.custodian_review()
    status = assess_asset_release(release, custodian_review=custodian_review).status
    return assess_qualification(
        kind=kind,
        claimed=report["qualification"],
        corpus_purpose=release.lock["purpose"],
        corpus_status=status,
        evidence=report["evidence"],
        artifacts=report.get("artifacts", ()),
        unverified=inputs.unverified,
        platform=report.get("platform"),
        record=record,
        report_sha256=report_sha256,
    )
