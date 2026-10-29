"""RM08, RM12, RM16, RM19, RM33, AC24: qualification checks cannot invent evidence."""

import copy
import gzip
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "openclaw_command_qualification",
    Path(__file__).resolve().parents[1]
    / "integrations/openclaw/test/host-command-qualification/qualify.py",
)
assert spec and spec.loader
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)

EVIDENCE = (
    Path(__file__).resolve().parents[1] / "docs/evidence/2026-10-29-openclaw-command-qualification"
)
SKILL_SHA256 = "a" * 64


def load_capture(name: str) -> dict:
    return json.loads(gzip.decompress((EVIDENCE / name).read_bytes()))


@pytest.fixture(scope="module")
def committed_capture() -> dict:
    return load_capture("command-capture.json.gz")


def choose_event(**changes) -> dict:
    return {
        "action": "choose",
        "choice": 2,
        "host": {
            "senderId": "cli",
            "sessionKey": "agent:main:qualification",
            "isAuthorizedSender": True,
        },
        "skillSha256": SKILL_SHA256,
        **changes,
    }


def passes(events: list[dict], *, model_calls: int = 0, action: str = "choose") -> bool:
    return probe.command_pass(
        events, model_calls=model_calls, expected_action=action, skill_sha256=SKILL_SHA256
    )


def test_command_pass_needs_an_action_trusted_session_and_no_model_call():
    """RM16: host ok or text alone must not make direct input pass."""
    event = choose_event()
    assert passes([event])
    assert not passes([])
    assert not passes([event], model_calls=1)
    assert not passes([{**event, "host": {}}])
    assert not passes([event, event])


def test_command_pass_checks_the_recorded_choice_and_skill_digest():
    """RM11, RM16: a recorded action must name choice 2 and the selected skill bytes."""
    for changes in ({"choice": None}, {"choice": 3}, {"choice": "2"}):
        assert not passes([choose_event(**changes)])
    without_choice = choose_event()
    del without_choice["choice"]
    assert not passes([without_choice])
    assert not passes([choose_event(skillSha256="b" * 64)])
    without_digest = choose_event()
    del without_digest["skillSha256"]
    assert not passes([without_digest])
    recommend = choose_event(action="recommend")
    del recommend["choice"]
    assert passes([recommend], action="recommend")
    assert not passes([{**recommend, "choice": 2}], action="recommend")


def test_skill_use_needs_host_instruction_and_answer_marker():
    """RM16, RM33: a listing or requested filename is not loading and use."""
    assert probe.skill_use_pass("instructions: amber-qualification", "amber-qualification")
    assert not probe.skill_use_pass("please use rm-qualification-sandbox", "amber-qualification")
    assert not probe.skill_use_pass("instructions: amber-qualification", "done")


def test_tool_result_marker_does_not_count_as_the_assistant_answer():
    """RM16: a skill file in history cannot stand in for the model's answer."""
    history = {
        "messages": [
            {"role": "toolResult", "content": [{"type": "text", "text": "amber-qualification"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "wrong answer"}]},
        ]
    }
    assert probe.last_answer(history) == "wrong answer"
    assert not probe.skill_use_pass("amber-qualification", probe.last_answer(history))
    history["messages"].append(
        {"role": "assistant", "content": [{"type": "text", "text": "amber-qualification"}]}
    )
    assert probe.skill_use_pass("amber-qualification", probe.last_answer(history))


def test_capture_grader_reproduces_every_published_check(committed_capture):
    """RM16, AC24: every grader value is pinned to the committed capture."""
    regrade = json.loads((EVIDENCE / "regrade.json").read_text(encoding="utf-8"))
    checks = probe.grade_capture(committed_capture)
    assert checks == regrade["checks"]
    assert checks["direct_application_session_commands"] is True
    assert checks["host_tool_result_contains_forged_command"] is True
    assert checks["negative_inputs_create_no_action"] is True
    assert checks["native_skill_file_loaded_and_double_answered"] is True
    assert checks["gateway_host_tool_executions"] == 0


