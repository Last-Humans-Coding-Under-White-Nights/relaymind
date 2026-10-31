"""Spec 0.60/0.64 section 12.6.5 adaptation episode driver (#202, #229).

RM37, RM42, RM44; AC28, AC33. Episodes run in-process against the real
recommendation service over the locked corpus projection, with a disposable
store per episode and arm and the evaluation-only action registration. The
exported report must grade with the offline grader. Fixture-qualified only.
"""

import asyncio

import pytest

from relaymind.canonical import canonical_digest
from relaymind.evaluation.asset_episode_driver import (
    locked_episode_arms,
    run_adaptation_episodes,
)
from relaymind.evaluation.asset_episode_export import (
    grade_adaptation_report,
    reset_preimages,
)
from relaymind.evaluation.asset_whole_task import WholeTaskRejected
from relaymind.evaluation.asset_whole_task_corpus import release_labels, resolve_corpus
from test_asset_adaptation_stage import config as adaptation_config
from test_embedding_backend import NativeDouble
from whole_task_helpers import ENVIRONMENT, effective_pair, expanded_release, fixture_production

H = "a" * 64


def corpus(tmp_path):
    from test_asset_whole_task_corpus import companions

    return resolve_corpus(*companions(expanded_release(tmp_path / "release")))


def setup(tmp_path, episode_count=1):
    from relaymind.assets.embedding_backends import EmbeddingEngine
    from relaymind.selection.adaptation_stage import AdaptationStage

    release = corpus(tmp_path)
    (tmp_path / "semantic").mkdir()
    semantic = adaptation_config(tmp_path / "semantic")
    adaptation = {"identity": "fixture", "manifest_digest": semantic.manifest.manifest_digest}
    off, on = effective_pair(canonical_digest(adaptation), release)
    freeze = dict(
        schema_version=1,
        off_config_digest=canonical_digest(off),
        on_config_digest=canonical_digest(on),
        adaptation_config_digest=canonical_digest(adaptation),
        seed=1,
    )
    record = release.catalog["assets"][0]["record"]
    episodes = dict(
        schema_version=1,
        dataset_lock_digest=H,
        config_digest=canonical_digest(freeze),
        episodes=[
            dict(
                id=f"e{n}",
                intent_family_ids=["task-calendar"],
                corrections=[
                    dict(
                        case_id=c,
                        target_asset_id=record["asset_id"],
                        content_hash=record["content_hash"],
                        schema_hash=None,
                    )
                    for c in ("case-03", "case-01", "case-02")
                ],
                paraphrase_case_ids=["case-04", "case-05"],
                control_case_ids=["case-06"],
                reset_probe_case_ids=["case-07"],
            )
            for n in range(1, episode_count + 1)
        ],
    )

    def stage(feedback):
        return AdaptationStage(
            feedback, semantic, EmbeddingEngine(semantic.manifest, NativeDouble())
        )

    arms = locked_episode_arms(
        release,
        dict(off=off, on=on),
        fixture_production().recommendations,
        tmp_path / "work",
        adaptation_stage=stage,
    )
    return release, freeze, episodes, arms


def run(tmp_path, *, interrupted=lambda: False, episode_count=1, qualification="fixture"):
    release, freeze, episodes, arms = setup(tmp_path, episode_count)
    report, evidence = asyncio.run(
        run_adaptation_episodes(
            episodes,
            freeze,
            release,
            arms,
            environment=ENVIRONMENT,
            qualification=qualification,
            input_manifest=dict(path="inputs.json", digest=H, bytes=1),
            interrupted=interrupted,
        )
    )
    return release, freeze, episodes, report, evidence


