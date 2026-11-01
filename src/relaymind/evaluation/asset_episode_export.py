"""Adaptation episode freeze, paired probes, reset evidence and offline grader.

Spec 0.60 section 12.6.5 (RM37, RM42, RM44; AC28, AC33). One episode runs from
empty feedback in a disposable scope: off probes use the frozen disabled
baseline, on probes follow exactly three chronological corrections. Rates keep
every planned probe in the denominator. Nothing here grants correction
authority; accepted corrections must carry the authentic product receipts.
"""

from collections.abc import Mapping, Sequence

from relaymind.canonical import canonical_digest, canonical_json
from relaymind.evaluation.asset_corpus_io import AssetCorpusError, parse_json
from relaymind.evaluation.asset_metrics import asset_rate, grade_asset_result
from relaymind.evaluation.asset_whole_task import (
    WholeTaskBlocked,
    WholeTaskRejected,
    validate_whole_task,
    verify_evidence,
)

ACCEPTANCE_EPISODES = 100
# Gates as exact fractions: paraphrases >= 9/10, control loss <= 1/50.
PARAPHRASE_TARGET = (9, 10)
CONTROL_LOSS_LIMIT = (1, 50)
PLANNED_OBSERVATIONS = 100_000
_RESET_FIELDS = {"case_id", "final_candidates", "final_decision"}
_DECISIONS = {"confident", "choice_required", "no_match", "empty_catalog"}
# A reset claim whose preimages were not supplied: uncredited, and blocking
# only when no structural contradiction or known gate failure precedes it.
PENDING = object()


def planned_observations(episode: Mapping) -> int:
    """Off/on probe pairs, before/after reset probes and three corrections."""
    probes = len(episode["paraphrase_case_ids"]) + len(episode["control_case_ids"])
    return 2 * probes + 2 * len(episode["reset_probe_case_ids"]) + 3


def _utf8(value: str) -> bytes:
    return value.encode("utf-8")


def verify_episode_freeze(freeze: Mapping) -> str:
    validate_whole_task("EpisodeFreezeV1", freeze)
    if freeze["off_config_digest"] == freeze["on_config_digest"]:
        # The named ablation varies adaptation enablement; equal configs cannot.
        raise WholeTaskRejected("ablation_identity")
    return canonical_digest(freeze)


def verify_episodes(episodes: Mapping, freeze: Mapping) -> str:
    """Check AssetEpisodesV1 against its freeze; return the input digest."""
    config_digest = verify_episode_freeze(freeze)
    validate_whole_task("AssetEpisodesV1", episodes)
    if episodes["config_digest"] != config_digest:
        raise WholeTaskRejected("config_binding")
    ids = [_utf8(e["id"]) for e in episodes["episodes"]]
    if any(a >= b for a, b in zip(ids, ids[1:], strict=False)):
        raise WholeTaskRejected("episode_order")
    if sum(planned_observations(e) for e in episodes["episodes"]) > PLANNED_OBSERVATIONS:
        raise WholeTaskRejected("observation_limit")
    for episode in episodes["episodes"]:
        corrections = [c["case_id"] for c in episode["corrections"]]
        if len(set(corrections)) != 3:
            raise WholeTaskRejected("correction_identity")
        probes = [
            *episode["paraphrase_case_ids"],
            *episode["control_case_ids"],
            *episode["reset_probe_case_ids"],
        ]
        if len(set(probes)) != len(probes) or set(probes) & set(corrections):
            raise WholeTaskRejected("probe_reuse")
    return canonical_digest(episodes)


def probe_plan(episode: Mapping) -> list[tuple[str, str]]:
    """Paired paraphrase/control probes in (kind, case_id) order."""
    pairs = [("paraphrase", c) for c in episode["paraphrase_case_ids"]]
    pairs += [("control", c) for c in episode["control_case_ids"]]
    return sorted(pairs, key=lambda p: (_utf8(p[0]), _utf8(p[1])))


def reset_preimage_digest(preimage: Sequence[Mapping]) -> str:
    """canonical_digest of the ordered closed reset semantic array."""
    return canonical_digest([dict(item) for item in preimage])


_RESET_DOCUMENT = {"schema_version", "episode_id", "baseline", "after"}


