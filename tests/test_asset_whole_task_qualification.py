"""Spec 0.65 section 12.6.5 qualified-evidence assessment of the S3 exports (#240).

RM16, RM40, RM42, RM44; AC31, AC33, AC36, AC37. Qualification is assessed from
the resolved inputs and evidence, never taken from the report's own label:
fixture inputs yield fixture, unverified actual evidence yields provisional,
and a report claiming more than its inputs establish is rejected (a known
fixture contradiction) or blocked (unverifiable). Every input here is a
synthetic fixture; none of it is labelled or graded as qualified evidence.
"""

import json

import pytest

from relaymind.canonical import canonical_digest
from relaymind.evaluation.asset_episode_export import episode_summary, probe_plan
from relaymind.evaluation.asset_platform_export import PLATFORM_STEPS, platform_gate
from relaymind.evaluation.asset_whole_task import WholeTaskRejected, host_summary
from relaymind.evaluation.asset_whole_task_cli import main
from relaymind.evaluation.asset_whole_task_corpus import release_labels, resolve_corpus
from relaymind.evaluation.asset_whole_task_qualification import (
    REVIEW_UNDEFINED,
    assess_qualification,
    enforce_qualification,
)
from test_asset_episode_platform_export import episode_freeze, episodes_doc
from test_asset_whole_task_inputs import Bundle, _grade, adaptation_case
from whole_task_helpers import ENVIRONMENT


@pytest.fixture
def bundle(tmp_path):
    return Bundle(tmp_path)


def _claim(bundle, report, qualification, *, provenance=None):
    report = dict(report, qualification=qualification)
    if provenance is not None:
        report["evidence"] = [dict(e, provenance=provenance) for e in report["evidence"]]
    report["summary"] = host_summary(report, bundle.tasks, bundle.freeze)
    return report


# --- command line over a real companion bundle (fixture corpus) ----------------


def test_fixture_host_report_names_its_assessed_qualification(bundle):
    """AC36: the grade reports assessed qualification with fixed safe reasons."""
    code, result = _grade(bundle, "grade-host", bundle.host_report())
    assert code == 3 and result["gate"] == "blocked"
    assert result["qualification"] == dict(
        claimed="fixture",
        assessed="fixture",
        reasons=[
            "configuration_semantics",
            "fixture_corpus",
            "fixture_evidence",
            "qualified_evidence_review",
        ],
    )


@pytest.mark.parametrize("claimed", ["qualified", "provisional"])
def test_non_fixture_claim_over_fixture_inputs_is_rejected(bundle, claimed):
    """Spec 12.6.2/12.6.5: fixture inputs yield fixture, never a caller flag."""
    report = _claim(bundle, bundle.host_report(), claimed)
    code, result = _grade(bundle, "grade-host", report)
    assert (code, result["status"], result["reason"]) == (
        2,
        "rejected",
        "qualification_contradiction",
    )


def test_observed_provenance_labels_cannot_lift_a_fixture_corpus(bundle):
    """Provenance labels alone establish neither authenticity nor observation."""
    report = _claim(bundle, bundle.host_report(), "qualified", provenance="observed")
    code, result = _grade(bundle, "grade-host", report)
    assert (code, result["reason"]) == (2, "qualification_contradiction")


def _platform_report(bundle, qualification):
    reference = bundle.write_manifest()
    registry = {
        e["id"]: e for e in json.loads((bundle.root / "inputs.json").read_text())["evidence"]
    }

    def ref(i):
        e = registry[i]
        return dict(
            id=i, path=e["path"], sha256=e["sha256"], bytes=e["bytes"], provenance="observed"
        )

    steps = [
        dict(name=s, status="passed", evidence_ids=["receipt-" + s], reason=None)
        for s in PLATFORM_STEPS
    ]
    report = dict(
        schema_version=1,
        qualification=qualification,
        input_digest=canonical_digest(bundle.tasks),
        input_manifest=reference,
        platform=ENVIRONMENT,
        artifacts=[],
        steps=steps,
        operations_evidence=ref("ops"),
        approvals_evidence=[ref("approval")],
        evidence=[
            ref(i) for i in sorted({"ops", "approval", *("receipt-" + s for s in PLATFORM_STEPS)})
        ],
        gate=None,
    )
    report["gate"] = platform_gate(report)
    return report


