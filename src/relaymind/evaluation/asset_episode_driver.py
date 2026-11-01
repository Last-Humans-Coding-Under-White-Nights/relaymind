"""Adaptation episode driver over the real recommendation lifecycle (#202).

Spec 0.60/0.64 section 12.6.5 (RM37, RM42, RM44; AC28, AC33). Each episode runs
from empty feedback in disposable stores, one per arm: off probes use the frozen
disabled baseline; the on arm records reset baselines, applies exactly three
chronological corrections through the product feedback store, runs the paired
probes, resets feedback through the real lifecycle and repeats the reset probes.

The corpus is served through the explicit evaluation-only locked projection and
the corrections through an evaluation-only action registration in that
disposable store. This is never qualified host or person evidence, so a report
from this driver cannot claim ``qualified``.
"""

import asyncio
import contextlib
import copy
import hashlib
import secrets
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from relaymind.canonical import canonical_digest, canonical_json
from relaymind.evaluation.asset_benchmark import (
    _LockedProjectionProvider,
    _not_run,
    execute_asset_slot,
)
from relaymind.evaluation.asset_episode_export import (
    episode_preregistration,
    episode_summary,
    probe_plan,
    reset_preimage_digest,
)
from relaymind.evaluation.asset_metrics import grade_asset_result
from relaymind.evaluation.asset_run_journal import RunJournal, bound_header
from relaymind.evaluation.asset_whole_task import WholeTaskRejected
from relaymind.evaluation.asset_whole_task_corpus import release_labels

_ADAPTER = "relaymind-evaluation-episodes"
_CONTRACT = "relaymind-evaluation-episodes.v1"


@dataclass
class EpisodeArm:
    service: Any
    provider: Any
    normal: Mapping
    feedback: Any = None
    action: Any = None


class _LockedCatalog:
    """The catalog view the feedback store needs, over the locked projection."""

    def __init__(self, provider):
        self.provider = provider

    def revision(self) -> int:
        return self.provider.snapshot.catalog_revision

    def snapshot(self):
        return self.provider.snapshot


def _normal(effective: Mapping) -> dict:
    settings = effective["recommendations"]
    return dict(
        scope={k: settings[k] for k in ("user_id", "workspace_id")},
        permissions=list(effective["permissions"]),
        requested_kinds=list(effective["requested_kinds"]),
        selection_scope=effective["selection_scope"],
        budgets=effective["budgets"],
    )


def locked_episode_arms(
    release,
    effective: Mapping[str, Mapping],
    settings,
    work_dir: Path,
    *,
    adaptation_stage: Callable[[Any], Any],
):
    """Disposable in-process arms over the locked corpus projection.

    ``effective`` maps ``off``/``on`` to the verified effective configurations;
    ``adaptation_stage(feedback_store)`` builds the on arm's adaptation stage.
    """
    from relaymind.recommendations.registrations import (
        ActionRegistrations,
        Registration,
        RegistrationSnapshot,
    )
    from relaymind.recommendations.service import RecommendationService
    from relaymind.store import RunStore
    from relaymind.store.asset_feedback import AssetFeedbackStore

    work_dir = Path(work_dir)

    @contextlib.asynccontextmanager
    async def open_arm(episode_id: str, arm: str):
        config = effective[arm]
        work_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=f"episode-{arm}-", dir=work_dir) as directory:
            runs = RunStore(Path(directory) / "episode.sqlite3")
            provider = _LockedProjectionProvider(release, config)
            service = RecommendationService.for_provider(runs, provider, settings)
            feedback = action = None
            try:
                await provider.publish()
                if arm == "on":
                    token = secrets.token_urlsafe(32)
                    contract = (_ADAPTER, "evaluation-host", "evaluation-session", _CONTRACT)
                    registrations = ActionRegistrations(
                        runs, qualification=lambda *args: args == contract
                    )
                    registrations.replace(
                        RegistrationSnapshot(
                            (
                                Registration(
                                    _ADAPTER,
                                    provider.scope["user_id"],
                                    provider.scope["workspace_id"],
                                    hashlib.sha256(token.encode()).hexdigest(),
                                    True,
                                    *contract[1:],
                                ),
                            )
                        )
                    )
                    feedback = AssetFeedbackStore(
                        service.store,
                        _LockedCatalog(provider),
                        registrations,
                        policy=provider.policy,
                    )
                    action = registrations.authenticate(token, provider.scope)
                    service.adaptation = adaptation_stage(feedback)
                yield EpisodeArm(service, provider, _normal(config), feedback, action)
            finally:
                await service.stop()
                runs.close()

    return open_arm


