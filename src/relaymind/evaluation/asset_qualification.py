"""Externally supplied review assertions are separate from integrity (RM40/AC31)."""

from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING

from relaymind.canonical import canonical_digest
from relaymind.evaluation.asset_judgments import OWNER_MACHINE
from relaymind.evaluation.asset_splits import input_identity
from relaymind.schema_util import freeze_json

# Spec 0.66 section 12.6.6: owner-approved machine-drafted prompts count in
# the cohort under the owner-run profile; translated or generated never do.
DRAFTED = "machine_drafted_owner_approved"
COUNTED_AUTHORSHIP = ("original", DRAFTED)

if TYPE_CHECKING:
    from relaymind.evaluation.asset_corpus import VerifiedAssetRelease


@dataclass(frozen=True)
class CustodianReview:
    """Trusted caller assertion after actual offline review, never parsed from JSON.

    This records what a custodian attests; software cannot authenticate native
    speakers, human independence or legal permissions from strings and hashes.
    """

    lock_digest: str
    custodian_id: str
    review_record_evidence_ids: tuple[str, ...]
    reviewed_evidence: Mapping[str, frozenset[str]]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "review_record_evidence_ids", tuple(self.review_record_evidence_ids)
        )
        object.__setattr__(
            self,
            "reviewed_evidence",
            MappingProxyType(
                {key: frozenset(domains) for key, domains in self.reviewed_evidence.items()}
            ),
        )


@dataclass(frozen=True)
class QualificationAssessment:
    status: str
    reasons: tuple[str, ...]
    counts: Mapping
    unavailable_evidence_ids: tuple[str, ...]
    missing_review_domains: Mapping


def _source_identity(entry: Mapping) -> tuple:
    record = entry["record"]
    return (
        entry["origin"],
        record["asset_id"],
        record["kind"],
        record["source_id"],
        record["source_locator"],
        record["source_version"],
        record["content_hash"],
        canonical_digest(record["identity"]),
    )


def _claim_requirements(documents, need, reasons: set[str]) -> None:
    catalog, cases, labels, lock = (
        documents.catalog,
        documents.cases,
        documents.labels,
        documents.lock,
    )
    people = {p["id"]: p for p in catalog["people"]}
    if lock["state"] != "frozen":
        reasons.add("provisional_release")
    if any(a["origin"] == "fixture" for a in catalog["assets"]):
        reasons.add("fixture_assets")
    for asset in catalog["assets"]:
        rights = asset["rights"]
        if any(rights[key] != "permitted" for key in ("acquisition", "evaluation", "retention")):
            reasons.add("rights_unqualified")
        need(rights["evidence_ids"], "rights", ("license", "permission"))
        need(rights["notice_evidence_ids"], "rights", ("notice",))
        need(asset["provenance_evidence_ids"], "source_provenance", ("source_provenance",))
        need([asset["original_evidence_id"]], "source_provenance", ("original",))
        need(asset["language_review_evidence_ids"], "language", ("language_review",))
        if asset["origin"] == "matrix_original_zh" and "zh-Hans" not in asset["original_languages"]:
            reasons.add("original_language_unqualified")
        if asset["origin"] == "original_en" and "en" not in asset["original_languages"]:
            reasons.add("original_language_unqualified")
    for pair in catalog["equivalent_pairs"]:
        need(pair["evidence_ids"], "language")
        for reviewer_id in pair["reviewer_ids"]:
            need(people[reviewer_id]["evidence_ids"], "language")
    for person in people.values():
        roles = person["roles"]
        if "author" in roles:
            need(person["evidence_ids"], "authorship")
            if person["competencies"]:
                need(person["evidence_ids"], "language")
        if "annotator" in roles or "adjudicator" in roles:
            need(person["evidence_ids"], "independence")
        if "custodian" in roles:
            need(person["evidence_ids"], "access_control")
    need(cases["split_review_evidence_ids"], "split", ("split_review",))
    need(lock["derivation"]["evidence_ids"], "derivation", ("derivation",))
    need(lock["qualification_evidence_ids"], "access_control")
    for case in cases["cases"]:
        need(case["authorship_evidence_ids"], "authorship", ("authorship",))
        if case["authorship"] == DRAFTED:
            # Spec 0.66 section 12.6.6: the recorded drafter and the owner's
            # approval replace the author competence rule.
            if not case["author_ids"]:
                reasons.add("authorship_unqualified")
            continue
        if case["authorship"] != "original":
            continue
        competencies = {v for p in case["author_ids"] for v in people[p]["competencies"]}
        if not case["author_ids"]:
            reasons.add("authorship_unqualified")
        if (
            case["lane"] in ("zh_hans_to_zh_hans", "zh_hans_to_en", "code_switch")
            and "native_zh" not in competencies
        ):
            reasons.add("author_competence_unqualified")
        if (
            case["lane"] in ("en_to_en", "en_to_zh_hans", "code_switch")
            and "proficient_en" not in competencies
        ):
            reasons.add("author_competence_unqualified")
    for row in labels["judgments"]:
        need(row["independence_evidence_ids"], "independence", ("independence",))
    for row in labels["adjudications"]:
        need(row["evidence_ids"], "adjudication", ("adjudication",))


