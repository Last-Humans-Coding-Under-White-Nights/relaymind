"""RM39; AC30, AC35 (#238): record the packaged bilingual walkthrough.

The recorder reuses the ordinary installed-host qualification
(``host-ordinary-qualification/qualify.py``, itself built on the person-ingress
harness) unchanged: the same disposable host, the same packaged tarball
installed with the pinned CLI, the same signed-in person, the same backend
observations and the same pass predicates. It only

- opens every browser context with the walkthrough locale and video recording,
- clicks the plugin's own Control UI labels for that locale (read from
  ``src/control-ui.ts``; an unknown label fails instead of falling back), and
- takes a screenshot after every UI action and once each step has settled.

It runs once per language (``en-US`` and ``zh-CN``) and writes ``manifest.json``
with the SHA-256 and size of every capture, screenshot and video. A step is
``recorded`` only when the harness's own predicate accepts the capture and the
step has a screenshot. Steps the pinned host does not qualify are ``blocked``
with a fixed reason and are never attempted: loaded skill instruction use
(spec 7.6.1; #239), MCP use (spec 7.6.2; #204) and its host approval (AC35).

The tier is ``functional_fixture``: a deterministic gateway and disposable
fixture accounts. It is not a customer or human walkthrough, a live-model
result or platform acceptance. Screenshots and video stay private until
reviewed; never publish the work directory.

Usage (Linux x86-64 with the person-ingress prerequisites)::

    python integrations/openclaw/test/host-walkthrough/record.py record \\
        --tarball /abs/relaymind-openclaw-provider-0.1.0.tgz \\
        --work-root /abs/new-private-work --out-dir /abs/new-walkthrough
"""

import hashlib
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

HERE = Path(__file__).resolve()
ROOT = HERE.parents[4]
CONTROL_UI = ROOT / "integrations/openclaw/src/control-ui.ts"
ORDINARY_PATH = ROOT / "integrations/openclaw/test/host-ordinary-qualification/qualify.py"
LANGUAGES = ("en-US", "zh-CN")
NAVIGATION = {"en-US": "RelayMind recommendations", "zh-CN": "RelayMind 推荐"}
VIDEO_SIZE = {"width": 1280, "height": 800}
CHILD_SECONDS = 900
UI_STEPS = ("open", "recommend", "choose", "correct", "inspect", "delete", "reset")
BLOCKED = {
    "skill_use": dict(reason="skill_loaded_use_unqualified", references=["spec 7.6.1", "#239"]),
    "mcp_use": dict(reason="mcp_use_unqualified", references=["spec 7.6.2", "#204"]),
    "mcp_approval": dict(
        reason="host_mcp_approval_unqualified", references=["spec 7.6.2", "AC35", "#204"]
    ),
}
STEP_NAMES = (*UI_STEPS, "select_skill", *BLOCKED)


