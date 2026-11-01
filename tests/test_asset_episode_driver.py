"""Spec 0.60/0.64 section 12.6.5 adaptation episode driver (#202, #229).

RM37, RM42, RM44; AC28, AC33. Episodes run in-process against the real
recommendation service over the locked corpus projection, with a disposable
store per episode and arm and the evaluation-only action registration. The
exported report must grade with the offline grader. Fixture-qualified only.
"""

import asyncio

import pytest

from helpers import FakeClock
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
        clock=FakeClock(),
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


def test_interruption_conserves_every_planned_episode(tmp_path, monkeypatch):
    from relaymind.evaluation import asset_episode_driver

    finished = []
    original = asset_episode_driver._run_episode

    async def counted(*args, **kwargs):
        result = await original(*args, **kwargs)
        finished.append(result["episode_id"])
        return result

    monkeypatch.setattr(asset_episode_driver, "_run_episode", counted)
    release, freeze, episodes, report, evidence = run(
        tmp_path, interrupted=lambda: bool(finished), episode_count=2
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


# --- review follow-ups (P1/P2) ---------------------------------------------------


def test_every_episode_invocation_has_its_own_claim(tmp_path, monkeypatch):
    from relaymind.evaluation import asset_episode_driver

    keys = []
    original = asset_episode_driver.execute_asset_slot

    async def spy(*args, key=None, **kwargs):
        keys.append(key)
        return await original(*args, key=key, **kwargs)

    monkeypatch.setattr(asset_episode_driver, "execute_asset_slot", spy)
    release, freeze, episodes, report, evidence = run(tmp_path)
    assert report["episodes"][0]["status"] == "completed"
    assert None not in keys and len(keys) == len(set(keys))


def test_a_failed_reset_probe_leaves_reset_unknown(tmp_path, monkeypatch):
    from relaymind.evaluation import asset_episode_driver

    original = asset_episode_driver.execute_asset_slot

    async def failing(service, provider, normal, case, slot, label, *, key=None):
        row, status, rid = await original(service, provider, normal, case, slot, label, key=key)
        if key and key.endswith(":after:case-07"):
            row = dict(row, status="failed", reason="internal_error", result=None)
        return row, status, rid

    monkeypatch.setattr(asset_episode_driver, "execute_asset_slot", failing)
    release, freeze, episodes, report, evidence = run(tmp_path)
    result = report["episodes"][0]
    assert result["reset_equal"] is None and result["reset_result_digest"] is None
    assert result["status"] == "failed" and evidence == {}


def test_a_refused_correction_conserves_the_report_and_later_episodes(tmp_path):
    release, freeze, episodes, arms = setup(tmp_path, episode_count=2)
    first = episodes["episodes"][0]["corrections"][0]
    first.update(target_asset_id="absent", content_hash="0" * 64)
    report, evidence = asyncio.run(
        run_adaptation_episodes(
            episodes,
            freeze,
            release,
            arms,
            environment=ENVIRONMENT,
            qualification="fixture",
            input_manifest=dict(path="inputs.json", digest=H, bytes=1),
            interrupted=lambda: False,
        )
    )
    one, two = report["episodes"]
    assert one["status"] == "failed" and one["corrections"][0]["status"] == "failed"
    assert [c["status"] for c in one["corrections"][1:]] == ["not_run", "not_run"]
    assert two["status"] == "completed"
    grade_adaptation_report(
        report, episodes, freeze, release_labels(release), reset_preimages=reset_preimages(evidence)
    )


def test_an_exception_during_reset_keeps_the_episode_work(tmp_path, monkeypatch):
    from relaymind.evaluation import asset_episode_driver

    async def broken(*args, **kwargs):
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(asset_episode_driver, "_reset", broken)
    release, freeze, episodes, report, evidence = run(tmp_path)
    result = report["episodes"][0]
    assert result["status"] == "failed" and result["reason"] == "internal_error"
    assert [c["status"] for c in result["corrections"]] == ["accepted"] * 3
    assert all(p["on"]["status"] == "ready" for p in result["probes"])
    assert result["reset_equal"] is None


def test_interruption_inside_an_episode_cancels_the_rest(tmp_path):
    calls = []

    def interrupted():
        calls.append(1)
        return len(calls) > 4

    release, freeze, episodes, report, evidence = run(tmp_path, interrupted=interrupted)
    result = report["episodes"][0]
    assert result["status"] == "cancelled" and result["reason"] == "interrupted"
    statuses = [p[arm]["status"] for p in result["probes"] for arm in ("off", "on")]
    assert "not_run" in statuses
    grade_adaptation_report(
        report, episodes, freeze, release_labels(release), reset_preimages=reset_preimages(evidence)
    )


def test_a_failed_dispatched_probe_must_still_bind_its_offer(tmp_path):
    release, freeze, episodes, report, evidence = run(tmp_path)
    probe = report["episodes"][0]["probes"][0]["on"]
    probe.update(
        status="failed",
        reason="internal_error",
        result=None,
        catalog_revision=999,
        offered_projection_digest="c" * 64,
        request_digest=None,
    )
    from relaymind.evaluation.asset_metrics import grade_asset_result

    probe["grade"] = grade_asset_result(release_labels(release)["case-06"], None)
    with pytest.raises(WholeTaskRejected, match="probe_offer"):
        grade_adaptation_report(
            report,
            episodes,
            freeze,
            release_labels(release),
            reset_preimages=reset_preimages(evidence),
            offers=offers_for(tmp_path, release),
        )


@pytest.mark.parametrize(
    "mutate", ["duplicate", "not_canonical", "unclaimed_malformed", "unknown_episode"]
)
def test_reset_evidence_is_strict_and_always_checked(tmp_path, mutate):
    from relaymind.canonical import canonical_json

    release, freeze, episodes, report, evidence = run(tmp_path)
    item = evidence["reset:e1"]
    if mutate == "duplicate":
        item["payload"] = item["payload"].replace(
            b'"schema_version":1', b'"schema_version":2,"schema_version":1'
        )
    elif mutate == "not_canonical":
        item["payload"] = b" " + item["payload"]
    elif mutate == "unclaimed_malformed":
        report["episodes"][0].update(
            reset_baseline_digest=None, reset_result_digest=None, reset_equal=None
        )
        report["summary"]["reset_passed"] = 0
        report["summary"]["reset_unknown"] = 1
        item["payload"] = canonical_json(
            dict(schema_version=1, episode_id="e1", baseline=["not a result"], after=[])
        ).encode()
    else:
        evidence["reset:e9"] = dict(
            item,
            id="reset:e9",
            payload=canonical_json(
                dict(schema_version=1, episode_id="e9", baseline=[], after=[])
            ).encode(),
        )
    with pytest.raises(WholeTaskRejected, match="reset"):
        grade_adaptation_report(
            report,
            episodes,
            freeze,
            release_labels(release),
            reset_preimages=reset_preimages(evidence),
        )


# --- second review residuals (R3/R7) ---------------------------------------------


def test_cancellation_during_arm_startup_returns_a_conserved_report(tmp_path):
    release, freeze, episodes, arms = setup(tmp_path, episode_count=2)
    calls = []

    import contextlib

    @contextlib.asynccontextmanager
    async def cancelling(episode_id, arm):
        calls.append((episode_id, arm))
        if episode_id == "e2" and arm == "on":
            raise asyncio.CancelledError
        async with arms(episode_id, arm) as value:
            yield value

    report, evidence = asyncio.run(
        run_adaptation_episodes(
            episodes,
            freeze,
            release,
            cancelling,
            environment=ENVIRONMENT,
            qualification="fixture",
            input_manifest=dict(path="inputs.json", digest=H, bytes=1),
            interrupted=lambda: False,
        )
    )
    assert [e["status"] for e in report["episodes"]] == ["completed", "cancelled"]
    assert report["episodes"][1]["reason"] == "interrupted"
    grade_adaptation_report(
        report, episodes, freeze, release_labels(release), reset_preimages=reset_preimages(evidence)
    )


def test_an_interrupt_during_the_parent_request_admits_no_correction(tmp_path, monkeypatch):
    from relaymind.evaluation import asset_episode_driver

    flag = []
    original = asset_episode_driver.execute_asset_slot
    corrected = []

    async def parent_then_interrupt(*args, key=None, **kwargs):
        result = await original(*args, key=key, **kwargs)
        if key and ":correction-1:" in key:
            flag.append(True)
        return result

    real_correct = asset_episode_driver._correct

    async def spy(*args, **kwargs):
        corrected.append(1)
        return await real_correct(*args, **kwargs)

    monkeypatch.setattr(asset_episode_driver, "execute_asset_slot", parent_then_interrupt)
    monkeypatch.setattr(asset_episode_driver, "_correct", spy)
    release, freeze, episodes, report, evidence = run(tmp_path, interrupted=lambda: bool(flag))
    assert corrected == []
    result = report["episodes"][0]
    assert result["status"] == "cancelled"
    assert [c["status"] for c in result["corrections"]] == ["not_run"] * 3


def test_declared_inference_cost_requires_the_offer(tmp_path):
    release, freeze, episodes, report, evidence = run(tmp_path)
    probe = report["episodes"][0]["probes"][0]["on"]
    probe.update(
        status="failed",
        reason="internal_error",
        result=None,
        attempts=[],
        embeddings=[],
        request_digest=None,
        offered_projection_digest=None,
        catalog_revision=None,
        observed_ms=1.0,
    )
    probe["observed_costs"] = [
        dict(
            provenance="observed",
            value=dict(
                input_tokens=10,
                output_tokens=0,
                cached_tokens=None,
                reasoning_tokens=None,
                calls=1,
                cpu_ms=None,
                peak_bytes=None,
            ),
        )
    ]
    from relaymind.evaluation.asset_metrics import grade_asset_result

    probe["grade"] = grade_asset_result(release_labels(release)["case-06"], None)
    with pytest.raises(WholeTaskRejected, match="probe_offer"):
        grade_adaptation_report(
            report,
            episodes,
            freeze,
            release_labels(release),
            reset_preimages=reset_preimages(evidence),
            offers=offers_for(tmp_path, release),
        )


# --- #232 review (ArcsinX, 1 Nov) ------------------------------------------------


def test_a_one_shot_interruption_still_ends_the_whole_run(tmp_path):
    calls = []

    def once():
        calls.append(1)
        return len(calls) == 1

    release, freeze, episodes, report, evidence = run(tmp_path, interrupted=once, episode_count=2)
    assert [e["status"] for e in report["episodes"]] == ["not_run", "not_run"]
    assert {e["reason"] for e in report["episodes"]} == {"interrupted"}


def test_a_not_run_episode_never_needs_its_labels(tmp_path):
    from relaymind.evaluation.asset_episode_driver import _not_run_episode

    release, freeze, episodes, arms = setup(tmp_path)
    result = _not_run_episode(episodes["episodes"][0], {}, "interrupted")
    assert result["status"] == "not_run" and result["probes"]


def test_a_refused_reset_keeps_its_own_reason(tmp_path, monkeypatch):
    """RM42/AC33: a refused reset ends the episode as reset_refused, not internal_error."""
    from relaymind.evaluation import asset_episode_driver

    async def refused(*args, **kwargs):
        raise WholeTaskRejected("reset_refused")

    monkeypatch.setattr(asset_episode_driver, "_reset", refused)
    release, freeze, episodes, report, evidence = run(tmp_path)
    result = report["episodes"][0]
    assert result["status"] == "failed" and result["reason"] == "reset_refused"
    assert [c["status"] for c in result["corrections"]] == ["accepted"] * 3
    assert result["reset_equal"] is None


@pytest.mark.parametrize(
    "value",
    [
        {},
        dict(result=dict(episode_id="e1")),
        dict(evidence=[]),
        dict(evidence=None, result=dict(episode_id="e1")),
        dict(evidence=[3], result=dict(episode_id="e1")),
        dict(evidence=[], result=None),
        dict(evidence=[], result=[]),
    ],
)
def test_a_malformed_journaled_episode_rejects_with_a_fixed_reason(value):
    """Review 4436631723: a malformed journal entry is journal_evidence, never a raw error."""
    from relaymind.evaluation.asset_episode_driver import _restored

    with pytest.raises(WholeTaskRejected, match="journal_evidence"):
        _restored(value, "e1", {})
