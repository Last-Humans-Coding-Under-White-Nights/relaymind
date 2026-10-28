"""macOS qualification: the live AC24 turn on the user's machine (RM33, AC24, spec section 12.5).

A target platform of section 1, qualified on the real pinned OpenClaw host
(integrations/openclaw/node_modules/openclaw, openclaw@2026.10.7, run as
`node .../openclaw/openclaw.mjs`) against the real `relaymind serve` with the
pinned local model (llama-server build b11785, n_ctx 8192) and no external
model configured. The host reaches the gateway through the packaged plugin's
own resolved model (`resolveDynamicModel`), installed from the release
artifact; the earlier run on this Mac (docs/evidence/2026-10-27-openclaw-mac-qualification/,
Step 5) needed a config-row workaround because that hook did not exist yet.

One `agent exec "Say hello"` runs in each of the plugin's executionMode auto
and local_only. A host request is about 10,500 tokens, over the 8192 local
context, so the gateway is expected to answer 422 needs_external
(context_limit); the script records what happens and judges nothing about
the turns. After each turn it reads the plugin's attempt records
(diagnosticsPath) and fetches each named run's view and events from the
gateway (GET /relay/v1/runs/<run_id> and /relay/v1/runs/<run_id>/events,
Authorization: Bearer <application token>).

Host state is isolated in the scratch dir (OPENCLAW_HOME, OPENCLAW_STATE_DIR,
OPENCLAW_CONFIG_PATH, --config and a fresh --state-dir on agent exec, TMPDIR);
the operator's ~/.openclaw is never touched. The host log goes to the scratch
home (logging.file), and gateway.port names a closed loopback port, so the
host never looks for an OpenClaw Gateway of its own (OPENCLAW_GATEWAY_PORT
is not set; scripts/openclaw_install_check.py). The application token is
read from the line `relaymind serve` prints ("Application token: <token>"),
held in memory, and given to the host only on standard input
(`models auth paste-api-key`): never in an argument, never in the host's
environment, never in a file under the evidence folder. Only <scratch>/raw/serve.out
holds it.

Usage (from the repository root, the shared venv with `pip install -e .`,
Node on PATH, `npm ci` done in integrations/openclaw, the pinned llama-server
running per models/README.md, the port free, and <scratch> missing or empty):
    python docs/evidence/2026-10-29-openclaw-mac-qualification/scaffold/live_turn.py <scratch> \\
        --tarball <release tgz> [--llama-url http://127.0.0.1:8033] [--port 8080]

Writes raw outputs under <scratch>/raw and a sanitized copy in ../live/
(live-turn.json, steps.json, serve.out, relaymind.toml, diagnostics.jsonl,
llama-props.json). Exits 2 when a precondition fails (nothing written), else
0 when every step with an expected exit met it, else 1.
"""

