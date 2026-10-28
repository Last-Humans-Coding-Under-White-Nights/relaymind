"""macOS qualification of the #138 abort and error mapping on the user's machine (RM33, AC24, spec section 12.5).

A target platform of section 1, qualified on the real pinned OpenClaw host
(integrations/openclaw/node_modules/openclaw, openclaw@2026.10.7, run as
`node .../openclaw/openclaw.mjs`), through the qualification harness's host
mode (scripts/openclaw_qualification.py host), which serves the real gateway
over scripted model doubles: the abort scenario (SIGINT to the host mid-turn,
then the gateway's run state) and the external-failed scenario (503
external_failed). No live model, no llama-server. Adapted from the Linux VPS
driver docs/evidence/2026-10-28-openclaw-vps-functional-138/scaffold/.

Host state is isolated in the scratch dir (OPENCLAW_HOME, OPENCLAW_STATE_DIR,
OPENCLAW_CONFIG_PATH, --config on agent exec, TMPDIR); the operator's
~/.openclaw is never touched. The host log goes to the scratch home
(logging.file), and gateway.port names a closed loopback port, so the host
never reaches an OpenClaw Gateway of the operator (scripts/openclaw_install_check.py).
No proxy variable, OPENCLAW_* or RELAYMIND_* setting of the operator's shell
reaches the host; the token comes from the harness only (RELAYMIND_APP_TOKEN).

Usage (from the repository root, the shared venv, Node on PATH, `npm ci` done
in integrations/openclaw):
    python docs/evidence/2026-10-29-openclaw-mac-qualification/scaffold/cancel_errors.py <scratch> \\
        [--tarball <release tgz>]

Without --tarball the provider is built and packed from the checkout (npm run
build, npm pack), as the VPS driver did; with it the given release artifact is
installed. Writes raw outputs under <scratch>/raw (holding host paths, never
the token) and a sanitized copy in ../cancel-errors/ (steps.json,
exports/<name>.json). Exits 0 when every step with an expected exit met it.
"""

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
PACKAGE = ROOT / "integrations" / "openclaw"
OPENCLAW = PACKAGE / "node_modules" / "openclaw" / "openclaw.mjs"
HARNESS = ROOT / "scripts" / "openclaw_qualification.py"
EVIDENCE = Path(__file__).resolve().parents[1]
OUT = EVIDENCE / "cancel-errors"
MODEL = "relaymind/relaymind-client-tools"

