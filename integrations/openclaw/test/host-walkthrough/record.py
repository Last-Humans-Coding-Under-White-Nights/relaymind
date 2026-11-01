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
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath

HERE = Path(__file__).resolve()
ROOT = HERE.parents[4]
CONTROL_UI = ROOT / "integrations/openclaw/src/control-ui.ts"
ORDINARY_PATH = ROOT / "integrations/openclaw/test/host-ordinary-qualification/qualify.py"
LANGUAGES = ("en-US", "zh-CN")
NAVIGATION = {"en-US": "RelayMind recommendations", "zh-CN": "RelayMind 推荐"}
VIDEO_SIZE = {"width": 1280, "height": 800}
CHILD_SECONDS = 900
STOP_GRACE_SECONDS = 15
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


# The number part of a numbered-label regex: digits, digit classes and quantifiers only.
_NUMBER_PART = re.compile(r"(?:\\d|[0-9\[\]+*?{},-])+")
_NUMBER_SAMPLES = range(1, 100)


def _numbered_families(name: re.Pattern) -> list:
    """The numbered labels (``Choose N``, ``Correct to N``) a regex names for some N."""
    return [
        key
        for key in _NUMBERED
        if any(name.fullmatch(f"{_ENGLISH[key]} {n}") for n in _NUMBER_SAMPLES)
    ]


def _translate_pattern(name: re.Pattern, language: str) -> re.Pattern:
    """A regex name translates only when it names exactly one plugin label.

    A label is one fixed label or one numbered label family, as in translate().
    A numbered regex keeps its number part, which may only constrain the number.
    """
    if not isinstance(name.pattern, str):
        raise ValueError("walkthrough_label_unknown")
    labels = {NAVIGATION["en-US"], *_ENGLISH.values()}
    matches = [label for label in labels if name.fullmatch(label)]
    families = _numbered_families(name)
    if len(matches) + len(families) != 1:
        raise ValueError("walkthrough_label_unknown")
    if matches:
        return re.compile(re.escape(translate(matches[0], language)), name.flags)
    prefix = _ENGLISH[families[0]] + " "
    number = name.pattern[len(prefix) :]
    if not name.pattern.startswith(prefix) or not _NUMBER_PART.fullmatch(number):
        raise ValueError("walkthrough_label_unknown")
    return re.compile(re.escape(_CHINESE[families[0]] + " ") + number, name.flags)


def translate(name, language: str):
    """An English role name of the ordinary harness in the walkthrough language.

    Playwright also accepts a compiled pattern; it is kept in English and, in
    Chinese, replaced by the exact translation of the one label it names.
    """
    if language not in LANGUAGES:
        raise ValueError("walkthrough_language")
    if isinstance(name, re.Pattern):
        return name if language == "en-US" else _translate_pattern(name, language)
    if not isinstance(name, str):
        raise ValueError("walkthrough_label_unknown")
    if language == "en-US":
        return name
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


def _timeout_errors() -> tuple:
    """The builtin and Playwright timeouts; Playwright's is not a builtin subclass."""
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeout
    except ImportError:  # Unit tests may run without Playwright installed.
        return (TimeoutError,)
    return (TimeoutError, PlaywrightTimeout)


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
        except Exception as error:
            if isinstance(error, _timeout_errors()):
                raise  # The harness's overall deadline is never swallowed.
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
        text = label.pattern if isinstance(label, re.Pattern) else str(label)
        return LocalizedLocator(locator, self._page, self._shooter, text)

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


def _safe(relative: str) -> bool:
    path = PurePosixPath(relative)
    return not (path.is_absolute() or not path.parts or ".." in path.parts or "\\" in relative)


def _media(base: Path, relative: str) -> Path:
    if not _safe(relative):
        raise ValueError("walkthrough_media_path")
    target = base.joinpath(*PurePosixPath(relative).parts)
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


