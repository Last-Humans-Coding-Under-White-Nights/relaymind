"""The #136 provider package on the real pinned OpenClaw host, Linux VPS (RM12, RM19, RM33; #136).

A Linux VPS functional check (not a supported-platform qualification):
``docs/evidence/2026-10-28-openclaw-vps-functional-136/`` ran the pinned
``openclaw@2026.10.7`` against the real gateway over model doubles, through
a recording proxy. These tests read that export, so the README and the
design doc rest on it:

- the host found ``relaymind/relaymind-client-tools`` in ``agent exec`` with
  no catalog refresh (``resolveDynamicModel``; the "Unknown model" fix);
- the host honored the plugin-supplied model's switches on the wire (spec
  0.49, section 7.5): no ``stream_options`` and no other field section 7.1
  rejects;
- a stale token gives the 401 reconnect line and a re-pasted token recovers;
- uninstall leaves the config entry, the auth profile, and the token in the
  host state, and ``models auth logout`` plus ``config unset`` remove them.

No host or model runs here; the export is data.
"""

import json
import re
import secrets
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
RUN = ROOT / "docs" / "evidence" / "2026-10-28-openclaw-vps-functional-136"
README = ROOT / "integrations" / "openclaw" / "README.md"
LABEL = "Linux VPS functional check (not a supported-platform qualification)"
# Fields section 7.1 rejects or the plugin's switches turn off.
SWITCHED_OFF = (
    "stream_options",
    "max_tokens",
    "reasoning_effort",
    "store",
    "prompt_cache_key",
    "parallel_tool_calls",
    "response_format",
)


def load(name):
    return json.loads((RUN / name).read_text("utf-8"))


def steps():
    return {step["name"]: step for step in load("steps.json")}


def chat_requests():
    return [
        x["request"]
        for x in load("wire.json")
        if x["request"]["method"] == "POST" and x["request"]["path"] == "/v1/chat/completions"
    ]


def test_the_report_is_labeled_and_names_the_host_and_commit():
    text = (RUN / "REPORT.md").read_text("utf-8")
    assert LABEL in text
    assert "OpenClaw 2026.10.7 (157d8c2)" in text
    assert "not_run" in text


# A gateway token is secrets.token_urlsafe(32): 43 base64url characters. Any
# run of 40 or more base64url characters in the export must have one of the
# known non-secret shapes: a lowercase hex digest or commit, a run ID (also
# inside a chatcmpl- ID or a skill-stage ID), or a W3C traceparent.
LONG_RUN = re.compile(r"[A-Za-z0-9_-]{40,}")
KNOWN_SHAPES = (
    re.compile(r"[0-9a-f]{40,}"),
    re.compile(r"(chatcmpl-)?run_[0-9a-f]{32}(_gen\d+_[a-z_]+)?"),
    re.compile(r"00-[0-9a-f]{32}-[0-9a-f]{16}-0[0-9a-f]"),
)


def token_shaped_values(root: Path) -> list:
    found = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        for value in LONG_RUN.findall(path.read_text("utf-8")):
            if not any(shape.fullmatch(value) for shape in KNOWN_SHAPES):
                found.append((path.relative_to(root).as_posix(), value))
    return found


def test_the_export_holds_no_credential():
    """RM12: no exported file holds a token-shaped value, wherever it would sit."""
    for request in [x["request"] for x in load("wire.json")]:
        assert request["headers"].get("authorization") == "<redacted>"
    for step in load("steps.json"):
        if step["stdin"] is not None:
            assert step["stdin"] == "<the gateway token>"
    assert token_shaped_values(RUN) == []


def test_the_credential_scan_finds_a_planted_token(tmp_path):
    """RM12: the scan is not vacuous; a token left in any field is caught."""
    copy = tmp_path / "export"
    shutil.copytree(RUN, copy)
    token = secrets.token_urlsafe(32)
    runs = copy / "gateway-runs.json"
    runs.write_text(runs.read_text("utf-8") + "\n" + token + "\n", "utf-8")
    assert ("gateway-runs.json", token) in token_shaped_values(copy)


