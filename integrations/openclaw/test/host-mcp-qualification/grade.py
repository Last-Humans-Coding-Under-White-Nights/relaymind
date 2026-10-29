"""Offline raw-capture validation (#190; RM16, RM33, AC24).

The runtime capture keeps its original source hashes. This offline grader has
its own identity in report.json; it never rewrites historical capture checks.
"""

import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "mcp_runtime_probe", Path(__file__).with_name("qualify.py")
)
assert spec and spec.loader
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


CHECKS = (
    "host_roundtrips",
    "changed_schema_roundtrip",
    "excluded_or_unavailable_not_passed",
    "raw_server_calls_and_traces_consistent",
)


def grade_capture(capture: dict) -> dict:
    """Re-read raw history, requests, traces, captures and server execution records.

    A malformed or truncated capture fails every check with a reason; it never
    raises, and a missing field never matches a declared value (RM16).
    """
    try:
        return _grade(capture)
    except (KeyError, TypeError, AttributeError, IndexError, ValueError) as error:
        return {
            **dict.fromkeys(CHECKS, False),
            "error": f"malformed capture: {type(error).__name__}: {error}",
        }


def _grade(capture: dict) -> dict:
    exchanges = capture["exchanges"]
    by_name = {exchange["name"]: exchange for exchange in exchanges}
    actual_calls = [
        {key: event.get(key) for key in ("server", "tool", "arguments", "result")}
        for event in capture["mcp_events"]
        if event.get("kind") == "call"
    ]
    declared_calls = [call for exchange in exchanges for call in exchange["roundtrip"]["calls"]]
    consistent = (
        actual_calls == declared_calls
        and [run for exchange in exchanges for run in exchange["runs"]] == capture["runs"]
    )
    graded = {}
    for exchange in exchanges:
        history = exchange["history"]
        final = next(
            (m for m in reversed(history.get("messages", [])) if m.get("role") == "assistant"),
            {},
        )
        start, end = exchange["capture_window"]
        requests = exchange["model_requests"]
        raw_events = [event for run in exchange["runs"] for event in run["events"]]
        observed = {
            **exchange["roundtrip"],
            "settled": history.get("sessionInfo", {}).get("hasActiveRun") is False
            and exchange["response"]["exit_code"] == 0,
            "answer": "".join(
                part.get("text", "")
                for part in final.get("content", [])
                if part.get("type") == "text"
            ),
            "tool_results": [
                request["messages"][-1]["content"]
                for request in requests
                if request["messages"] and request["messages"][-1]["role"] == "tool"
            ],
            "offered": [name for request in requests for name in request["tools"]],
            "proposals": [
                {
                    "name": (event.get("payload") or {}).get("tool_name"),
                    "arguments": (event.get("payload") or {}).get("arguments"),
                }
                for event in raw_events
                if event.get("type") == "tool.proposed"
            ],
            "observations": [
                tool
                for snapshot in capture["schema_captures"][start:end]
                for tool in snapshot["tools"]
            ],
            "gateway_executions": sum(event.get("type") == "tool.started" for event in raw_events),
        }
        graded[exchange["name"]] = consistent and probe.roundtrip_pass(observed)
    return {
        "host_roundtrips": all(graded.get(name) for name in ("first_server", "second_server")),
        "changed_schema_roundtrip": graded.get("changed_schema", False),
        "excluded_or_unavailable_not_passed": all(
            name in by_name and not graded.get(name)
            for name in ("disconnected", "policy_denied", "removed")
        ),
        "raw_server_calls_and_traces_consistent": consistent,
    }