def _present(base: Path, relative: str) -> bool:
    target = base / relative
    return target.is_symlink() or target.exists()


def _missing_steps(reason: str) -> list:
    return [
        dict(name=name, status="blocked", reason=BLOCKED[name]["reason"], evidence_ids=[])
        if name in BLOCKED
        else dict(name=name, status="not_run", reason=reason, evidence_ids=[])
        for name in STEP_NAMES
    ]


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


def _screens(base: Path, out: Path, evidence: dict, language: str, problems: set) -> tuple:
    """screens.json and every screenshot under ``screens/``, listed or not.

    A killed child may never save ``screens.json``, and a torn one may name a
    missing or unsafe file. Every PNG that exists is listed by digest; only a
    safe listed one is attached to its step. Problems get fixed reasons.
    """
    screens_id, shots, listed = None, {}, []
    if _present(base, "screens.json"):
        try:
            screens_path = _media(base, "screens.json")
        except ValueError:
            problems.add("screens_invalid")
        else:
            screens_id = _digest(evidence, out, language + "/screens", screens_path)
            try:
                listed = json.loads(screens_path.read_bytes())
            except ValueError:
                listed = None
            if not isinstance(listed, list):
                problems.add("screens_invalid")
                listed = []
    attached = set()
    for shot in listed:
        if not (
            isinstance(shot, dict)
            and shot.get("step") in STEP_NAMES
            and isinstance(shot.get("file"), str)
        ):
            problems.add("screens_invalid")
            continue
        try:
            path = _media(base, shot["file"])
        except ValueError:
            problems.add("screenshot_missing" if _safe(shot["file"]) else "screenshot_path")
            continue
        relative = path.relative_to(base).as_posix()
        attached.add(relative)
        shots.setdefault(shot["step"], []).append(
            _digest(evidence, out, language + "/" + relative, path)
        )
    for path in sorted((base / "screens").glob("*.png")):
        relative = "screens/" + path.name
        if relative in attached:
            continue
        try:
            _digest(evidence, out, language + "/" + relative, _media(base, relative))
        except ValueError:
            problems.add("screenshot_path")
            continue
        problems.add("screenshot_unlisted")
    return screens_id, shots


