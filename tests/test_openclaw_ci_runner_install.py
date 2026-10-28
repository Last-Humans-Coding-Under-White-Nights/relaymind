"""The #145 install check on GitHub-hosted runners (RM12, RM19, RM33; AC24).

GitHub-hosted runner functional check over doubles (not a user-machine
qualification): the ``openclaw-plugin`` CI job of run 40652444733 (PR #182)
ran ``scripts/openclaw_install_check.py`` with the pinned
``openclaw@2026.10.7`` against the release artifact it had built, on the
``ubuntu-latest``, ``macos-latest``, and ``windows-latest`` runners, with the
real gateway over model doubles. Its uploaded exports are kept (line ends as LF) in
``docs/evidence/2026-10-28-openclaw-install-check-github-runners/``. These
tests read them, so the platform rows of ``docs/EVIDENCE.md`` and its
section on these runs rest on them.

Section 12.5: a runner is not a user's machine. The macOS and Windows rows of
a user's machine stay ``not_run`` and are support gaps; each runner row
carries the runner's OS version from its export.

No host or model runs here; the exports are data.
"""

import hashlib
import json
import re
from pathlib import Path

import pytest

from helpers import load_script_module

ROOT = Path(__file__).resolve().parent.parent
RUN = ROOT / "docs" / "evidence" / "2026-10-28-openclaw-install-check-github-runners"
EVIDENCE = ROOT / "docs" / "EVIDENCE.md"
LABEL = "GitHub-hosted runner functional check over doubles (not a user-machine qualification)"
CI_RUN = "40652444733"
# The pull request's merge commit the runners checked out: PR #182's head
# b7021d5 merged onto its base at the time.
MERGE = "78fc94dd74811724a19b42a36ddfcd9382741a8f"
PR_HEAD = "b7021d51b4d7d500bc8cee9496cf8de8f6b4303e"

# runner -> the OS and machine its export records (Python's platform module)
RUNNERS = {
    "ubuntu-latest": ("Linux 6.17.0-1022-azure", "x86_64"),
    "macos-latest": ("Darwin 25.6.0", "arm64"),
    "windows-latest": ("Windows 2025Server", "AMD64"),
}

install_check = load_script_module("openclaw_install_check")

runners = pytest.mark.parametrize("runner", sorted(RUNNERS))


def export(runner):
    return json.loads((RUN / runner / "install-check.json").read_text("utf-8"))


def steps(runner):
    return {step["name"]: step for step in export(runner)["steps"]}


@runners
def test_the_export_is_the_runner_job_and_never_live_model_evidence(runner):
    data = export(runner)
    assert data["kind"] == "openclaw-install-check"
    assert data["evidence"].startswith(f"GitHub Actions {runner} runner, Node v24.22.1;")
    assert "not live model evidence" in data["evidence"]
    assert "not a supported-platform qualification" in data["evidence"]
    assert data["host_version"] == "OpenClaw 2026.10.7 (157d8c2)"
    versions = data["versions"]
    assert (versions["openclaw"], versions["@openclaw/ai"]) == ("2026.10.7", "2026.10.7")
    assert versions["node"] == "v24.22.1"
    assert (versions["os"], versions["machine"]) == RUNNERS[runner]
    assert versions["relaymind_revision"] == {"head": MERGE, "dirty": False}


@runners
def test_every_step_of_the_lifecycle_passed_on_the_pinned_host(runner):
    data = export(runner)
    assert (data["status"], data["passed"]) == ("completed", True)
    assert [s["name"] for s in data["steps"]] == list(install_check.STEPS)
    assert len(data["steps"]) == 15
    assert all(s["status"] == "passed" and s["exit_code"] == 0 for s in data["steps"])
    inspect = steps(runner)["inspect"]["check"]
    assert inspect["status"] == "loaded" and inspect["provider_ids"] == ["relaymind"]
    assert inspect["diagnostics"] == []
    found = steps(runner)["discover"]["check"]["relaymind_models"]
    assert found == ["relaymind/relaymind-client-tools"]


@runners
def test_the_artifact_is_the_release_build_the_job_uploaded(runner):
    data = export(runner)
    sums = (RUN / runner / "SHA256SUMS").read_text("utf-8").split()
    assert sums == [data["artifact"]["sha256"], data["artifact"]["filename"]]
    release = json.loads((RUN / runner / "release.json").read_text("utf-8"))
    assert release["sha256"] == data["artifact"]["sha256"]
    assert release["openclaw"]["peer"] == "2026.10.7"
    assert release["node"] == "v24.22.1"


