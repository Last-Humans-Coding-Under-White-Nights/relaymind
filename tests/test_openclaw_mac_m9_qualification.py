"""The macOS qualification of the OpenClaw plugin at the M9 tip (RM33, AC24; section 12.5).

Issues #135 to #138 and #145. A target platform of section 1, the user's Apple
M1 Mac (macOS 27.0.1 arm64, OpenClaw 2026.10.7 from the package's
devDependency, Node v26.12.1), ran the plugin at one clean commit of the M9
tip:

- the install check of the release artifact over gateway doubles, with
  ``--target-platform`` (the #145 macOS row);
- the harness host scenarios of #137 (``text-tool``, ``tool-deny``,
  ``tool-approve``, ``exec-deny``, ``exec-full``) and of #138 (``abort``,
  ``external-failed``) over gateway doubles;
- one live run with the pinned llama-server b11785 and no external model: the
  packaged plugin's own resolved model against the real ``relaymind serve``,
  in auto and in local_only. This is the live AC24 turn through the plugin as
  packaged, which the 2026-10-27 run could only make with a config-row
  workaround (#135, #136).

These tests read the stored exports under
``docs/evidence/2026-10-29-openclaw-mac-qualification/``, so what
``docs/EVIDENCE.md``, ``docs/RELEASE.md``, ``docs/ROADMAP.md`` and the
operator guide state about macOS rests on them. The doubles runs are not live
model evidence. The live run is live evidence for the one thing it shows: at
the 8192 local context an OpenClaw turn ends needs_external / context_limit
before any model call, through the plugin as packaged. No host or model runs
here; the exports are data.
"""

import hashlib
import json
import re
from pathlib import Path

import pytest

from helpers import load_script_module

ROOT = Path(__file__).resolve().parent.parent
RUN = ROOT / "docs" / "evidence" / "2026-10-29-openclaw-mac-qualification"
EVIDENCE = ROOT / "docs" / "EVIDENCE.md"
RELEASE = ROOT / "docs" / "RELEASE.md"
ROADMAP = ROOT / "docs" / "ROADMAP.md"
TESTING = ROOT / "docs" / "TESTING.md"
CHANGELOG = ROOT / "CHANGELOG.md"
README = ROOT / "integrations" / "openclaw" / "README.md"
RUN_NAME = "2026-10-29-openclaw-mac-qualification"
SECTION = "## 2026-10-29 — OpenClaw plugin qualification on macOS at the M9 tip"

LABEL = "macOS 27.0.1 arm64, Apple M1 (user's machine), Node v26.12.1"
HOST_VERSION = "OpenClaw 2026.10.7 (157d8c2)"
NODE = "v26.12.1"
OS_MACHINE = ("Darwin 27.0.0", "arm64")
PINNED_BUILD = "b11785"
LOCAL_LIMIT = 8192
ROUND_TRIPS = ("text-tool", "tool-deny", "tool-approve", "exec-deny", "exec-full")
PASSING_ROUND_TRIPS = ("text-tool", "tool-deny", "exec-deny", "exec-full")
CANCEL_ERRORS = ("abort", "external-failed")
MODES = ("auto", "local_only")

install_check = load_script_module("openclaw_install_check")
qualification = load_script_module("openclaw_qualification")

# A gateway token is secrets.token_urlsafe(32): 43 base64url characters. Any
# run of 40 or more base64url characters in an export must have a known
# non-secret shape: a hex digest or commit, a run ID (also inside a chatcmpl-
# ID or a skill-stage ID), a W3C traceparent, or the plugin's Idempotency-Key
# payload digest.
LONG_RUN = re.compile(r"[A-Za-z0-9_-]{40,}")
KNOWN_SHAPES = (
    re.compile(r"[0-9a-f]{40,}"),
    re.compile(r"(chatcmpl-)?run_[0-9a-f]{32}(_gen\d+_[a-z_]+)?"),
    re.compile(r"00-[0-9a-f]{32}-[0-9a-f]{16}-0[0-9a-f]"),
    re.compile(r"relaymind-openclaw-v1-[0-9a-f]{64}(-retry\d+)?"),
    # llama-server's /props carries the model's chat template with its media marker.
    re.compile(r"__media_[A-Za-z0-9_]+"),
)
# The host records the tarball's npm integrity (its sha512, base64): not a secret.
INTEGRITY = re.compile(r"sha512-[A-Za-z0-9+/=]+")


