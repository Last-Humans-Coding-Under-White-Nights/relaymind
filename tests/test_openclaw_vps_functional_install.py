"""The #145 install check on the real pinned OpenClaw host, Linux VPS (RM12, RM19, RM33; AC24).

A Linux VPS functional check (not a supported-platform qualification):
``docs/evidence/2026-10-28-openclaw-vps-functional-145/`` ran
``scripts/openclaw_install_check.py`` with the pinned ``openclaw@2026.10.7``
against the release artifact (packed before the check with
``npm run pack:release``) and the real gateway over model doubles: its 15
steps are the host version, the log and Gateway port isolation, install,
configure, enable, inspect, the token, discovery, a text and a host-tool
round trip, disable, uninstall, and the removal of what uninstall leaves. These
tests read that export, so the operator guide, ``docs/RELEASE.md``, and
``docs/EVIDENCE.md`` rest on it:

- the operator guide's command forms are the ones the host accepted (Q4 of
  the macOS qualification: ``--accept-capabilities``, ``plugins enable``,
  ``models list --refresh``, ``uninstall --force``);
- uninstall left the config entry, the auth profile, and the token, and the
  guide's removal steps cleared them (Q12);
- macOS and Windows stay ``not_run`` and are listed as support gaps.

No host or model runs here; the export is data.
"""

import hashlib
import json
import re
from pathlib import Path

from helpers import load_script_module

ROOT = Path(__file__).resolve().parent.parent
RUN = ROOT / "docs" / "evidence" / "2026-10-28-openclaw-vps-functional-145"
README = ROOT / "integrations" / "openclaw" / "README.md"
RELEASE = ROOT / "docs" / "RELEASE.md"
EVIDENCE = ROOT / "docs" / "EVIDENCE.md"
LABEL = "Linux VPS functional check (not a supported-platform qualification)"

install_check = load_script_module("openclaw_install_check")


def export():
    return json.loads((RUN / "install-check.json").read_text("utf-8"))


def steps():
    return {step["name"]: step for step in export()["steps"]}


def guide():
    """The README's operator guide section, up to the next top-level heading."""
    text = README.read_text("utf-8")
    start = text.index("## Operator guide (#145)")
    end = text.index("\n## ", start + 1)
    return text[start:end]


def test_the_export_is_labeled_and_never_live_model_evidence():
    data = export()
    assert data["kind"] == "openclaw-install-check"
    assert data["evidence"].startswith(LABEL)
    assert "not live model evidence" in data["evidence"]
    assert data["host_version"] == "OpenClaw 2026.10.7 (157d8c2)"
    versions = data["versions"]
    assert (versions["openclaw"], versions["node"]) == ("2026.10.7", "v24.22.1")
    assert versions["relaymind_revision"]["dirty"] is False
    assert re.fullmatch(r"[0-9a-f]{40}", versions["relaymind_revision"]["head"])


def test_every_step_of_the_lifecycle_passed_on_the_pinned_host():
    """The 15 recorded steps of ``install_check.STEPS``, version to removal.

    The artifact was packed before them (``npm run pack:release``); the pack is
    not a step of the export.
    """
    data = export()
    assert (data["status"], data["passed"]) == ("completed", True)
    assert [s["name"] for s in data["steps"]] == list(install_check.STEPS)
    assert all(s["status"] == "passed" and s["exit_code"] == 0 for s in data["steps"])
    inspect = steps()["inspect"]["check"]
    assert inspect["status"] == "loaded" and inspect["provider_ids"] == ["relaymind"]
    assert inspect["diagnostics"] == []
    found = steps()["discover"]["check"]["relaymind_models"]
    assert found == ["relaymind/relaymind-client-tools"]


def test_the_artifact_is_the_recorded_release_build():
    data = export()
    sums = (RUN / "SHA256SUMS").read_text("utf-8").split()
    assert sums == [data["artifact"]["sha256"], data["artifact"]["filename"]]
    release = json.loads((RUN / "release.json").read_text("utf-8"))
    assert release["sha256"] == data["artifact"]["sha256"]
    assert release["openclaw"]["peer"] == "2026.10.7"


def test_the_round_trips_went_through_the_gateway_and_the_host():
    """AC24, RM08: the host ran its own read tool; the gateway executed nothing."""
    data = export()
    text, tool = data["exchanges"]["text"], data["exchanges"]["tool"]
    assert text["passed"] and text["answer_in_host_output"]
    assert tool["passed"] and tool["result_reached_next_run"]
    assert tool["host_result_holds_the_file"] is True
    assert [p["tool_name"] for p in tool["proposed"]] == ["read"]
    # The host replays the gateway's call ID without underscores (Q6).
    assert (tool["proposed"][0]["proposal_id"], tool["replayed_call_id"]) == (
        "call_read_1",
        "callread1",
    )
    assert data["host_tool_executions_by_gateway"] == 0
    assert data["runs"] and all(r["state"] == "completed" for r in data["runs"])