def _slot(case_id: str, ordinal: int) -> dict:
    return dict(phase="measured", repetition=0, case_id=case_id, ordinal=ordinal)


def _semantics(row: Mapping, case_id: str) -> dict:
    """The reset semantic item of one ready product result (never invented)."""
    result = row["result"]
    return dict(
        case_id=case_id,
        final_candidates=list(result["final_candidates"]),
        final_decision=result["final_decision"],
    )


def _now():
    return datetime.now(UTC)


async def _correct(arm: EpisodeArm, recommendation_id, target: Mapping, revision: int, key: str):
    """One correction through the product store; returns (feedback_id, event_digest, revision)."""
    from relaymind.api.recommendations import request_digest
    from relaymind.store.recommendations import RecommendationClaim

    endpoint = "/relay/v1/recommendations/{id}/feedback"
    body = dict(
        schema_version=1,
        event_id=key,
        expected_feedback_revision=revision,
        feedback=dict(
            kind="correction",
            asset_id=target["target_asset_id"],
            content_hash=target["content_hash"],
        ),
    )
    claim = RecommendationClaim(
        arm.action.scope,
        arm.action.adapter_id,
        "POST",
        endpoint,
        recommendation_id,
        key,
        request_digest("POST", endpoint, recommendation_id, body),
    )
    response = arm.feedback.create(arm.action, recommendation_id, body, claim, now=_now())
    if response.status != 200:
        raise WholeTaskRejected("correction_refused")
    feedback_id = response.body["feedback_id"]
    events = arm.service.events(arm.provider.scope, recommendation_id, after_seq=0, limit=100)
    recorded = [
        e
        for e in events["events"]
        if e["event_type"] == "feedback.recorded" and e["payload"]["feedback_id"] == feedback_id
    ]
    if len(recorded) != 1:
        raise WholeTaskRejected("correction_event")
    return feedback_id, canonical_digest(recorded[0]), response.body["feedback_revision"]


async def _reset(arm: EpisodeArm, revision: int) -> None:
    from relaymind.api.recommendations import request_digest
    from relaymind.store.recommendations import RecommendationClaim

    endpoint = "/relay/v1/feedback/reset"
    body = dict(schema_version=1, expected_feedback_revision=revision)
    claim = RecommendationClaim(
        arm.action.scope,
        arm.action.adapter_id,
        "POST",
        endpoint,
        None,
        "reset",
        request_digest("POST", endpoint, None, body),
    )
    response = arm.feedback.reset(arm.action, revision, claim, now=_now())
    if response.status != 200:
        raise WholeTaskRejected("reset_refused")


def _not_run_episode(episode: Mapping, labels: Mapping, reason: str) -> dict:
    probes = []
    for ordinal, (kind, case_id) in enumerate(probe_plan(episode), start=1):
        # A row that never ran needs no label; the grader still checks every
        # planned probe against the frozen labels.
        label = labels.get(case_id, _NEGATIVE)
        probes.append(
            dict(
                case_id=case_id,
                kind=kind,
                off=_not_run(_slot(case_id, ordinal), label, reason),
                on=_not_run(_slot(case_id, ordinal), label, reason),
            )
        )
    return dict(
        episode_id=episode["id"],
        status="not_run",
        corrections=[
            dict(
                ordinal=n,
                case_id=c["case_id"],
                status="not_run",
                feedback_id=None,
                event_digest=None,
            )
            for n, c in enumerate(episode["corrections"], start=1)
        ],
        probes=probes,
        reset_baseline_digest=None,
        reset_result_digest=None,
        reset_equal=None,
        reason=reason,
    )


class _Stop(Exception):
    """Ends one episode with a fixed status and reason; earlier work is kept."""

    def __init__(self, status: str, reason: str):
        super().__init__(reason)
        self.status, self.reason = status, reason


