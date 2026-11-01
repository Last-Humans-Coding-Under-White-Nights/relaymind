"""Bilingual packaged walkthrough recorder for the installed OpenClaw host (#238).

RM39; AC30, AC35. The recorder drives the existing ordinary installed-host
qualification through the plugin's own Control UI labels in English and
Simplified Chinese, takes screenshots and video, and writes a manifest with
SHA-256 digests and evidence references. Steps the pinned host does not
qualify (loaded skill use, MCP use and its host approval) are recorded as
blocked with a fixed reason, never performed or faked. These fixture tests run
no browser and no host: the page is a local double.
"""

import contextlib
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RECORDER = ROOT / "integrations/openclaw/test/host-walkthrough/record.py"


def recorder():
    spec = importlib.util.spec_from_file_location("walkthrough_recorder", RECORDER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def rec():
    return recorder()


# --- labels -------------------------------------------------------------------


def test_labels_come_from_the_plugin_control_ui(rec):
    english, chinese = rec.control_ui_labels()
    assert set(english) == set(chinese) and len(english) >= 20
    assert (english["recommend"], chinese["recommend"]) == ("Recommend", "推荐")
    assert chinese["selected"].startswith("已为下一轮选择准确的本机版本")


def test_the_ordinary_harness_waits_for_the_plugin_selected_label(rec):
    english, _ = rec.control_ui_labels()
    assert rec.ORDINARY.SELECTED_LABEL == english["selected"]


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Recommend", "推荐"),
        ("Choose 1", "选择 1"),
        ("Correct to 2", "纠正为 2"),
        ("Task", "任务"),
        ("Delete feedback", "删除反馈"),
        ("RelayMind recommendations", "RelayMind 推荐"),
        ("Select native skill", "选择本机技能"),
        ("Select for next turn", "供下一轮使用"),
    ],
)
def test_names_translate_to_the_chinese_labels(rec, name, expected):
    assert rec.translate(name, "zh-CN") == expected
    assert rec.translate(name, "en-US") == name


def test_an_unknown_name_fails_rather_than_falling_back_to_english(rec):
    with pytest.raises(ValueError, match="walkthrough_label_unknown"):
        rec.translate("Something else", "zh-CN")


def test_navigation_label_is_the_plugin_label(rec):
    source = (ROOT / "integrations/openclaw/src/control-ui.ts").read_text(encoding="utf-8")
    for language in rec.LANGUAGES:
        assert rec.translate("RelayMind recommendations", language) in source


# --- browser context ------------------------------------------------------------


def test_context_defaults_add_locale_and_video_without_overriding(rec, tmp_path):
    options = rec.context_defaults({"ignore_https_errors": True}, "zh-CN", tmp_path)
    assert options == dict(
        ignore_https_errors=True,
        locale="zh-CN",
        record_video_dir=str(tmp_path),
        record_video_size=rec.VIDEO_SIZE,
    )
    assert rec.context_defaults({"locale": "en-US"}, "zh-CN", tmp_path)["locale"] == "en-US"


# --- the ordinary UI flow through a localized page double -----------------------


class FakeLocator:
    def __init__(self, page, kind, name):
        self.page, self.kind, self.name = page, kind, name

    def click(self, **_):
        self.page.actions.append(("click", self.name))

    def fill(self, value, **_):
        self.page.actions.append(("fill", self.name))

    def wait_for(self, **_):
        self.page.actions.append(("wait", self.name))

    def select_option(self, **_):
        self.page.actions.append(("select", self.name))


class FakePage:
    def __init__(self):
        self.actions, self.shots = [], []

    def get_by_role(self, role, name=None, exact=None):
        return FakeLocator(self, role, name)

    def get_by_label(self, text, exact=None):
        return FakeLocator(self, "label", text)

    def get_by_text(self, text, exact=None):
        return FakeLocator(self, "text", text)

    def wait_for_timeout(self, _):
        pass

    def once(self, event, handler):
        self.actions.append(("once", event))

    def screenshot(self, path, full_page):
        Path(path).write_bytes(b"png:" + str(len(self.shots)).encode())
        self.shots.append(path)


def state_after(actions):
    """Backend states the ordinary harness waits for, by the last click."""
    clicks = [name for kind, name in actions if kind == "click"]
    last = clicks[-1] if clicks else None
    view = dict(state="ready")
    records, audits = [], []
    if last in ("选择 1", "纠正为 1", "查看反馈", "删除反馈", "清除反馈"):
        view = dict(state="chosen")
    if last in ("纠正为 1", "查看反馈"):
        records = [dict(id="r")]
    if last == "删除反馈":
        audits = [dict(operation="delete")]
    if last == "清除反馈":
        audits = [dict(operation="delete"), dict(operation="reset")]
    return dict(views=[view], records=records, audits=audits)