def reset_preimages(evidence: Mapping[str, Mapping]) -> dict[str, tuple]:
    """Spec 0.64 reset evidence: ``reset:<episode_id>`` registry entries.

    Each entry's original bytes are the closed JSON object
    ``{schema_version:1, episode_id, baseline, after}``. Returns the
    (baseline, after) preimages by episode ID; their content and digests are
    checked by the grader. A malformed document rejects.
    """
    found = {}
    for evidence_id, item in evidence.items():
        if not evidence_id.startswith("reset:"):
            continue
        if not isinstance(item, Mapping) or not isinstance(item.get("payload"), bytes):
            raise WholeTaskRejected("reset_evidence")
        try:
            document = parse_json(item["payload"])
        except (ValueError, UnicodeError, AssetCorpusError):
            raise WholeTaskRejected("reset_evidence") from None
        if item["payload"] != canonical_json(document).encode("utf-8"):
            # The spec 0.64 document is canonical JSON, byte for byte.
            raise WholeTaskRejected("reset_evidence")
        if (
            not isinstance(document, dict)
            or set(document) != _RESET_DOCUMENT
            or document["schema_version"] != 1
            or type(document["schema_version"]) is not int
            or document["episode_id"] != evidence_id.removeprefix("reset:")
            or not isinstance(document["baseline"], list)
            or not isinstance(document["after"], list)
        ):
            raise WholeTaskRejected("reset_evidence")
        found[document["episode_id"]] = (document["baseline"], document["after"])
    return found


def _check_reset_preimage(preimage: Sequence[Mapping], case_ids: Sequence[str]) -> None:
    """Complete product results: exact candidate records and a published decision."""
    if (
        not isinstance(preimage, (list, tuple))
        or any(not isinstance(item, Mapping) or set(item) != _RESET_FIELDS for item in preimage)
        or [item["case_id"] for item in preimage] != list(case_ids)
    ):
        raise WholeTaskRejected("reset_preimage")
    for item in preimage:
        candidates = item["final_candidates"]
        if (
            not isinstance(item["final_decision"], str)
            or item["final_decision"] not in _DECISIONS
            or not isinstance(candidates, list)
            or len(candidates) > 10
        ):
            raise WholeTaskRejected("reset_preimage")
        try:
            for candidate in candidates:
                validate_whole_task("CandidateV2", candidate)
        except WholeTaskRejected:
            raise WholeTaskRejected("reset_preimage") from None


def _top1(row: Mapping, label: Mapping) -> bool:
    """Correct only for an admitted ready result whose first final candidate is acceptable."""
    result = row["result"]
    if row["status"] != "ready" or result is None or not result["final_candidates"]:
        return False
    return result["final_candidates"][0]["asset_id"] in set(label["acceptable_asset_ids"])


def _check_probe_row(row: Mapping, case_id: str, ordinal: int, label: Mapping, offer=None) -> None:
    validate_whole_task("Row", row)
    if row["slot"] != dict(phase="measured", repetition=0, case_id=case_id, ordinal=ordinal):
        raise WholeTaskRejected("probe_slot")
    status, result = row["status"], row["result"]
    if (status == "ready") != (result is not None) or (status == "ready") != (
        row["reason"] is None
    ):
        raise WholeTaskRejected("probe_status")
    if status == "not_run" and (
        any(row[k] for k in ("attempts", "embeddings", "observed_costs"))
        or any(
            row[k] is not None
            for k in (
                "request_digest",
                "offered_projection_digest",
                "catalog_revision",
                "observed_ms",
            )
        )
        or row["resources"] != dict(cpu_ms=None, peak_bytes=None, coverage="unknown")
    ):
        raise WholeTaskRejected("not_run_work")
    if result is not None and result["catalog_revision"] != row["catalog_revision"]:
        raise WholeTaskRejected("probe_revision")
    if row["grade"] != grade_asset_result(label, result):
        raise WholeTaskRejected("probe_grade")
    identity = (row["request_digest"], row["offered_projection_digest"], row["catalog_revision"])
    if any(value is None for value in identity) and any(v is not None for v in identity):
        raise WholeTaskRejected("probe_offer")
    declared_inference = any(
        (cost["value"]["calls"] or 0) > 0
        or any(
            (cost["value"][k] or 0) > 0
            for k in ("input_tokens", "output_tokens", "cached_tokens", "reasoning_tokens")
        )
        for cost in row["observed_costs"]
    )
    dispatched = (
        status == "ready"
        or bool(row["attempts"])
        or bool(row["embeddings"])
        or declared_inference
        or None not in identity
    )
    if offer is not None and dispatched:
        # Any dispatched probe, ready or not, binds its exact offered request
        # and catalog projection; only a pre-dispatch failure may omit it.
        if None in identity or identity[:2] != offer(row["catalog_revision"]):
            raise WholeTaskRejected("probe_offer")


