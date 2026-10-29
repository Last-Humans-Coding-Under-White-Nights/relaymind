"""RM08, RM09, RM12, RM16, RM33, AC24: honest host MCP qualification grading."""

import copy
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def probe():
    spec = importlib.util.spec_from_file_location(
        "mcp_qualification", ROOT / "integrations/openclaw/test/host-mcp-qualification/qualify.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def roundtrip():
    return {
        "settled": True,
        "server": "alpha.one",
        "tool": "alpha-one__echo",
        "marker": "marker-alpha",
        "arguments": {"text": "sandbox"},
        "calls": [
            {
                "server": "alpha.one",
                "tool": "echo",
                "arguments": {"text": "sandbox"},
                "result": "marker-alpha",
            }
        ],
        "proposals": [{"name": "alpha-one__echo", "arguments": {"text": "sandbox"}}],
        "offered": ["alpha-one__echo"],
        "observations": [
            {
                "exposed_name": "alpha-one__echo",
                "source": "mcp",
                "mcp": {"serverName": "alpha.one", "toolName": "echo"},
            }
        ],
        "tool_results": ["marker-alpha"],
        "answer": "marker-alpha",
        "gateway_executions": 0,
    }


def test_roundtrip_requires_host_execution_result_proposal_and_same_instance_provenance(probe):
    """RM16: list/probe/host-ok alone cannot pass actual use."""
    record = roundtrip()
    assert probe.roundtrip_pass(record)
    for field in ("calls", "proposals", "observations", "offered", "tool_results"):
        changed = copy.deepcopy(record)
        changed[field] = []
        assert not probe.roundtrip_pass(changed), field
    for field, value in (("settled", False), ("answer", "wrong"), ("gateway_executions", 1)):
        assert not probe.roundtrip_pass({**record, field: value}), field


def test_same_name_call_cannot_be_attributed_to_another_server(probe):
    """RM09, RM33: equal original names require authoritative server provenance."""
    record = roundtrip()
    record["calls"][0]["server"] = "alpha?one"
    assert not probe.roundtrip_pass(record)
    record = roundtrip()
    record["observations"][0]["mcp"]["serverName"] = "alpha?one"
    assert not probe.roundtrip_pass(record)


def test_credential_export_fails_closed_before_writing(probe, tmp_path):
    """RM12: reject a configured env/header credential in any nested export field."""
    out = tmp_path / "report.json"
    for payload in ({"stderr": "header-credential"}, {"nested": [{"schema": "env-credential"}]}):
        with pytest.raises(ValueError, match="credential"):
            probe.write_export(out, payload, ["header-credential", "env-credential"])
        assert not out.exists()
    probe.write_export(out, {"safe": "public schema"}, ["header-credential", "env-credential"])
    assert out.exists()


def test_checked_in_host_capture_proves_two_real_servers_and_does_not_claim_currentness(probe):
    """RM16, RM33, AC24: qualify actual host use, not SDK source or an imported catalog."""
    import gzip
    import json

    path = ROOT / "docs/evidence/2026-10-29-openclaw-mcp-qualification/mcp-capture.json.gz"
    captured = json.loads(gzip.decompress(path.read_bytes()))
    used = [r for r in captured["exchanges"] if r["name"] in ("first_server", "second_server")]
    assert len(used) == 2
    assert all(probe.roundtrip_pass(r["roundtrip"]) for r in used)
    assert {r["roundtrip"]["server"] for r in used} == {"alpha.one", "alpha?one"}
    assert len({r["roundtrip"]["tool"] for r in used}) == 2
    observations = [t for s in captured["schema_captures"] for t in s["tools"]]
    assert any(t["source"] == "mcp" for t in observations)
    assert all(
        t["availability"] == "unknown" and t["host_revision"] is None and t["session"] is None
        for t in observations
    )
    assert captured["integration_decision"]["current_availability_revision"] == "unsupported"
    assert captured["gateway_executions"] == 0
    exchanges = {e["name"]: e for e in captured["exchanges"]}
    assert probe.roundtrip_pass(exchanges["changed_schema"]["roundtrip"])
    changed = exchanges["changed_schema"]["roundtrip"]
    schemas = [
        t
        for t in changed["observations"]
        if t.get("mcp", {}) and t["mcp"]["serverName"] == "alpha.one"
    ]
    assert any(t["schema"]["required"] == ["count"] for t in schemas)
    for name in ("disconnected", "policy_denied", "removed"):
        assert exchanges[name]["roundtrip"]["calls"] == []
        assert exchanges[name]["roundtrip"]["answer"] == "tool unavailable"
        assert not probe.roundtrip_pass(exchanges[name]["roundtrip"])
    disconnected = exchanges["disconnected"]["roundtrip"]
    assert "alpha-one__echo" in disconnected["offered"]
    for name in ("policy_denied", "removed"):
        assert "alpha-one-2__echo" not in exchanges[name]["roundtrip"]["offered"]
    access = [s for s in captured["schema_captures"] if s["hook"] == "before_prompt_build"]
    assert len(access) == 6
    assert all(s["host_context"]["sessionKey"] == "agent:main:mcp-qualification" for s in access)
    assert all(s["inventory"]["status"] == "refused" for s in access)


def test_double_answers_only_from_the_current_calls_returned_result(probe):
    """RM16: prior successful history must not fabricate success after disconnect or deny."""
    historical = [
        {"role": "tool", "content": "marker-alpha"},
        {"role": "user", "content": "new task"},
    ]
    assert probe.result_answer("marker-alpha", historical, issued=True) == "tool unavailable"
    assert (
        probe.result_answer(
            "marker-alpha", [{"role": "tool", "content": "transport closed"}], issued=True
        )
        == "tool unavailable"
    )
    assert (
        probe.result_answer(
            "marker-alpha", [{"role": "tool", "content": "marker-alpha"}], issued=False
        )
        == "tool unavailable"
    )
    assert (
        probe.result_answer(
            "marker-alpha", [{"role": "tool", "content": "marker-alpha"}], issued=True
        )
        == "marker-alpha"
    )


def test_offline_grader_checks_raw_history_model_result_trace_and_server_events():
    """RM16: changing raw evidence must fail even if a derived roundtrip summary says pass."""
    import gzip
    import json

    spec = importlib.util.spec_from_file_location(
        "mcp_capture_grade", ROOT / "integrations/openclaw/test/host-mcp-qualification/grade.py"
    )
    assert spec and spec.loader
    grader = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(grader)
    path = ROOT / "docs/evidence/2026-10-29-openclaw-mcp-qualification/mcp-capture.json.gz"
    captured = json.loads(gzip.decompress(path.read_bytes()))
    assert grader.grade_capture(captured)["host_roundtrips"]
    changed = copy.deepcopy(captured)
    final = next(
        m
        for m in reversed(changed["exchanges"][0]["history"]["messages"])
        if m["role"] == "assistant"
    )
    final["content"] = [{"type": "text", "text": "wrong final answer"}]
    assert not grader.grade_capture(changed)["host_roundtrips"]
    changed = copy.deepcopy(captured)
    changed["exchanges"][0]["model_requests"][-1]["messages"][-1]["content"] = "wrong tool result"
    assert not grader.grade_capture(changed)["host_roundtrips"]
    changed = copy.deepcopy(captured)
    proposal = next(
        e
        for r in changed["exchanges"][0]["runs"]
        for e in r["events"]
        if e["type"] == "tool.proposed"
    )
    proposal["payload"]["tool_name"] = "different_tool"
    assert not grader.grade_capture(changed)["host_roundtrips"]
    changed = copy.deepcopy(captured)
    changed["mcp_events"] = [e for e in changed["mcp_events"] if e["kind"] != "call"]
    assert not grader.grade_capture(changed)["host_roundtrips"]