def _ordinary():
    spec = importlib.util.spec_from_file_location("walkthrough_ordinary", ORDINARY_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ORDINARY = _ordinary()


# --- labels ---------------------------------------------------------------------


def _table(source: str, start: str) -> dict:
    body = source[source.index(start) :]
    body = body[body.index("{") + 1 : body.index("\n};")]
    return {
        key: json.loads('"' + value + '"')
        for key, value in re.findall(r'(\w+):\s*"((?:[^"\\]|\\.)*)"', body)
    }


def control_ui_labels() -> tuple[dict, dict]:
    """The English and Chinese label tables of the packaged Control UI."""
    source = CONTROL_UI.read_text(encoding="utf-8")
    english = _table(source, "const english = {")
    chinese = _table(source, "const chinese: typeof english = {")
    if set(english) != set(chinese):
        raise ValueError("walkthrough_label_tables")
    return english, chinese


_ENGLISH, _CHINESE = control_ui_labels()
_NUMBERED = {"choose": re.compile(r"Choose (\d+)"), "correction": re.compile(r"Correct to (\d+)")}


def translate(name: str, language: str) -> str:
    """An English role name of the ordinary harness in the walkthrough language."""
    if language == "en-US":
        return name
    if language != "zh-CN":
        raise ValueError("walkthrough_language")
    if name == NAVIGATION["en-US"]:
        return NAVIGATION["zh-CN"]
    for key, value in _ENGLISH.items():
        if value == name:
            return _CHINESE[key]
    for key, pattern in _NUMBERED.items():
        match = pattern.fullmatch(name)
        if match:
            return f"{_CHINESE[key]} {match.group(1)}"
    raise ValueError("walkthrough_label_unknown")


def context_defaults(options: dict, locale: str, video_dir: Path) -> dict:
    """Browser context options with the walkthrough locale and video recording."""
    value = dict(options)
    value.setdefault("locale", locale)
    value.setdefault("record_video_dir", str(video_dir))
    value.setdefault("record_video_size", dict(VIDEO_SIZE))
    return value


# --- screenshots and the localized page ---------------------------------------------


class Shooter:
    """Screenshots per walkthrough step under ``root/screens``."""

    def __init__(self, root: Path):
        self.root = Path(root)
        (self.root / "screens").mkdir(parents=True, exist_ok=True)
        self.step = None
        self.shots = []

    def shoot(self, page, label: str) -> None:
        if self.step is None:
            return
        name = f"screens/{len(self.shots):04d}-{self.step}.png"
        try:
            page.screenshot(path=str(self.root / name), full_page=True)
        except TimeoutError:
            raise  # The harness's overall deadline is never swallowed.
        except Exception:
            return  # A missing screenshot leaves the step unrecorded.
        self.shots.append(dict(step=self.step, file=name, label=label))

    def transition(self, page, step: str) -> None:
        self.shoot(page, "settled")
        self.step = step

    def save(self) -> None:
        (self.root / "screens.json").write_text(json.dumps(self.shots, indent=1), "utf-8")


class LocalizedLocator:
    def __init__(self, locator, page, shooter, label):
        self._locator, self._page, self._shooter, self._label = locator, page, shooter, label

    def _act(self, method, *args, **kwargs):
        result = getattr(self._locator, method)(*args, **kwargs)
        self._shooter.shoot(self._page, self._label)
        return result

    def click(self, *args, **kwargs):
        return self._act("click", *args, **kwargs)

    def fill(self, *args, **kwargs):
        return self._act("fill", *args, **kwargs)

    def select_option(self, *args, **kwargs):
        return self._act("select_option", *args, **kwargs)

    def wait_for(self, *args, **kwargs):
        return self._act("wait_for", *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._locator, name)


class LocalizedPage:
    """The real page, with the harness's English names in the walkthrough language."""

    def __init__(self, page, language: str, shooter: Shooter):
        self._page, self._language, self._shooter = page, language, shooter

    def _wrap(self, locator, label):
        return LocalizedLocator(locator, self._page, self._shooter, label)

    def get_by_role(self, role, name=None, **kwargs):
        label = translate(name, self._language) if name is not None else None
        return self._wrap(self._page.get_by_role(role, name=label, **kwargs), label or role)

    def get_by_label(self, text, **kwargs):
        label = translate(text, self._language)
        return self._wrap(self._page.get_by_label(label, **kwargs), label)

    def get_by_text(self, text, **kwargs):
        label = translate(text, self._language)
        return self._wrap(self._page.get_by_text(label, **kwargs), label)

    def __getattr__(self, name):
        return getattr(self._page, name)


def localized_run_ui(language: str, shooter: Shooter, run_ui=None):
    """The ordinary harness's own ``run_ui`` through the localized page."""
    run_ui = run_ui or ORDINARY.run_ui

    def run(page, session, observe, record_action=lambda action: None):
        localized = LocalizedPage(page, language, shooter)

        def action(name):
            shooter.transition(page, name)
            record_action(name)

        result = run_ui(localized, session, observe, action)
        shooter.shoot(page, "settled")
        return dict(result, locale=language)

    return run


def walkthrough_composition(language: str, shooter: Shooter):
    """The ordinary composition with its native skill selection localized."""

    class WalkthroughComposition(ORDINARY.OrdinaryComposition):
        def source_paths(self):
            # Bind the recorder source into the capture with the harness sources.
            return super().source_paths() + [HERE]

        def select_library_skill(self, rpc, pages, sessions, ui_page, skill):
            shooter.transition(ui_page, "select_skill")
            localized = LocalizedPage(ui_page, language, shooter)
            result = super().select_library_skill(rpc, pages, sessions, localized, skill)
            shooter.shoot(ui_page, "settled")
            return result

    return WalkthroughComposition


# --- manifest -------------------------------------------------------------------------


def _media(base: Path, relative: str) -> Path:
    path = PurePosixPath(relative)
    if path.is_absolute() or not path.parts or ".." in path.parts or "\\" in relative:
        raise ValueError("walkthrough_media_path")
    target = base.joinpath(*path.parts)
    if target.is_symlink() or not target.is_file():
        raise ValueError("walkthrough_media_path")
    return target


def _default_predicates() -> dict:
    def safe(check):
        def run(capture):
            try:
                return check(capture) is True
            except Exception:
                return False

        return run

    return dict(
        ordinary_actions=safe(ORDINARY.ordinary_pass),
        ordinary_skill=safe(ORDINARY.ordinary_skill_pass),
    )


def _steps(capture: dict, shots: dict, passed: dict) -> list:
    failed_step = None
    if capture.get("ordinary_failure_step") == "ui_actions":
        failed_step = capture.get("ordinary_ui_action")
    elif capture.get("ordinary_failure_step") == "select_library_skill":
        failed_step = "select_skill"
    reached = True
    steps = []
    for name in STEP_NAMES:
        ids = shots.get(name, [])
        if name in BLOCKED:
            steps.append(
                dict(name=name, status="blocked", reason=BLOCKED[name]["reason"], evidence_ids=[])
            )
            continue
        check = "ordinary_skill" if name == "select_skill" else "ordinary_actions"
        if passed[check]:
            if ids:
                step = dict(status="recorded", reason=None)
            else:
                step = dict(status="failed", reason="screenshot_missing")
        elif name == failed_step:
            step = dict(status="failed", reason=capture.get("reason") or "qualification_failed")
            reached = False
        elif reached and ids:
            step = dict(status="not_verified", reason="harness_predicate_failed")
        else:
            step = dict(status="not_run", reason="not_reached")
        steps.append(dict(name=name, **step, evidence_ids=ids))
    return steps


def _digest(evidence: dict, out: Path, evidence_id: str, path: Path) -> str:
    payload = path.read_bytes()
    evidence[evidence_id] = dict(
        id=evidence_id,
        path=path.relative_to(out).as_posix(),
        sha256=hashlib.sha256(payload).hexdigest(),
        bytes=len(payload),
        provenance="fixture",
    )
    return evidence_id


def build_manifest(out: Path, outcomes: dict, *, predicates: dict | None = None) -> dict:
    """The walkthrough manifest for the languages recorded under ``out``."""
    out = Path(out)
    predicates = predicates or _default_predicates()
    evidence, languages = {}, []
    for language in LANGUAGES:
        if language not in outcomes:
            continue
        base = out / language
        capture_path = _media(base, "capture.json")
        capture = json.loads(capture_path.read_bytes())
        capture_id = _digest(evidence, out, language + "/capture", capture_path)
        screens_id = _digest(evidence, out, language + "/screens", _media(base, "screens.json"))
        shots = {}
        for shot in json.loads((base / "screens.json").read_bytes()):
            path = _media(base, shot["file"])
            shots.setdefault(shot["step"], []).append(
                _digest(evidence, out, language + "/" + shot["file"], path)
            )
        videos = [
            _digest(
                evidence, out, f"{language}/video/{path.name}", _media(base, "video/" + path.name)
            )
            for path in sorted((base / "video").glob("*.webm"))
        ]
        passed = {name: check(capture) for name, check in predicates.items()}
        environment = capture.get("environment") or {}
        installation = (capture.get("ordinary_actions") or {}).get("installation") or {}
        languages.append(
            dict(
                language=language,
                exit_code=outcomes[language]["exit_code"],
                host_version=environment.get("observed_host_version"),
                archive_sha256=installation.get("archive_sha256"),
                checks=passed,
                capture=capture_id,
                screens=screens_id,
                videos=videos,
                steps=_steps(capture, shots, passed),
            )
        )
    complete = [entry["language"] for entry in languages] == list(LANGUAGES) and all(
        entry["exit_code"] == 0
        and all(s["status"] in ("recorded", "blocked") for s in entry["steps"])
        for entry in languages
    )
    return dict(
        schema_version=1,
        kind="relaymind-openclaw-walkthrough",
        tier="functional_fixture",
        harness=dict(
            ordinary=hashlib.sha256(ORDINARY_PATH.read_bytes()).hexdigest(),
            recorder=hashlib.sha256(HERE.read_bytes()).hexdigest(),
        ),
        blocked={name: dict(value) for name, value in BLOCKED.items()},
        languages=languages,
        evidence=[evidence[key] for key in sorted(evidence)],
        complete=complete,
    )


# --- commands -------------------------------------------------------------------------


def child(args) -> int:
    """One language: the ordinary harness with the walkthrough patches applied."""
    from playwright.sync_api import Browser

    media = args.media.resolve(strict=True)
    video = media / "video"
    video.mkdir(exist_ok=True)
    original = Browser.new_context

    def new_context(self, **options):
        return original(self, **context_defaults(options, args.language, video))

    shooter = Shooter(media)
    Browser.new_context = new_context
    ORDINARY.run_ui = localized_run_ui(args.language, shooter)
    ORDINARY.OrdinaryComposition = walkthrough_composition(args.language, shooter)
    try:
        return ORDINARY.main(
            [
                "--tarball",
                str(args.tarball),
                "--work-dir",
                str(args.work_dir),
                "--out",
                str(args.out),
            ]
        )
    finally:
        shooter.save()
        Browser.new_context = original


def record(args) -> int:
    work_root, out = args.work_root.absolute(), args.out_dir.absolute()
    work_root.mkdir(mode=0o700, parents=True, exist_ok=False)
    out.mkdir(mode=0o700, parents=True, exist_ok=False)
    outcomes = {}
    for language in args.languages:
        media = out / language
        media.mkdir(mode=0o700)
        command = [sys.executable, str(HERE), "child", "--language", language]
        command += ["--tarball", str(args.tarball.absolute()), "--work-dir"]
        command += [str(work_root / language), "--out", str(media / "capture.json")]
        command += ["--media", str(media)]
        try:
            code = subprocess.run(command, timeout=CHILD_SECONDS).returncode
        except subprocess.TimeoutExpired:
            code = None
        outcomes[language] = dict(exit_code=code)
    manifest = build_manifest(out, outcomes)
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", "utf-8")
    print(json.dumps(dict(complete=manifest["complete"], manifest=str(out / "manifest.json"))))
    return 0 if manifest["complete"] else 2


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("record", help="record every language and write manifest.json")
    run.add_argument("--tarball", type=Path, required=True)
    run.add_argument("--work-root", type=Path, required=True, help="new private directory")
    run.add_argument("--out-dir", type=Path, required=True, help="new output directory")
    run.add_argument("--languages", nargs="+", choices=LANGUAGES, default=list(LANGUAGES))
    one = sub.add_parser("child", help="internal: one language")
    one.add_argument("--language", choices=LANGUAGES, required=True)
    one.add_argument("--tarball", type=Path, required=True)
    one.add_argument("--work-dir", type=Path, required=True)
    one.add_argument("--out", type=Path, required=True)
    one.add_argument("--media", type=Path, required=True)
    args = parser.parse_args(argv)
    return record(args) if args.command == "record" else child(args)


if __name__ == "__main__":
    raise SystemExit(main())