def test_ordinary_ui_flow_runs_in_chinese_and_shoots_every_step(rec, tmp_path):
    page = FakePage()
    shooter = rec.Shooter(tmp_path)
    actions = []
    run_ui = rec.localized_run_ui("zh-CN", shooter)
    result = run_ui(page, {}, lambda: state_after(page.actions), actions.append)
    assert actions == ["open", "recommend", "choose", "correct", "inspect", "delete", "reset"]
    clicked = [name for kind, name in page.actions if kind == "click"]
    assert clicked == [
        "RelayMind 推荐",
        "推荐",
        "选择 1",
        "纠正为 1",
        "查看反馈",
        "删除反馈",
        "清除反馈",
    ]
    assert ("fill", "任务") in page.actions
    assert result["locale"] == "zh-CN"
    assert set(result["snapshots"]) == {
        "recommend",
        "choice",
        "correction",
        "inspect",
        "delete",
        "reset",
    }
    steps = {shot["step"] for shot in shooter.shots}
    assert steps == set(actions)
    for shot in shooter.shots:
        assert (tmp_path / shot["file"]).is_file()


# --- manifest -----------------------------------------------------------------


def language_dir(tmp_path, language, *, passed=True, steps=None, failure=None):
    base = tmp_path / "out" / language
    (base / "screens").mkdir(parents=True)
    (base / "video").mkdir()
    shots = []
    for index, step in enumerate(steps or rec_steps()):
        name = f"screens/{index:04d}-{step}.png"
        (base / name).write_bytes(f"{language}-{step}".encode())
        shots.append(dict(step=step, file=name, label=step))
    (base / "screens.json").write_text(json.dumps(shots), encoding="utf-8")
    (base / "video" / "page.webm").write_bytes(b"video-" + language.encode())
    capture = dict(
        status="passed" if passed else "failed",
        environment=dict(observed_host_version="2026.10.7"),
        ordinary_actions=dict(installation=dict(archive_sha256="b" * 64)),
    )
    if failure:
        capture.update(failure)
    (base / "capture.json").write_text(json.dumps(capture), encoding="utf-8")
    return base


def rec_steps():
    return ["open", "recommend", "choose", "correct", "inspect", "delete", "reset", "select_skill"]


def build(rec, tmp_path, outcomes, **checks):
    actions = checks.get("actions", True)
    skill = checks.get("skill", True)
    return rec.build_manifest(
        tmp_path / "out",
        {language: dict(exit_code=code) for language, code in outcomes.items()},
        predicates=dict(ordinary_actions=lambda c: actions, ordinary_skill=lambda c: skill),
    )


def test_manifest_records_both_languages_with_digests_and_blocked_steps(rec, tmp_path):
    for language in rec.LANGUAGES:
        language_dir(tmp_path, language)
    manifest = build(rec, tmp_path, {"en-US": 0, "zh-CN": 0})
    assert manifest["tier"] == "functional_fixture"
    assert [entry["language"] for entry in manifest["languages"]] == list(rec.LANGUAGES)
    evidence = {e["id"]: e for e in manifest["evidence"]}
    assert [e["id"] for e in manifest["evidence"]] == sorted(evidence)
    for entry in evidence.values():
        payload = (tmp_path / "out" / entry["path"]).read_bytes()
        assert entry["sha256"] == hashlib.sha256(payload).hexdigest()
        assert entry["bytes"] == len(payload)
    for language in manifest["languages"]:
        status = {s["name"]: s for s in language["steps"]}
        assert [s["name"] for s in language["steps"]] == list(rec.STEP_NAMES)
        for name in rec_steps():
            assert status[name]["status"] == "recorded"
            assert status[name]["evidence_ids"]
            assert set(status[name]["evidence_ids"]) <= set(evidence)
        for name, reason in rec.BLOCKED.items():
            assert status[name]["status"] == "blocked"
            assert status[name]["reason"] == reason["reason"]
            assert status[name]["evidence_ids"] == []
        assert language["host_version"] == "2026.10.7"
        assert language["videos"] and language["capture"] in evidence
    assert manifest["complete"] is True


def test_a_failed_ui_action_is_failed_and_later_steps_not_run(rec, tmp_path):
    language_dir(
        tmp_path,
        "en-US",
        passed=False,
        steps=["open", "recommend"],
        failure=dict(
            reason="ordinary_ui_timeout",
            ordinary_failure_step="ui_actions",
            ordinary_ui_action="choose",
        ),
    )
    manifest = build(rec, tmp_path, {"en-US": 2}, actions=False, skill=False)
    status = {s["name"]: s for s in manifest["languages"][0]["steps"]}
    # A screenshot alone never makes a step recorded: the harness predicate failed.
    assert status["open"]["status"] == "not_verified"
    assert status["choose"] == dict(
        name="choose", status="failed", reason="ordinary_ui_timeout", evidence_ids=[]
    )
    assert status["reset"]["status"] == "not_run"
    assert status["mcp_use"]["status"] == "blocked"
    assert manifest["complete"] is False


