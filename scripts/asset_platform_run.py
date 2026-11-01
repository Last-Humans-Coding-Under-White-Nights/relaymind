"""Platform runs on GitHub-hosted runners (#241, #235; RM38, RM39, RM44; AC29, AC37).

The ``platform-runs`` workflow calls these steps on ``macos-latest``,
``windows-latest`` and ``ubuntu-latest``:

    ordinary  run the ordinary installed-host qualification
              (``integrations/openclaw/test/host-ordinary-qualification/qualify.py``)
              and fail only on a harness error: its person-ingress harness runs
              only on Linux x86-64, so elsewhere its truthful
              ``linux_runner_required`` capture is kept and the step passes
    platform  write the runner's environment, a fixture companion input
              bundle locked to that environment, and the AssetPlatformReportV1
              of ``scripts/asset_operations_run.py platform``
    verify    grade downloaded run directories again with the existing
              graders: ``summarize_operations`` for each operations report and
              ``asset_whole_task.py grade-platform`` for each platform report

Spec 0.65 section 12.6.5: CI runners, Linux and fixture inputs never satisfy
the Apple Silicon macOS / native x86-64 Windows acceptance. Every report stays
``fixture`` and every platform gate ``not_applicable``. The companion bundle
reuses the deterministic test fixtures (``tests/test_asset_whole_task_inputs``),
not genuine host tasks; it exists so the grader can check the report's
structure and evidence bytes.
"""

import argparse
import json
import platform
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT / "src", ROOT / "tests"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from relaymind.evaluation.asset_evidence import collect_benchmark_environment  # noqa: E402
from relaymind.evaluation.asset_operations import summarize_operations  # noqa: E402
from relaymind.evaluation.asset_operations_run import (  # noqa: E402
    load_operations_report,
    ordinary_product_failure,
)

QUALIFY = ROOT / "integrations/openclaw/test/host-ordinary-qualification/qualify.py"
KIT = ROOT / "scripts/asset_operations_run.py"


def _load(path: Path, name: str):
    import importlib.util

    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- ordinary installed-host step -------------------------------------------------------


def ordinary_exit(code: int, capture: Mapping | None, *, system: str, machine: str) -> int:
    """0 for a passing run, a recorded product failure or a truthful off-Linux capture.

    A product failure is a failed recommendation UI action after setup
    completed (``ordinary_product_failure``); the platform report records it as
    a failed step. Everything else nonzero is a harness error (2).
    """
    if not isinstance(capture, Mapping):
        return 2
    if code == 0 or ordinary_product_failure(capture) is not None:
        return 0
    linux_x86 = system == "Linux" and machine.lower() in {"x86_64", "amd64"}
    if not linux_x86 and capture.get("reason") == "linux_runner_required":
        return 0
    return 2


def _ordinary(args) -> int:
    out = Path(args.out).absolute()
    out.parent.mkdir(parents=True, exist_ok=True)
    # Only this attempt's capture is graded: an earlier attempt's output is
    # removed first, so a run that exits before writing is ``capture_missing``
    # (exit 2) and the platform step cannot pick up the stale capture.
    out.unlink(missing_ok=True)
    argv = [sys.executable, str(QUALIFY), "--tarball", args.tarball]
    argv += ["--work-dir", str(Path(args.work_dir).absolute()), "--out", str(out)]
    try:
        code = subprocess.run(argv, timeout=args.timeout).returncode
    except subprocess.TimeoutExpired:
        print(
            f"ordinary qualification ordinary_timeout after {args.timeout} s: step exit 2",
            file=sys.stderr,
        )
        return 2
    try:
        capture = json.loads(out.read_bytes())
    except (OSError, ValueError):
        capture = None
    result = ordinary_exit(code, capture, system=platform.system(), machine=platform.machine())
    reason = capture.get("reason") if isinstance(capture, Mapping) else "capture_missing"
    print(
        f"ordinary qualification exit {code}, reason {reason}: step exit {result}",
        file=sys.stderr if result else sys.stdout,
    )
    return result


# --- platform report ----------------------------------------------------------------------