# (export name, harness scenario, prompt)
RUNS = (
    ("abort", "abort", "Say hello"),
    ("external-failed", "external-failed", "Say hello"),
)
# A field over this many JSON characters (the host's system prompt) is kept by size only.
TRIM = 2000
PROXY_VARIABLES = {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"}


def trim(value):
    if isinstance(value, dict):
        return {k: trim(v) for k, v in value.items()}
    if isinstance(value, list):
        return [trim(v) for v in value]
    if isinstance(value, str) and len(value) > TRIM:
        return {"omitted_chars": len(value)}
    return value


def closed_port() -> int:
    """A loopback port nothing listens on (bound, then released)."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def path_forms(path: Path) -> list[str]:
    """The spellings of a path a tool may print: as given, resolved, and ~/ relative."""
    forms = {str(path), str(path.resolve())}
    home = Path.home()
    for candidate in (path, path.resolve()):
        if candidate != home and candidate.is_relative_to(home):
            forms.add(f"~/{candidate.relative_to(home).as_posix()}")
    return sorted(forms, key=len, reverse=True)


def main(scratch: Path, tarball: Path | None) -> int:
    node = shutil.which("node")
    if node is None:
        print("node is not on PATH", file=sys.stderr)
        return 2
    if not OPENCLAW.is_file():
        print(f"no pinned host at {OPENCLAW} (npm ci in integrations/openclaw)", file=sys.stderr)
        return 2
    if tarball is not None and not tarball.is_file():
        print(f"no release artifact at {tarball}", file=sys.stderr)
        return 2
    raw, home, pack, tmp = (scratch / d for d in ("raw", "home", "pack", "tmp"))
    for d in (raw / "exports", home, pack, tmp):
        d.mkdir(parents=True, exist_ok=True)
    config = home / "config.json"
    env = {
        k: v
        for k, v in os.environ.items()
        if k.upper() not in PROXY_VARIABLES
        and not k.upper().startswith(("RELAYMIND_", "OPENCLAW_"))
    }
    env.update(
        OPENCLAW_HOME=str(home),
        OPENCLAW_STATE_DIR=str(home / "state"),
        OPENCLAW_CONFIG_PATH=str(config),
        NO_PROXY="127.0.0.1,localhost,::1",
        # The harness's sandbox goes under the scratch dir, so it can be sanitized.
        TMPDIR=str(tmp),
        PYTHONPATH=str(ROOT / "src"),
    )
    steps: list[dict] = []

    def run(name, argv, *, expect=None, timeout=300):
        started = time.monotonic()
        try:
            done = subprocess.run(
                [str(a) for a in argv], cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                capture_output=True, text=True, timeout=timeout,
            )
            code, out, err = done.returncode, done.stdout, done.stderr
        except subprocess.TimeoutExpired as e:
            code, out, err = None, str(e.stdout or ""), str(e.stderr or "") + "\ntimed out"
        except OSError as e:
            code, out, err = None, "", f"could not start: {e}"
        step = {
            "name": name,
            "argv": [str(a) for a in argv],
            "exit_code": code,
            "seconds": round(time.monotonic() - started, 2),
            "stdout": out[-20000:],
            "stderr": err[-20000:],
        }
        if expect is not None:
            step["expected_exit"] = expect
        steps.append(step)
        print(f"{name}: exit {code} ({step['seconds']} s)", flush=True)
        return step

    oc = [node, OPENCLAW]
    # 0. Identity of the machine and the host.
    run("sw_vers", ["sw_vers"], expect=0)
    run("node --version", [node, "--version"], expect=0)
    run("npm --version", ["npm", "--version"], expect=0)
    run("openclaw --version", oc + ["--version"], expect=0)
    # Isolation: the host log in the scratch home, and no OpenClaw Gateway reachable.
    run("config set logging.file", oc + ["config", "set", "logging.file", home / "openclaw.log"], expect=0)
    run("config set gateway.port", oc + ["config", "set", "gateway.port", str(closed_port())], expect=0)
    # 1. The provider (packed from the checkout, or the given release artifact);
    #    install and enable it (README forms).
    if tarball is None:
        run("npm run build", ["npm", "--prefix", PACKAGE, "run", "build"], expect=0)
        packed = run("npm pack", ["npm", "pack", PACKAGE, "--json", "--pack-destination", pack], expect=0)
        try:
            tgz = pack / json.loads(packed["stdout"])[0]["filename"]
        except (ValueError, LookupError, TypeError):
            print("npm pack gave no artifact; see the npm pack step", file=sys.stderr)
            (raw / "steps.json").write_text(json.dumps(steps, indent=2), "utf-8")
            return 1
    else:
        tgz = tarball
    run("artifact sha256", ["shasum", "-a", "256", tgz], expect=0)
    run("install provider", oc + ["plugins", "install", f"npm-pack:{tgz}", "--force", "--accept-capabilities"], expect=0)
    run("config set baseUrl", oc + ["config", "set", "plugins.entries.relaymind.config.baseUrl", "${RELAYMIND_BASE_URL}"], expect=0)
    run("enable provider", oc + ["plugins", "enable", "relaymind"], expect=0)
    # 2. The harness scenarios; the token comes from RELAYMIND_APP_TOKEN only.
    exports = {}
    for name, scenario, prompt in RUNS:
        command = [str(a) for a in oc] + ["agent", "exec", "--config", str(config), "--json", "--model", MODEL, prompt]
        out = raw / "exports" / f"{name}.json"
        run(
            f"harness {name}",
            [sys.executable, HARNESS, "host", "--scenario", scenario, "--timeout", "240", "--out", out, "--", *command],
            expect=0,
        )
        try:
            exports[name] = json.loads(out.read_text("utf-8"))
        except (OSError, ValueError) as e:
            print(f"harness {name}: no export ({e})", file=sys.stderr)

    (raw / "steps.json").write_text(json.dumps(steps, indent=2), "utf-8")
    replacements = [(form, "<tmp>") for form in path_forms(tmp)]
    replacements += [(form, "<scratch>") for form in path_forms(scratch)]
    replacements += [(form, "<repo>") for form in path_forms(ROOT)]
    replacements += [(str(Path.home()), "<home>"), (str(Path.home().resolve()), "<home>")]
    hostname = socket.gethostname()
    for name_form in sorted({hostname, hostname.split(".")[0]}, key=len, reverse=True):
        if len(name_form) >= 3:
            replacements.append((name_form, "<hostname>"))

    def clean(text: str) -> str:
        for old, new in replacements:
            text = text.replace(old, new)
        text = re.sub(r"(\\u001b|\x1b)\[[0-9;]*m", "", text)
        return re.sub(r"127\.0\.0\.1:\d+", "127.0.0.1:<port>", text)

    (OUT / "exports").mkdir(parents=True, exist_ok=True)
    for name, export in exports.items():
        for run_ in export.get("runs", []):
            for event in run_["events"]:
                event["payload"] = trim(event["payload"])
        text = clean(json.dumps(export, indent=2, ensure_ascii=False, default=str))
        (OUT / "exports" / f"{name}.json").write_text(text + "\n", "utf-8")
    (OUT / "steps.json").write_text(clean(json.dumps(steps, indent=2)) + "\n", "utf-8")
    missed = [s["name"] for s in steps if "expected_exit" in s and s["exit_code"] != s["expected_exit"]]
    if missed:
        print(f"steps that missed their expected exit: {', '.join(missed)}", file=sys.stderr)
    return 1 if missed else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("scratch", type=Path, help="an empty scratch directory")
    parser.add_argument("--tarball", type=Path, help="install this release artifact instead of packing")
    args = parser.parse_args()
    raise SystemExit(
        main(args.scratch.resolve(), args.tarball.resolve() if args.tarball else None)
    )