def test_the_host_used_the_operator_guide_command_forms():
    """Q4: the pinned host's command corrections, as the guide gives them."""
    s = steps()
    assert {"--force", "--accept-capabilities"} <= set(s["install"]["argv"])
    assert s["enable"]["argv"] == ["plugins", "enable", "relaymind"]
    assert s["discover"]["argv"][:3] == ["models", "list", "--refresh"]
    assert s["uninstall"]["argv"] == ["plugins", "uninstall", "relaymind", "--force"]
    text = guide()
    for form in (
        "--force --accept-capabilities",
        "openclaw plugins enable relaymind",
        "openclaw models list --refresh",
        "openclaw plugins uninstall relaymind --force",
    ):
        assert form in text, form


def test_uninstall_left_the_credential_until_the_guide_removal_cleared_it():
    """Q12: the config entry, the auth profile, and the stored token, then none."""
    data = export()
    assert data["leftovers"]["after_uninstall"] == {
        "config_entry": True,
        "auth_profile": True,
        "credential_in_host_state": True,
    }
    assert data["leftovers"]["after_removal"] == {
        "config_entry": False,
        "auth_profile": False,
        "credential_in_host_state": False,
    }
    s = steps()
    assert s["remove-config"]["argv"] == ["config", "unset", "plugins.entries.relaymind"]
    assert s["remove-credential"]["argv"] == [
        "models",
        "auth",
        "logout",
        "relaymind:manual",
        "--yes",
    ]
    text = guide()
    for form in (
        "openclaw config unset plugins.entries.relaymind",
        "openclaw models auth logout relaymind:manual --yes",
        "relaymind:manual",
    ):
        assert form in text, form


def test_the_host_state_paths_were_isolated_and_the_closed_port_was_set_in_the_config():
    """Section 12.5: the host paths are in the temp host home; the closed Gateway
    port was set with ``config set gateway.port``, not the default 18789.

    The export records no environment, so this does not show what the host's
    environment held. That the check passes no ``OPENCLAW_*`` setting (no
    ``OPENCLAW_GATEWAY_PORT`` either) is the script's code, tested in
    ``tests/test_openclaw_install_check.py``.
    """
    data = export()
    isolation = data["isolation"]
    assert all(isolation[k].startswith("<host-home>") for k in isolation if k != "gateway_port")
    assert isolation["gateway_port"] != 18789
    port = str(isolation["gateway_port"])
    assert steps()["isolate-gateway"]["argv"] == ["config", "set", "gateway.port", port]


# Machine path forms that must not be left in the export. The host prints a
# path under the user's home as ``~/...``; the run's checkout under the home
# became ``<repo>`` (REPORT.md, "Redacted after the run"). These are fixed
# forms, not this machine's checkout path: the export was recorded elsewhere.
MACHINE_PATHS = ("/home/", "/Users/", "/root/", "/tmp/claude", "~/")


def machine_paths(text: str) -> list:
    return [form for form in MACHINE_PATHS if form in text]


def test_the_export_holds_no_token_and_no_machine_path():
    """RM12: the token is never recorded; paths are placeholders."""
    raw = (RUN / "install-check.json").read_text("utf-8")
    assert export()["token"] == install_check.NOT_RECORDED
    assert machine_paths(raw) == []
    # The checkout's place under the home was replaced by <repo> (PR #182 review).
    assert "<repo>/integrations/openclaw/node_modules/openclaw/skills/" in raw
    for name in ("REPORT.md", "release.json"):
        text = (RUN / name).read_text("utf-8")
        assert "/home/" not in text and "~/" not in text, name


def test_the_machine_path_check_finds_a_planted_home_relative_checkout(tmp_path):
    """RM12: the check is not vacuous; a home-relative checkout path is caught."""
    copy = tmp_path / "install-check.json"
    raw = (RUN / "install-check.json").read_text("utf-8")
    planted = raw.replace("<repo>/integrations/", "~/x/relaymind/integrations/", 1)
    assert planted != raw
    copy.write_text(planted, "utf-8")
    assert machine_paths(copy.read_text("utf-8")) == ["~/"]


def test_the_report_states_the_label_and_the_platform_gaps():
    report = (RUN / "REPORT.md").read_text("utf-8")
    assert LABEL in report
    assert "not live model evidence" in report
    assert export()["versions"]["relaymind_revision"]["head"] in report
    digest = hashlib.sha256((RUN / "install-check.json").read_bytes()).hexdigest()
    assert digest in report
    for platform in ("macOS", "Windows"):
        assert re.search(rf"{platform}[^\n]*`not_run`", report), platform


def test_release_and_evidence_list_macos_and_windows_as_support_gaps():
    """Section 12.5: no Mac or Windows access; those rows stay not_run."""
    release = RELEASE.read_text("utf-8")
    section = release[release.index("### OpenClaw provider plugin (#145") :]
    assert "2026-10-28-openclaw-vps-functional-145" in section
    gaps = section[section.index("Support gaps") :].split("\n\n")[0]
    assert "macOS" in gaps and "Windows" in gaps and "native" in gaps
    evidence = EVIDENCE.read_text("utf-8")
    table = evidence[evidence.index("## OpenClaw plugin platforms (#145)") :]
    assert "2026-10-28-openclaw-vps-functional-145" in table
    assert "Support gaps" in table
    for row in ("| macOS (target) | the same |", "| Windows native (target) |"):
        line = next(x for x in table.splitlines() if x.startswith(row))
        assert "`not_run`" in line, row
    # The native Windows note stays: the host documents native support.
    assert "native Windows" in table