# The files a platform run executes or loads (spec 0.65 section 12.6.5: the
# runner inventory names the actual driver/grader and the shared modules it
# imports). The S3 grader's own ``runner_inventory`` is closed to its package
# and anchors, so these are bound by a separate registered evidence document,
# ``platform-runner-files``. The release tarball the ordinary harness installs
# is bound by ``release.json`` / ``SHA256SUMS``.
PLATFORM_RUNNER_EVIDENCE = "platform-runner-files"
_PLATFORM_RUNNER_FILES = (
    ".github/workflows/platform-runs.yml",
    "pyproject.toml",
    "scripts/asset_operations_run.py",
    "scripts/asset_platform_run.py",
    "scripts/asset_whole_task.py",
    "scripts/openclaw_install_check.py",
    # The fixture companion bundle builder.
    "tests/test_asset_whole_task_inputs.py",
    "tests/whole_task_helpers.py",
    # The test helpers the ordinary installed-host harness imports.
    "tests/api_helpers.py",
    "tests/gateway_helpers.py",
    "tests/helpers.py",
    "tests/recommendation_helpers.py",
    "tests/test_asset_catalog.py",
    "tests/test_asset_sources.py",
)
# The ordinary harness and the action and person harnesses it is built on.
_PLATFORM_HARNESS_DIRECTORIES = (
    "integrations/openclaw/test/host-action-qualification",
    "integrations/openclaw/test/host-ordinary-qualification",
    "integrations/openclaw/test/host-person-qualification",
)


def _tracked(*pathspecs: str) -> list[str]:
    """The checkout's tracked files under ``pathspecs``, as POSIX paths."""
    listed = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z", "--", *pathspecs],
        capture_output=True,
        check=True,
    ).stdout
    return [path for path in listed.decode("utf-8").split("\0") if path]


def platform_runner_files() -> list[str]:
    """Every tracked repository file the platform run executes or loads, as sorted paths.

    Only tracked files are bound, so untracked local files (``.DS_Store``,
    ``.pyo``, editor swap files, scratch modules) never change the inventory.
    """
    paths = set(_PLATFORM_RUNNER_FILES)
    paths.update(p for p in _tracked("src/relaymind") if p.endswith(".py"))
    paths.update(_tracked(*_PLATFORM_HARNESS_DIRECTORIES))
    return sorted(paths)


def platform_runner_inventory() -> bytes:
    """Path, SHA-256 and size of every ``platform_runner_files`` file, as JSON bytes."""
    import hashlib

    files = []
    for path in platform_runner_files():
        payload = (ROOT / path).read_bytes()
        files.append(
            dict(path=path, sha256=hashlib.sha256(payload).hexdigest(), bytes=len(payload))
        )
    return (json.dumps(dict(schema_version=1, files=files), indent=1) + "\n").encode("utf-8")


def _bundle(out: Path, environment: Mapping, evidence: Mapping[str, bytes]):
    """The fixture companion inputs, locked to the observed runner environment,
    registering ``platform-runner-files`` next to the run's own evidence."""
    from test_asset_whole_task_inputs import Bundle

    bundle = Bundle(out)
    bundle.documents = [d for d in bundle.documents if d["id"] != "environment_lock"]
    bundle.add("environment_lock", dict(environment))
    bundle.extra_evidence.update(evidence)
    bundle.extra_evidence[PLATFORM_RUNNER_EVIDENCE] = platform_runner_inventory()
    bundle.write_manifest()
    return bundle


NOT_BUILT = "platform-not-built.json"


def _platform(args) -> int:
    """Exit 2 with a recorded ``not_built`` reason when the lifecycle record is missing.

    An existing bundle directory is refused: the companion bundle is written once.
    """
    out = Path(args.out_dir).absolute()
    if (out / "bundle").exists():
        raise ValueError(f"platform_out_dir_exists: {out}")
    out.mkdir(parents=True, exist_ok=True)
    if not Path(args.install_check).is_file():
        record = {"status": "not_built", "reason": "install_check_missing"}
        (out / NOT_BUILT).write_text(json.dumps(record) + "\n", encoding="utf-8")
        print("asset_platform_run: install_check_missing", file=sys.stderr)
        return 2
    environment = collect_benchmark_environment()
    (out / "environment.json").write_text(json.dumps(environment, indent=2) + "\n")
    originals = {"install-check": args.install_check}
    if args.ordinary:
        originals["ordinary-capture"] = args.ordinary
    if args.operations:
        originals["operations-report"] = args.operations
    bundle = _bundle(out, environment, {k: Path(v).read_bytes() for k, v in originals.items()})
    argv = ["platform", "--install-check", args.install_check]
    if args.ordinary:
        argv += ["--ordinary", args.ordinary]
    if args.operations:
        argv += ["--operations", args.operations]
    argv += ["--inputs", str(bundle.root / "inputs.json")]
    argv += ["--platform", str(out / "environment.json"), "--output", str(out / "platform.json")]
    return _load(KIT, "asset_operations_run_script").main(argv)


