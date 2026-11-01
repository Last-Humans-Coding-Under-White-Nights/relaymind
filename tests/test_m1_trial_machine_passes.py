"""Checks of the #259 early-trial evidence tooling (review of #276).

Spec 0.66 section 12.6.6 (RM40, RM44; AC31, AC36): both blind judges record a
verdict for every transcript, and every disagreement is adjudicated with a
recorded reason; a repeated machine pass never replaces earlier call bytes;
drafted prompts take their lane's language; the record step writes a record
the current checker loads. ``machine_passes.py`` and ``build_trial_inputs.py``
are operator evidence tooling under ``docs/evidence``; these tests drive their
pure parts with synthetic inputs and re-check the committed M1 evidence. No
model is called.
"""

import importlib.util
import json
from pathlib import Path

import pytest

TRIAL = Path(__file__).resolve().parents[1] / "docs/evidence/m10/early-trial-m1-20261101"


@pytest.fixture(scope="module")
def passes():
    spec = importlib.util.spec_from_file_location("m1_machine_passes", TRIAL / "machine_passes.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def v(blind_id, success, reason="r"):
    return dict(blind_id=blind_id, success=success, reason=reason)


IDS = ["aaaa", "bbbb"]


def test_agreeing_judges_need_no_adjudication(passes):
    finals, disagree = passes.check_judges(
        IDS,
        [v("aaaa", True), v("bbbb", False)],
        [
            v("bbbb", False),
            v("aaaa", True),
        ],
    )
    assert disagree == []
    assert {b: f["success"] for b, f in finals.items()} == {"aaaa": True, "bbbb": False}


def test_a_disagreement_takes_the_adjudicated_verdict(passes):
    a = [v("aaaa", True), v("bbbb", True)]
    b = [v("aaaa", True), v("bbbb", False)]
    finals, disagree = passes.check_judges(IDS, a, b)
    assert disagree == ["bbbb"]
    finals = passes.adjudicated(finals, disagree, [v("bbbb", False, "no final answer")])
    assert finals["bbbb"] == v("bbbb", False, "no final answer")
    assert finals["aaaa"]["success"] is True


@pytest.mark.parametrize(
    "verdicts",
    [
        [],  # the adjudicator omitted the disagreement
        [v("bbbb", False, "")],
        [v("bbbb", False, "  \n")],
        [v("bbbb", "false", "no final answer")],
        [v("bbbb", 0, "no final answer")],
        [v("bbbb", None, "no final answer")],
        [v("bbbb", False, "x"), v("bbbb", False, "x")],
        [v("bbbb", False, "x"), v("aaaa", True, "x")],  # not a disagreement
        [v("cccc", False, "x")],
        [dict(blind_id="bbbb", success=False)],
        ["bbbb"],
    ],
)
def test_every_disagreement_needs_one_strict_adjudication_with_a_reason(passes, verdicts):
    finals, disagree = passes.check_judges(
        IDS,
        [v("aaaa", True), v("bbbb", True)],
        [
            v("aaaa", True),
            v("bbbb", False),
        ],
    )
    with pytest.raises(SystemExit, match="success_adjudication"):
        passes.adjudicated(finals, disagree, verdicts)


@pytest.mark.parametrize(
    "judge",
    [
        [v("aaaa", True)],  # missing a blind ID
        [v("aaaa", True), v("bbbb", True), v("cccc", True)],  # unknown blind ID
        [v("aaaa", True), v("aaaa", False), v("bbbb", True)],  # duplicate
        [v("aaaa", "false"), v("bbbb", True)],  # string, not a boolean
        [v("aaaa", 1), v("bbbb", True)],
        [v("aaaa", None), v("bbbb", True)],
        [dict(blind_id="aaaa"), v("bbbb", True)],
        [v("aaaa", True, None), v("bbbb", True)],
        ["aaaa", v("bbbb", True)],
        {"aaaa": True},
    ],
)
@pytest.mark.parametrize("side", ["a", "b"])
def test_each_judge_covers_every_blind_id_once_with_a_boolean(passes, judge, side):
    good = [v("aaaa", True), v("bbbb", True)]
    a, b = (judge, good) if side == "a" else (good, judge)
    with pytest.raises(SystemExit, match="success_verdicts"):
        passes.check_judges(IDS, a, b)


def test_duplicate_packet_blind_ids_reject(passes):
    with pytest.raises(SystemExit, match="success_verdicts"):
        passes.check_judges(["aaaa", "aaaa"], [v("aaaa", True)], [v("aaaa", True)])


def test_a_rejected_adjudication_writes_no_observation(passes, tmp_path, monkeypatch):
    """Nothing is written and the blind map stays closed until every verdict checks."""
    private, observations, machine = tmp_path / "private", tmp_path / "obs", tmp_path / "m"
    for path in (private, observations, machine):
        path.mkdir()
    packet = dict(transcripts=[dict(blind_id=b, transcript={}) for b in IDS])
    (private / "blind-packet.json").write_text(json.dumps(packet))
    (tmp_path / "rubric.md").write_text("rubric")
    answers = {
        "judge_a": [v("aaaa", True), v("bbbb", True)],
        "judge_b": [v("aaaa", True), v("bbbb", False)],
        "success_adjudicator": [],
    }
    monkeypatch.setattr(passes, "MACHINE", machine)
    monkeypatch.setattr(
        passes, "call", lambda role, name, payload: dict(value=dict(verdicts=answers[role]))
    )
    args = passes.argparse.Namespace(
        private=private,
        observations=observations,
        rubric=tmp_path / "rubric.md",
        request_date="2026-11-01",
    )
    with pytest.raises(SystemExit, match="success_adjudication"):
        passes.judge(args)
    assert list(observations.iterdir()) == [] and list(machine.iterdir()) == []


def test_the_committed_m1_verdicts_pass_the_strict_check(passes):
    """The committed verdicts (judged by the as-run script at b5ef7a9) still hold."""
    rows = json.loads((TRIAL / "machine/success-verdicts.json").read_text())["rows"]
    ids = [r["blind_id"] for r in rows]
    finals, disagree = passes.check_judges(
        ids, [r["judge_a"] for r in rows], [r["judge_b"] for r in rows]
    )
    assert disagree == []
    assert {b: f for b, f in finals.items()} == {r["blind_id"]: r["final"] for r in rows}


def test_a_blind_map_for_other_transcripts_writes_no_observation(passes, tmp_path, monkeypatch):
    from relaymind.canonical import canonical_digest

    private, observations, machine = tmp_path / "private", tmp_path / "obs", tmp_path / "m"
    for path in (private, observations, machine):
        path.mkdir()
    packet = dict(transcripts=[dict(blind_id=b, transcript={}) for b in IDS])
    (private / "blind-packet.json").write_text(json.dumps(packet))
    mapping = dict(schema_version=1, seed=1, entries=[dict(blind_id="aaaa", ordinal=1, arm="x")])
    (private / "blind-map.json").write_text(json.dumps(mapping))
    digest = dict(schema_version=1, blind_map_sha256=canonical_digest(mapping))
    (machine / "blind-map.digest.json").write_text(json.dumps(digest))
    (tmp_path / "rubric.md").write_text("rubric")
    monkeypatch.setattr(passes, "MACHINE", machine)
    monkeypatch.setattr(
        passes,
        "call",
        lambda role, name, payload: dict(value=dict(verdicts=[v("aaaa", True), v("bbbb", True)])),
    )
    args = passes.argparse.Namespace(
        private=private,
        observations=observations,
        rubric=tmp_path / "rubric.md",
        request_date="2026-11-01",
    )
    with pytest.raises(SystemExit, match="success_verdicts"):
        passes.judge(args)
    assert list(observations.iterdir()) == []
    assert [p.name for p in machine.iterdir()] == ["blind-map.digest.json"]


# --- repeated passes keep earlier call bytes (spec 12.6.6) -------------------------


def test_a_repeated_pass_keeps_the_earlier_call_bytes(passes, tmp_path, monkeypatch):
    """A rejected draft is counted in the drafting evidence, never silently replaced."""
    monkeypatch.setattr(passes, "CALLS", tmp_path / "calls")
    passes.keep_call("drafting", b"req-1", b"resp-1")
    passes.keep_call("drafting", b"req-2", b"resp-2")
    passes.keep_call("drafting", b"req-3", b"resp-3")
    calls = tmp_path / "calls"
    assert {p.name: p.read_bytes() for p in calls.iterdir()} == {
        "drafting.request.json": b"req-3",
        "drafting.response.json": b"resp-3",
        "drafting.superseded-1.request.json": b"req-1",
        "drafting.superseded-1.response.json": b"resp-1",
        "drafting.superseded-2.request.json": b"req-2",
        "drafting.superseded-2.response.json": b"resp-2",
    }


@pytest.mark.parametrize(
    "lane,tags,mixed",
    [
        ("en_to_en", ["en"], False),
        ("en_to_zh_hans", ["en"], False),
        ("zh_hans_to_zh_hans", ["zh-Hans"], False),
        ("zh_hans_to_en", ["zh-Hans"], False),
        ("code_switch", ["en", "zh-Hans"], True),
    ],
)
def test_a_drafted_case_takes_its_lane_language(passes, lane, tags, mixed):
    """Spec 12.6.6: each prompt is written in its lane's language or mix."""
    assert passes.lane_language(lane) == (tags, mixed)


def test_drafted_prompts_never_fill_the_diagnostic_lane(passes):
    with pytest.raises(SystemExit):
        passes.lane_language("zh_hant_diagnostic")


# --- the record step of build_trial_inputs.py (spec 12.6.6) ------------------------


RECORD = TRIAL.parent / "m10-early-trial-m1-openclaw-2026.10.7-20261101.json"


@pytest.fixture(scope="module")
def builder():
    spec = importlib.util.spec_from_file_location(
        "m1_build_trial_inputs", TRIAL / "build_trial_inputs.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _bundle_doc(kind):
    manifest = json.loads((TRIAL / "bundle/inputs.json").read_text())
    path = next(d["path"] for d in manifest["documents"] if d["kind"] == kind)
    return json.loads((TRIAL / "bundle" / path).read_text())


def test_the_record_step_checks_the_committed_record_with_the_current_checker(builder):
    """The post-write check passes the catalog people to check_labels and loads."""
    checked = builder.check_written(
        RECORD, _bundle_doc("labels"), _bundle_doc("catalog"), _bundle_doc("cases")
    )
    assert checked.prompt_authorship == "machine_drafted_owner_approved"


def test_the_record_machine_carries_os_version(builder):
    """Spec 12.6.6 Machine: os_version is the Environment string, os_build the fuller build."""
    committed = json.loads(RECORD.read_text())["machine"]
    machine = builder._record_machine(_bundle_doc("environment_lock"), "26A434", "host")
    assert machine["hostname_sha256"] == builder._sha(b"host")
    assert {k: v for k, v in machine.items() if k != "hostname_sha256"} == {
        k: v for k, v in committed.items() if k != "hostname_sha256"
    }


def test_the_first_dispatch_orders_attempt_times_as_times(builder):
    def report(*times):
        return dict(rows=[dict(attempts=[dict(started_at=t, ended_at=None) for t in times])])

    # As text, "...02.500000Z" sorts before "...02Z"; as times it is later.
    assert (
        builder._first_dispatch(report("2026-11-01T09:46:02.500000Z", "2026-11-01T09:46:02Z"))
        == "2026-11-01T09:46:02Z"
    )
    assert builder._first_dispatch(dict(rows=[dict(attempts=[])])) is None
    committed = json.loads(RECORD.read_text())["runs_started_at"]
    assert builder._first_dispatch(json.loads((TRIAL / "bundle/report.json").read_text())) == (
        committed
    )
