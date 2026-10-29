"""The #138 abort and error mapping on the real pinned OpenClaw host, Linux VPS.

RM14, RM16, RM17, RM22, RM33; AC12, AC24; spec sections 7.4, 7.5, and 11.

A Linux VPS functional check (not a supported-platform qualification):
``docs/evidence/2026-10-28-openclaw-vps-functional-138/`` ran the pinned
``openclaw@2026.10.7`` through the harness's ``abort`` and
``external-failed`` host scenarios against the real gateway over model
doubles. These tests read those exports, so the README and the report rest
on them:

- SIGINT to ``agent exec`` mid-generation: the host exited 130 with no JSON
  report and the gateway's one run ended cancelled (``cancel_requested``),
  the adapter asked to cancel, no answer text (Q11 on macOS saw the same);
- 503 ``external_failed``: every run failed with that code; the plugin's
  named message and attempt records reached the host's log, the host's own
  final message was its generic one, and the host retried the same model 7
  times with a rewritten conversation, so each retry was a new key and run.

No host or model runs here; the exports are data.
"""

import json
import re
from pathlib import Path

from test_openclaw_vps_functional import KNOWN_SHAPES, LONG_RUN

ROOT = Path(__file__).resolve().parent.parent
RUN = ROOT / "docs" / "evidence" / "2026-10-28-openclaw-vps-functional-138"
README = ROOT / "integrations" / "openclaw" / "README.md"
LABEL = "Linux VPS functional check (not a supported-platform qualification)"
SCENARIOS = ("abort", "external-failed")
# npm pack's tarball integrity digest is base64, not a token.
NPM_INTEGRITY = re.compile(r"sha512-[A-Za-z0-9+/]+=*")
# The plugin's Idempotency-Key is a payload digest, not a token (section 7.5).
IDEMPOTENCY_KEY = re.compile(r"relaymind-openclaw-v1-[0-9a-f]{64}(-retry\d+)?")
ATTEMPT = re.compile(r"RelayMind inference attempt (\{.*\})")
GENERIC = "The AI service is having trouble. Please try again in a moment."


def export(name):
    return json.loads((RUN / "exports" / f"{name}.json").read_text("utf-8"))


def steps():
    return {s["name"]: s for s in json.loads((RUN / "steps.json").read_text("utf-8"))}


def accepted_messages(run):
    (event,) = [e for e in run["events"] if e["type"] == "run.accepted"]
    return event["payload"]["original_messages"]


def test_the_report_is_labeled_and_names_the_host_the_commit_and_q11():
    text = (RUN / "REPORT.md").read_text("utf-8")
    assert LABEL in text
    assert "OpenClaw 2026.10.7 (157d8c2)" in text
    assert "Q11" in text
    assert "not_run" in text
    head = export("abort")["versions"]["relaymind_revision"]["head"]
    assert head in text


def test_the_exports_hold_no_credential():
    """RM12: no token-shaped value in any exported file."""
    found = []
    for path in sorted(p for p in RUN.rglob("*") if p.is_file()):
        text = NPM_INTEGRITY.sub("", path.read_text("utf-8"))
        for value in LONG_RUN.findall(text):
            if not any(shape.fullmatch(value) for shape in (*KNOWN_SHAPES, IDEMPOTENCY_KEY)):
                found.append((path.name, value))
    assert found == []
    for name in SCENARIOS:
        assert export(name)["token"] == "<not recorded>"


def test_the_runs_were_on_the_pinned_host_from_one_clean_commit():
    heads = set()
    for name in SCENARIOS:
        versions = export(name)["versions"]
        assert versions["openclaw"] == "2026.10.7"
        assert versions["relaymind_revision"]["dirty"] is False
        heads.add(versions["relaymind_revision"]["head"])
    assert len(heads) == 1
    assert steps()["openclaw --version"]["stdout"].startswith("OpenClaw 2026.10.7 (157d8c2)")


def test_sigint_mid_generation_cancelled_the_gateway_run():
    """RM14, AC12, section 7.4: closing the host's request cancelled the run it owned."""
    e = export("abort")
    assert e["status"] == "interrupted"
    assert e["exit_code"] == 130
    assert e["stdout"].strip() == ""
    check = e["checks"]
    (run,) = e["runs"]
    assert run["state"] == "cancelled"
    (cancelled,) = [x for x in run["events"] if x["type"] == "run.cancelled"]
    assert cancelled["payload"]["reason"] == "cancel_requested"
    assert check["cancelled_runs"] == [run["run_id"]]
    assert check["signal_sent"] is True
    assert check["adapter_cancel_requested"] is True
    assert check["answer_deltas"] == 0
    assert check["passed"] is True
    # The generation had started on the local double: no external call.
    assert not [x for x in run["events"] if x["type"] == "handoff.started"]


def test_external_failed_was_a_named_failure_of_every_run():
    """Section 11: 503 external_failed, never an empty answer or another actor's text."""
    e = export("external-failed")
    check = e["checks"]
    assert e["exit_code"] == 1
    assert check["passed"] is True
    assert {r["state"] for r in e["runs"]} == {"failed"}
    assert {(f["error_code"], f["reason_code"]) for f in check["failed_runs"]} == {
        ("external_failed", "invalid_output")
    }
    assert check["answer_deltas"] == 0
    assert check["gateway_tool_executions"] == 0
    assert check["host_message"] is True
    assert check["named_runs"] and set(check["named_runs"]) <= {r["run_id"] for r in e["runs"]}


def test_the_host_retried_the_same_model_with_a_new_key_and_run_each_time():
    """RM17, RM22, section 7.5: 1 request and 7 same-model retries, 8 attempts,
    8 keys, 8 turns, 8 gateway runs; the plugin itself retried none."""
    e = export("external-failed")
    records = [json.loads(m) for m in ATTEMPT.findall(e["stderr"])]
    assert len(records) == len(e["runs"]) == 8
    assert {r["runId"] for r in records} == {r["run_id"] for r in e["runs"]}
    assert len({r["idempotencyKey"] for r in records}) == 8
    assert len({r["turn"] for r in records}) == 8
    assert {(r["status"], r["code"], r["outcome"]) for r in records} == {
        (503, "external_failed", "failed")
    }
    assert {r["runIdSource"] for r in records} == {"error_body"}
    assert not any(r["retried"] for r in records)
    assert {r["usage"] for r in records} == {"unknown"}
    assert "after 7/8 retries; stopping same-model retries" in e["stderr"]
    # The host rewrote the conversation on its retries: never one payload sent again.
    shapes = {json.dumps(accepted_messages(r), sort_keys=True) for r in e["runs"]}
    assert len(shapes) == 8
    assert any(
        "Earlier unanswered user message" in json.dumps(accepted_messages(r)) for r in e["runs"]
    )


def test_the_host_s_final_message_was_its_own_generic_one():
    """The plugin's named message reached the host's log; its final output did not."""
    e = export("external-failed")
    report = json.loads(e["stdout"])
    assert report["ok"] is False
    assert report["error"]["message"] == f"⚠️ {GENERIC}"
    assert "RelayMind external_failed (HTTP 503)" not in e["stdout"]
    assert e["stderr"].rstrip().endswith(GENERIC)
    assert "detail=RelayMind external_failed (HTTP 503): " in e["stderr"]


def test_the_readme_states_what_the_host_did():
    text = " ".join(README.read_text("utf-8").split())
    assert "docs/evidence/2026-10-28-openclaw-vps-functional-138/" in text
    assert "7 same-model retries" in text
    assert "Host interrupt (#138, operator, not run yet)" not in text