def read(*parts):
    return json.loads(RUN.joinpath(*parts).read_text("utf-8"))


def install():
    return read("install-check", "install-check.json")


def scenario(group, name):
    return read(group, "exports", f"{name}.json")


def live():
    return read("live", "live-turn.json")


def every_export():
    yield "install-check", install()
    for name in ROUND_TRIPS:
        yield name, scenario("round-trips", name)
    for name in CANCEL_ERRORS:
        yield name, scenario("cancel-errors", name)
    yield "live", live()


def head():
    heads = {export["versions"]["relaymind_revision"]["head"] for _, export in every_export()}
    (one,) = heads
    return one


def evidence_section():
    text = EVIDENCE.read_text("utf-8")
    start = text.index(SECTION)
    end = text.find("\n## ", start + 1)
    return text[start : end if end != -1 else len(text)]


# --- one clean commit, the pinned host, the user's machine -----------------------------------


def test_every_export_is_from_one_clean_commit_on_the_pinned_host_on_this_mac():
    """Section 12.5: the platform, host, Node, and revision are recorded in every export."""
    assert install()["host_version"] == HOST_VERSION
    assert live()["host_version"] == HOST_VERSION
    for group in ("round-trips", "cancel-errors"):
        steps = {s["name"]: s for s in read(group, "steps.json")}
        assert steps["openclaw --version"]["stdout"].strip() == HOST_VERSION, group
        assert "ProductVersion:\t\t27.0.1" in steps["sw_vers"]["stdout"], group
    for name, export in every_export():
        versions = export["versions"]
        assert (versions["openclaw"], versions["@openclaw/ai"]) == ("2026.10.7", "2026.10.7"), name
        assert versions["node"] == NODE, name
        assert (versions["os"], versions["machine"]) == OS_MACHINE, name
        assert versions["relaymind_revision"]["dirty"] is False, name
        assert re.fullmatch(r"[0-9a-f]{40}", versions["relaymind_revision"]["head"]), name
    assert head()


# --- the install check: the #145 macOS row ---------------------------------------------------


def test_the_install_check_is_the_platform_qualification_over_doubles():
    """RM33, AC24, section 12.5: a target platform's run says so, and says it is
    not live model evidence."""
    data = install()
    assert data["kind"] == "openclaw-install-check"
    assert data["evidence"].startswith(LABEL + ";")
    assert data["platform_qualification"] is True
    assert "platform qualification" in data["evidence"]
    assert "not live model evidence" in data["evidence"]
    assert "not a supported-platform qualification" not in data["evidence"]


def test_every_install_step_passed_and_the_gateway_executed_no_host_tool():
    """RM08, RM33, AC24: install, enable, inspect, token, discovery, a text and a
    host ``read`` round trip, disable, uninstall, and removal on the pinned host."""
    data = install()
    assert (data["status"], data["passed"]) == ("completed", True)
    assert [s["name"] for s in data["steps"]] == list(install_check.STEPS)
    assert len(data["steps"]) == 15
    assert all(s["status"] == "passed" and s["exit_code"] == 0 for s in data["steps"])
    text, tool = data["exchanges"]["text"], data["exchanges"]["tool"]
    assert text["passed"] is True and text["gateway_answer"] == install_check.TEXT_ANSWER
    assert tool["passed"] is True and tool["gateway_answer"] == install_check.TOOL_ANSWER
    assert tool["replayed_call_id"] == qualification.host_replayed_call_id(
        install_check.TOOL_CALL_ID
    )
    assert tool["result_reached_next_run"] is True
    assert text["gateway_tool_executions"] == tool["gateway_tool_executions"] == 0
    assert data["host_tool_executions_by_gateway"] == 0
    assert all(run["state"] == "completed" for run in data["runs"])