def _check_episode(
    result: Mapping,
    episode: Mapping,
    labels: Mapping[str, Mapping],
    preimages: Mapping[str, tuple] | None,
    offers=None,
) -> bool | None | object:
    """Verify one EpisodeResult; return the verified reset equality, None or PENDING."""
    status = result["status"]
    corrections = result["corrections"]
    if [(c["ordinal"], c["case_id"]) for c in corrections] != [
        (n, c["case_id"]) for n, c in enumerate(episode["corrections"], start=1)
    ]:
        raise WholeTaskRejected("correction_order")
    for correction in corrections:
        accepted = correction["status"] == "accepted"
        if accepted != (correction["feedback_id"] is not None) or accepted != (
            correction["event_digest"] is not None
        ):
            raise WholeTaskRejected("correction_receipt")
    if status == "completed" and any(c["status"] != "accepted" for c in corrections):
        raise WholeTaskRejected("correction_receipt")
    if status == "not_run" and any(c["status"] != "not_run" for c in corrections):
        raise WholeTaskRejected("correction_receipt")
    plan = probe_plan(episode)
    if [(p["kind"], p["case_id"]) for p in result["probes"]] != plan:
        raise WholeTaskRejected("probe_conservation")
    for ordinal, probe in enumerate(result["probes"], start=1):
        label = labels.get(probe["case_id"])
        if label is None:
            raise WholeTaskRejected("probe_label")
        for arm in ("off", "on"):
            offer = (
                None
                if offers is None
                else (lambda revision, c=probe["case_id"], a=arm: offers(c, a, revision))
            )
            _check_probe_row(probe[arm], probe["case_id"], ordinal, label, offer)
    if status == "not_run" and any(
        p[arm]["status"] != "not_run" for p in result["probes"] for arm in ("off", "on")
    ):
        raise WholeTaskRejected("not_run_work")
    baseline, after = result["reset_baseline_digest"], result["reset_result_digest"]
    equal = result["reset_equal"]
    if baseline is None or after is None:
        if equal is not None:
            raise WholeTaskRejected("reset_equality")
        return None
    # With both digests present, reset_equal is their exact equality, never null.
    if equal is not (baseline == after):
        raise WholeTaskRejected("reset_equality")
    preimage = None if preimages is None else preimages.get(result["episode_id"])
    if preimage is None:
        # A claimed reset needs its evidence preimages; never a pass. The
        # blocker is reported only after every other check (see below).
        return PENDING if equal is not None else None
    for digest, items in zip((baseline, after), preimage, strict=True):
        _check_reset_preimage(items, episode["reset_probe_case_ids"])
        if reset_preimage_digest(items) != digest:
            raise WholeTaskRejected("reset_preimage")
    return equal


def _verified(report, episodes, labels, preimages, offers=None) -> list[bool | None]:
    by_id = {e["id"]: e for e in episodes["episodes"]}
    if [r["episode_id"] for r in report["episodes"]] != list(by_id):
        raise WholeTaskRejected("episode_conservation")
    return [
        _check_episode(r, by_id[r["episode_id"]], labels, preimages, offers)
        for r in report["episodes"]
    ]