def _baseline_continuity(catalog: Mapping, baseline) -> tuple[bool, dict]:
    origins = {"matrix_original_zh", "original_en"}
    previous = {a["record"]["asset_id"]: a for a in baseline.catalog["assets"]}
    current = {a["record"]["asset_id"]: a for a in catalog["assets"] if a["origin"] in origins}
    matched = sorted(
        key
        for key in previous.keys() & current.keys()
        if _source_identity(previous[key]) == _source_identity(current[key])
    )
    counts = Counter(a["origin"] for a in baseline.catalog["assets"])
    valid = (
        len(previous) == 200
        and counts["matrix_original_zh"] == 100
        and counts["original_en"] == 100
        and len(current) == 200
        and len(matched) == 200
    )
    return valid, {
        "baseline_matched": len(matched),
        "baseline_missing_asset_ids": sorted(previous.keys() - current.keys()),
        "baseline_changed_asset_ids": sorted((previous.keys() & current.keys()) - set(matched)),
        "baseline_unexpected_asset_ids": sorted(current.keys() - previous.keys()),
    }


def assess_asset_release(
    release: "VerifiedAssetRelease", *, custodian_review: CustodianReview | None = None
) -> QualificationAssessment:
    """Assess the declared release purpose without manufacturing authenticity."""
    return _assess_asset_release(
        release,
        purpose=release.lock["purpose"],
        development=None,
        custodian_review=custodian_review,
    )


def assess_acceptance_release(
    release: "VerifiedAssetRelease",
    development: "VerifiedAssetRelease",
    *,
    custodian_review: CustodianReview | None = None,
) -> QualificationAssessment:
    """RM40/AC31: assess the exact 200→300 extension through the same review gate."""
    return _assess_asset_release(
        release, purpose="acceptance", development=development, custodian_review=custodian_review
    )