# --- verify ------------------------------------------------------------------------------


def _normal(value) -> object:
    return json.loads(json.dumps(value, default=str))


def verify_operations(path: Path) -> str | None:
    """None when the stored summary is exactly what the grader computes again
    and the run itself held: a report with harness errors (its command exited
    2) is ``harness_errors``, never ok."""
    try:
        report = json.loads(Path(path).read_bytes())
        if (report.get("qualification"), report.get("acceptance")) != (
            "fixture",
            "not_applicable",
        ):
            return "qualification"
        harness = report["harness_errors"]
        if not isinstance(harness, list):
            return "invalid_report"
        recomputed = summarize_operations(*load_operations_report(report))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return "invalid_report"
    if _normal(recomputed) != _normal(report["summary"]):
        return "summary_mismatch"
    if harness:
        return "harness_errors"
    return None


def verify_platform(directory: Path) -> str | None:
    """None when grade-platform accepts the report as a fixture (not_applicable).

    The grade goes to a temporary file: a committed run directory is bound by
    SHA-256 in its qualification record, so verify writes nothing into it.
    """
    from relaymind.evaluation.asset_trial_kit import real_path
    from relaymind.evaluation.asset_whole_task_cli import main as grade

    directory = Path(directory)
    with tempfile.TemporaryDirectory(prefix="relaymind-platform-grade-") as scratch:
        # The grader refuses a symlinked ancestor such as /var on macOS.
        output = real_path(Path(scratch) / "platform-grade.json")
        code = grade(
            [
                "grade-platform",
                "--report", str(directory / "platform.json"),
                "--inputs", str(directory / "bundle" / "inputs.json"),
                "--resolver", str(directory / "bundle" / "resolver.json"),
                "--output", str(output),
            ]
        )  # fmt: skip
        try:
            result = json.loads(output.read_bytes())
        except (OSError, ValueError):
            return f"grade_exit_{code}"
    if code != 0 or result.get("gate") != "not_applicable":
        return f"grade_exit_{code}_{result.get('reason')}"
    return _check_runner_files(directory) or _rederive_platform(directory)


def _check_runner_files(directory: Path) -> str | None:
    """The bound platform runner files must be this checkout's: the steps are
    derived again with this checkout's driver and ordinary check."""
    try:
        manifest = json.loads((directory / "bundle" / "inputs.json").read_bytes())
        registry = {e["id"]: e for e in manifest["evidence"]}
        entry = registry[PLATFORM_RUNNER_EVIDENCE]
        bound = (directory / "bundle" / entry["path"]).read_bytes()
    except (OSError, ValueError, KeyError, TypeError):
        return "platform_runner_files_missing"
    return None if bound == platform_runner_inventory() else "platform_runner_files_changed"


def _rederive_platform(directory: Path) -> str | None:
    """The steps again from the registered originals; the grader checks only their shape."""
    from relaymind.evaluation.asset_operations_run import platform_report

    try:
        report = json.loads((directory / "platform.json").read_bytes())
        manifest = json.loads((directory / "bundle" / "inputs.json").read_bytes())
        registry = {e["id"]: e for e in manifest["evidence"]}
        originals = {
            key: directory / "bundle" / registry[key]["path"]
            for key in ("install-check", "ordinary-capture", "operations-report")
            if key in registry
        }
        kit = _load(KIT, "asset_operations_run_script")
        ordinary = originals.get("ordinary-capture")
        expected = platform_report(
            install_check=originals["install-check"],
            ordinary=ordinary,
            ordinary_check=kit.ordinary_check() if ordinary else None,
            operations=originals.get("operations-report"),
            evidence_paths={k: registry[k]["path"] for k in originals},
            input_digest=report["input_digest"],
            input_manifest=report["input_manifest"],
            platform=report["platform"],
        )
    except (OSError, ValueError, KeyError, TypeError):
        return "platform_rederivation_failed"
    return None if _normal(expected) == _normal(report) else "steps_mismatch"


