"""The #137 round trips on the real pinned OpenClaw host, Linux VPS (RM08, RM12, RM19, RM33; AC24).

A Linux VPS functional check (not a supported-platform qualification):
``docs/evidence/2026-10-28-openclaw-vps-functional-137/`` ran the pinned
``openclaw@2026.10.7`` through the harness host scenarios against the real
gateway over model doubles. These tests read those exports, so the README
and the report rest on them:

- the test host tools registered (``contracts.tools``, Q9) and the default
  scenario passed in one ``agent exec`` (Q8);
- a headless plugin approval fails closed, so tool-deny passed and
  tool-approve failed with no line written, though the host said ok;
- the host's exec policy pair decided exec-deny and exec-full;
- the host replayed every call ID without underscores (Q6).

No host or model runs here; the exports are data.
"""

import json
import re
from pathlib import Path

from test_openclaw_vps_functional import KNOWN_SHAPES, LONG_RUN

ROOT = Path(__file__).resolve().parent.parent
RUN = ROOT / "docs" / "evidence" / "2026-10-28-openclaw-vps-functional-137"
README = ROOT / "integrations" / "openclaw" / "README.md"
LABEL = "Linux VPS functional check (not a supported-platform qualification)"
# npm pack's tarball integrity digest is base64, not a token.
NPM_INTEGRITY = re.compile(r"sha512-[A-Za-z0-9+/]+=*")


def export(name):
    return json.loads((RUN / "exports" / f"{name}.json").read_text("utf-8"))


def steps():
    return {s["name"]: s for s in json.loads((RUN / "steps.json").read_text("utf-8"))}


def test_the_report_is_labeled_and_names_the_host_and_commit():
    text = (RUN / "REPORT.md").read_text("utf-8")
    assert LABEL in text
    assert "OpenClaw 2026.10.7 (157d8c2)" in text
    assert "not_run" in text


def test_the_exports_hold_no_credential():
    """RM12: no token-shaped value in any exported file."""
    found = []
    for path in sorted(p for p in RUN.rglob("*") if p.is_file()):
        text = NPM_INTEGRITY.sub("", path.read_text("utf-8"))
        for value in LONG_RUN.findall(text):
            if not any(shape.fullmatch(value) for shape in KNOWN_SHAPES):
                found.append((path.name, value))
    assert found == []
    for name in ("text-tool", "tool-deny", "tool-approve", "exec-deny", "exec-full"):
        assert export(name)["token"] == "<not recorded>"


def test_the_runs_were_on_the_pinned_host_from_a_clean_commit():
    for name in ("text-tool", "tool-deny", "tool-approve", "exec-deny", "exec-full"):
        versions = export(name)["versions"]
        assert versions["openclaw"] == "2026.10.7"
        assert versions["relaymind_revision"]["dirty"] is False


def test_the_test_host_tools_registered_on_the_real_host():
    """Q9: with contracts.tools in the manifest the host loads both tools."""
    plugin = json.loads(steps()["inspect host tools"]["stdout"])["plugin"]
    assert plugin["status"] == "loaded"
    assert plugin["toolNames"] == ["relaymind_test_echo", "relaymind_test_write"]
    assert plugin["contracts"]["tools"] == plugin["toolNames"]


def test_the_default_scenario_passed_in_one_agent_exec():
    """Q8: one host turn proposes the echo call and gets the closing text."""
    e = export("text-tool")
    assert e["status"] == "completed"
    assert [p["tool_name"] for p in e["checks"]["proposed"]] == ["relaymind_test_echo"]
    assert e["checks"]["gateway_tool_executions"] == 0
    assert e["checks"]["passed"] is True
    assert json.loads(e["stdout"])["final"] == "The tool said: echo: lantern."


def test_a_headless_plugin_approval_fails_closed():
    """RM08: tool-deny passed and tool-approve failed; the host said ok for both."""
    blocked = "Plugin approval unavailable: non-interactive CLI runs"
    deny, approve = export("tool-deny"), export("tool-approve")
    for e in (deny, approve):
        assert e["checks"]["sandbox_lines"] == 0
        assert e["checks"]["host_report"]["ok"] is True
        assert e["checks"]["host_report"]["tool_failures"] == 1
        assert blocked in e["stderr"]
    assert deny["checks"]["passed"] is True
    assert approve["checks"]["passed"] is False
    assert approve["checks"]["expected_sandbox_lines"] == 1


def test_the_exec_policy_pair_decided_the_exec_scenarios():
    deny, full = export("exec-deny"), export("exec-full")
    assert (deny["checks"]["sandbox_lines"], deny["checks"]["passed"]) == (0, True)
    assert (full["checks"]["sandbox_lines"], full["checks"]["passed"]) == (1, True)
    assert "exec denied" in deny["stderr"]


def test_the_host_rewrote_every_call_id():
    """Q6, section 7.5: underscores stripped; matched through answers_call."""
    for name, issued in (
        ("text-tool", "call_echo_1"),
        ("tool-deny", "call_write_1"),
        ("tool-approve", "call_write_1"),
        ("exec-deny", "call_exec_1"),
        ("exec-full", "call_exec_1"),
    ):
        check = export(name)["checks"]
        assert [p["proposal_id"] for p in check["proposed"]] == [issued]
        assert check["replayed_call_ids"] == [issued.replace("_", "")]
        assert check["result_reached_next_run"] is True


def test_the_command_forms_behaved_as_the_readme_says():
    s = steps()
    assert s["install host tools without --accept-capabilities"]["exit_code"] == 1
    assert s["install host tools"]["exit_code"] == 0
    assert s["uninstall host tools without --force"]["exit_code"] == 1
    assert s["uninstall host tools --force"]["exit_code"] == 0
    readme = README.read_text("utf-8")
    assert "plugins install integrations/openclaw/test/host-tool-plugin --force --accept-capabilities" in readme
    assert "openclaw plugins uninstall relaymind-test-host-tools --force" in readme
    assert "tools.alsoAllow" in readme
    assert "tools.toolSearch false" in readme
    assert '"Echo lantern with the test echo tool"' in readme