def test_skill_selection_needs_its_own_predicate(rec, tmp_path):
    language_dir(tmp_path, "zh-CN")
    manifest = build(rec, tmp_path, {"zh-CN": 0}, skill=False)
    status = {s["name"]: s for s in manifest["languages"][0]["steps"]}
    assert status["reset"]["status"] == "recorded"
    assert status["select_skill"]["status"] == "not_verified"
    assert manifest["complete"] is False


def test_a_recorded_step_needs_a_screenshot(rec, tmp_path):
    language_dir(tmp_path, "en-US", steps=["open", "recommend", "choose"])
    manifest = build(rec, tmp_path, {"en-US": 0})
    status = {s["name"]: s for s in manifest["languages"][0]["steps"]}
    assert status["inspect"]["status"] == "failed"
    assert status["inspect"]["reason"] == "screenshot_missing"


def test_blocked_steps_stay_blocked_whatever_the_capture_claims(rec, tmp_path):
    base = language_dir(tmp_path, "en-US")
    capture = json.loads((base / "capture.json").read_bytes())
    capture["checks"] = dict(ordinary_skill_loaded_use_observation=True, mcp_use=True)
    (base / "capture.json").write_text(json.dumps(capture), encoding="utf-8")
    manifest = build(rec, tmp_path, {"en-US": 0})
    status = {s["name"]: s["status"] for s in manifest["languages"][0]["steps"]}
    assert {status[name] for name in rec.BLOCKED} == {"blocked"}


def test_media_outside_the_output_is_refused(rec, tmp_path):
    base = language_dir(tmp_path, "en-US")
    (tmp_path / "outside.png").write_bytes(b"outside")
    shots = json.loads((base / "screens.json").read_bytes())
    shots[0]["file"] = "../../outside.png"
    (base / "screens.json").write_text(json.dumps(shots), encoding="utf-8")
    manifest = build(rec, tmp_path, {"en-US": 0})
    entry = manifest["languages"][0]
    # The unsafe reference is never followed; the manifest is still written.
    assert all("outside" not in e["path"] for e in manifest["evidence"])
    assert "screenshot_path" in entry["media_problems"]
    assert {s["name"]: s for s in entry["steps"]}["open"]["evidence_ids"] == []
    assert manifest["complete"] is False


# --- fonts for the isolated browser home ---------------------------------------------


def font_dir(tmp_path):
    fonts = tmp_path / "fonts"
    fonts.mkdir()
    (fonts / "NotoSansCJKsc-Regular.otf").write_bytes(b"otf-bytes")
    (fonts / "Extra.ttc").write_bytes(b"ttc-bytes")
    (fonts / "README.txt").write_bytes(b"not a font")
    (fonts / "Linked.ttf").symlink_to(fonts / "Extra.ttc")
    return fonts


class StepBase:
    """A stand-in for the ordinary composition: records its named steps (RM39)."""

    def __init__(self):
        self.steps, self.failed = [], []

    @contextlib.contextmanager
    def step(self, name):
        self.steps.append(name)
        try:
            yield
        except Exception:
            self.failed.append(name)
            raise


def test_fonts_reach_the_isolated_home_before_the_host_is_installed(rec, tmp_path, monkeypatch):
    """The harness gives the browser HOME=<work>/home; Chinese labels need a CJK font there.

    RM39; AC30. Fonts go to $XDG_DATA_HOME/fonts, the fontconfig user directory the
    person harness sets, not the deprecated ~/.fonts.
    """
    fonts, home = font_dir(tmp_path), tmp_path / "work" / "home"
    home.mkdir(parents=True)
    data = home / ".local/share"
    seen = {}

    class Base(StepBase):
        def prepare_host(self, host, environment, work, config_path):
            seen["fonts"] = sorted(p.name for p in (data / "fonts").iterdir())

    monkeypatch.setattr(rec.ORDINARY, "OrdinaryComposition", Base)
    composition = rec.walkthrough_composition("zh-CN", rec.Shooter(tmp_path / "media"), fonts)
    environment = dict(HOME=str(home), XDG_DATA_HOME=str(data))
    composition().prepare_host(["node"], environment, tmp_path / "work", None)
    assert seen["fonts"] == ["Extra.ttc", "NotoSansCJKsc-Regular.otf"]
    assert (data / "fonts" / "Extra.ttc").read_bytes() == b"ttc-bytes"
    assert not (home / ".fonts").exists()


def test_a_font_copy_failure_is_an_install_step_failure(rec, tmp_path, monkeypatch):
    """RM39: a font that cannot be installed fails the named install step, not unclassified."""
    fonts, home = font_dir(tmp_path), tmp_path / "home"
    home.mkdir()
    (home / ".local/share/fonts/Extra.ttc").mkdir(parents=True)  # a colliding directory
    reached = []

    class Base(StepBase):
        def prepare_host(self, host, environment, work, config_path):
            reached.append(True)

    monkeypatch.setattr(rec.ORDINARY, "OrdinaryComposition", Base)
    composition = rec.walkthrough_composition("zh-CN", rec.Shooter(tmp_path / "media"), fonts)()
    with pytest.raises(OSError):
        composition.prepare_host([], dict(HOME=str(home)), tmp_path, None)
    assert composition.failed == ["install"] and reached == []