import argparse
import hashlib
import http.client
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[4]
PACKAGE = ROOT / "integrations" / "openclaw"
OPENCLAW = PACKAGE / "node_modules" / "openclaw" / "openclaw.mjs"
RELAYMIND = ROOT / ".venv" / "bin" / "relaymind"
EXAMPLE = ROOT / "relaymind.example.toml"
EVIDENCE = Path(__file__).resolve().parents[1]
OUT = EVIDENCE / "live"
MODEL = "relaymind/relaymind-client-tools"
PROMPT = "Say hello"
MODES = ("auto", "local_only")
RUNTIME_BUILD = "b11785"
RUNTIME_CTX = 8192
SERVE_START_SECONDS = 300
SERVE_STOP_SECONDS = 60
# The launcher's lines (src/relaymind/launcher.py main, orchestrator/warmup.py summary).
SERVING_PREFIX = "Serving on "
TOKEN_PREFIX = "Application token: "
WARMUP_PREFIX = "Local warm-up finished"
# A SIGINT stop: uvicorn shuts down, then re-raises the signal, so Python ends
# with KeyboardInterrupt (-2); 0 if the signal is not re-raised.
CLEAN_STOP = [0, -signal.SIGINT]
NOT_RECORDED = "<not recorded>"
REDACTED = "<redacted>"
EVIDENCE_LINE = (
    "macOS 27.0.1 arm64, Apple M1 (user's machine), Node v26.12.1; live local model "
    "through the packaged plugin's resolved model, no external model configured: "
    "a target platform of section 1 (section 12.5)"
)
RUN_ID = re.compile(r"^run_[A-Za-z0-9_-]{1,120}$")
OUTPUT_TAIL = 4000
# A field over this many JSON characters (the host's system prompt) is kept by size only.
TRIM = 2000
PROXY_VARIABLES = {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"}

sys.path.insert(0, str(ROOT / "scripts"))


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


def listening(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(1)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def path_forms(path: Path) -> list[str]:
    """The spellings of a path a tool may print: as given, resolved, and ~/ relative."""
    forms = {str(path), str(path.resolve())}
    home = Path.home()
    for candidate in (path, path.resolve()):
        if candidate != home and candidate.is_relative_to(home):
            forms.add(f"~/{candidate.relative_to(home).as_posix()}")
    return sorted(forms, key=len, reverse=True)


def clean_environment() -> dict[str, str]:
    """This process's environment without proxies or any OPENCLAW_* / RELAYMIND_* setting."""
    env = {
        k: v
        for k, v in os.environ.items()
        if k.upper() not in PROXY_VARIABLES
        and not k.upper().startswith(("RELAYMIND_", "OPENCLAW_"))
    }
    env["NO_PROXY"] = "127.0.0.1,localhost,::1"
    return env


def http_get(host: str, port: int, path: str, headers=None, timeout: float = 30):
    """(status, body bytes) of one loopback GET, no proxy; (None, error text) if it fails."""
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request("GET", path, headers=headers or {})
        response = conn.getresponse()
        return response.status, response.read()
    except OSError as e:
        return None, f"{type(e).__name__}: {e}".encode()
    finally:
        conn.close()


def gateway_toml(example: str, scratch: Path, port: int, llama_url: str) -> tuple[str, dict]:
    """The example config with the store and sandbox under scratch, the port, and the runtime URL."""
    values = {
        ("server", "port"): str(port),
        ("store", "path"): json.dumps(str(scratch / "relaymind.sqlite3")),
        ("sandbox", "work_dir"): json.dumps(str(scratch / "sandbox")),
        ("local_model", "base_url"): json.dumps(llama_url),
    }
    table, seen, lines = None, set(), []
    for line in example.splitlines():
        if header := re.match(r"^\[([A-Za-z0-9_.]+)\]\s*(#.*)?$", line):
            table = header.group(1)
        key = re.match(r"^([A-Za-z0-9_]+)\s*=", line)
        if key and (table, key.group(1)) in values:
            seen.add((table, key.group(1)))
            comment = re.search(r"\s+#[^\"]*$", line)
            value = values[(table, key.group(1))]
            lines.append(f"{key.group(1)} = {value}{comment.group(0) if comment else ''}")
            continue
        lines.append(line)
    if missing := set(values) - seen:
        raise SystemExit(f"relaymind.example.toml has no {sorted(missing)}: update this script")
    text = "\n".join(lines) + "\n"
    data = tomllib.loads(text)
    if "external_model" in data or "local_model" not in data:
        raise SystemExit("the gateway config must keep [local_model] and have no [external_model]")
    return text, data


def sse_events(port: int, run_id: str, token: str, seconds: float = 60) -> tuple[list, str | None]:
    """The run's trace events from its SSE stream, which ends after the terminal event."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    events: list = []
    data: list[str] = []
    try:
        conn.request(
            "GET",
            f"/relay/v1/runs/{run_id}/events",
            headers={"Authorization": f"Bearer {token}", "Accept": "text/event-stream"},
        )
        response = conn.getresponse()
        if response.status != 200:
            return events, f"HTTP {response.status}: {response.read()[:2000].decode('utf-8', 'replace')}"
        deadline = time.monotonic() + seconds
        while True:
            if time.monotonic() > deadline:
                return events, f"stream still open after {seconds} s"
            line = response.readline()
            if not line:
                return events, None
            text = line.decode("utf-8").rstrip("\r\n")
            if text == "":
                if data:
                    event = json.loads("\n".join(data))
                    if isinstance(event, dict) and "payload" in event:
                        event["payload"] = trim(event["payload"])
                    events.append(event)
                    data = []
            elif text.startswith("data:"):
                data.append(text[5:].removeprefix(" "))
    except (OSError, ValueError) as e:
        return events, f"{type(e).__name__}: {e}"
    finally:
        conn.close()


def read_records(path: Path, offset: int) -> tuple[list, int]:
    """The attempt records appended to diagnosticsPath since offset, and the new offset."""
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return [], offset
    records = []
    for line in data[offset:].decode("utf-8", "replace").splitlines():
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except ValueError:
            records.append({"unparsed": line})
    return records, len(data)


def main(scratch: Path, tarball: Path, llama_url: str, port: int) -> int:
    # -- preconditions: nothing is written when one fails ---------------------
    node = shutil.which("node")
    for ok, why in (
        (node is not None, "node is not on PATH"),
        (OPENCLAW.is_file(), f"no pinned host at {OPENCLAW} (npm ci in integrations/openclaw)"),
        (RELAYMIND.is_file(), f"no {RELAYMIND} (pip install -e . in the .venv)"),
        (tarball.is_file(), f"no release artifact at {tarball} (npm run pack:release)"),
        (not listening(port), f"127.0.0.1:{port} is in use; stop it or pass another --port"),
        (
            not scratch.exists() or (scratch.is_dir() and not any(scratch.iterdir())),
            f"{scratch} must be a fresh scratch dir: missing or empty",
        ),
    ):
        if not ok:
            print(why, file=sys.stderr)
            return 2
    llama = urlsplit(llama_url)
    status, body = http_get(llama.hostname or "127.0.0.1", llama.port or 80, "/props", timeout=10)
    try:
        props = json.loads(body) if status == 200 else None
    except ValueError:
        props = None
    if not isinstance(props, dict):
        print(f"{llama_url}/props did not answer ({status}): start the pinned llama-server", file=sys.stderr)
        return 2
    build_info = props.get("build_info")
    n_ctx = (props.get("default_generation_settings") or {}).get("n_ctx")
    if not (isinstance(build_info, str) and build_info.startswith(RUNTIME_BUILD)) or n_ctx != RUNTIME_CTX:
        print(
            f"refusing: the runtime at {llama_url} has build_info {build_info!r} and n_ctx {n_ctx!r}; "
            f"the qualification needs build {RUNTIME_BUILD} with n_ctx {RUNTIME_CTX}",
            file=sys.stderr,
        )
        return 2

    import openclaw_qualification as qualification  # the shared venv (relaymind, tests helpers)

    raw, home, tmp = (scratch / d for d in ("raw", "home", "tmp"))
    for d in (raw, home, tmp):
        d.mkdir(parents=True, exist_ok=True)
    config = home / "config.json"
    diagnostics = scratch / "diagnostics.jsonl"
    toml_path = scratch / "relaymind.toml"
    toml_text, toml_data = gateway_toml(EXAMPLE.read_text("utf-8"), scratch, port, llama_url.rstrip("/"))
    toml_path.write_text(toml_text, "utf-8")
    gateway_port = closed_port()
    env = clean_environment()
    env.update(
        OPENCLAW_HOME=str(home),
        OPENCLAW_STATE_DIR=str(home / "state"),
        OPENCLAW_CONFIG_PATH=str(config),
        # The host's temp files go under the scratch dir, so they can be sanitized.
        TMPDIR=str(tmp),
    )
    secret: dict[str, str] = {}
    steps: list[dict] = []

    def scrub(text: str) -> str:
        token = secret.get("token")
        return text.replace(token, REDACTED) if token else text

    def run(name, argv, *, expect=None, timeout=300, stdin_text=None):
        started = time.monotonic()
        feed = {"input": stdin_text} if stdin_text is not None else {"stdin": subprocess.DEVNULL}
        try:
            done = subprocess.run(
                [str(a) for a in argv], cwd=ROOT, env=env, capture_output=True, text=True,
                timeout=timeout, **feed,
            )
            code, out, err = done.returncode, done.stdout, done.stderr
        except subprocess.TimeoutExpired as e:
            out = e.stdout.decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
            err = e.stderr.decode("utf-8", "replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
            code, err = None, err + "\ntimed out"
        except OSError as e:
            code, out, err = None, "", f"could not start: {e}"
        step = {
            "name": name,
            "argv": [scrub(str(a)) for a in argv],
            "exit_code": code,
            "seconds": round(time.monotonic() - started, 2),
            "stdout": scrub(out)[-20000:],
            "stderr": scrub(err)[-20000:],
        }
        if expect is not None:
            step["expected_exit"] = expect
        steps.append(step)
        print(f"{name}: exit {code} ({step['seconds']} s)", flush=True)
        return step

    oc = [node, OPENCLAW]
    # -- 0. identity -----------------------------------------------------------
    run("sw_vers", ["sw_vers"], expect=0)
    run("node --version", [node, "--version"], expect=0)
    run("npm --version", ["npm", "--version"], expect=0)
    version = run("openclaw --version", oc + ["--version"], expect=0)

    # -- 1. the gateway: relaymind serve with the pinned local model -----------
    serve_out = raw / "serve.out"
    serve_started = time.monotonic()
    with serve_out.open("wb") as log:
        serve = subprocess.Popen(
            [str(RELAYMIND), "serve", "--config", str(toml_path)],
            cwd=ROOT, env=clean_environment(), stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT,
        )
    turns: list[dict] = []
    serve_code = None
    try:
        deadline = time.monotonic() + SERVE_START_SECONDS
        while "token" not in secret:
            for line in serve_out.read_text("utf-8", "replace").splitlines():
                if line.startswith(TOKEN_PREFIX) and line[len(TOKEN_PREFIX):].strip():
                    secret["token"] = line[len(TOKEN_PREFIX):].strip()
            if "token" in secret:
                break
            if serve.poll() is not None or time.monotonic() > deadline:
                print(f"relaymind serve did not print its token; see {serve_out}", file=sys.stderr)
                return 1
            time.sleep(0.5)
        token = secret["token"]
        auth = {"Authorization": f"Bearer {token}"}
        while http_get("127.0.0.1", port, "/healthz", auth, timeout=2)[0] != 200:
            if serve.poll() is not None or time.monotonic() > deadline:
                print(f"the gateway did not answer /healthz; see {serve_out}", file=sys.stderr)
                return 1
            time.sleep(0.2)
        steps.append(
            {
                "name": "relaymind serve started",
                "argv": [str(RELAYMIND), "serve", "--config", str(toml_path)],
                "exit_code": None,
                "seconds": round(time.monotonic() - serve_started, 2),
                "stdout": "",
                "stderr": "",
            }
        )
        print(f"relaymind serve started ({steps[-1]['seconds']} s)", flush=True)

        # -- 2. the plugin in an isolated host home ---------------------------
        base_url = f"http://127.0.0.1:{port}/v1"
        run("config set logging.file", oc + ["config", "set", "logging.file", home / "openclaw.log"], expect=0)
        run("config set gateway.port", oc + ["config", "set", "gateway.port", str(gateway_port)], expect=0)
        run("install provider", oc + ["plugins", "install", f"npm-pack:{tarball}", "--force", "--accept-capabilities"], expect=0)
        run("config set baseUrl", oc + ["config", "set", "plugins.entries.relaymind.config.baseUrl", base_url], expect=0)
        run(
            "config set diagnosticsPath",
            oc + ["config", "set", "plugins.entries.relaymind.config.diagnosticsPath", diagnostics],
            expect=0,
        )
        run("enable provider", oc + ["plugins", "enable", "relaymind"], expect=0)
        run("inspect provider", oc + ["plugins", "inspect", "relaymind", "--runtime", "--json"], expect=0)
        run(
            "store token (stdin)",
            oc + ["models", "auth", "paste-api-key", "--provider", "relaymind"],
            expect=0,
            stdin_text=token + "\n",
        )
        run("models list --refresh", oc + ["models", "list", "--refresh", "--provider", "relaymind", "--json"], expect=0)

        # -- 3. one live turn per mode ----------------------------------------
        offset = 0
        for mode in MODES:
            run(
                f"config set executionMode {mode}",
                oc + ["config", "set", "plugins.entries.relaymind.config.executionMode", mode],
                expect=0,
            )
            sandbox = scratch / "sandboxes" / mode
            state = home / "agent-exec" / mode
            for d in (sandbox, state):
                d.mkdir(parents=True)
            step = run(
                f"agent exec {mode}",
                oc + [
                    "agent", "exec", "--config", config, "--json", "--cwd", sandbox,
                    "--state-dir", state, "--model", MODEL, PROMPT,
                ],
                timeout=600,
            )
            records, offset = read_records(diagnostics, offset)
            run_ids = list(
                dict.fromkeys(
                    r["runId"] for r in records
                    if isinstance(r, dict) and isinstance(r.get("runId"), str) and RUN_ID.match(r["runId"])
                )
            )
            runs = []
            for run_id in run_ids:
                status, body = http_get("127.0.0.1", port, f"/relay/v1/runs/{run_id}", auth)
                try:
                    view = json.loads(body) if status == 200 else None
                except ValueError:
                    view = None
                if view is None:
                    view = {"http_status": status, "body": body[:2000].decode("utf-8", "replace")}
                events, problem = sse_events(port, run_id, token)
                entry = {"run": view, "events": events}
                if problem:
                    entry["events_error"] = problem
                runs.append(entry)
            try:
                report = json.loads(step["stdout"].strip())
            except ValueError:
                report = None
            turns.append(
                {
                    "mode": mode,
                    "prompt": PROMPT,
                    "host_exit_code": step["exit_code"],
                    "host_stdout_tail": step["stdout"][-OUTPUT_TAIL:],
                    "host_stderr_tail": step["stderr"][-OUTPUT_TAIL:],
                    "host_report": report,
                    "attempt_records": records,
                    "runs": runs,
                }
            )

        # -- 4. removal --------------------------------------------------------
        run("disable provider", oc + ["plugins", "disable", "relaymind"], expect=0)
        run("uninstall provider", oc + ["plugins", "uninstall", "relaymind", "--force"], expect=0)
        run("config unset plugin entry", oc + ["config", "unset", "plugins.entries.relaymind"], expect=0)
        run("models auth logout", oc + ["models", "auth", "logout", "relaymind:manual", "--yes"], expect=0)
    finally:
        # -- 5. stop the gateway (SIGINT, as an operator's Ctrl-C) -------------
        stopped = time.monotonic()
        note = ""
        if serve.poll() is None:
            serve.send_signal(signal.SIGINT)
            try:
                serve.wait(timeout=SERVE_STOP_SECONDS)
            except subprocess.TimeoutExpired:
                serve.kill()
                serve.wait()
                note = f"no exit {SERVE_STOP_SECONDS} s after SIGINT; killed"
        else:
            note = "relaymind serve had already exited"
        serve_code = serve.returncode if not note.startswith("no exit") else None
        if "token" in secret:
            steps.append(
                {
                    "name": "stop relaymind serve (SIGINT)",
                    "argv": ["<SIGINT to relaymind serve>"],
                    "exit_code": serve_code,
                    "seconds": round(time.monotonic() - stopped, 2),
                    "stdout": "",
                    "stderr": note,
                    "expected_exit": CLEAN_STOP,
                }
            )
            print(f"stop relaymind serve: exit {serve_code}", flush=True)

    # -- 6. the export ---------------------------------------------------------
    token = secret["token"]
    serve_text = serve_out.read_text("utf-8", "replace")
    serve_lines = serve_text.splitlines()
    tgz = tarball.read_bytes()
    export = {
        "kind": "openclaw-live-turn",
        "evidence": EVIDENCE_LINE,
        "runtime": {
            "build_info": build_info,
            "n_ctx": n_ctx,
            "model_path": props.get("model_path"),
            "manifest": toml_data["local_model"].get("manifest"),
        },
        "gateway": {
            "config_path": str(toml_path),
            "port": port,
            "serve_exit_code": serve_code,
            "warm_up_line": next((ln for ln in serve_lines if ln.startswith(WARMUP_PREFIX)), None),
            "serving_line": next((ln for ln in serve_lines if ln.startswith(SERVING_PREFIX)), None),
        },
        "host_version": version["stdout"].strip() or None,
        "artifact": {
            "filename": tarball.name,
            "bytes": len(tgz),
            "sha256": hashlib.sha256(tgz).hexdigest(),
        },
        "isolation": {
            **{k: env[k] for k in ("OPENCLAW_HOME", "OPENCLAW_STATE_DIR", "OPENCLAW_CONFIG_PATH")},
            "gateway_port": gateway_port,
        },
        "token": NOT_RECORDED,
        "steps": steps,
        "turns": turns,
        "versions": qualification.versions(node),
    }
    (raw / "steps.json").write_text(json.dumps(steps, indent=2), "utf-8")
    (raw / "live-turn.json").write_text(json.dumps(export, indent=2, ensure_ascii=False, default=str), "utf-8")

    replacements = [(token, REDACTED)]
    replacements += [(form, "<tmp>") for form in path_forms(tmp)]
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

    OUT.mkdir(parents=True, exist_ok=True)
    try:
        diagnostics_text = diagnostics.read_text("utf-8", "replace")
    except FileNotFoundError:
        diagnostics_text = ""
    written = {
        "live-turn.json": json.dumps(export, indent=2, ensure_ascii=False, default=str) + "\n",
        "steps.json": json.dumps(steps, indent=2) + "\n",
        "serve.out": serve_text,
        "relaymind.toml": toml_text,
        "diagnostics.jsonl": diagnostics_text,
        "llama-props.json": json.dumps(props, indent=2, ensure_ascii=False) + "\n",
    }
    for name, text in written.items():
        (OUT / name).write_text(clean(text), "utf-8")
    # The leak scan: the token is in no file under the evidence folder, nor in
    # the raw files other than serve.out.
    leaked = [
        str(p) for p in [*OUT.rglob("*"), raw / "steps.json", raw / "live-turn.json"]
        if p.is_file() and token.encode() in p.read_bytes()
    ]
    if leaked:
        raise RuntimeError(f"the application token reached: {', '.join(leaked)}")

    def met(step: dict) -> bool:
        expected = step["expected_exit"]
        return step["exit_code"] in expected if isinstance(expected, list) else step["exit_code"] == expected

    missed = [s["name"] for s in steps if "expected_exit" in s and not met(s)]
    if missed:
        print(f"steps that missed their expected exit: {', '.join(missed)}", file=sys.stderr)
    return 1 if missed else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("scratch", type=Path, help="an empty scratch directory")
    parser.add_argument("--tarball", type=Path, required=True, help="the release artifact")
    parser.add_argument("--llama-url", default="http://127.0.0.1:8033", help="the pinned llama-server")
    parser.add_argument("--port", type=int, default=8080, help="the gateway's loopback port")
    args = parser.parse_args()
    raise SystemExit(main(args.scratch.resolve(), args.tarball.resolve(), args.llama_url, args.port))
