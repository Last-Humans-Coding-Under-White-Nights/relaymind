"""Spec 0.60 section 12.6.5 adaptation episodes and platform lifecycle exports.

RM37, RM38, RM42, RM44; AC28, AC29, AC33, AC37. Offline tooling over explicitly
fixture-qualified documents: no correction authority, platform measurement or
acceptance is established here.
"""

import copy
import hashlib

import pytest
from relaymind.evaluation.asset_episode_export import (
    episode_summary,
    grade_adaptation_report,
    probe_plan,
    reset_preimage_digest,
    verify_episode_freeze,
    verify_episodes,
)
from relaymind.evaluation.asset_platform_export import (
    PLATFORM_STEPS,
    grade_platform_report,
    platform_gate,
)

from relaymind.canonical import canonical_digest
from relaymind.evaluation.asset_metrics import grade_asset_result
from relaymind.evaluation.asset_whole_task import WholeTaskRejected
from whole_task_helpers import ENVIRONMENT, freeze_doc, tasks_doc

H = "a" * 64


def h(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# --- adaptation episodes ---------------------------------------------------------


def episode_freeze() -> dict:
    return dict(
        schema_version=1,
        off_config_digest=h("off"),
        on_config_digest=h("on"),
        adaptation_config_digest=h("adaptation"),
        seed=3,
    )


def episode(id: str) -> dict:
    return dict(
        id=id,
        intent_family_ids=["family-" + id],
        corrections=[
            dict(
                case_id=f"{id}-c{n}",
                target_asset_id="asset-a",
                content_hash=h("a"),
                schema_hash=None,
            )
            for n in (3, 1, 2)  # chronological order is preserved, not sorted
        ],
        paraphrase_case_ids=[f"{id}-p1", f"{id}-p2"],
        control_case_ids=[f"{id}-k1"],
        reset_probe_case_ids=[f"{id}-r1"],
    )


def episodes_doc(ids=("e1", "e2")) -> dict:
    return dict(
        schema_version=1,
        dataset_lock_digest=h("lock"),
        config_digest=canonical_digest(episode_freeze()),
        episodes=[episode(i) for i in ids],
    )


def labels_for(episodes: dict) -> dict:
    labels = {}
    for e in episodes["episodes"]:
        for case in (*e["paraphrase_case_ids"], *e["control_case_ids"]):
            labels[case] = {"class": "positive", "acceptable_asset_ids": ["asset-a"]}
    return labels


def candidate(asset_id: str) -> dict:
    return dict(
        asset_id=asset_id,
        kind="skill",
        content_hash=h(asset_id),
        source_version=None,
        score=1.0,
        score_kind="lexical",
        evidence_refs=[],
    )


def result(top: str) -> dict:
    return dict(
        schema_version=1,
        decision="confident",
        catalog_revision=1,
        host_snapshot_revision=None,
        attempts=[],
        primary_attempt=None,
        base_candidates=[candidate(top)],
        base_decision="confident",
        final_candidates=[candidate(top)],
        final_decision="confident",
        fallback_reason=None,
        adaptation=None,
        admission=dict(
            outcome="accepted", rechecked_catalog_revision=1, checked_at="2026-10-31T00:00:00Z"
        ),
        observed_ms=1.0,
        embeddings=[],
        host_run_ids=[],
    )


def probe_row(case_id: str, ordinal: int, label: dict, top: str | None) -> dict:
    if top is None:
        return dict(
            slot=dict(phase="measured", repetition=0, case_id=case_id, ordinal=ordinal),
            status="not_run",
            reason="interrupted",
            request_digest=None,
            offered_projection_digest=None,
            catalog_revision=None,
            result=None,
            attempts=[],
            embeddings=[],
            observed_ms=None,
            observed_costs=[],
            resources=dict(cpu_ms=None, peak_bytes=None, coverage="unknown"),
            grade=grade_asset_result(label, None),
        )
    r = result(top)
    return dict(
        slot=dict(phase="measured", repetition=0, case_id=case_id, ordinal=ordinal),
        status="ready",
        reason=None,
        request_digest=h("req" + case_id),
        offered_projection_digest=h("proj"),
        catalog_revision=1,
        result=r,
        attempts=[],
        embeddings=[],
        observed_ms=1.0,
        observed_costs=[],
        resources=dict(cpu_ms=None, peak_bytes=None, coverage="unknown"),
        grade=grade_asset_result(label, r),
    )


def reset_array(e: dict, top="asset-a") -> list:
    return [
        dict(case_id=c, final_candidates=[candidate(top)], final_decision="confident")
        for c in e["reset_probe_case_ids"]
    ]


def episode_result(
    e: dict, labels: dict, *, off="asset-b", on="asset-a", status="completed", reset_after="asset-a"
) -> dict:
    probes = []
    for ordinal, (kind, case_id) in enumerate(probe_plan(e), start=1):
        top_off = None if status == "not_run" else off
        top_on = None if status == "not_run" else (on if kind == "paraphrase" else off)
        probes.append(
            dict(
                case_id=case_id,
                kind=kind,
                off=probe_row(case_id, ordinal, labels[case_id], top_off),
                on=probe_row(case_id, ordinal, labels[case_id], top_on),
            )
        )
    accepted = status == "completed"
    return dict(
        episode_id=e["id"],
        status=status,
        corrections=[
            dict(
                ordinal=n,
                case_id=c["case_id"],
                status="accepted" if accepted else "not_run",
                feedback_id=f"fb-{c['case_id']}" if accepted else None,
                event_digest=h("event" + c["case_id"]) if accepted else None,
            )
            for n, c in enumerate(e["corrections"], start=1)
        ],
        probes=probes,
        reset_baseline_digest=reset_preimage_digest(reset_array(e)) if accepted else None,
        reset_result_digest=(
            reset_preimage_digest(reset_array(e, reset_after)) if accepted else None
        ),
        reset_equal=(reset_after == "asset-a") if accepted else None,
        reason=None if accepted else "interrupted",
    )


def adaptation_report(episodes: dict, results: list, *, qualification="fixture") -> dict:
    labels = labels_for(episodes)
    report = dict(
        schema_version=1,
        kind="adaptation",
        qualification=qualification,
        input_digest=canonical_digest(episodes),
        input_manifest=dict(path="inputs.json", digest=H, bytes=1),
        off_config_digest=h("off"),
        on_config_digest=h("on"),
        environment=ENVIRONMENT,
        episodes=results,
        summary=None,
        evidence=[],
    )
    report["summary"] = episode_summary(report, episodes, labels)
    return report


def preimages(episodes: dict, reset_after="asset-a") -> dict:
    return {e["id"]: (reset_array(e), reset_array(e, reset_after)) for e in episodes["episodes"]}


def test_episode_freeze_digest_binds_the_episode_document():
    freeze = episode_freeze()
    episodes = episodes_doc()
    assert verify_episode_freeze(freeze) == canonical_digest(freeze)
    assert verify_episodes(episodes, freeze) == canonical_digest(episodes)


@pytest.mark.parametrize(
    "mutate, reason",
    [
        (lambda d: d["episodes"].reverse(), "episode_order"),
        (lambda d: d.update(config_digest=H), "config_binding"),
        (
            lambda d: d["episodes"][0]["corrections"][1].update(case_id="e1-c3"),
            "correction_identity",
        ),
        (lambda d: d["episodes"][0].update(control_case_ids=["e1-c1"]), "probe_reuse"),
        (lambda d: d["episodes"][0].update(reset_probe_case_ids=["e1-p1"]), "probe_reuse"),
    ],
)
def test_episode_input_contradictions_reject(mutate, reason):
    episodes = episodes_doc()
    mutate(episodes)
    with pytest.raises(WholeTaskRejected, match=reason):
        verify_episodes(episodes, episode_freeze())


def test_off_and_on_must_differ():
    freeze = dict(episode_freeze(), on_config_digest=h("off"))
    with pytest.raises(WholeTaskRejected, match="ablation_identity"):
        verify_episode_freeze(freeze)


def test_probe_plan_orders_kind_then_case_and_excludes_corrections_and_reset():
    e = episode("e1")
    assert probe_plan(e) == [("control", "e1-k1"), ("paraphrase", "e1-p1"), ("paraphrase", "e1-p2")]


def test_rates_use_every_planned_probe_and_fixture_gate_is_not_applicable():
    episodes = episodes_doc()
    labels = labels_for(episodes)
    results = [
        episode_result(episodes["episodes"][0], labels),
        episode_result(episodes["episodes"][1], labels, status="not_run"),
    ]
    report = adaptation_report(episodes, results)
    summary = grade_adaptation_report(
        report, episodes, episode_freeze(), labels, reset_preimages=preimages(episodes)
    )
    # Two of four planned paraphrases were corrected; the not_run episode stays
    # in the denominator.
    assert summary["paraphrases"]["numerator"] == 2
    assert summary["paraphrases"]["denominator"] == 4
    assert summary["off_control_top1"]["denominator"] == 2
    assert summary["completed_episodes"] == 1
    assert summary["reset_passed"] == 1 and summary["reset_unknown"] == 1
    assert summary["control_loss"] == 0
    assert summary["gate"] == "not_applicable"


def test_qualified_report_with_a_known_reset_failure_fails_before_blocking():
    episodes = episodes_doc()
    labels = labels_for(episodes)
    results = [episode_result(e, labels, reset_after="asset-b") for e in episodes["episodes"]]
    report = adaptation_report(episodes, results, qualification="qualified")
    summary = grade_adaptation_report(
        report, episodes, episode_freeze(), labels, reset_preimages=preimages(episodes, "asset-b")
    )
    assert summary["reset_passed"] == 0 and summary["gate"] == "fail"


def test_qualified_report_with_too_few_episodes_is_blocked_not_passed():
    episodes = episodes_doc()
    labels = labels_for(episodes)
    results = [episode_result(e, labels) for e in episodes["episodes"]]
    report = adaptation_report(episodes, results, qualification="qualified")
    summary = grade_adaptation_report(
        report, episodes, episode_freeze(), labels, reset_preimages=preimages(episodes)
    )
    assert summary["paraphrases"]["value"] == 1 and summary["gate"] == "blocked"


def test_reset_digest_without_its_preimage_is_unknown():
    episodes = episodes_doc()
    labels = labels_for(episodes)
    results = [episode_result(e, labels) for e in episodes["episodes"]]
    report = adaptation_report(episodes, results)
    summary = grade_adaptation_report(
        report, episodes, episode_freeze(), labels, reset_preimages={}
    )
    assert summary["reset_unknown"] == 2 and summary["reset_passed"] == 0


@pytest.mark.parametrize(
    "mutate, reason",
    [
        (lambda r: r["episodes"].reverse(), "episode_conservation"),
        (lambda r: r.update(input_digest=H), "input_binding"),
        (lambda r: r.update(on_config_digest=H), "config_binding"),
        (lambda r: r["episodes"][0]["corrections"].reverse(), "correction_order"),
        (
            lambda r: r["episodes"][0]["corrections"][0].update(feedback_id=None),
            "correction_receipt",
        ),
        (lambda r: r["episodes"][0]["probes"].pop(), "probe_conservation"),
        (lambda r: r["episodes"][0]["probes"][0]["on"]["slot"].update(ordinal=5), "probe_slot"),
        (lambda r: r["episodes"][0]["probes"][1]["on"]["grade"].update(hit3=False), "probe_grade"),
        (lambda r: r["episodes"][0].update(reset_equal=False), "reset_equality"),
        (
            lambda r: r["episodes"][0].update(reset_result_digest=H, reset_equal=False),
            "reset_preimage",
        ),
        (lambda r: r["episodes"][0].update(status="failed"), "episode_reason"),
        (lambda r: r["summary"].update(reset_passed=0), "summary_mismatch"),
    ],
)
def test_adaptation_report_contradictions_reject(mutate, reason):
    episodes = episodes_doc()
    labels = labels_for(episodes)
    results = [episode_result(e, labels) for e in episodes["episodes"]]
    report = adaptation_report(episodes, results)
    mutate(report)
    with pytest.raises(WholeTaskRejected, match=reason):
        grade_adaptation_report(
            report, episodes, episode_freeze(), labels, reset_preimages=preimages(episodes)
        )


def test_reset_preimage_must_cover_exactly_the_reset_probes():
    episodes = episodes_doc()
    labels = labels_for(episodes)
    results = [episode_result(e, labels) for e in episodes["episodes"]]
    report = adaptation_report(episodes, results)
    bad = preimages(episodes)
    baseline, after = bad["e1"]
    bad["e1"] = (baseline + [dict(baseline[0], case_id="e1-zz")], after)
    with pytest.raises(WholeTaskRejected, match="reset_preimage"):
        grade_adaptation_report(report, episodes, episode_freeze(), labels, reset_preimages=bad)


# --- platform lifecycle -----------------------------------------------------------


def platform_report(*, qualification="fixture", statuses=None) -> tuple[dict, dict, dict]:
    freeze = freeze_doc()
    tasks = tasks_doc(freeze)
    statuses = statuses or {}
    steps = []
    for name in PLATFORM_STEPS:
        status = statuses.get(name, "passed")
        steps.append(
            dict(
                name=name,
                status=status,
                evidence_ids=["receipt-" + name] if status == "passed" else [],
                reason=None if status == "passed" else "not_observed",
            )
        )
    evidence = [
        dict(id=i, path=None, sha256=h(i), bytes=1, provenance="fixture")
        for i in sorted({*(i for s in steps for i in s["evidence_ids"]), "ops", "approval"})
    ]
    by_id = {e["id"]: e for e in evidence}
    report = dict(
        schema_version=1,
        qualification=qualification,
        input_digest=canonical_digest(tasks),
        input_manifest=dict(path="inputs.json", digest=H, bytes=1),
        platform=ENVIRONMENT,
        artifacts=[],
        steps=steps,
        operations_evidence=copy.deepcopy(by_id["ops"]),
        approvals_evidence=[copy.deepcopy(by_id["approval"])],
        evidence=evidence,
        gate=None,
    )
    report["gate"] = platform_gate(report)
    return report, tasks, freeze


def test_platform_steps_follow_the_lifecycle_order():
    assert PLATFORM_STEPS == (
        "install",
        "configure",
        "recommend",
        "choose",
        "skill_use",
        "mcp_use",
        "feedback",
        "delete",
        "reset",
        "restart",
        "uninstall",
    )


def test_fixture_platform_report_is_not_applicable():
    report, tasks, freeze = platform_report()
    assert grade_platform_report(report, tasks, freeze) == "not_applicable"


def test_measured_failure_takes_precedence_over_missing_evidence():
    report, tasks, freeze = platform_report(
        qualification="measured", statuses={"mcp_use": "not_run", "uninstall": "failed"}
    )
    assert grade_platform_report(report, tasks, freeze) == "fail"


def test_measured_missing_step_blocks():
    report, tasks, freeze = platform_report(
        qualification="measured", statuses={"skill_use": "not_run"}
    )
    assert grade_platform_report(report, tasks, freeze) == "blocked"


def test_measured_all_passed_without_a_review_path_is_blocked():
    report, tasks, freeze = platform_report(qualification="measured")
    assert grade_platform_report(report, tasks, freeze) == "blocked"


@pytest.mark.parametrize(
    "mutate, reason",
    [
        (lambda r: r["steps"].reverse(), "step_order"),
        (lambda r: r["steps"][0].update(evidence_ids=[]), "step_receipt"),
        (lambda r: r["steps"][1].update(reason="x"), "step_reason"),
        (lambda r: r.update(input_digest=H), "input_binding"),
        (lambda r: r["operations_evidence"].update(sha256=H), "evidence_conflict"),
        (
            lambda r: r["approvals_evidence"].append(dict(r["approvals_evidence"][0], id="zz")),
            "evidence_dangling",
        ),
        (lambda r: r.update(gate="pass"), "gate_mismatch"),
    ],
)
def test_platform_contradictions_reject(mutate, reason):
    report, tasks, freeze = platform_report()
    mutate(report)
    with pytest.raises(WholeTaskRejected, match=reason):
        grade_platform_report(report, tasks, freeze)