def test_uninstall_left_the_credential_until_the_guide_removal_cleared_it():
    """RM12, RM19 (Q12 of the 2026-10-27 run): the config entry, the profile, and
    the token stay after ``plugins uninstall --force``; the guide's two removal
    steps clear all three."""
    leftovers = install()["leftovers"]
    assert leftovers["after_uninstall"] == {
        "config_entry": True,
        "auth_profile": True,
        "credential_in_host_state": True,
    }
    assert leftovers["after_removal"] == {
        "config_entry": False,
        "auth_profile": False,
        "credential_in_host_state": False,
    }


def test_the_artifact_is_the_release_build_next_to_the_export():
    """RM18: the artifact the host installed is the recorded release build."""
    artifact = install()["artifact"]
    sums = (RUN / "install-check" / "SHA256SUMS").read_text("utf-8")
    assert f"{artifact['sha256']}  {artifact['filename']}" in sums
    release = read("install-check", "release.json")
    assert release["filename"] == artifact["filename"]
    assert release["sha256"] == artifact["sha256"]
    assert live()["artifact"]["sha256"] == artifact["sha256"]


# --- the #137 round trips ---------------------------------------------------------------------


@pytest.mark.parametrize("name", PASSING_ROUND_TRIPS)
def test_the_round_trip_scenario_passed_on_the_real_host(name):
    """RM08, RM09, RM20, AC24: the pass is read from the sandbox and the trace."""
    data = scenario("round-trips", name)
    assert data["kind"] == "openclaw-host-qualification"
    assert data["scenario"] == name
    assert (data["status"], data["exit_code"]) == ("completed", 0)
    assert data["checks"]["passed"] is True
    assert data["checks"]["gateway_tool_executions"] == 0
    assert data["host_tool_executions_by_gateway"] == 0
    assert all(run["state"] == "completed" for run in data["runs"])


def test_a_headless_plugin_approval_fails_closed_on_macos_too():
    """RM11, RM33: headless ``agent exec`` has no approval route, so the write
    never happens, the host still says ok, and the checks fail it."""
    data = scenario("round-trips", "tool-approve")
    checks = data["checks"]
    assert checks["passed"] is False
    assert (checks["sandbox_lines"], checks["expected_sandbox_lines"]) == (0, 1)
    assert [p["tool_name"] for p in checks["proposed"]] == ["relaymind_test_write"]
    assert checks["host_report"]["ok"] is True
    assert "Plugin approval unavailable" in data["stderr"]


@pytest.mark.parametrize("name", ROUND_TRIPS)
def test_the_host_rewrote_every_call_id_it_replayed(name):
    """Spec 0.49 section 7.5: the call ID comes back without underscores, and the
    harness matched it by ``answers_call``, never by the exact string."""
    checks = scenario("round-trips", name)["checks"]
    issued = [p["proposal_id"] for p in checks["proposed"]]
    assert issued
    assert checks["replayed_call_ids"] == [qualification.host_replayed_call_id(i) for i in issued]
    assert all(r != i for r, i in zip(checks["replayed_call_ids"], issued, strict=True))


def test_the_test_host_tools_registered_with_contracts_tools():
    """Q9 of the 2026-10-27 run, fixed in #137: the host loads both test tools."""
    steps = {s["name"]: s for s in read("round-trips", "steps.json")}
    plugin = json.loads(steps["inspect host tools"]["stdout"])["plugin"]
    assert plugin["status"] == "loaded" and plugin["enabled"] is True
    assert sorted(plugin["toolNames"]) == ["relaymind_test_echo", "relaymind_test_write"]
    assert sorted(plugin["contracts"]["tools"]) == sorted(plugin["toolNames"])
    assert steps["install host tools without --accept-capabilities"]["exit_code"] == 1
    assert steps["uninstall host tools without --force"]["exit_code"] == 1
    assert steps["uninstall host tools --force"]["exit_code"] == 0