def test_an_episode_runs_through_the_real_lifecycle_and_grades(tmp_path):
    release, freeze, episodes, report, evidence = run(tmp_path)
    result = report["episodes"][0]
    assert result["status"] == "completed", result["reason"]
    assert [c["status"] for c in result["corrections"]] == ["accepted"] * 3
    assert all(c["feedback_id"] and len(c["event_digest"]) == 64 for c in result["corrections"])
    assert [(p["kind"], p["case_id"]) for p in result["probes"]] == [
        ("control", "case-06"),
        ("paraphrase", "case-04"),
        ("paraphrase", "case-05"),
    ]
    assert all(
        p["off"]["status"] == "ready" and p["on"]["status"] == "ready" for p in result["probes"]
    )
    assert all(
        p["off"]["request_digest"] and p["on"]["offered_projection_digest"]
        for p in result["probes"]
    )
    assert result["reset_equal"] is True
    summary = grade_adaptation_report(
        report,
        episodes,
        freeze,
        release_labels(release),
        reset_preimages=reset_preimages(evidence),
    )
    assert summary["reset_passed"] == 1 and summary["gate"] == "not_applicable"


def test_reset_evidence_is_the_spec_0_64_document(tmp_path):
    release, freeze, episodes, report, evidence = run(tmp_path)
    preimages = reset_preimages(evidence)
    baseline, after = preimages["e1"]
    assert [item["case_id"] for item in baseline] == ["case-07"]
    assert set(evidence) == {"reset:e1"}


def test_interruption_conserves_every_planned_episode(tmp_path):
    calls = []

    def interrupted():
        calls.append(1)
        return len(calls) > 1

    release, freeze, episodes, report, evidence = run(
        tmp_path, interrupted=interrupted, episode_count=2
    )
    assert [e["status"] for e in report["episodes"]] == ["completed", "not_run"]
    skipped = report["episodes"][1]
    assert all(c["status"] == "not_run" for c in skipped["corrections"])
    assert all(p["off"]["status"] == "not_run" for p in skipped["probes"])
    grade_adaptation_report(
        report,
        episodes,
        freeze,
        release_labels(release),
        reset_preimages=reset_preimages(evidence),
    )


def test_the_evaluation_registration_cannot_produce_a_qualified_report(tmp_path):
    with pytest.raises(WholeTaskRejected, match="qualification"):
        run(tmp_path, qualification="qualified")


def offers_for(tmp_path, release):
    from relaymind.evaluation.asset_whole_task_corpus import corpus_offers

    adaptation = {"identity": "fixture"}
    off, on = effective_pair(canonical_digest(adaptation), release)
    return corpus_offers(release, dict(off=off, on=on))


def test_observed_probe_offers_match_the_independent_reconstruction(tmp_path):
    release, freeze, episodes, report, evidence = run(tmp_path)
    grade_adaptation_report(
        report,
        episodes,
        freeze,
        release_labels(release),
        reset_preimages=reset_preimages(evidence),
        offers=offers_for(tmp_path, release),
    )


@pytest.mark.parametrize("field", ["request_digest", "offered_projection_digest"])
def test_a_changed_probe_offer_digest_rejects(tmp_path, field):
    release, freeze, episodes, report, evidence = run(tmp_path)
    report["episodes"][0]["probes"][0]["on"][field] = H
    with pytest.raises(WholeTaskRejected, match="probe_offer"):
        grade_adaptation_report(
            report,
            episodes,
            freeze,
            release_labels(release),
            reset_preimages=reset_preimages(evidence),
            offers=offers_for(tmp_path, release),
        )


def test_a_ready_probe_without_its_offer_rejects(tmp_path):
    release, freeze, episodes, report, evidence = run(tmp_path)
    probe = report["episodes"][0]["probes"][0]["off"]
    probe["request_digest"] = probe["offered_projection_digest"] = None
    with pytest.raises(WholeTaskRejected, match="probe_offer"):
        grade_adaptation_report(
            report,
            episodes,
            freeze,
            release_labels(release),
            reset_preimages=reset_preimages(evidence),
            offers=offers_for(tmp_path, release),
        )
