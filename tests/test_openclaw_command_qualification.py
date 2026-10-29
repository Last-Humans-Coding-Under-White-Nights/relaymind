"""RM08, RM12, RM16, RM19, RM33, AC24: qualification checks cannot invent evidence."""

import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "openclaw_command_qualification",
    Path(__file__).resolve().parents[1]
    / "integrations/openclaw/test/host-command-qualification/qualify.py",
)
assert spec and spec.loader
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def test_command_pass_needs_an_action_trusted_session_and_no_model_call():
    """RM16: host ok or text alone must not make direct input pass."""
    event = {
        "action": "choose",
        "choice": 2,
        "host": {
            "senderId": "cli",
            "sessionKey": "agent:main:qualification",
            "isAuthorizedSender": True,
        },
    }
    assert probe.command_pass([event], model_calls=0, expected_action="choose")
    assert not probe.command_pass([], model_calls=0, expected_action="choose")
    assert not probe.command_pass([event], model_calls=1, expected_action="choose")
    assert not probe.command_pass([{**event, "host": {}}], model_calls=0, expected_action="choose")
    assert not probe.command_pass([event, event], model_calls=0, expected_action="choose")


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


def test_capture_grader_requires_the_final_assistant_marker():
    """RM16, RM33: the actual run grader must reject a marker found only in tool history."""
    import gzip
    import json

    capture_path = (
        Path(__file__).resolve().parents[1]
        / "docs/evidence/2026-10-29-openclaw-command-qualification/command-capture.json.gz"
    )
    captured = json.loads(gzip.decompress(capture_path.read_bytes()))
    assert probe.grade_capture(captured)["native_skill_file_loaded_and_double_answered"]
    skill_exchange = next(x for x in captured["exchanges"] if x["name"] == "native_skill_use")
    history = json.loads(skill_exchange["history"]["stdout"])
    final_assistant = next(m for m in reversed(history["messages"]) if m["role"] == "assistant")
    final_assistant["content"] = [{"type": "text", "text": "wrong answer"}]
    skill_exchange["history"]["stdout"] = json.dumps(history)
    assert not probe.grade_capture(captured)["native_skill_file_loaded_and_double_answered"]
