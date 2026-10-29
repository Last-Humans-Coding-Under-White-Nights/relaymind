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


def test_mcp_source_without_provenance_fails_without_crashing(probe):
    """RM16: a malformed observation claiming MCP without server metadata cannot pass."""
    record = roundtrip()
    record["observations"][0]["mcp"] = None
    assert not probe.roundtrip_pass(record)


def test_control_file_is_replaced_atomically(probe, tmp_path, monkeypatch):
    """RM16: the disposable server must never read a truncated control file."""
    import json
    import os

    path = tmp_path / "server-0.json"
    probe.write_json_atomic(path, {"revision": 1})
    replaced = []
    real_replace = os.replace
    monkeypatch.setattr(
        probe.os, "replace", lambda src, dst: (replaced.append((src, dst)), real_replace(src, dst))
    )
    probe.write_json_atomic(path, {"revision": 2})
    assert json.loads(path.read_text(encoding="utf-8")) == {"revision": 2}
    assert len(replaced) == 1 and Path(replaced[0][1]) == path
    assert Path(replaced[0][0]).parent == path.parent
    assert [p.name for p in tmp_path.iterdir()] == ["server-0.json"]


def test_host_startup_without_listener_times_out(probe):
    """RM16: a host that never listens is a startup failure, not a later RPC error."""
    import socket

    class Running:
        def poll(self):
            return None

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with pytest.raises(TimeoutError, match="did not start listening"):
        probe.wait_for_listener(Running(), port, timeout=0.5)

    class Exited:
        def poll(self):
            return 1

    with pytest.raises(RuntimeError, match="failed startup"):
        probe.wait_for_listener(Exited(), port, timeout=0.5)


def test_fixture_module_hashes_are_in_a_stable_order(probe, tmp_path):
    """RM16: the export's source hash order must not depend on the filesystem."""
    for name in ("server.mjs", "capture.mjs", "probe.mjs", "notes.txt"):
        (tmp_path / name).write_text("", encoding="utf-8")
    assert [p.name for p in probe.fixture_modules(tmp_path)] == [
        "capture.mjs",
        "probe.mjs",
        "server.mjs",
    ]


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
    assert any(t.get("schema", {}).get("required") == ["count"] for t in schemas)
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


def load_grader():
    spec = importlib.util.spec_from_file_location(
        "mcp_capture_grade", ROOT / "integrations/openclaw/test/host-mcp-qualification/grade.py"
    )
    assert spec and spec.loader
    grader = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(grader)
    return grader


def test_offline_grader_reports_a_malformed_capture_as_not_passed():
    """RM16: tampered or truncated raw evidence fails as a check, never as a crash."""
    import gzip
    import json

    grader = load_grader()
    path = ROOT / "docs/evidence/2026-10-29-openclaw-mcp-qualification/mcp-capture.json.gz"
    captured = json.loads(gzip.decompress(path.read_bytes()))
    checks = (
        "host_roundtrips",
        "changed_schema_roundtrip",
        "excluded_or_unavailable_not_passed",
        "raw_server_calls_and_traces_consistent",
    )
    changed = copy.deepcopy(captured)
    call = next(e for e in changed["mcp_events"] if e["kind"] == "call")
    del call["result"]
    graded = grader.grade_capture(changed)
    assert not graded["host_roundtrips"]
    assert not graded["raw_server_calls_and_traces_consistent"]
    changed = copy.deepcopy(captured)
    proposal = next(
        e
        for r in changed["exchanges"][0]["runs"]
        for e in r["events"]
        if e["type"] == "tool.proposed"
    )
    del proposal["payload"]["tool_name"]
    assert not grader.grade_capture(changed)["host_roundtrips"]
    for key in ("mcp_events", "exchanges", "schema_captures"):
        changed = copy.deepcopy(captured)
        del changed[key]
        graded = grader.grade_capture(changed)
        assert all(graded[check] is False for check in checks), key
        assert "malformed capture" in graded["error"]