def _assess_asset_release(
    release: "VerifiedAssetRelease",
    *,
    purpose: str,
    development: "VerifiedAssetRelease | None",
    custodian_review: CustodianReview | None,
) -> QualificationAssessment:
    """A complete review can qualify a freeze, never manufacture authenticity."""
    reasons, required = set(), defaultdict(set)
    catalog, cases, labels, lock = release.catalog, release.cases, release.labels, release.lock
    counts = Counter(entry["origin"] for entry in catalog["assets"])
    counts["assets"] = len(catalog["assets"])
    counts["mcp_tools"] = sum(a["record"]["kind"] == "mcp_tool" for a in catalog["assets"])
    counts["cases"] = len(cases["cases"])
    counts["equivalent_pairs"] = len(catalog["equivalent_pairs"])
    unavailable = tuple(f.evidence_id for f in release.evidence_findings if f.status != "verified")
    people = {p["id"]: p for p in catalog["people"]}
    evidence = {e["id"]: e for e in catalog["evidence"]}
    for related in release.related_releases:
        evidence.update({e["id"]: e for e in related.catalog["evidence"]})

    def need(ids, domain: str, kinds: tuple[str, ...] = ()) -> None:
        if not ids:
            reasons.add("supporting_evidence_missing")
        for evidence_id in ids:
            required[evidence_id].add(domain)
            if kinds and evidence[evidence_id]["kind"] not in kinds:
                reasons.add("evidence_kind_mismatch")

    if unavailable:
        reasons.add("evidence_unavailable")
    expected = {"matrix_original_zh": 100, "original_en": 100}
    if purpose == "acceptance":
        expected["genuine_mcp"] = 100
    if any(counts[origin] != count for origin, count in expected.items()) or counts[
        "assets"
    ] != sum(expected.values()):
        reasons.add("asset_cohort_incomplete")
    for documents in (release, *release.related_releases):
        _claim_requirements(documents, need, reasons)
    finals = {row["case_id"]: row for row in labels["adjudications"]}
    lanes = Counter()
    hard_counts = Counter()
    seen_inputs, repeated_inputs = set(), []
    for case in cases["cases"]:
        final = finals[case["id"]]
        if case["authorship"] not in COUNTED_AUTHORSHIP or case["split"] == "diagnostic":
            continue
        if purpose == "acceptance" and case["split"] != "held_out":
            continue
        if final["disposition"] == "include":
            identity = input_identity(case)
            if identity in seen_inputs:
                repeated_inputs.append(case["id"])
                reasons.add("repeated_independent_input")
                continue
            seen_inputs.add(identity)
            if purpose == "acceptance" and set(case["requested_kinds"]) != {
                "skill",
                "mcp_tool",
            }:
                reasons.add("discovery_kinds_incomplete")
            lanes[(case["lane"], final["class"])] += 1
            diagnostic = release.hard_subsets.get(case["id"])
            if (
                final["class"] == "positive"
                and diagnostic
                and diagnostic["subset"] == "no_shared_latin"
            ):
                hard_counts[case["lane"]] += 1
    counts["repeated_input_case_ids"] = repeated_inputs
    counts["lanes"] = {
        lane: {kind: lanes[(lane, kind)] for kind in ("positive", "negative", "ambiguous")}
        for lane in (
            "code_switch",
            "en_to_en",
            "en_to_zh_hans",
            "zh_hans_to_en",
            "zh_hans_to_zh_hans",
        )
    }
    counts["no_shared_latin"] = dict(hard_counts)
    if purpose == "acceptance":
        if counts["equivalent_pairs"] < 25:
            reasons.add("equivalent_pairs_incomplete")
        if any(
            lanes[(lane, kind)] < minimum
            for lane in counts["lanes"]
            for kind, minimum in [("positive", 200), ("negative", 50), ("ambiguous", 30)]
        ):
            reasons.add("case_cohort_incomplete")
        if any(
            2 * hard_counts[lane] < lanes[(lane, "positive")]
            for lane in ("en_to_zh_hans", "zh_hans_to_en")
        ):
            reasons.add("hard_subset_incomplete")
        baselines = (
            [development]
            if development is not None
            else [r for r in release.related_releases if r.lock["purpose"] == "development"]
        )
        best = None
        valid = False
        for baseline in baselines:
            continuous, diagnostic = _baseline_continuity(catalog, baseline)
            if best is None or diagnostic["baseline_matched"] > best["baseline_matched"]:
                best = diagnostic
            valid = valid or continuous
        diagnostic = best or {
            "baseline_matched": 0,
            "baseline_missing_asset_ids": [],
            "baseline_changed_asset_ids": [],
            "baseline_unexpected_asset_ids": [],
        }
        for name, value in diagnostic.items():
            counts[name] = value
        if not valid:
            reasons.add("development_baseline_missing")
            if baselines:
                reasons.add("development_baseline_changed")
        if development is not None:
            committed = {item.lock["digest"] for item in release.related_releases}
            if development.lock["digest"] not in committed:
                reasons.add("development_baseline_uncommitted")
            if development.lock["purpose"] not in ("fixture", "development"):
                reasons.add("development_purpose_required")
            if development.lock["state"] != "frozen":
                reasons.add("development_baseline_unfrozen")
        if lock["purpose"] not in ("fixture", "acceptance"):
            reasons.add("acceptance_purpose_required")
    missing = {}
    if custodian_review is None:
        reasons.add("external_review_missing")
    else:
        review = custodian_review
        custodian = people.get(review.custodian_id)
        if review.lock_digest != lock["digest"]:
            reasons.add("external_review_lock_mismatch")
        # Spec 0.66 section 12.6.6: under owner_machine labelling the owner is
        # custodian and tuner; the reviewed record and its label disclose it.
        owner_machine = labels["agreement"]["method"] == OWNER_MACHINE
        if (
            custodian is None
            or "custodian" not in custodian["roles"]
            or ("tuner" in custodian["roles"] and not owner_machine)
        ):
            reasons.add("custodian_unqualified")
        if (
            not review.review_record_evidence_ids
            or any(e not in evidence or e in unavailable for e in review.review_record_evidence_ids)
            or custodian is None
            or not set(review.review_record_evidence_ids) <= set(custodian["evidence_ids"])
        ):
            reasons.add("review_record_unverified")
        for evidence_id, domains in required.items():
            absent = domains - set(review.reviewed_evidence.get(evidence_id, ()))
            if absent:
                missing[evidence_id] = sorted(absent)
        if missing:
            reasons.add("review_domain_incomplete")
    fixture = lock["purpose"] == "fixture" or (
        development is not None and development.lock["purpose"] == "fixture"
    )
    if fixture:
        reasons.add("fixture_purpose")
    return QualificationAssessment(
        "fixture" if fixture else ("unverified" if reasons else "qualified"),
        tuple(sorted(reasons)),
        freeze_json(dict(counts)),
        unavailable,
        freeze_json(missing),
    )
