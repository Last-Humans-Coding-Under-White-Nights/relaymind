"""Bilingual packaged walkthrough recorder for the installed OpenClaw host (#238).

RM39; AC30, AC35. The recorder drives the existing ordinary installed-host
qualification through the plugin's own Control UI labels in English and
Simplified Chinese, takes screenshots and video, and writes a manifest with
SHA-256 digests and evidence references. Steps the pinned host does not
qualify (loaded skill use, MCP use and its host approval) are recorded as
blocked with a fixed reason, never performed or faked. These fixture tests run
no browser and no host: the page is a local double.
"""

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
    shots = json.loads((base / "screens.json").read_bytes())
    shots[0]["file"] = "../../outside.png"
    (base / "screens.json").write_text(json.dumps(shots), encoding="utf-8")
    with pytest.raises(ValueError, match="walkthrough_media_path"):
        build(rec, tmp_path, {"en-US": 0})