async def _run_episode(episode, cases, labels, open_arm, evidence, interrupted):
    """Run one episode; every planned entry is conserved whatever happens."""
    from relaymind.recommendations.errors import RecommendationError

    episode_id = episode["id"]
    plan = probe_plan(episode)
    result = _not_run_episode(episode, labels, "interrupted")
    result.update(status="completed", reason=None)

    async def run(arm, arm_name, phase, case_id, ordinal=1):
        if interrupted():
            raise _Stop("cancelled", "interrupted")
        row, forced, recommendation_id = await execute_asset_slot(
            arm.service,
            arm.provider,
            arm.normal,
            cases[case_id],
            _slot(case_id, ordinal),
            labels.get(case_id, _NEGATIVE),
            key=f"{episode_id}:{arm_name}:{phase}:{case_id}",
        )
        return row, forced, recommendation_id

    def stop_on(forced):
        if forced == "interrupted":
            raise _Stop("cancelled", "interrupted")
        if forced == "blocked":
            raise _Stop("failed", "internal_error")

    try:
        async with open_arm(episode_id, "off") as off:
            for ordinal, (_, case_id) in enumerate(plan, start=1):
                row, forced, _ = await run(off, "off", "probe", case_id, ordinal)
                result["probes"][ordinal - 1]["off"] = row
                stop_on(forced)
        async with open_arm(episode_id, "on") as on:
            baseline = []
            for case_id in episode["reset_probe_case_ids"]:
                row, forced, _ = await run(on, "on", "baseline", case_id)
                baseline.append(row)
                stop_on(forced)
            revision = 0
            for n, correction in enumerate(episode["corrections"], start=1):
                entry = result["corrections"][n - 1]
                row, forced, recommendation_id = await run(
                    on, "on", f"correction-{n}", correction["case_id"]
                )
                stop_on(forced)
                if row["status"] != "ready" or recommendation_id is None:
                    entry["status"] = "failed"
                    raise _Stop("failed", "correction_parent_failed")
                if interrupted():
                    # No correction is admitted once an interruption is pending.
                    raise _Stop("cancelled", "interrupted")
                try:
                    feedback_id, event_digest, revision = await _correct(
                        on, recommendation_id, correction, revision, f"{episode_id}-c{n}"
                    )
                except (RecommendationError, WholeTaskRejected):
                    entry["status"] = "failed"
                    raise _Stop("failed", "correction_refused") from None
                entry.update(status="accepted", feedback_id=feedback_id, event_digest=event_digest)
            for ordinal, (_, case_id) in enumerate(plan, start=1):
                row, forced, _ = await run(on, "on", "probe", case_id, ordinal)
                result["probes"][ordinal - 1]["on"] = row
                stop_on(forced)
            if interrupted():
                raise _Stop("cancelled", "interrupted")
            try:
                await _reset(on, revision)
            except (RecommendationError, WholeTaskRejected):
                raise _Stop("failed", "reset_refused") from None
            after = []
            for case_id in episode["reset_probe_case_ids"]:
                row, forced, _ = await run(on, "on", "after", case_id)
                after.append(row)
                stop_on(forced)
        # Reset preimages come only from complete ready product results.
        if any(row["status"] != "ready" for row in (*baseline, *after)):
            raise _Stop("failed", "reset_probe_failed")
        before = [
            _semantics(r, c) for r, c in zip(baseline, episode["reset_probe_case_ids"], strict=True)
        ]
        later = [
            _semantics(r, c) for r, c in zip(after, episode["reset_probe_case_ids"], strict=True)
        ]
        before_digest, after_digest = reset_preimage_digest(before), reset_preimage_digest(later)
        payload = canonical_json(
            dict(schema_version=1, episode_id=episode_id, baseline=before, after=later)
        ).encode("utf-8")
        evidence["reset:" + episode_id] = dict(
            id="reset:" + episode_id,
            sha256=hashlib.sha256(payload).hexdigest(),
            bytes=len(payload),
            payload=payload,
        )
        result.update(
            reset_baseline_digest=before_digest,
            reset_result_digest=after_digest,
            reset_equal=before_digest == after_digest,
        )
    except _Stop as stop:
        result.update(status=stop.status, reason=stop.reason)
    except asyncio.CancelledError:
        # Actual task cancellation conserves this episode's completed work; the
        # caller then fills every later planned episode as not_run.
        result.update(status="cancelled", reason="interrupted")
    except RecommendationError:
        result.update(status="failed", reason="recommendation_unavailable")
    except Exception:
        result.update(status="failed", reason="internal_error")
    if result["status"] != "completed":
        for probe in result["probes"]:
            for arm in ("off", "on"):
                if probe[arm]["status"] == "not_run":
                    probe[arm]["reason"] = result["reason"]
    return result