def episode_summary(
    report: Mapping,
    episodes: Mapping,
    labels: Mapping[str, Mapping],
    *,
    resets: Sequence[bool | None] | None = None,
) -> dict:
    """Recompute EpisodeSummary with full planned denominators."""
    paraphrase = [0, 0]
    off_control = [0, 0]
    on_control = [0, 0]
    for result in report["episodes"]:
        for probe in result["probes"]:
            label = labels.get(probe["case_id"])
            if label is None:
                raise WholeTaskRejected("probe_label")
            if probe["kind"] == "paraphrase":
                paraphrase[0] += _top1(probe["on"], label)
                paraphrase[1] += 1
            else:
                off_control[0] += _top1(probe["off"], label)
                on_control[0] += _top1(probe["on"], label)
                off_control[1] += 1
                on_control[1] += 1
    if resets is None:
        resets = [r["reset_equal"] for r in report["episodes"]]
    paraphrases = asset_rate(*paraphrase)
    off_rate, on_rate = asset_rate(*off_control), asset_rate(*on_control)
    controls = off_control[1]
    lost = off_control[0] - on_control[0]
    control_loss = lost / controls if controls else None
    reset_passed = sum(r is True for r in resets)
    reset_unknown = sum(r is None for r in resets)
    completed = sum(r["status"] == "completed" for r in report["episodes"])
    planned = len(report["episodes"])
    if report["qualification"] != "qualified":
        gate = "not_applicable"
    elif (
        # Exact integer comparisons: the 0.90 and 0.02 boundaries are allowed.
        (
            paraphrase[1]
            and paraphrase[0] * PARAPHRASE_TARGET[1] < PARAPHRASE_TARGET[0] * paraphrase[1]
        )
        or (controls and lost * CONTROL_LOSS_LIMIT[1] > CONTROL_LOSS_LIMIT[0] * controls)
        or any(r is False for r in resets)
    ):
        gate = "fail"
    else:
        # Unknown evidence, too few episodes and the absent qualified-review
        # path all block; only verified independent acceptance could pass.
        gate = "blocked"
    return dict(
        planned_episodes=planned,
        completed_episodes=completed,
        paraphrases=paraphrases,
        off_control_top1=off_rate,
        on_control_top1=on_rate,
        control_loss=control_loss,
        reset_passed=reset_passed,
        reset_unknown=reset_unknown,
        gate=gate,
    )


def grade_adaptation_report(
    report: Mapping,
    episodes: Mapping,
    freeze: Mapping,
    labels: Mapping[str, Mapping],
    *,
    reset_preimages: Mapping[str, tuple] | None = None,
    registry: Mapping[str, Mapping] | None = None,
    offers=None,
) -> dict:
    """Verify an AssetAdaptationReportV1 and return its recomputed summary.

    ``labels`` maps each probe case ID to its frozen ``{class,
    acceptable_asset_ids}``. ``reset_preimages`` maps an episode ID to its
    (baseline, after) reset arrays resolved from evidence. ``offers`` (see
    ``asset_whole_task_corpus.corpus_offers``) recomputes every dispatched
    probe's offer; library callers that omit it skip that binding, while the
    command line always supplies it. A claimed reset
    without its preimages is never credited: after all structural checks, a
    known gate failure is returned, otherwise the grade is blocked
    (``reset_evidence_unresolved``).
    """
    input_digest = verify_episodes(episodes, freeze)
    validate_whole_task("AssetAdaptationReportV1", report)
    # Every supplied reset document is checked, whether or not a reset is claimed.
    planned = {e["id"]: e for e in episodes["episodes"]}
    for episode_id, pair in (reset_preimages or {}).items():
        if episode_id not in planned or not isinstance(pair, (tuple, list)) or len(pair) != 2:
            raise WholeTaskRejected("reset_evidence")
        for items in pair:
            _check_reset_preimage(items, planned[episode_id]["reset_probe_case_ids"])
    if report["input_digest"] != input_digest:
        raise WholeTaskRejected("input_binding")
    if (report["off_config_digest"], report["on_config_digest"]) != (
        freeze["off_config_digest"],
        freeze["on_config_digest"],
    ):
        raise WholeTaskRejected("config_binding")
    checked = _verified(report, episodes, labels, reset_preimages, offers)
    verify_evidence(report["evidence"], set(), registry)
    pending = [value is PENDING for value in checked]
    # The stored summary must agree with the report's own rows and claims
    # (verified values where available): a disagreement is a contradiction.
    claimed = [
        result["reset_equal"] if wait else value
        for result, value, wait in zip(report["episodes"], checked, pending, strict=True)
    ]
    if canonical_json(report["summary"]) != canonical_json(
        episode_summary(report, episodes, labels, resets=claimed)
    ):
        raise WholeTaskRejected("summary_mismatch")
    # The returned summary credits only verified resets. Known violations
    # fail before a missing reset preimage blocks.
    verified = [None if wait else value for value, wait in zip(checked, pending, strict=True)]
    summary = episode_summary(report, episodes, labels, resets=verified)
    if any(pending) and summary["gate"] != "fail":
        raise WholeTaskBlocked("reset_evidence_unresolved")
    return summary