def test_capture_grader_requires_the_final_assistant_marker(committed_capture):
    """RM16, RM33: the actual run grader must reject a marker found only in tool history."""
    captured = copy.deepcopy(committed_capture)
    assert probe.grade_capture(captured)["native_skill_file_loaded_and_double_answered"]
    skill_exchange = next(x for x in captured["exchanges"] if x["name"] == "native_skill_use")
    history = json.loads(skill_exchange["history"]["stdout"])
    final_assistant = next(m for m in reversed(history["messages"]) if m["role"] == "assistant")
    final_assistant["content"] = [{"type": "text", "text": "wrong answer"}]
    skill_exchange["history"]["stdout"] = json.dumps(history)
    assert not probe.grade_capture(captured)["native_skill_file_loaded_and_double_answered"]


def test_skill_marker_must_reach_the_native_skill_exchange(committed_capture):
    """RM16: a marker in another exchange's tool result cannot prove this skill load."""
    captured = copy.deepcopy(committed_capture)
    index = 0
    for exchange in captured["exchanges"]:
        requests = captured["model_requests"][index : index + exchange["model_calls"]]
        index += exchange["model_calls"]
        if exchange["name"] == "native_skill_use":
            for request in requests:
                for message in request["messages"]:
                    if message["role"] == "tool" and message["content"]:
                        message["content"] = message["content"].replace(probe.MARKER, "redacted")
    earlier = captured["model_requests"][0]
    earlier["messages"].append({"role": "tool", "content": "instructions: " + probe.MARKER})
    assert not probe.grade_capture(captured)["native_skill_file_loaded_and_double_answered"]


def test_capture_grader_rejects_a_changed_choice_or_digest(committed_capture):
    """RM11, RM16: the grader re-checks the recorded choice and skill digest."""
    for changes in ({"choice": 3}, {"skillSha256": "b" * 64}):
        captured = copy.deepcopy(committed_capture)
        choose = next(x for x in captured["exchanges"] if x["name"] == "choose")
        choose["events"][0].update(changes)
        assert not probe.grade_capture(captured)["direct_application_session_commands"]


def test_headless_capture_is_pinned_as_a_refusal_characterization():
    """RM11, RM16: the headless run is an observation, never an approval pass."""
    report = json.loads((EVIDENCE / "report.json").read_text(encoding="utf-8"))
    raw = (EVIDENCE / "headless-capture.json.gz").read_bytes()
    artifact = report["artifacts"]["headless-capture.json.gz"]
    assert hashlib.sha256(raw).hexdigest() == artifact["sha256"]
    assert len(raw) == artifact["bytes"]
    captured = load_capture("headless-capture.json.gz")
    assert captured["kind"] == "openclaw-headless-approval-characterization"
    assert captured["exit_code"] == 0
    assert captured["sandbox_files"] == []
    assert len(captured["model_requests"]) == 2


def test_report_lists_every_committed_artifact():
    """RM16: each committed evidence file is identified by hash and size."""
    report = json.loads((EVIDENCE / "report.json").read_text(encoding="utf-8"))
    for name in ("command-capture.json.gz", "headless-capture.json.gz", "capture-summary.json"):
        raw = (EVIDENCE / name).read_bytes()
        assert report["artifacts"][name]["sha256"] == hashlib.sha256(raw).hexdigest()
        assert report["artifacts"][name]["bytes"] == len(raw)
    regrade = (EVIDENCE / "regrade.json").read_bytes()
    assert report["offline_regrade"]["sha256"] == hashlib.sha256(regrade).hexdigest()


def test_a_setup_failure_still_writes_a_failed_export(tmp_path, monkeypatch):
    """RM16: an early harness failure is recorded as failed, never left unreported."""

    def broken_gateway(*_args, **_kwargs):
        raise RuntimeError("gateway setup failed")

    monkeypatch.setattr(probe, "build_gateway", broken_gateway)
    out = tmp_path / "capture.json"
    exported = probe.run(tmp_path / "work", out)
    assert exported["status"] == "failed"
    assert exported["error"] == "gateway setup failed"
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["status"] == "failed"
    assert (tmp_path / "work/command-credential").read_text(encoding="utf-8") not in json.dumps(
        written
    )