def _restored(value: Mapping, episode_id: str, evidence: dict) -> dict:
    """A journaled episode result and its reset evidence, bytes rechecked."""
    if (
        not isinstance(value, Mapping)
        or not isinstance(value.get("evidence"), list)
        or not all(isinstance(item, Mapping) for item in value["evidence"])
        or not isinstance(value.get("result"), Mapping)
    ):
        raise WholeTaskRejected("journal_evidence")
    for item in value["evidence"]:
        payload = item["payload"].encode("utf-8")
        if (
            item["id"] != "reset:" + episode_id
            or hashlib.sha256(payload).hexdigest() != item["sha256"]
            or len(payload) != item["bytes"]
        ):
            raise WholeTaskRejected("journal_evidence")
        evidence[item["id"]] = dict(item, payload=payload)
    if value["result"].get("episode_id") != episode_id:
        raise WholeTaskRejected("journal_evidence")
    return copy.deepcopy(value["result"])


# Correction and reset cases need not be labeled probes; they are graded only
# through the probe rows, so a neutral label keeps their rows well formed.
_NEGATIVE = {"class": "negative", "acceptable_asset_ids": []}


async def run_adaptation_episodes(
    episodes: Mapping,
    freeze: Mapping,
    release,
    open_arm,
    *,
    environment: Mapping,
    qualification: str,
    input_manifest: Mapping,
    interrupted: Callable[[], bool],
    journal: Path | None = None,
) -> tuple[dict, dict[str, dict]]:
    """Run every frozen episode and export ``(AssetAdaptationReportV1, reset evidence)``.

    The reset evidence maps ``reset:<episode_id>`` to ``{id, sha256, bytes,
    payload}`` holding the spec 0.64 document; it belongs in the companion
    evidence registry.

    With ``journal``, progress is kept in a crash-safe run journal bound to the
    preregistered episode, freeze and plan digests and to the execution
    identity (environment, input manifest, qualification and the corpus
    release lock): a finished episode (in any
    status) is restored, never rerun; an episode started before a crash and
    never recorded is ``cancelled`` with reason ``interrupted_unrecorded``.
    """
    if qualification == "qualified":
        # Evaluation-only projection and registration are never qualified evidence.
        raise WholeTaskRejected("qualification")
    cases = {case["id"]: case for case in release.cases["cases"]}
    labels = release_labels(release)
    evidence: dict[str, dict] = {}
    results = []
    bound = contextlib.nullcontext()
    if journal is not None:
        # The release (its lock binds catalog, cases and labels) is part of the
        # identity: restored results are never mixed with another release's.
        identity = dict(
            environment=environment,
            input_manifest=input_manifest,
            qualification=qualification,
            release_lock_digest=canonical_digest(dict(release.lock)),
        )
        header = bound_header("adaptation", episode_preregistration(episodes, freeze), identity)
        bound = RunJournal(journal, header)
    with bound as book:
        for episode in episodes["episodes"]:
            key, fresh = episode["id"], False
            if book is not None and key in book.finished:
                result = _restored(book.finished[key], key, evidence)
            elif book is not None and key in book.started:
                result = _not_run_episode(episode, labels, "interrupted_unrecorded")
                result["status"] = "cancelled"
                book.finish(key, dict(result=result, evidence=[]))
            elif interrupted():
                # An interruption ends the whole run, even if the check reports it once.
                results.extend(
                    _not_run_episode(later, labels, "interrupted")
                    for later in episodes["episodes"][len(results) :]
                )
                break
            else:
                if book is not None:
                    book.start(key)
                own: dict[str, dict] = {}
                fresh = True
                result = await _run_episode(episode, cases, labels, open_arm, own, interrupted)
                if book is not None:
                    book.finish(
                        key,
                        dict(
                            result=result,
                            evidence=[
                                dict(item, payload=item["payload"].decode("utf-8"))
                                for item in own.values()
                            ],
                        ),
                    )
                evidence.update(own)
            results.append(result)
            # Only this run's own interruption ends it; a restored one does not.
            if fresh and result["status"] == "cancelled":
                for later in episodes["episodes"][len(results) :]:
                    results.append(_not_run_episode(later, labels, "interrupted"))
                break
    report = dict(
        schema_version=1,
        kind="adaptation",
        qualification=qualification,
        input_digest=canonical_digest(episodes),
        input_manifest=dict(input_manifest),
        off_config_digest=freeze["off_config_digest"],
        on_config_digest=freeze["on_config_digest"],
        environment=copy.deepcopy(dict(environment)),
        episodes=results,
        summary=None,
        evidence=[],
    )
    report["summary"] = episode_summary(report, episodes, labels)
    for result in results:
        for probe in result["probes"]:
            label = labels[probe["case_id"]]
            for arm in ("off", "on"):
                probe[arm]["grade"] = grade_asset_result(label, probe[arm]["result"])
    return report, evidence