def _describe_operations(path: Path) -> str:
    report = json.loads(path.read_bytes())
    summary = report["summary"]
    startup, scheduled, batches = summary["startup"], summary["scheduled"], summary["batches"]
    gateway_increment = (report.get("memory_samples") or {}).get("increment_bytes")
    return (
        f"startups useful {startup['useful']}/10, scheduled useful "
        f"{scheduled['useful']}/120 (p95 service {scheduled['p95_observed_ms']} ms, "
        f"scheduled {scheduled['p95_scheduled_elapsed_ms']} ms), batches visible "
        f"{batches['visible']}/4, gateway-only memory increment {gateway_increment} bytes, "
        f"harness errors {report.get('harness_errors')}"
    )


def _describe_platform(directory: Path) -> str:
    report = json.loads((directory / "platform.json").read_bytes())
    steps = ", ".join(f"{s['name']}={s['status']}" for s in report["steps"])
    return f"{report['platform']['os']} {report['platform']['architecture']}: {steps}"


def _line(root: Path, path: Path, reason: str | None, detail: str) -> None:
    status = "ok" if reason is None else "FAIL"
    print(f"{path.relative_to(root).as_posix()}: {status} ({detail})")


def _verify(paths: Sequence[str]) -> int:
    found, failed = 0, 0
    for root in paths:
        root = Path(root)
        for path in sorted(root.rglob("operations.json")):
            found += 1
            reason = verify_operations(path)
            failed += reason is not None
            if reason is None:
                detail = _describe_operations(path)
            elif reason == "harness_errors":
                detail = f"{reason}: {_describe_operations(path)}"
            else:
                detail = reason
            _line(root, path, reason, detail)
        for path in sorted(root.rglob(NOT_BUILT)):
            found += 1
            failed += 1
            try:
                reason = json.loads(path.read_bytes()).get("reason") or "not_built"
            except (OSError, ValueError, AttributeError):
                reason = "not_built"
            _line(root, path, reason, reason)
        # A platform report and its bundle are one unit: either half alone is a
        # partial download or a failed run, and fails instead of being skipped.
        for inputs in sorted(root.rglob("bundle/inputs.json")):
            if not (inputs.parent.parent / "platform.json").exists():
                found += 1
                failed += 1
                _line(root, inputs, "platform_report_missing", "platform_report_missing")
        for path in sorted(root.rglob("platform.json")):
            found += 1
            if not (path.parent / "bundle" / "inputs.json").exists():
                failed += 1
                _line(root, path, "platform_bundle_missing", "platform_bundle_missing")
                continue
            reason = verify_platform(path.parent)
            failed += reason is not None
            detail = _describe_platform(path.parent) if reason is None else reason
            _line(root, path, reason, detail)
    if not found:
        print("no operations or platform reports found", file=sys.stderr)
    return 0 if found and not failed else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    ordinary = sub.add_parser("ordinary")
    ordinary.add_argument("--tarball", required=True)
    ordinary.add_argument("--work-dir", required=True)
    ordinary.add_argument("--out", required=True)
    ordinary.add_argument("--timeout", type=float, default=900)
    platform_ = sub.add_parser("platform")
    platform_.add_argument("--install-check", required=True)
    platform_.add_argument("--ordinary")
    platform_.add_argument("--operations")
    platform_.add_argument("--out-dir", required=True)
    verify = sub.add_parser("verify")
    verify.add_argument("paths", nargs="+", help="downloaded run directories")
    args = parser.parse_args(argv)
    try:
        if args.command == "ordinary":
            return _ordinary(args)
        if args.command == "platform":
            return _platform(args)
        return _verify(args.paths)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print(f"asset_platform_run: {type(error).__name__}: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
