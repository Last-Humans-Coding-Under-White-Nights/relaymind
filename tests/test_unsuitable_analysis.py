"""The offline #170 analysis script (RM26; spec 0.48 section 14.4.7).

It replays the spec 0.48 rules with the gateway's own functions over recorded
run stores. Recorded handoff.started events have no budget field, so the
script counts a local_budget handoff that reached active_seconds as one that
ran out of local time, and reports how often it had to.
"""

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import analyze_unsuitable as analysis  # noqa: E402


def events(mode, end_kind, end_payload, *, local=True):
    started = [("model.started", "local", "2026-10-25T10:00:01.000000Z", {"purpose": "routing"})]
    return [
        (
            "run.accepted",
            "gateway",
            "2026-10-25T10:00:00.000000Z",
            {"effective_execution_mode": mode},
        ),
        *(started if local else []),
        (end_kind, "gateway", "2026-10-25T10:00:05.000000Z", end_payload),
    ]


def test_a_recorded_handoff_that_spent_the_stage_counts_by_proxy():
    recorded = events(
        "auto", "handoff.started", {"reason_code": "local_budget", "local_active_ms": 4003}
    )
    outcome, proxy = analysis.stage_of("run_a", recorded, 4.0)
    assert outcome.ran_out_of_time is True and proxy is True


def test_a_local_budget_handoff_short_of_the_stage_is_a_generation_limit():
    recorded = events(
        "auto", "handoff.started", {"reason_code": "local_budget", "local_active_ms": 2100}
    )
    outcome, proxy = analysis.stage_of("run_a", recorded, 4.0)
    assert outcome.ran_out_of_time is False and proxy is False


def test_a_named_budget_needs_no_proxy():
    recorded = events(
        "local_only",
        "run.failed",
        {"error_code": "needs_external", "budget": "active_seconds", "local_active_ms": 4002},
    )
    outcome, proxy = analysis.stage_of("run_a", recorded, 4.0)
    assert outcome.ran_out_of_time is True and proxy is False


def test_a_run_without_a_local_generation_has_no_stage():
    recorded = events(
        "external_only",
        "handoff.started",
        {"reason_code": "execution_mode", "local_active_ms": 0},
        local=False,
    )
    assert analysis.stage_of("run_a", recorded, 4.0) == (None, False)


def write_store(path: Path, runs) -> None:
    """A minimal recorded store: the runs, events, and warm-up samples the replay reads."""
    db = sqlite3.connect(path)
    db.executescript(
        "CREATE TABLE runs (run_id TEXT); CREATE TABLE events (run_id TEXT, seq INTEGER,"
        " event_type TEXT, actor TEXT, timestamp TEXT, payload TEXT);"
        " CREATE TABLE model_timings (sample_id INTEGER, purpose TEXT, kind TEXT,"
        " duration_ms INTEGER, succeeded INTEGER);"
        " INSERT INTO model_timings VALUES (1, 'routing', 'warm_up', 3315, 1),"
        " (2, 'skill', 'warm_up', 2281, 1);"
    )
    for run_id, recorded in runs:
        db.execute("INSERT INTO runs VALUES (?)", (run_id,))
        for seq, (kind, actor, timestamp, payload) in enumerate(recorded, 1):
            db.execute(
                "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, seq, kind, actor, timestamp, json.dumps(payload)),
            )
    db.commit()
    db.close()


def shifted(recorded, minute):
    return [(k, a, t.replace("10:00", f"10:{minute:02d}"), p) for k, a, t, p in recorded]


def test_the_replay_fires_after_three_stages_that_ran_out_of_time(tmp_path):
    spent = events(
        "local_only",
        "run.failed",
        {"error_code": "needs_external", "budget": "active_seconds", "local_active_ms": 4002},
    )
    for minute in range(4):
        case = tmp_path / "checkpoint.work" / f"case-{minute}"
        case.mkdir(parents=True)
        recorded = (
            shifted(spent, minute)
            if minute < 3
            else shifted(events("auto", "run.completed", {"local_active_ms": 900}), minute)
        )
        write_store(case / "runs.sqlite3", [(f"run_{minute}", recorded)])

    result = analysis.replay_attempt(
        tmp_path,
        {
            "budgets": {"active_seconds": 4.0},
            "placement": {"probe_after_seconds": 600, "warmup_seconds": 30},
        },
    )

    assert result["stages"] == 4 and result["ran_out_of_time"] == 3
    assert result["longest_streak"] == 3
    assert result["auto_runs"] == 1 and result["auto_runs_the_rule_classifies_unsuitable"] == 1
    # Each case's cold warm-up, 3315 + 2281 ms, is over the 4-second stage.
    assert result["warmup_classes"] == {"unsuitable": 4}


def test_the_report_renders_counts_and_rates_as_text():
    """The markdown report shows numbers a reader can read, never a Python repr."""
    evidence = ROOT / "docs" / "evidence" / "2026-10-27-unsuitable" / "analysis.json"
    text = analysis.render(json.loads(evidence.read_text()))

    assert "{'" not in text and "None" not in text
    assert "| normal | 8 | 0 | 2 | ready 1248 |" in text
    assert "success 45/52 (86.5%) (control) vs 43/52 (82.7%) (candidate)" in text