@runners
def test_the_round_trips_went_through_the_gateway_and_the_host(runner):
    """AC24, RM08: the host ran its own read tool; the gateway executed nothing."""
    data = export(runner)
    text, tool = data["exchanges"]["text"], data["exchanges"]["tool"]
    assert text["passed"] and text["answer_in_host_output"]
    assert tool["passed"] and tool["result_reached_next_run"]
    assert tool["host_result_holds_the_file"] is True
    assert [p["tool_name"] for p in tool["proposed"]] == ["read"]
    assert (tool["proposed"][0]["proposal_id"], tool["replayed_call_id"]) == (
        "call_read_1",
        "callread1",
    )
    assert data["host_tool_executions_by_gateway"] == 0
    assert len(data["runs"]) == 3 and all(r["state"] == "completed" for r in data["runs"])


@runners
def test_uninstall_left_the_credential_until_the_guide_removal_cleared_it(runner):
    leftovers = export(runner)["leftovers"]
    assert leftovers["after_uninstall"] == dict.fromkeys(
        ("config_entry", "auth_profile", "credential_in_host_state"), True
    )
    assert leftovers["after_removal"] == dict.fromkeys(
        ("config_entry", "auth_profile", "credential_in_host_state"), False
    )


@runners
def test_the_host_state_was_isolated_with_a_closed_gateway_port(runner):
    data = export(runner)
    isolation = data["isolation"]
    assert all(isolation[k].startswith("<host-home>") for k in isolation if k != "gateway_port")
    port = str(isolation["gateway_port"])
    assert port != "18789"
    assert steps(runner)["isolate-gateway"]["argv"] == ["config", "set", "gateway.port", port]


@runners
def test_the_export_holds_no_token_and_no_machine_path(runner):
    """RM12: the token is never recorded; the runner's paths are placeholders."""
    raw = (RUN / runner / "install-check.json").read_text("utf-8")
    assert export(runner)["token"] == install_check.NOT_RECORDED
    for path in ("/home/", "/Users/", "\\\\Users\\\\", "runneradmin", "/tmp/claude", "~/"):
        assert path not in raw, path
    assert not re.search(r"[A-Za-z]:(\\\\|/)", raw.replace("http://", "").replace("https://", ""))
    assert "<repo>" in raw


def runner_section():
    """The EVIDENCE.md section that describes these exports."""
    text = EVIDENCE.read_text("utf-8")
    start = text.index("### OpenClaw install check on GitHub-hosted runners (#145)")
    ends = [i for i in (text.find("\n## ", start), text.find("\n### ", start + 1)) if i != -1]
    return text[start : min(ends, default=len(text))]


def test_evidence_states_the_label_the_run_and_each_runner():
    section = runner_section()
    assert LABEL in section
    assert "not live model evidence" in section
    assert CI_RUN in section and MERGE in section and PR_HEAD in section
    for runner, (os_version, machine) in RUNNERS.items():
        assert runner in section and os_version in section and machine in section
        digest = hashlib.sha256((RUN / runner / "install-check.json").read_bytes()).hexdigest()
        assert digest in section, runner
        assert export(runner)["artifact"]["sha256"] in section, runner
    for platform in ("macOS", "Windows"):
        assert re.search(rf"{platform}[^\n]*`not_run`", section), platform


def test_evidence_records_each_runner_row_next_to_the_user_machine_gaps():
    evidence = EVIDENCE.read_text("utf-8")
    table = evidence[evidence.index("## OpenClaw plugin platforms (#145)") :]
    table = table[: table.index("\n## ")] if "\n## " in table else table
    rows = [line for line in table.splitlines() if line.startswith("|")]
    for runner, (os_version, _) in RUNNERS.items():
        row = next(r for r in rows if r.startswith(f"| GitHub Actions `{runner}`"))
        assert LABEL in row and os_version in row and "v24.22.1" in row, runner
        assert "15" in row and "2026-10-28-openclaw-install-check-github-runners" in row
    # A runner row never closes a user-machine row.
    for start in ("| macOS (target) | the same |", "| Windows native (target) |"):
        line = next(r for r in rows if r.startswith(start))
        assert "`not_run`" in line, start
    assert "result is on the pull request, not recorded here" not in table


def test_release_names_the_runner_exports_and_keeps_the_user_machine_gaps():
    release = (ROOT / "docs" / "RELEASE.md").read_text("utf-8")
    section = release[release.index("### OpenClaw provider plugin (#145") :]
    row = next(r for r in section.splitlines() if r.startswith("| GitHub Actions `ubuntu-latest`"))
    assert "2026-10-28-openclaw-install-check-github-runners" in row
    assert "not a user-machine qualification" in row
    assert "result is on the pull request, not recorded here" not in section
    gaps = section[section.index("Support gaps") :].split("\n\n")[0]
    assert "not a user-machine" in gaps