def test_without_a_font_dir_the_host_preparation_is_unchanged(rec, tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()

    class Base(StepBase):
        def prepare_host(self, host, environment, work, config_path):
            return "installed"

    monkeypatch.setattr(rec.ORDINARY, "OrdinaryComposition", Base)
    composition = rec.walkthrough_composition("en-US", rec.Shooter(tmp_path / "media"))()
    assert composition.prepare_host([], dict(HOME=str(home)), tmp_path, None) == "installed"
    assert not (home / ".local").exists() and composition.steps == []


def test_a_font_dir_without_fonts_is_refused(rec, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ValueError, match="walkthrough_font_dir"):
        rec.font_files(empty)


def test_the_manifest_lists_the_installed_font_digests(rec, tmp_path):
    for language in rec.LANGUAGES:
        language_dir(tmp_path, language)
    fonts = font_dir(tmp_path)
    manifest = rec.build_manifest(
        tmp_path / "out",
        {language: dict(exit_code=0) for language in rec.LANGUAGES},
        predicates=dict(ordinary_actions=lambda c: True, ordinary_skill=lambda c: True),
        fonts=rec.font_files(fonts),
    )
    assert manifest["fonts"] == [
        dict(name=name, sha256=hashlib.sha256(data).hexdigest(), bytes=len(data))
        for name, data in (("Extra.ttc", b"ttc-bytes"), ("NotoSansCJKsc-Regular.otf", b"otf-bytes"))
    ]
    assert build(rec, tmp_path, {"en-US": 0, "zh-CN": 0})["fonts"] == []


def record_args(rec, tmp_path, fonts):
    """Parsed `record` arguments with new work and output roots under ``tmp_path``."""
    paths = ["--tarball", tmp_path / "a.tgz", "--work-root", tmp_path / "w"]
    paths += ["--out-dir", tmp_path / "o", "--font-dir", fonts]
    return rec.parse_args(["record", *map(str, paths)])


def test_record_passes_one_font_snapshot_to_every_language(rec, tmp_path, monkeypatch):
    """RM39: the manifest digests are the bytes every language installs.

    The recorder copies the fonts once into its private work root and hands that
    copy to each child, so a change to the operator's directory during a run
    cannot make the installed fonts differ from the listed ones.
    """
    fonts, commands, seen = font_dir(tmp_path), [], []

    def run(command, timeout):
        commands.append(command)
        snapshot = Path(command[command.index("--font-dir") + 1])
        seen.append({p.name: p.read_bytes() for p in snapshot.iterdir()})
        (fonts / "Extra.ttc").write_bytes(b"changed during the run")
        return 0

    monkeypatch.setattr(rec, "run_child", run)
    listed = {}
    monkeypatch.setattr(
        rec,
        "build_manifest",
        lambda out, outcomes, fonts: listed.update(fonts=fonts) or dict(complete=True),
    )
    args = record_args(rec, tmp_path, fonts)
    assert rec.record(args) == 0
    assert len(commands) == 2
    snapshots = {command[command.index("--font-dir") + 1] for command in commands}
    assert snapshots == {str(tmp_path / "w" / "fonts")}
    original = {"Extra.ttc": b"ttc-bytes", "NotoSansCJKsc-Regular.otf": b"otf-bytes"}
    assert seen == [original, original]
    expected = sorted(original.items())
    assert [(f["name"], f["sha256"], f["bytes"]) for f in listed["fonts"]] == [
        (name, hashlib.sha256(data).hexdigest(), len(data)) for name, data in expected
    ]


def test_record_refuses_an_empty_font_dir_before_creating_anything(rec, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    args = record_args(rec, tmp_path, empty)
    with pytest.raises(ValueError, match="walkthrough_font_dir"):
        rec.record(args)
    assert not (tmp_path / "w").exists() and not (tmp_path / "o").exists()


# --- review follow-ups (PR #262) ----------------------------------------------


def test_regex_names_translate_only_when_they_name_one_plugin_label(rec):
    import re

    pattern = re.compile("Delete feedback", re.IGNORECASE)
    assert rec.translate(pattern, "en-US") is pattern
    translated = rec.translate(pattern, "zh-CN")
    assert translated.fullmatch("删除反馈") and translated.flags & re.IGNORECASE
    with pytest.raises(ValueError, match="walkthrough_label_unknown"):
        rec.translate(re.compile(".*feedback"), "zh-CN")
    with pytest.raises(ValueError, match="walkthrough_label_unknown"):
        rec.translate(7, "zh-CN")


def test_a_child_without_any_output_is_recorded_incomplete(rec, tmp_path):
    (tmp_path / "out" / "en-US").mkdir(parents=True)
    manifest = rec.build_manifest(
        tmp_path / "out", {"en-US": dict(exit_code=1, reason="child_failed")}
    )
    entry = manifest["languages"][0]
    assert entry["capture"] is None and entry["screens"] is None and entry["videos"] == []
    assert entry["reason"] == "child_failed"
    assert {s["status"] for s in entry["steps"] if s["name"] not in rec.BLOCKED} == {"not_run"}
    assert {s["reason"] for s in entry["steps"] if s["name"] not in rec.BLOCKED} == {
        "capture_missing"
    }
    assert manifest["complete"] is False and manifest["evidence"] == []


def test_a_torn_capture_is_not_trusted(rec, tmp_path):
    base = tmp_path / "out" / "zh-CN"
    base.mkdir(parents=True)
    (base / "capture.json").write_bytes(b'{"status": "pass')
    manifest = rec.build_manifest(tmp_path / "out", {"zh-CN": dict(exit_code=None)})
    entry = manifest["languages"][0]
    assert {s["reason"] for s in entry["steps"] if s["name"] not in rec.BLOCKED} == {
        "capture_invalid"
    }
    # The torn original is still referenced by digest for review.
    assert entry["capture"] == "zh-CN/capture"


@pytest.mark.parametrize(
    ("outcome", "reason"), [("timeout", "child_timeout"), ("failed", "child_failed")]
)
def test_record_writes_an_incomplete_manifest_when_a_child_fails(
    rec, tmp_path, monkeypatch, outcome, reason
):
    import subprocess

    def fake_run(command, timeout):
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(command, timeout)
        return 2

    monkeypatch.setattr(rec, "run_child", fake_run)
    tarball = tmp_path / "plugin.tgz"
    tarball.write_bytes(b"tgz")
    argv = ["record", "--tarball", str(tarball), "--work-root", str(tmp_path / "work")]
    argv += ["--out-dir", str(tmp_path / "out")]
    assert rec.main(argv) == 2
    manifest = json.loads((tmp_path / "out" / "manifest.json").read_bytes())
    assert manifest["complete"] is False
    assert [e["language"] for e in manifest["languages"]] == list(rec.LANGUAGES)
    assert {e["reason"] for e in manifest["languages"]} == {reason}
    assert {e["exit_code"] for e in manifest["languages"]} == {None if outcome == "timeout" else 2}


# --- child process group (PR #262 adversarial review) -------------------------

# A child that starts a persistent grandchild (as the harness starts the gateway
# and proxy), records its PID, then hangs. With ``ignore``, both ignore SIGTERM.
HANGING_CHILD = r"""
import signal, subprocess, sys, time
if sys.argv[2] == "ignore":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
grand = subprocess.Popen([sys.executable, "-c",
    "import signal, sys, time\n"
    "if sys.argv[1] == 'ignore': signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "time.sleep(600)", sys.argv[2]])
open(sys.argv[1], "w").write(str(grand.pid))
time.sleep(600)
"""


def _alive(pid: int) -> bool:
    import os
    import time

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        time.sleep(0.05)  # An orphan may be reaped by init a moment later.
    return True


def _hanging(tmp_path, mode):
    import sys

    script = tmp_path / "hanging_child.py"
    script.write_text(HANGING_CHILD, encoding="utf-8")
    pid_file = tmp_path / "grandchild.pid"
    return [sys.executable, str(script), str(pid_file), mode], pid_file


def _wait_for(path):
    import time

    deadline = time.monotonic() + 10
    while not (path.exists() and path.read_text()):
        assert time.monotonic() < deadline
        time.sleep(0.05)
    return int(path.read_text())


@pytest.mark.skipif(__import__("sys").platform == "win32", reason="POSIX process groups")
@pytest.mark.parametrize("mode", ["graceful", "ignore"])
def test_a_child_timeout_stops_the_whole_process_group(rec, tmp_path, mode):
    """A timed-out child leaves no gateway or proxy running behind it."""
    import subprocess

    command, pid_file = _hanging(tmp_path, mode)
    with pytest.raises(subprocess.TimeoutExpired):
        rec.run_child(command, timeout=2, grace=0.5)
    grandchild = int(pid_file.read_text())
    assert not _alive(grandchild)


@pytest.mark.skipif(__import__("sys").platform == "win32", reason="POSIX process groups")
def test_an_interrupted_record_stops_the_whole_process_group(rec, tmp_path, monkeypatch):
    import subprocess

    command, pid_file = _hanging(tmp_path, "graceful")
    real_wait = subprocess.Popen.wait

    def wait(self, timeout=None):
        if timeout is not None and timeout > 1:  # The child's own wait, not the stop.
            _wait_for(pid_file)
            raise KeyboardInterrupt
        return real_wait(self, timeout=timeout)

    monkeypatch.setattr(subprocess.Popen, "wait", wait)
    with pytest.raises(KeyboardInterrupt):
        rec.run_child(command, timeout=60, grace=0.5)
    monkeypatch.undo()
    assert not _alive(int(pid_file.read_text()))


def test_a_child_that_finishes_returns_its_exit_code(rec):
    import sys

    assert rec.run_child([sys.executable, "-c", "raise SystemExit(4)"], timeout=30) == 4


# --- second review follow-ups (PR #262) ---------------------------------------


def test_unlisted_partial_screenshots_are_still_listed_by_digest(rec, tmp_path):
    """A killed child never saved screens.json: every PNG is still evidence."""
    base = language_dir(tmp_path, "en-US")
    (base / "screens" / "9999-orphan.png").write_bytes(b"orphan")
    manifest = build(rec, tmp_path, {"en-US": 0})
    evidence = {e["id"]: e for e in manifest["evidence"]}
    orphan = evidence["en-US/screens/9999-orphan.png"]
    assert orphan["sha256"] == hashlib.sha256(b"orphan").hexdigest()
    entry = manifest["languages"][0]
    assert entry["media_problems"] == ["screenshot_unlisted"]
    # An unlisted screenshot belongs to no step and never records one.
    assert all("en-US/screens/9999-orphan.png" not in s["evidence_ids"] for s in entry["steps"])
    assert manifest["complete"] is False


def test_screenshots_without_screens_json_are_listed(rec, tmp_path):
    base = tmp_path / "out" / "zh-CN" / "screens"
    base.mkdir(parents=True)
    (base / "0000-open.png").write_bytes(b"first")
    manifest = rec.build_manifest(
        tmp_path / "out", {"zh-CN": dict(exit_code=None, reason="child_timeout")}
    )
    assert [e["id"] for e in manifest["evidence"]] == ["zh-CN/screens/0000-open.png"]
    assert manifest["languages"][0]["screens"] is None
    assert manifest["complete"] is False


def test_a_listed_screenshot_that_is_missing_still_writes_the_manifest(rec, tmp_path):
    base = language_dir(tmp_path, "en-US")
    (base / "screens" / "0002-choose.png").unlink()
    manifest = build(rec, tmp_path, {"en-US": 0})
    entry = manifest["languages"][0]
    assert entry["media_problems"] == ["screenshot_missing"]
    status = {s["name"]: s for s in entry["steps"]}
    assert status["choose"] == dict(
        name="choose", status="failed", reason="screenshot_missing", evidence_ids=[]
    )
    assert manifest["complete"] is False


def test_a_torn_screens_list_still_writes_the_manifest(rec, tmp_path):
    base = language_dir(tmp_path, "en-US")
    (base / "screens.json").write_bytes(b'[{"step": "open", "fi')
    manifest = build(rec, tmp_path, {"en-US": 0})
    entry = manifest["languages"][0]
    assert entry["screens"] == "en-US/screens"
    assert set(entry["media_problems"]) == {"screens_invalid", "screenshot_unlisted"}
    assert len([e for e in manifest["evidence"] if "/screens/" in e["id"]]) == len(rec_steps())
    assert manifest["complete"] is False


class FakeOrdinaryBase:
    """The real ordinary methods replaced by doubles that record their inputs."""

    received = []

    def source_paths(self):
        return [Path("ordinary.py")]

    def select_library_skill(self, rpc, pages, sessions, ui_page, skill):
        ui_page.get_by_role("button", name="Select for next turn").click()
        FakeOrdinaryBase.received.append(ui_page)
        return dict(selected=skill)


def test_exact_skill_selection_is_localized_and_shot(rec, tmp_path, monkeypatch):
    """RM39/AC30: the recorder's select_skill branch, through a page double."""
    for name in ("source_paths", "select_library_skill"):
        monkeypatch.setattr(rec.ORDINARY.OrdinaryComposition, name, getattr(FakeOrdinaryBase, name))
    FakeOrdinaryBase.received = []
    shooter = rec.Shooter(tmp_path)
    composition = rec.walkthrough_composition("zh-CN", shooter)
    assert issubclass(composition, rec.ORDINARY.OrdinaryComposition)
    instance = object.__new__(composition)
    page = FakePage()
    assert instance.select_library_skill(None, [], [{}], page, "skill") == dict(selected="skill")
    (received,) = FakeOrdinaryBase.received
    assert isinstance(received, rec.LocalizedPage) and received._page is page
    assert ("click", "供下一轮使用") in page.actions
    assert {shot["step"] for shot in shooter.shots} == {"select_skill"}
    assert [shot["label"] for shot in shooter.shots] == ["供下一轮使用", "settled"]
    assert rec.HERE in instance.source_paths()


# --- review follow-ups, round 2 (PR #262) -------------------------------------


@pytest.mark.parametrize(
    ("pattern", "sample", "expected"),
    [
        ("Choose 1", "Choose 1", "选择 1"),
        (r"Choose \d+", "Choose 12", "选择 12"),
        ("Correct to 1", "Correct to 1", "纠正为 1"),
        (r"Correct to [0-9]+", "Correct to 3", "纠正为 3"),
    ],
)
def test_numbered_regex_names_translate_like_numbered_strings(rec, pattern, sample, expected):
    """A regex for a numbered button translates as translate() does for its string."""
    import re

    compiled = re.compile(pattern, re.IGNORECASE)
    assert compiled.fullmatch(sample) and rec.translate(sample, "zh-CN") == expected
    translated = rec.translate(compiled, "zh-CN")
    assert translated.fullmatch(expected) and translated.flags & re.IGNORECASE
    assert not translated.fullmatch(expected.split(" ")[0] + " x")


@pytest.mark.parametrize("pattern", [r"Choose .*", r"Choose \d+|Delete feedback", r"Choose (x)"])
def test_numbered_regex_names_beyond_the_number_are_unknown(rec, pattern):
    import re

    with pytest.raises(ValueError, match="walkthrough_label_unknown"):
        rec.translate(re.compile(pattern), "zh-CN")


def test_a_playwright_screenshot_timeout_is_not_swallowed(rec, tmp_path):
    """Playwright's TimeoutError is not the builtin one; the deadline still propagates."""
    playwright = pytest.importorskip("playwright.sync_api")

    class TimingOutPage:
        def screenshot(self, **kwargs):
            raise playwright.TimeoutError("screenshot timed out")

    shooter = rec.Shooter(tmp_path)
    shooter.step = "open"
    with pytest.raises(playwright.TimeoutError):
        shooter.shoot(TimingOutPage(), "label")
    assert shooter.shots == []


@pytest.mark.parametrize("existing", ["work", "out"])
def test_record_refuses_an_existing_directory_with_a_structured_outcome(
    rec, tmp_path, monkeypatch, capsys, existing
):
    monkeypatch.setattr(rec, "run_child", lambda command, timeout: pytest.fail("ran"))
    (tmp_path / existing).mkdir()
    tarball = tmp_path / "plugin.tgz"
    tarball.write_bytes(b"tgz")
    argv = ["record", "--tarball", str(tarball), "--work-root", str(tmp_path / "work")]
    argv += ["--out-dir", str(tmp_path / "out")]
    assert rec.main(argv) == 2
    outcome = json.loads(capsys.readouterr().out)
    reason = "work_root_exists" if existing == "work" else "out_dir_exists"
    assert outcome == dict(complete=False, reason=reason, manifest=None)
    # Nothing is created next to the refused directory.
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted([existing, "plugin.tgz"])


def test_child_restores_every_patched_harness_attribute(rec, tmp_path, monkeypatch):
    import argparse

    from playwright.sync_api import Browser

    originals = (rec.ORDINARY.run_ui, rec.ORDINARY.OrdinaryComposition, Browser.new_context)
    seen = []

    def fake_main(argv):
        seen.append((rec.ORDINARY.run_ui, rec.ORDINARY.OrdinaryComposition))
        raise RuntimeError("harness failed")

    monkeypatch.setattr(rec.ORDINARY, "main", fake_main)
    media = tmp_path / "media"
    media.mkdir()
    args = argparse.Namespace(
        language="zh-CN",
        tarball=tmp_path / "plugin.tgz",
        work_dir=tmp_path / "work",
        out=media / "capture.json",
        media=media,
        font_dir=None,
    )
    with pytest.raises(RuntimeError, match="harness failed"):
        rec.child(args)
    ((run_ui, composition),) = seen
    assert run_ui is not originals[0] and composition is not originals[1]
    assert (rec.ORDINARY.run_ui, rec.ORDINARY.OrdinaryComposition, Browser.new_context) == (
        originals
    )


# --- third review follow-ups (PR #262) ----------------------------------------

EXITING_CHILD = r"""
import subprocess, sys
grand = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
open(sys.argv[1], "w").write(str(grand.pid))
raise SystemExit(3)
"""


@pytest.mark.skipif(__import__("sys").platform == "win32", reason="POSIX process groups")
def test_a_child_that_exits_on_its_own_leaves_nothing_running(rec, tmp_path):
    """RM39/AC30: a crashed child's gateway or proxy never outlives it."""
    import sys

    script = tmp_path / "exiting_child.py"
    script.write_text(EXITING_CHILD, encoding="utf-8")
    pid_file = tmp_path / "grandchild.pid"
    command = [sys.executable, str(script), str(pid_file)]
    assert rec.run_child(command, timeout=30, grace=0.5) == 3
    assert not _alive(int(pid_file.read_text()))


def test_record_refuses_a_repeated_language_before_creating_anything(
    rec, tmp_path, monkeypatch, capsys
):
    """RM39/AC30: a repeated language is a structured refusal, not a crash."""
    monkeypatch.setattr(rec, "run_child", lambda command, timeout: pytest.fail("ran"))
    tarball = tmp_path / "plugin.tgz"
    tarball.write_bytes(b"tgz")
    argv = ["record", "--tarball", str(tarball), "--work-root", str(tmp_path / "work")]
    argv += ["--out-dir", str(tmp_path / "out"), "--languages", "en-US", "en-US"]
    assert rec.main(argv) == 2
    outcome = json.loads(capsys.readouterr().out)
    assert outcome == dict(complete=False, reason="languages_repeated", manifest=None)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["plugin.tgz"]


def test_numbered_labels_follow_the_control_ui_table(rec):
    """The numbered label families are built from the parsed Control UI labels."""
    for key, pattern in rec._NUMBERED.items():
        assert pattern.fullmatch(f"{rec._ENGLISH[key]} 7").group(1) == "7"


# --- review follow-ups, round 4 (PR #262) -------------------------------------


@pytest.mark.parametrize(
    ("pattern", "sample", "expected", "other"),
    [
        ("Choose 100", "Choose 100", "选择 100", "选择 10"),
        (r"Choose \d{3}", "Choose 123", "选择 123", "选择 12"),
        (r"Correct to [1-9][0-9]{2}", "Correct to 250", "纠正为 250", "纠正为 25"),
        (r"Choose 1000", "Choose 1000", "选择 1000", "选择 100"),
    ],
)
def test_numbered_regex_names_translate_for_any_positive_number(
    rec, pattern, sample, expected, other
):
    """A numbered button regex is not limited to 1-99; translate() takes any number."""
    import re

    compiled = re.compile(pattern)
    assert compiled.fullmatch(sample) and rec.translate(sample, "zh-CN") == expected
    translated = rec.translate(compiled, "zh-CN")
    assert translated.fullmatch(expected) and not translated.fullmatch(other)


@pytest.mark.parametrize(
    "pattern",
    [r"Choose [A-Z]+", r"Choose [,-]", r"Choose {3}", r"Choose \d+|Choose none", r"Choose \w+"],
)
def test_numbered_regex_names_with_a_non_digit_tail_are_unknown(rec, pattern):
    import re

    with pytest.raises(ValueError, match="walkthrough_label_unknown"):
        rec.translate(re.compile(pattern), "zh-CN")


class _ExitedProcess:
    pid = 424242

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0


@pytest.mark.parametrize(
    ("alive", "expected"),
    [
        # The group empties during the grace period: no SIGKILL to a reusable PGID.
        ({"SIGTERM": True, 0: False}, ["SIGTERM", 0]),
        # The group is already empty: nothing but the one SIGTERM attempt.
        ({"SIGTERM": False}, ["SIGTERM"]),
        # The group outlives the grace period: SIGKILL after a final liveness check.
        ({"SIGTERM": True, 0: True, "SIGKILL": True}, None),
    ],
)
def test_stop_group_kills_only_a_group_still_alive_after_the_grace(
    rec, monkeypatch, alive, expected
):
    import signal

    sent = []

    def fake_signal(group, signum):
        name = signum if signum == 0 else signal.Signals(signum).name
        sent.append(name)
        assert group == _ExitedProcess.pid
        return alive[name]

    monkeypatch.setattr(rec, "_signal_group", fake_signal)
    rec._stop_group(_ExitedProcess(), grace=0.1)
    if expected is not None:
        assert sent == expected
    else:
        assert sent[0] == "SIGTERM" and sent[-2:] == [0, "SIGKILL"]
        assert sent.count("SIGKILL") == 1


@pytest.mark.parametrize("layout", ["same", "out_inside_work", "work_inside_out", "alias"])
def test_record_refuses_overlapping_work_and_out_before_creating_anything(
    rec, tmp_path, monkeypatch, capsys, layout
):
    """RM39/AC30: the work root and the output never overlap; nothing is created."""
    monkeypatch.setattr(rec, "run_child", lambda command, timeout: pytest.fail("ran"))
    tarball = tmp_path / "plugin.tgz"
    tarball.write_bytes(b"tgz")
    (tmp_path / "link").symlink_to(tmp_path, target_is_directory=True)
    work, out = {
        "same": (tmp_path / "w", tmp_path / "w"),
        "out_inside_work": (tmp_path / "w", tmp_path / "w" / "out"),
        "work_inside_out": (tmp_path / "o" / "work", tmp_path / "o"),
        "alias": (tmp_path / "w", tmp_path / "link" / "w"),
    }[layout]
    argv = ["record", "--tarball", str(tarball), "--work-root", str(work), "--out-dir", str(out)]
    assert rec.main(argv) == 2
    outcome = json.loads(capsys.readouterr().out)
    assert outcome == dict(complete=False, reason="work_root_out_dir_overlap", manifest=None)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["link", "plugin.tgz"]
