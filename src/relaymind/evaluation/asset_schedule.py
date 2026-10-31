"""Preregistered SHA-256 slot order and row conservation (RM40/AC31)."""

from collections.abc import Iterable, Mapping, Sequence

from relaymind.canonical import canonical_digest

MAX_SLOTS = 100_000


def plan_asset_slots(
    case_ids: Iterable[str], *, seed: int, repetitions: int, warmups: int
) -> list[dict]:
    """Freeze all slots before dispatch; repetitions never create new cases."""
    cases = list(case_ids)
    if (
        type(seed) is not int
        or not 0 <= seed <= 2**32 - 1
        or type(repetitions) is not int
        or not 1 <= repetitions <= 100
        or type(warmups) is not int
        or not 0 <= warmups <= 100
        or any(not isinstance(c, str) or not c or len(c.encode("utf-8")) > 256 for c in cases)
        or len(set(cases)) != len(cases)
        or len(cases) * (repetitions + warmups) > MAX_SLOTS
    ):
        raise ValueError("invalid_slot_plan")
    slots = []
    for phase, count in [("warmup", warmups), ("measured", repetitions)]:
        for repetition in range(count):
            order = sorted(
                cases,
                key=lambda case_id: (
                    canonical_digest(
                        dict(seed=seed, phase=phase, repetition=repetition, case_id=case_id)
                    ),
                    case_id,
                ),
            )
            for case_id in order:
                slots.append(
                    dict(
                        phase=phase, repetition=repetition, case_id=case_id, ordinal=len(slots) + 1
                    )
                )
    return slots


def validate_asset_rows(slots: Sequence[Mapping], rows: Sequence[Mapping]) -> None:
    """Require exactly one ordered row for every planned slot."""
    if (
        len(slots) > MAX_SLOTS
        or len(rows) != len(slots)
        or any(row["slot"] != slot for slot, row in zip(slots, rows, strict=True))
    ):
        raise ValueError("row_conservation")