def test_measured_platform_claim_over_fixture_inputs_is_rejected(bundle):
    """AC37: a caller-supplied measured flag does not satisfy the platform gate."""
    code, result = _grade(bundle, "grade-platform", _platform_report(bundle, "measured"))
    assert (code, result["reason"]) == (2, "qualification_contradiction")


def test_fixture_platform_report_is_not_applicable_and_assessed_fixture(bundle):
    code, result = _grade(bundle, "grade-platform", _platform_report(bundle, "fixture"))
    assert (code, result["gate"]) == (0, "not_applicable")
    assert result["qualification"]["assessed"] == "fixture"
    # Linux is not an actual target platform (spec 12.6.5).
    assert "platform_not_target" in result["qualification"]["reasons"]


def test_qualified_adaptation_claim_over_fixture_inputs_is_rejected(bundle):
    """AC33: a fixture driver run cannot become qualified by its label."""
    report = adaptation_case(bundle)
    report["qualification"] = "qualified"

    documents = {d["id"]: (bundle.root / d["path"]).read_bytes() for d in bundle.documents}
    evidence = {
        e["id"]: dict(e, payload=(bundle.root / e["path"]).read_bytes())
        for e in bundle.corpus_evidence
    }
    release = resolve_corpus(canonical_digest(bundle.lock), documents, evidence)
    episodes = next(
        json.loads((bundle.root / d["path"]).read_text())
        for d in bundle.documents
        if d["kind"] == "episodes"
    )
    report["summary"] = episode_summary(report, episodes, release_labels(release))
    code, result = _grade(bundle, "grade-adaptation", report)
    assert (code, result["reason"]) == (2, "qualification_contradiction")


def test_fixture_adaptation_report_is_assessed_fixture(bundle):
    code, result = _grade(bundle, "grade-adaptation", adaptation_case(bundle))
    assert (code, result["gate"]) == (0, "not_applicable")
    assert result["qualification"]["assessed"] == "fixture"
    assert "fixture_corpus" in result["qualification"]["reasons"]


# --- the assessment itself -------------------------------------------------------


def _assess(**overrides):
    values = dict(
        kind="host",
        claimed="qualified",
        corpus_purpose="acceptance",
        corpus_status="unverified",
        evidence=[{"provenance": "observed"}],
        artifacts=[{"provenance": "observed"}],
        unverified=("configuration_semantics",),
        platform=None,
    )
    values.update(overrides)
    return assess_qualification(**values)


def test_unverified_actual_evidence_is_provisional_with_every_reason():
    assessment = _assess(artifacts=[{"provenance": "unknown"}])
    assert assessment.assessed == "provisional"
    assert assessment.reasons == (
        "artifact_provenance_unknown",
        "configuration_semantics",
        "corpus_unqualified",
        REVIEW_UNDEFINED,
    )


def test_no_assessment_reaches_qualified_without_a_defined_review_record():
    """Spec 0.65 defines no S3 review record: the review stays unverified.

    Even with every other machine-checkable condition clear, the assessment
    stays provisional, so no synthetic input can become qualified evidence.
    """
    assessment = _assess(corpus_status="qualified", unverified=())
    assert (assessment.assessed, assessment.reasons) == ("provisional", (REVIEW_UNDEFINED,))


def test_fixture_signals_take_precedence():
    assert _assess(corpus_purpose="fixture").assessed == "fixture"
    assert _assess(evidence=[{"provenance": "fixture"}]).assessed == "fixture"


