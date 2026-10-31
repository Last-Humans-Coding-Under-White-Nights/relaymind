"""RM37/RM42 AC28/AC33: latest-three total votes, bounded moves and exact reset."""

from relaymind.canonical import canonical_json


def api():
    from relaymind.selection import asset_adaptation

    return asset_adaptation


def candidate(key):
    return dict(
        asset_id=key,
        content_hash=key * 64,
        score=1,
        evidence_refs=["/description"],
        score_kind="fixture",
        kind="skill",
        source_version=None,
    )


def correction(key, target, when="2026-10-30T00:00:00Z"):
    return dict(
        feedback_id=key,
        recorded_at=when,
        feedback=dict(kind="correction", asset_id=target, content_hash=target * 64),
    )


def apply(candidates, records, matches):
    return api().adapt_ranking(
        candidates,
        "confident",
        records,
        matches,
        identity={"name": "fixture", "version": "1"},
        feedback_revision=7,
    )


def test_latest_three_total_include_out_of_pool_and_do_not_replace_with_older_helpful():
    base = [candidate("a"), candidate("b")]
    records = [
        correction("1", "b"),
        correction("2", "x"),
        correction("3", "y"),
        correction("4", "z"),
    ]
    result = apply(base, records, {"1", "2", "3", "4"})
    assert list(result.candidates) == base and result.adaptation["changed"] is False
    assert result.decision == "confident"


def test_every_candidate_moves_at_most_two_and_fields_are_byte_identical():
    base = [candidate(x) for x in "abcde"]
    result = apply(
        base, [correction("1", "e"), correction("2", "e"), correction("3", "e")], {"1", "2", "3"}
    )
    assert [c["asset_id"] for c in result.candidates] == ["a", "b", "e", "c", "d"]
    assert result.decision == "confident"
    assert {c["asset_id"]: canonical_json(c) for c in result.candidates} == {
        c["asset_id"]: canonical_json(c) for c in base
    }


def test_changed_top_forces_choice_ties_are_stable_and_empty_history_resets():
    base = [candidate("a"), candidate("b"), candidate("c")]
    result = apply(base, [correction("1", "b")], {"1"})
    assert [c["asset_id"] for c in result.candidates] == ["b", "a", "c"]
    assert result.decision == "choice_required" and result.adaptation["changed"]
    tied = apply(base, [correction("1", "b"), correction("2", "a")], {"1", "2"})
    assert list(tied.candidates) == base and not tied.adaptation["changed"]
    reset = apply(base, [], set())
    assert canonical_json(list(reset.candidates)) == canonical_json(base)
    assert reset.decision == "confident"


def test_history_is_truncated_to_twenty_before_similarity_matches_and_ties_use_id():
    base = [candidate("a"), candidate("b")]
    records = [correction(f"{i:02}", "b") for i in range(21)]
    result = apply(base, list(reversed(records)), {"00"})
    assert list(result.candidates) == base
    result = apply(
        base,
        [correction("a", "b"), correction("b", "x"), correction("c", "y"), correction("d", "z")],
        {"a", "b", "c", "d"},
    )
    assert list(result.candidates) == base