# --- the #138 abort and error mapping -----------------------------------------------------------


def test_sigint_mid_generation_cancelled_the_gateway_run():
    """RM14, AC12: the host exits 130 and the one run ends cancelled with no answer."""
    data = scenario("cancel-errors", "abort")
    assert (data["status"], data["exit_code"]) == ("interrupted", 130)
    checks = data["checks"]
    assert checks["passed"] is True
    assert checks["signal_sent"] is True and checks["adapter_cancel_requested"] is True
    assert checks["answer_deltas"] == 0
    (run_id,) = checks["cancelled_runs"]
    (run,) = data["runs"]
    assert run["run_id"] == run_id and run["state"] == "cancelled"
    types = [event["type"] for event in run["events"]]
    assert "run.cancelled" in types and "handoff.started" not in types


def test_external_failed_was_a_named_failure_of_every_run():
    """RM16, RM20, section 11: a 503 is a named failure, never an empty answer."""
    data = scenario("cancel-errors", "external-failed")
    assert (data["status"], data["exit_code"]) == ("failed", 1)
    checks = data["checks"]
    assert checks["passed"] is True
    assert checks["expected"] == {"code": "external_failed", "status": 503}
    assert data["runs"] and all(run["state"] == "failed" for run in data["runs"])
    assert len(checks["failed_runs"]) == len(data["runs"])
    assert all(f["error_code"] == "external_failed" for f in checks["failed_runs"])
    assert checks["host_message"] is True
    assert checks["attempt_runs_recorded"] is True
    assert len(checks["attempts"]) == len(data["runs"])
    assert checks["answer_deltas"] == 0 and checks["gateway_tool_executions"] == 0
    assert checks["host_report"]["ok"] is False


# --- the live turn through the packaged plugin ----------------------------------------------


def test_the_live_run_used_the_pinned_runtime_and_the_packaged_plugin():
    """RM18, RM33, AC24: llama-server b11785 at the 8192 context, the release
    artifact, the plugin's resolved model, no external model."""
    data = live()
    assert data["kind"] == "openclaw-live-turn"
    assert data["evidence"].startswith(LABEL + ";")
    assert "live local model" in data["evidence"] and "target platform" in data["evidence"]
    assert "no external model" in data["evidence"]
    assert data["runtime"]["build_info"].startswith(PINNED_BUILD)
    assert data["runtime"]["n_ctx"] == LOCAL_LIMIT
    assert data["token"] == install_check.NOT_RECORDED
    steps = {s["name"]: s for s in data["steps"]}
    plugin = json.loads(steps["inspect provider"]["stdout"])["plugin"]
    assert plugin["status"] == "loaded" and plugin["enabled"] is True
    assert plugin["providerIds"] == ["relaymind"] and plugin["toolNames"] == []
    assert "relaymind/relaymind-client-tools" in steps["models list --refresh"]["stdout"]
    assert [turn["mode"] for turn in data["turns"]] == list(MODES)
    assert data["gateway"]["serving_line"].startswith("Serving on http://127.0.0.1:")
    assert "Local warm-up finished" in data["gateway"]["warm_up_line"]


@pytest.mark.parametrize("mode", MODES)
def test_each_live_turn_ended_needs_external_context_limit_before_any_model_call(mode):
    """Section 7.5 and decision D1: at the 8192 local context the host's request
    does not fit, so the run ends needs_external / context_limit with 0 model
    calls, and the plugin records the attempt with the gateway's run ID."""
    (turn,) = [t for t in live()["turns"] if t["mode"] == mode]
    assert turn["prompt"] == "Say hello"
    assert turn["host_exit_code"] == 1
    records = turn["attempt_records"]
    assert records
    for record in records:
        assert (record["outcome"], record["status"], record["code"]) == (
            "failed",
            422,
            "needs_external",
        )
        assert re.fullmatch(r"run_[0-9a-f]{32}", record["runId"])
    assert {r["runId"] for r in records} == {run["run"]["run_id"] for run in turn["runs"]}
    for entry in turn["runs"]:
        view = entry["run"]
        assert view["requested_execution_mode"] == view["effective_execution_mode"] == mode
        assert view["state"] == "failed"
        assert (view["error_code"], view["reason_code"]) == ("needs_external", "context_limit")
        assert view["metrics"]["model_calls"] == 0
        assert view["metrics"]["external_calls"] == 0
        events = json.dumps(entry["events"])
        assert f"(tokenizer); the local limit is {LOCAL_LIMIT}" in events
        assert "answer.delta" not in events


