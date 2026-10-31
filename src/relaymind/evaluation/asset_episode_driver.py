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
    episode_summary,
    probe_plan,
    reset_preimage_digest,
)
from relaymind.evaluation.asset_metrics import grade_asset_result
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
    result = row["result"] or {}
    return dict(
        case_id=case_id,
        final_candidates=list(result.get("final_candidates", [])),
        final_decision=result.get("final_decision", "no_match"),
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
        label = labels[case_id]
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


async def _run_episode(episode, cases, labels, open_arm, evidence):
    plan = probe_plan(episode)
    result = _not_run_episode(episode, labels, "interrupted")
    result["reason"] = None
    off_rows = {}
    async with open_arm(episode["id"], "off") as off:
        for ordinal, (_, case_id) in enumerate(plan, start=1):
            row, _, _ = await execute_asset_slot(
                off.service,
                off.provider,
                off.normal,
                cases[case_id],
                _slot(case_id, ordinal),
                labels.get(case_id, _NEGATIVE),
            )
            off_rows[case_id] = row
    async with open_arm(episode["id"], "on") as on:

        async def probe(case_id, ordinal=1):
            return await execute_asset_slot(
                on.service,
                on.provider,
                on.normal,
                cases[case_id],
                _slot(case_id, ordinal),
                labels.get(case_id, _NEGATIVE),
            )

        baseline = []
        for case_id in episode["reset_probe_case_ids"]:
            row, _, _ = await probe(case_id)
            baseline.append(_semantics(row, case_id))
        revision = 0
        for n, correction in enumerate(episode["corrections"], start=1):
            entry = result["corrections"][n - 1]
            try:
                _, _, recommendation_id = await probe(correction["case_id"])
                if recommendation_id is None:
                    raise WholeTaskRejected("correction_parent")
                feedback_id, event_digest, revision = await _correct(
                    on, recommendation_id, correction, revision, f"{episode['id']}-c{n}"
                )
            except WholeTaskRejected as exc:
                entry["status"] = "failed"
                result.update(status="failed", reason=str(exc))
                break
            entry.update(status="accepted", feedback_id=feedback_id, event_digest=event_digest)
        for ordinal, (kind, case_id) in enumerate(plan, start=1):
            row, _, _ = await probe(case_id, ordinal)
            result["probes"][ordinal - 1] = dict(
                case_id=case_id, kind=kind, off=off_rows[case_id], on=row
            )
        if result["status"] == "failed":
            return result
        await _reset(on, revision)
        after = []
        for case_id in episode["reset_probe_case_ids"]:
            row, _, _ = await probe(case_id)
            after.append(_semantics(row, case_id))
    baseline_digest, after_digest = reset_preimage_digest(baseline), reset_preimage_digest(after)
    payload = canonical_json(
        dict(schema_version=1, episode_id=episode["id"], baseline=baseline, after=after)
    ).encode("utf-8")
    evidence["reset:" + episode["id"]] = dict(
        id="reset:" + episode["id"],
        sha256=hashlib.sha256(payload).hexdigest(),
        bytes=len(payload),
        payload=payload,
    )
    result.update(
        status="completed",
        reset_baseline_digest=baseline_digest,
        reset_result_digest=after_digest,
        reset_equal=baseline_digest == after_digest,
    )
    return result


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
) -> tuple[dict, dict[str, dict]]:
    """Run every frozen episode and export ``(AssetAdaptationReportV1, reset evidence)``.

    The reset evidence maps ``reset:<episode_id>`` to ``{id, sha256, bytes,
    payload}`` holding the spec 0.64 document; it belongs in the companion
    evidence registry.
    """
    if qualification == "qualified":
        # Evaluation-only projection and registration are never qualified evidence.
        raise WholeTaskRejected("qualification")
    cases = {case["id"]: case for case in release.cases["cases"]}
    labels = release_labels(release)
    evidence: dict[str, dict] = {}
    results = []
    for episode in episodes["episodes"]:
        if interrupted():
            results.append(_not_run_episode(episode, labels, "interrupted"))
            continue
        results.append(await _run_episode(episode, cases, labels, open_arm, evidence))
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
