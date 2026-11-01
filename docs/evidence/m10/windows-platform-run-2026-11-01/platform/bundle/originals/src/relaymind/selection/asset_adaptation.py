"""Deterministic bounded equal correction voting, without insertion (RM37/RM42)."""

from collections import Counter
from dataclasses import dataclass
from datetime import datetime

from relaymind.schema_util import freeze_json


@dataclass(frozen=True)
class AdaptedRanking:
    candidates: tuple
    decision: str
    adaptation: object


def correction_order(record) -> tuple:
    return (
        datetime.fromisoformat(record["recorded_at"].replace("Z", "+00:00")),
        record["feedback_id"].encode("utf-8"),
    )


def adapt_ranking(
    base_candidates, base_decision, eligible_records, matches, *, identity, feedback_revision
) -> AdaptedRanking:
    """Consume one store-owned coherent eligible snapshot, never caller feedback.

    Authentication, exact current binding/retention and scope admission precede
    this finite kernel and are rechecked by its owner before ready publication.
    """
    if len(base_candidates) > 10 or base_decision not in (
        "confident",
        "choice_required",
        "no_match",
    ):
        raise ValueError("invalid_adaptation_base")
    records = [r for r in eligible_records if r["feedback"]["kind"] == "correction"]
    if len({r["feedback_id"] for r in records}) != len(records):
        raise ValueError("duplicate_feedback")
    records = sorted(records, key=correction_order)[-20:]
    selected = [r for r in records if r["feedback_id"] in matches][-3:]
    votes = Counter((r["feedback"]["asset_id"], r["feedback"]["content_hash"]) for r in selected)
    values = [freeze_json(c) for c in base_candidates]
    positions = {c["asset_id"]: i for i, c in enumerate(values)}
    if len(positions) != len(values):
        raise ValueError("duplicate_candidate")
    for _ in range(46):
        changed = False
        for i in range(len(values) - 1):
            left, right = values[i], values[i + 1]
            if (
                votes[(right["asset_id"], right["content_hash"])]
                > votes[(left["asset_id"], left["content_hash"])]
                and abs(i - positions[right["asset_id"]]) <= 2
                and abs(i + 1 - positions[left["asset_id"]]) <= 2
            ):
                values[i], values[i + 1] = right, left
                changed = True
        if not changed:
            break
    final_ids = [c["asset_id"] for c in values]
    base_ids = [c["asset_id"] for c in base_candidates]
    decision = "choice_required" if final_ids and final_ids[0] != base_ids[0] else base_decision
    adaptation = freeze_json(
        dict(
            identity=identity,
            feedback_revision=feedback_revision,
            changed=final_ids != base_ids or decision != base_decision,
        )
    )
    return AdaptedRanking(tuple(values), decision, adaptation)