@pytest.mark.parametrize(
    ("os", "architecture", "target"),
    [
        ("Linux", "x86_64", False),
        ("Darwin", "x86_64", False),
        ("Windows", "ARM64", False),
        ("Darwin", "arm64", True),
        ("Windows", "AMD64", True),
        ("Windows", "x86_64", True),
    ],
)
def test_platform_reports_need_an_actual_target_platform(os, architecture, target):
    """Spec 12.6.5: Apple Silicon macOS or named native x86-64 Windows only."""
    assessment = _assess(
        kind="platform",
        claimed="measured",
        platform=dict(ENVIRONMENT, os=os, architecture=architecture),
    )
    assert ("platform_not_target" not in assessment.reasons) is target
    assert assessment.assessed == "provisional"


def test_enforcement_rejects_contradictions_and_never_passes_unqualified_claims():
    fixture = _assess(corpus_purpose="fixture")
    with pytest.raises(WholeTaskRejected, match="qualification_contradiction"):
        enforce_qualification(fixture, "blocked")
    provisional = _assess()
    # A known failure still takes precedence; nothing unqualified passes.
    assert enforce_qualification(provisional, "fail") == "fail"
    assert enforce_qualification(provisional, "pass") == "blocked"
    assert enforce_qualification(_assess(claimed="fixture", corpus_purpose="fixture"), "x") == "x"


def test_lower_claims_are_kept():
    """A conservative label over stronger inputs is honest and kept."""
    assessment = _assess(claimed="fixture")
    assert assessment.claimed == "fixture" and assessment.assessed == "provisional"
    assert enforce_qualification(assessment, "not_applicable") == "not_applicable"


@pytest.mark.parametrize(
    "platform", [None, {}, {"os": "Darwin"}, {"architecture": "arm64"}, "darwin"]
)
def test_a_missing_or_malformed_platform_is_not_a_target(platform):
    """RM44/AC37: a platform report without a usable environment never counts as a target."""
    assessment = _assess(kind="platform", claimed="fixture", platform=platform)
    assert "platform_not_target" in assessment.reasons


@pytest.mark.parametrize("item", [{}, {"provenance": None}, "fixture"])
def test_malformed_provenance_entries_do_not_crash(item):
    """RM44/AC36: an entry without provenance is unverified, never a crash."""
    assessment = _assess(evidence=[item], artifacts=[item])
    assert assessment.assessed == "provisional"


# --- preregistration commitments (#236, #237) ------------------------------------


def test_plan_host_records_the_preregistered_digests(bundle):
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
    plan = json.loads(output.read_text())
    assert code == 0
    assert plan["input_digest"] == canonical_digest(bundle.tasks)
    assert plan["freeze_digest"] == canonical_digest(bundle.freeze)


def test_plan_episodes_writes_the_frozen_probe_plan(bundle):
    """AC33: probes per episode in (kind, case_id) order, ordinals from one."""
    episodes = episodes_doc()
    output = bundle.root / "episodes-plan.json"
    code = main(
        [
            "plan-episodes",
            "--episodes",
            str(bundle.save("episodes.json", episodes)),
            "--freeze",
            str(bundle.save("episode-freeze.json", episode_freeze())),
            "--output",
            str(output),
        ]
    )
    plan = json.loads(output.read_text())
    assert code == 0
    assert plan["input_digest"] == canonical_digest(episodes)
    assert plan["config_digest"] == canonical_digest(episode_freeze())
    first = plan["episodes"][0]
    assert first["episode_id"] == "e1"
    assert first["corrections"] == ["e1-c3", "e1-c1", "e1-c2"]
    assert first["probes"] == [
        dict(kind=kind, case_id=case_id, ordinal=n)
        for n, (kind, case_id) in enumerate(probe_plan(episodes["episodes"][0]), start=1)
    ]
    assert first["reset_probe_case_ids"] == ["e1-r1"]


def test_plan_episodes_rejects_unsorted_episodes(bundle, capsys):
    episodes = episodes_doc(ids=("e2", "e1"))
    code = main(
        [
            "plan-episodes",
            "--episodes",
            str(bundle.save("episodes.json", episodes)),
            "--freeze",
            str(bundle.save("episode-freeze.json", episode_freeze())),
            "--output",
            str(bundle.root / "episodes-plan.json"),
        ]
    )
    assert code == 2 and "episode_order" in capsys.readouterr().err
    assert not (bundle.root / "episodes-plan.json").exists()