def build_manifest(out: Path, outcomes: dict, *, predicates: dict | None = None) -> dict:
    """The walkthrough manifest for the languages recorded under ``out``."""
    out = Path(out)
    predicates = predicates or _default_predicates()
    evidence, languages = {}, []
    for language in LANGUAGES:
        if language not in outcomes:
            continue
        base = out / language
        problems = set()
        # A failed or timed-out child may leave no, or torn, outputs: record
        # what exists by digest and leave every step unrecorded.
        capture, capture_id, problem = None, None, "capture_missing"
        if _present(base, "capture.json"):
            problem = "capture_invalid"
            try:
                capture_path = _media(base, "capture.json")
            except ValueError:
                problems.add("capture_path")
            else:
                capture_id = _digest(evidence, out, language + "/capture", capture_path)
                try:
                    capture = json.loads(capture_path.read_bytes())
                except ValueError:
                    capture = None
                capture = capture if isinstance(capture, dict) else None
        screens_id, shots = _screens(base, out, evidence, language, problems)
        videos = []
        for path in sorted((base / "video").glob("*.webm")):
            try:
                videos.append(
                    _digest(
                        evidence,
                        out,
                        f"{language}/video/{path.name}",
                        _media(base, "video/" + path.name),
                    )
                )
            except ValueError:
                problems.add("video_path")
        if capture is None:
            passed = {name: False for name in predicates}
            steps = _missing_steps(problem)
        else:
            passed = {name: check(capture) for name, check in predicates.items()}
            steps = _steps(capture, shots, passed)
        capture = capture or {}
        environment = capture.get("environment") or {}
        installation = (capture.get("ordinary_actions") or {}).get("installation") or {}
        languages.append(
            dict(
                language=language,
                exit_code=outcomes[language]["exit_code"],
                reason=outcomes[language].get("reason"),
                host_version=environment.get("observed_host_version"),
                archive_sha256=installation.get("archive_sha256"),
                checks=passed,
                capture=capture_id,
                screens=screens_id,
                videos=videos,
                media_problems=sorted(problems),
                steps=steps,
            )
        )
    complete = [entry["language"] for entry in languages] == list(LANGUAGES) and all(
        entry["exit_code"] == 0
        and not entry["media_problems"]
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


# --- child processes ------------------------------------------------------------------


def _signal_group(group: int, signum: int) -> bool:
    """Send ``signum`` to the process group; False once the group is empty."""
    try:
        os.killpg(group, signum)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Some kernels refuse a group whose members are all exiting; nothing to stop.
        return False
    return True


def _stop_group(process: subprocess.Popen, grace: float) -> None:
    """Stop the child's whole process group: SIGTERM, a bounded wait, then SIGKILL.

    The person-ingress harness starts a gateway and a proxy and stops them in a
    ``finally`` block; SIGTERM lets it do so, and SIGKILL leaves nothing behind
    if it cannot within ``grace`` seconds.
    """
    group = process.pid
    deadline = time.monotonic() + grace
    if _signal_group(group, signal.SIGTERM):
        while time.monotonic() < deadline:
            if process.poll() is not None and not _signal_group(group, 0):
                break
            time.sleep(0.05)
    _signal_group(group, signal.SIGKILL)
    process.wait()


def run_child(command: list[str], timeout: float, grace: float = STOP_GRACE_SECONDS) -> int:
    """Run one language in its own session and return its exit code.

    On timeout (``subprocess.TimeoutExpired``) or interruption the whole
    process group is stopped before the exception propagates, so no gateway or
    proxy the harness started outlives it.
    """
    process = subprocess.Popen(command, start_new_session=True)
    try:
        return process.wait(timeout=timeout)
    except BaseException:
        _stop_group(process, grace)
        raise


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
    run_ui, composition = ORDINARY.run_ui, ORDINARY.OrdinaryComposition
    Browser.new_context = new_context
    try:
        ORDINARY.run_ui = localized_run_ui(args.language, shooter)
        ORDINARY.OrdinaryComposition = walkthrough_composition(args.language, shooter)
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
        ORDINARY.run_ui, ORDINARY.OrdinaryComposition = run_ui, composition


def _refused(reason: str) -> int:
    """The record outcome line for a run that never started: incomplete, exit 2."""
    print(json.dumps(dict(complete=False, reason=reason, manifest=None)))
    return 2


def record(args) -> int:
    work_root, out = args.work_root.absolute(), args.out_dir.absolute()
    # Both must be new; an existing one is refused before either is created.
    for path, reason in ((work_root, "work_root_exists"), (out, "out_dir_exists")):
        if path.exists() or path.is_symlink():
            return _refused(reason)
    try:
        work_root.mkdir(mode=0o700, parents=True, exist_ok=False)
    except FileExistsError:
        return _refused("work_root_exists")
    try:
        out.mkdir(mode=0o700, parents=True, exist_ok=False)
    except FileExistsError:
        return _refused("out_dir_exists")
    outcomes = {}
    for language in args.languages:
        media = out / language
        media.mkdir(mode=0o700)
        command = [sys.executable, str(HERE), "child", "--language", language]
        command += ["--tarball", str(args.tarball.absolute()), "--work-dir"]
        command += [str(work_root / language), "--out", str(media / "capture.json")]
        command += ["--media", str(media)]
        try:
            code = run_child(command, CHILD_SECONDS)
            reason = None if code == 0 else "child_failed"
        except subprocess.TimeoutExpired:
            code, reason = None, "child_timeout"
        outcomes[language] = dict(exit_code=code, reason=reason)
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
