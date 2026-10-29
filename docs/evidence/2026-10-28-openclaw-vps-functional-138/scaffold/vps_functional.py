"""Linux VPS functional check of the #138 abort and error mapping (not a supported-platform qualification).

Drives the pinned real OpenClaw host (integrations/openclaw/node_modules,
openclaw@2026.10.7) through the qualification harness's host mode
(scripts/openclaw_qualification.py host), which serves the real gateway over
scripted model doubles: the abort scenario (SIGINT to the host mid-turn,
then the gateway's run state) and the external-failed scenario (503
external_failed). Host state is isolated in a temp dir (OPENCLAW_HOME,
OPENCLAW_STATE_DIR, OPENCLAW_CONFIG_PATH, and --config on agent exec). No live
model, no llama-server.

Usage (from the repository root, the shared venv, Node on PATH):
    python docs/evidence/2026-10-28-openclaw-vps-functional-138/scaffold/vps_functional.py <scratch>

Writes raw outputs under <scratch>/raw (holding host paths, never the token)
and a sanitized copy next to this folder (steps.json, exports/<scenario>.json).
"""

import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
PACKAGE = ROOT / "integrations" / "openclaw"
OPENCLAW = PACKAGE / "node_modules" / ".bin" / "openclaw"
HARNESS = ROOT / "scripts" / "openclaw_qualification.py"
EVIDENCE = Path(__file__).resolve().parents[1]
MODEL = "relaymind/relaymind-client-tools"

# (export name, harness scenario, prompt)
RUNS = (
    ("abort", "abort", "Say hello"),
    ("external-failed", "external-failed", "Say hello"),
)
# A field over this many JSON characters (the host's system prompt) is kept by size only.
TRIM = 2000


def trim(value):
    if isinstance(value, dict):
        return {k: trim(v) for k, v in value.items()}
    if isinstance(value, list):
        return [trim(v) for v in value]
    if isinstance(value, str) and len(value) > TRIM:
        return {"omitted_chars": len(value)}
    return value


def main(scratch: Path) -> int:
    raw, home, pack, tmp = (scratch / d for d in ("raw", "home", "pack", "tmp"))
    for d in (raw / "exports", home, pack, tmp):
        d.mkdir(parents=True, exist_ok=True)
    config = home / "config.json"
    env = {
        k: v
        for k, v in os.environ.items()
        if k.upper() not in {"HTTP_PROXY", "HTTPS_PROXY", "RELAYMIND_APP_TOKEN"}
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

    oc = [OPENCLAW]
    # 1. Pack the provider, install and enable it (README forms).
    run("openclaw --version", oc + ["--version"], expect=0)
    run("npm run build", ["npm", "--prefix", PACKAGE, "run", "build"], expect=0)
    packed = run("npm pack", ["npm", "pack", PACKAGE, "--json", "--pack-destination", pack], expect=0)
    tgz = pack / json.loads(packed["stdout"])[0]["filename"]
    run("install provider", oc + ["plugins", "install", f"npm-pack:{tgz}", "--force", "--accept-capabilities"], expect=0)
    run("config set baseUrl", oc + ["config", "set", "plugins.entries.relaymind.config.baseUrl", "${RELAYMIND_BASE_URL}"], expect=0)
    run("enable provider", oc + ["plugins", "enable", "relaymind"], expect=0)
    # 2. The harness scenarios; the token comes from RELAYMIND_APP_TOKEN only.
    exports = {}
    for name, scenario, prompt in RUNS:
        command = [str(OPENCLAW), "agent", "exec", "--config", str(config), "--json", "--model", MODEL, prompt]
        out = raw / "exports" / f"{name}.json"
        run(
            f"harness {name}",
            [sys.executable, HARNESS, "host", "--scenario", scenario, "--timeout", "240", "--out", out, "--", *command],
            expect=0,
        )
        exports[name] = json.loads(out.read_text("utf-8"))

    (raw / "steps.json").write_text(json.dumps(steps, indent=2), "utf-8")
    replacements = [
        (str(tmp), "<tmp>"),
        (str(scratch), "<scratch>"),
        (str(ROOT), "<repo>"),
        (str(Path.home()), "<home>"),
        (socket.gethostname(), "<hostname>"),
    ]

    def clean(text: str) -> str:
        for old, new in replacements:
            text = text.replace(old, new)
        text = re.sub(r"(\\u001b|\x1b)\[[0-9;]*m", "", text)
        return re.sub(r"127\.0\.0\.1:\d+", "127.0.0.1:<port>", text)

    (EVIDENCE / "exports").mkdir(exist_ok=True)
    for name, export in exports.items():
        for run_ in export["runs"]:
            for event in run_["events"]:
                event["payload"] = trim(event["payload"])
        text = clean(json.dumps(export, indent=2, ensure_ascii=False, default=str))
        (EVIDENCE / "exports" / f"{name}.json").write_text(text + "\n", "utf-8")
    (EVIDENCE / "steps.json").write_text(clean(json.dumps(steps, indent=2)) + "\n", "utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1]).resolve()))
