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

This revision has no path to ``qualified``. The specification defines the
evidence obligations but no reviewed S3 qualification record: native host,
provider, sampling and budget configuration validation is not enabled
(section 12.6.5, "this revision enables no native verifier"), and software
cannot establish authenticity, human observation or actual hardware from
labels, opaque reviewer IDs or hashes. ``qualified_evidence_review`` is
therefore always reported as unverified.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from relaymind.evaluation.asset_corpus import assess_asset_release
from relaymind.evaluation.asset_qualification import CustodianReview
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

    def as_output(self) -> dict:
        return dict(claimed=self.claimed, assessed=self.assessed, reasons=list(self.reasons))


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
) -> WholeTaskQualification:
    """Assess one S3 report's qualification tier from verified facts.

    ``corpus_status`` is the section 12.6.1 release assessment
    (``fixture|unverified|qualified``); ``unverified`` lists what input
    resolution could not verify; ``platform`` is the platform report's
    Environment (``None`` for host and adaptation reports).
    """
    reasons = set(unverified)
    reasons.add(REVIEW_UNDEFINED)
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
    return WholeTaskQualification(kind, claimed, assessed, tuple(sorted(reasons)))


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
) -> WholeTaskQualification:
    """Assess a report over its resolved ``WholeTaskInputs``.

    ``custodian_review`` is the trusted section 12.6.1 corpus review assertion,
    never parsed from a file; without it the corpus stays unqualified.
    """
    release = inputs.release
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
    )
