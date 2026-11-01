"""Episode preregistration and resumable episode runs (#236).

Spec 0.60/0.64/0.65 section 12.6.5 (RM42, RM44; AC33). The preregistration
check reports the digests an adaptation run is bound to and whether the frozen
episode set could be eligible for acceptance; the driver's run journal keeps
completed episodes across a crash and never reruns a started one. Fixture
episodes only, always unqualified.
"""

import asyncio

import pytest

from relaymind.canonical import canonical_digest
from relaymind.evaluation.asset_episode_driver import run_adaptation_episodes
from relaymind.evaluation.asset_episode_export import (
    episode_preregistration,
    grade_adaptation_report,
    reset_preimages,
)
from relaymind.evaluation.asset_run_journal import JournalError
from relaymind.evaluation.asset_whole_task_corpus import release_labels
from test_asset_episode_driver import setup
from whole_task_helpers import ENVIRONMENT, H


def run(tmp_path, release, freeze, episodes, arms, journal, interrupted=lambda: False):
    return asyncio.run(
        run_adaptation_episodes(
            episodes,
            freeze,
            release,
            arms,
            environment=ENVIRONMENT,
            qualification="fixture",
            input_manifest=dict(path="inputs.json", digest=H, bytes=1),
            interrupted=interrupted,
            journal=journal,
        )
    )


def test_preregistration_reports_digests_and_acceptance_eligibility(tmp_path):
    release, freeze, episodes, _ = setup(tmp_path, episode_count=2)
    record = episode_preregistration(episodes, freeze, release)
    assert record["input_digest"] == canonical_digest(episodes)
    assert record["freeze_digest"] == canonical_digest(freeze)
    assert record["episodes"] == 2
    assert record["acceptance_eligible"] is False
    # The fixture corpus is a provisional fixture release, and too small.
    assert record["reasons"] == [
        "below_minimum_episodes",
        "corpus_not_acceptance",
        "corpus_not_frozen",
    ]
    assert record["probes_not_held_out"] == 8  # diagnostic only
    assert record["unlabelled_probes"] == 0
    assert record["missing_pool_targets"] == 0


def test_preregistration_without_the_corpus_cannot_be_eligible(tmp_path):
    release, freeze, episodes, _ = setup(tmp_path)
    record = episode_preregistration(episodes, freeze, None)
    assert "corpus_unverified" in record["reasons"]
    assert record["probes_not_held_out"] is None


def test_a_journaled_run_resumes_without_rerunning_completed_episodes(tmp_path, monkeypatch):
    from relaymind.evaluation import asset_episode_driver

    release, freeze, episodes, arms = setup(tmp_path, episode_count=2)
    journal = tmp_path / "episodes.jsonl"
    ran = []
    original = asset_episode_driver._run_episode

    async def counted(episode, *args, **kwargs):
        result = await original(episode, *args, **kwargs)
        ran.append(episode["id"])
        return result

    monkeypatch.setattr(asset_episode_driver, "_run_episode", counted)
    partial, _ = run(tmp_path, release, freeze, episodes, arms, journal, lambda: bool(ran))
    assert [e["status"] for e in partial["episodes"]] == ["completed", "not_run"]
    report, evidence = run(tmp_path, release, freeze, episodes, arms, journal)
    assert ran == ["e1", "e2"]
    assert [e["status"] for e in report["episodes"]] == ["completed", "completed"]
    # The first episode's reset evidence is restored from the journal.
    assert set(evidence) == {"reset:e1", "reset:e2"}
    summary = grade_adaptation_report(
        report,
        episodes,
        freeze,
        release_labels(release),
        reset_preimages=reset_preimages(evidence),
    )
    assert summary["reset_passed"] == 2 and summary["gate"] == "not_applicable"


class Crash(BaseException):
    """A process death inside an episode."""


def test_an_episode_started_before_a_crash_is_cancelled_not_rerun(tmp_path, monkeypatch):
    from relaymind.evaluation import asset_episode_driver

    release, freeze, episodes, arms = setup(tmp_path, episode_count=2)
    journal = tmp_path / "episodes.jsonl"
    original = asset_episode_driver._run_episode

    async def crash(episode, *args, **kwargs):
        if episode["id"] == "e2":
            raise Crash()
        return await original(episode, *args, **kwargs)

    monkeypatch.setattr(asset_episode_driver, "_run_episode", crash)
    with pytest.raises(Crash):
        run(tmp_path, release, freeze, episodes, arms, journal)
    monkeypatch.setattr(asset_episode_driver, "_run_episode", original)
    report, evidence = run(tmp_path, release, freeze, episodes, arms, journal)
    first, second = report["episodes"]
    assert first["status"] == "completed"
    assert second["status"] == "cancelled" and second["reason"] == "interrupted_unrecorded"
    assert all(c["status"] == "not_run" for c in second["corrections"])
    grade_adaptation_report(
        report, episodes, freeze, release_labels(release), reset_preimages=reset_preimages(evidence)
    )


def test_changed_episodes_cannot_resume_a_journal(tmp_path):
    release, freeze, episodes, arms = setup(tmp_path)
    journal = tmp_path / "episodes.jsonl"
    run(tmp_path, release, freeze, episodes, arms, journal)
    episodes["episodes"][0]["control_case_ids"] = ["case-06", "case-07"]
    episodes["episodes"][0]["reset_probe_case_ids"] = ["case-00"]
    with pytest.raises(JournalError, match="journal_binding"):
        run(tmp_path, release, freeze, episodes, arms, journal)


def test_an_interrupted_episode_is_kept_and_the_resumed_run_continues(tmp_path):
    release, freeze, episodes, arms = setup(tmp_path, episode_count=2)
    journal = tmp_path / "episodes.jsonl"
    calls = []

    def interrupted():
        calls.append(1)
        return len(calls) > 4

    partial, _ = run(tmp_path, release, freeze, episodes, arms, journal, interrupted)
    assert [e["status"] for e in partial["episodes"]] == ["cancelled", "not_run"]
    report, evidence = run(tmp_path, release, freeze, episodes, arms, journal)
    # Its consumed work is conserved as cancelled; the next episode still runs.
    assert [e["status"] for e in report["episodes"]] == ["cancelled", "completed"]
    assert report["episodes"][0] == partial["episodes"][0]
    grade_adaptation_report(
        report, episodes, freeze, release_labels(release), reset_preimages=reset_preimages(evidence)
    )
