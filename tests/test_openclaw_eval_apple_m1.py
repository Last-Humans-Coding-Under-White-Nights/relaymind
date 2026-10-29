"""The M9 OpenClaw evaluation on the user's Apple M1 (RM16, RM25, RM26, RM33; AC24; #139).

Spec 0.50 section 12.5. One run of ``scripts/run_openclaw_eval.py`` over the frozen tasks of
``evaluation/openclaw_tasks.v1.json`` through the real pinned host in the
four arms (``direct``, ``relaymind_external_only``, ``relaymind_auto``,
``relaymind_no_local``), against the external model ``glm-5.3-flash`` and the
pinned local model, on macOS 27.0.1 arm64 with Node v26.12.1. The export is
``docs/evidence/2026-10-29-openclaw-eval-apple-m1/openclaw-eval.json`` and
its Markdown report sits next to it.

These tests read the export, so what ``docs/EVIDENCE.md``,
``docs/RELEASE.md``, and ``docs/ROADMAP.md`` state about the measurement rests
on it: exact denominators per arm and language, the host's ok never deciding
a success, 0 local turns reported with their reasons, Chinese apart, and no
credential in any file. No host or model runs here; the export is data.
"""

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
RUN_NAME = "2026-10-29-openclaw-eval-apple-m1"
RUN = ROOT / "docs" / "evidence" / RUN_NAME
EXPORT = RUN / "openclaw-eval.json"
REPORT = RUN / "openclaw-eval.md"
LOCK = ROOT / "evaluation" / "openclaw_tasks.v1.lock.json"
EVIDENCE = ROOT / "docs" / "EVIDENCE.md"
RELEASE = ROOT / "docs" / "RELEASE.md"
ROADMAP = ROOT / "docs" / "ROADMAP.md"
TESTING = ROOT / "docs" / "TESTING.md"
CHANGELOG = ROOT / "CHANGELOG.md"
README = ROOT / "integrations" / "openclaw" / "README.md"

ARMS = ("direct", "relaymind_external_only", "relaymind_auto", "relaymind_no_local")
RELAYMIND_ARMS = ARMS[1:]
LANGUAGES = ("en", "zh")
TASKS_PER_LANGUAGE = 6
REPEATS = 3
EXTERNAL_MODEL = "glm-5.3-flash"
EXTERNAL_HOST = "api.z.ai"
NODE = "v26.12.1"
PINNED_BUILD = "build 11785, commit 8baabd53b"
# The commit the run tested. The tree was dirty while it ran: two files unrelated
# to the run were being written (this test module, and the CI line that lists it).
TESTED_HEAD = "2751124e152e676150751afaac326d81ce1de580"
LOCAL_LIMIT = 8192

# Nothing credential-shaped may be in the record: a gateway token
# (secrets.token_urlsafe(32), 43 base64url characters) or the provider's key.
LONG_RUN = re.compile(r"[A-Za-z0-9_-]{40,}")
KNOWN_SHAPES = (
    re.compile(r"[0-9a-f]{40,}"),
    re.compile(r"(chatcmpl-)?run_[0-9a-f]{32}(_gen\d+_[a-z_]+)?"),
    re.compile(r"00-[0-9a-f]{32}-[0-9a-f]{16}-0[0-9a-f]"),
    re.compile(r"relaymind-openclaw-v1-[0-9a-f]{64}(-retry\d+)?"),
)


def export():
    return json.loads(EXPORT.read_text("utf-8"))


def metrics(arm, language):
    return export()["summary"]["arms"][arm]["languages"][language]


# --- identity -------------------------------------------------------------------------------


def test_the_export_is_one_complete_run_of_the_frozen_tasks():
    """Section 12.5: the frozen file, its digest, the repeats and the seed, all arms."""
    data = export()
    assert data["kind"] == "relaymind.openclaw_evaluation"
    assert data["specification"] == "0.50 section 12.5"
    assert not data["aborted"]
    lock = json.loads(LOCK.read_text("utf-8"))
    tasks = data["tasks"]
    assert tasks["file"] == "evaluation/openclaw_tasks.v1.json"
    assert tasks["sha256"] == lock["sha256"]
    assert tasks["full_set"] is True
    assert (tasks["repeats"], tasks["sampling_seed"]) == (REPEATS, 139)
    assert all(data["arms"][arm]["status"] == "ran" for arm in ARMS)