def test_agent_exec_found_the_plugin_model_before_any_catalog_refresh():
    """Q7: headless agent exec resolves the alias through resolveDynamicModel."""
    s = steps()
    assert json.loads(s["models list without --refresh"]["stdout"])["count"] == 0
    exec_step = s["agent exec text before any catalog refresh"]
    assert exec_step["exit_code"] == 0
    out = json.loads(exec_step["stdout"])
    assert (out["ok"], out["final"]) == (True, "Hello from RelayMind.")
    assert (out["provider"], out["model"]) == ("relaymind", "relaymind-client-tools")
    first, last = exec_step["wire_exchanges"]
    caused = [x["request"] for x in load("wire.json")[first:last]]
    assert [(r["method"], r["path"]) for r in caused] == [("POST", "/v1/chat/completions")]


def test_the_host_honored_the_plugin_model_switches_on_the_wire():
    """Spec 0.49, section 7.5: no stream_options on the plugin-supplied model."""
    requests = chat_requests()
    # Three answered requests and the one refused with the stale token.
    assert len(requests) == 4
    for request in requests:
        body = request["body"]
        for field in SWITCHED_OFF:
            assert field not in body, field
        assert set(body) <= {
            "model",
            "messages",
            "stream",
            "tools",
            "tool_choice",
            "max_completion_tokens",
        }
        assert body["stream"] is True
        assert {m["role"] for m in body["messages"]} <= {"system", "user", "assistant", "tool"}
        for tool in body["tools"]:
            assert "strict" not in tool["function_keys"]
        assert request["headers"]["x-relaymind-execution-mode"] == "auto"
    # With the copied output limit the host uses the plugin's maxTokensField.
    assert [r["body"].get("max_completion_tokens") for r in requests] == [None, 512, 512, 512]


def test_a_stale_token_gives_the_reconnect_line_and_a_new_token_recovers():
    s = steps()
    stale = s["models list --refresh with the stale token"]
    reconnect = "RelayMind rejected the stored application token (HTTP 401 unauthenticated)"
    assert reconnect in stale["stderr"]
    assert "paste-api-key --provider relaymind" in stale["stderr"]
    assert s["agent exec with the stale token"]["exit_code"] == 1
    assert "401" in s["agent exec with the stale token"]["stderr"]
    recovered = json.loads(s["agent exec after recovery"]["stdout"])
    assert (recovered["ok"], recovered["final"]) == (True, "Hello again after the restart.")


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("install without --accept-capabilities", 1),
        ("install", 0),
        ("enable", 0),
        ("config set executionMode parallel_demo", 1),
        ("uninstall without --force", 1),
        ("uninstall --force", 0),
        ("models auth logout", 0),
    ],
)
def test_the_command_forms_behaved_as_the_readme_says(name, expected):
    """Q4, Q5: the pinned host's command corrections."""
    assert steps()[name]["exit_code"] == expected


def test_the_host_schema_refuses_parallel_demo_before_plugin_code_runs():
    """Q5: the host validates the plugin's configSchema; the plugin's message never shows."""
    stderr = steps()["config set executionMode parallel_demo"]["stderr"]
    assert "Config validation failed" in stderr
    assert '(allowed: "auto", "local_only", "external_only")' in stderr
    assert "not a mode of relaymind-client-tools" not in stderr


def test_uninstall_leaves_the_credential_until_logout_removes_it():
    """Q12: the host keeps the config entry, the auth profile, and the token."""
    checks = load("state-checks.json")
    after = checks["after_uninstall"]
    assert after["plugins.entries.relaymind"] == {"enabled": False}
    assert after["auth.profiles"] == ["relaymind:manual"]
    assert after["files_holding_token"]
    cleaned = checks["after_logout_and_unset"]
    assert cleaned["plugins.entries.relaymind"] is None
    assert cleaned["auth.profiles"] == []
    assert cleaned["files_holding_token"] == []


def test_the_readme_gives_the_working_command_forms_and_the_removal_steps():
    """Q4, Q12: the operator steps match what the pinned host accepted."""
    text = README.read_text("utf-8")
    for form in (
        "--force --accept-capabilities",
        "openclaw plugins enable relaymind",
        "openclaw models list --refresh",
        "openclaw plugins uninstall relaymind --force",
        "openclaw models auth logout relaymind:manual",
        "openclaw config unset plugins.entries.relaymind",
        LABEL,
        "2026-10-28-openclaw-vps-functional-136",
    ):
        assert form in text, form
    assert "(operator, not run yet)" not in text