# --- nothing secret, no machine path ----------------------------------------------------------


def test_no_export_holds_a_token_or_a_machine_path():
    """RM12: the token and the machine's paths never reach the record."""
    kinds = {".json", ".jsonl", ".out", ".toml", ".md"}
    files = sorted(p for p in RUN.rglob("*") if p.is_file() and p.suffix in kinds)
    assert files
    for path in files:
        raw = path.read_text("utf-8")
        for marker in ("/Users/", "/private/tmp", "/private/var", "~/", "/tmp/claude"):
            assert marker not in raw, (path.name, marker)
        for value in LONG_RUN.findall(INTEGRITY.sub("sha512-<integrity>", raw)):
            assert any(shape.fullmatch(value) for shape in KNOWN_SHAPES), (path.name, value[:12])
        # The host's runtime line names the machine (its computer name on macOS,
        # not the network hostname); every such field must be masked.
        hosts = set(re.findall(r"\| host=([^|\n\"]+?) \|", raw))
        assert hosts <= {"<host>"}, (path.name, hosts)
    for _, export in every_export():
        assert export["token"] == install_check.NOT_RECORDED


# --- what the record states -------------------------------------------------------------------


def test_evidence_records_the_run_its_commit_and_its_exports():
    section = evidence_section()
    assert LABEL in section
    assert head() in section
    digest = hashlib.sha256((RUN / "install-check" / "install-check.json").read_bytes()).hexdigest()
    assert digest in section
    for name in ROUND_TRIPS + CANCEL_ERRORS:
        assert f"`{name}`" in section, name
    assert "needs_external" in section and "context_limit" in section
    assert "tool-approve" in section and "Plugin approval unavailable" in section
    assert "not live model evidence" in section


def test_the_platform_rows_name_this_run_and_keep_windows_a_gap():
    """Section 12.5: the macOS row of this revision is recorded; Windows stays
    ``not_run`` and is never inferred from a runner."""
    evidence = EVIDENCE.read_text("utf-8")
    table = evidence[evidence.index("## OpenClaw plugin platforms (#145)") :]
    table = table[: table.index("\n## ")]
    rows = [line for line in table.splitlines() if line.startswith("|")]
    macos = [r for r in rows if r.startswith("| macOS (target) |") and RUN_NAME in r]
    assert len(macos) == 1 and "`not_run`" not in macos[0]
    assert head()[:7] in macos[0]
    stale = [r for r in rows if r.startswith("| macOS (target) |") and "`not_run`" in r]
    assert not [r for r in stale if "this revision" in r]
    for start in ("| Windows native (target) |", "| Windows WSL2 |"):
        row = next(r for r in rows if r.startswith(start))
        assert "`not_run`" in row, start
    release = RELEASE.read_text("utf-8")
    plugin = release[release.index("### OpenClaw provider plugin (#145") :]
    row = next(r for r in plugin.splitlines() if r.startswith("| macOS 27.0.1 arm64"))
    assert RUN_NAME in row and "`not_run`" not in row
    gaps = plugin[plugin.index("Support gaps") :].split("\n\n")[0]
    assert "Windows" in gaps and "native" in gaps
    assert not re.search(r"macOS[^\n]*`not_run`", gaps)


@pytest.mark.parametrize(
    "doc",
    [ROADMAP, TESTING, CHANGELOG, README],
    ids=["roadmap", "testing", "changelog", "readme"],
)
def test_the_other_records_cite_this_run(doc):
    assert RUN_NAME in doc.read_text("utf-8")