def test_the_identities_are_recorded_and_the_host_is_the_pinned_release():
    """RM18, RM33: machine, Node, plugin artifact, external and local model, revision."""
    data = export()
    assert data["node"]["version"] == NODE
    assert re.fullmatch(r"[0-9a-f]{64}", data["plugin_artifact"]["sha256"])
    backend = data["backend"]
    assert backend["backend"] == "live"
    assert backend["external"]["model"] == EXTERNAL_MODEL
    assert EXTERNAL_HOST in backend["external"]["base_url"]
    assert backend["external"]["context_limit"] == 128000
    assert backend["local"] is not None
    assert PINNED_BUILD in backend["runtime_banner"]
    machine = json.dumps(data["machine"])
    assert "arm64" in machine or "Apple" in machine
    revision = data["application"]
    assert revision["head"] == TESTED_HEAD
    # Recorded dirty: the record names the two files and that the run used neither.
    assert revision["dirty"] is True


# --- denominators ---------------------------------------------------------------------------


def test_every_arm_has_every_slot_graded_with_exact_denominators():
    """Section 12.5: exact counts over every attempt, Chinese apart."""
    data = export()
    per_arm = TASKS_PER_LANGUAGE * len(LANGUAGES) * REPEATS
    assert len(data["attempts"]) == len(data["grades"]) == per_arm * len(ARMS)
    for arm in ARMS:
        grades = [g for g in data["grades"] if g["arm"] == arm]
        assert len(grades) == per_arm, arm
        for language in LANGUAGES:
            m = metrics(arm, language)
            assert m["attempts"] == TASKS_PER_LANGUAGE * REPEATS, (arm, language)
            assert m["best_effort"] is (language == "zh"), (arm, language)
            assert 0 <= m["successes"] <= m["attempts"], (arm, language)
            assert (
                m["successes"]
                + len([g for g in grades if g["language"] == language and not g["success"]])
                == m["attempts"]
            ), (arm, language)


def test_the_host_s_ok_never_decided_a_success():
    """Spec 0.50: a success needs the evidence, the tools, the sandbox, and the trace."""
    for grade in export()["grades"]:
        if grade["success"]:
            assert grade["failures"] == [] and grade["host_exit_code"] == 0
            assert not grade["timed_out"] and not grade["host_interrupted"]
            assert not grade["host_failover"]
        if grade["host_ok"] and not grade["success"]:
            assert grade["host_ok_but_failed"] is True


def test_relaymind_arms_correlate_every_run_and_execute_no_host_tool():
    """RM08, RM33: the gateway proposes; the host executes; every run is named."""
    for grade in export()["grades"]:
        if grade["arm"] in RELAYMIND_ARMS:
            assert grade["gateway_tool_executions"] == 0
            assert grade["uncorrelated_runs"] == []
            assert grade["unavailable_run_ids"] == 0
        else:
            assert grade["gateway_runs"] is None and grade["local_turns"] is None


def test_local_turns_are_reported_with_the_reasons_the_runs_left_the_local_stage():
    """Spec 0.50 and decision D1: 0 local turns is a result, never left out."""
    for arm in ("relaymind_auto", "relaymind_external_only"):
        for language in LANGUAGES:
            m = metrics(arm, language)
            assert m["local_turns"] is not None and m["escalation_reasons"] is not None
            assert (
                m["local_turns"] + sum(m["escalation_reasons"].values())
                >= m["gateway_runs"] - (m["errors"] or 0)
                or m["local_turns"] == 0
            )


# --- nothing secret -------------------------------------------------------------------------


def test_no_file_holds_a_credential_or_a_machine_path():
    """RM12: neither the gateway token nor the provider's key reaches the record."""
    for path in sorted(p for p in RUN.rglob("*") if p.is_file()):
        raw = path.read_text("utf-8")
        assert "Bearer " not in raw, path.name
        assert "RELAYMIND_EXTERNAL_API_KEY=" not in raw, path.name
        for marker in ("/Users/", "/private/tmp", "/private/var", "~/", "/tmp/claude"):
            assert marker not in raw, (path.name, marker)
        for value in LONG_RUN.findall(raw):
            assert any(shape.fullmatch(value) for shape in KNOWN_SHAPES), (path.name, value[:12])


# --- what the record states -----------------------------------------------------------------


def test_evidence_fills_the_arm_rows_from_this_export():
    text = EVIDENCE.read_text("utf-8")
    section = text[text.index("## M9 OpenClaw evaluation (#139") :]
    section = section[: section.index("\n## ")] if "\n## " in section else section
    rows = [line for line in section.splitlines() if line.startswith("| ")]
    for arm in ARMS:
        row = next(r for r in rows if r.startswith(f"| {arm} |"))
        assert RUN_NAME in row and "`not_run`" not in row, arm
        en = metrics(arm, "en")
        assert f"{en['successes']} of {en['attempts']}" in row, arm
    assert export()["application"]["head"][:7] in section
    assert EXTERNAL_MODEL in section


@pytest.mark.parametrize(
    "doc",
    [RELEASE, ROADMAP, TESTING, CHANGELOG, README],
    ids=["release", "roadmap", "testing", "changelog", "readme"],
)
def test_the_other_records_cite_this_run(doc):
    assert RUN_NAME in doc.read_text("utf-8")
