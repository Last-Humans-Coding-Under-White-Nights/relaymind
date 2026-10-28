"""Linux VPS functional check of the #136 provider package (not a supported-platform qualification).

Drives the pinned real OpenClaw host (integrations/openclaw/node_modules,
openclaw@2026.10.7) against the real gateway over scripted model doubles
(tests/ helpers), through a recording loopback proxy. Host state is isolated
in a temp dir (OPENCLAW_HOME, OPENCLAW_STATE_DIR, OPENCLAW_CONFIG_PATH, and
--config on agent exec). No live model, no llama-server.

Usage (from the repository root, the shared venv, Node on PATH):
    python docs/evidence/2026-10-28-openclaw-vps-functional-136/scaffold/vps_functional.py <scratch>

Writes raw outputs under <scratch>/raw (holding host paths, never the token)
and a sanitized export next to this folder (steps.json, wire.json,
gateway-runs.json, state-checks.json).
"""

import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from api_helpers import make_app, token_of  # noqa: E402
from gateway_helpers import build_gateway, text_turn  # noqa: E402
from relaymind.api.chat import ClientToolsChat  # noqa: E402
from relaymind.api.server import build_server  # noqa: E402

PACKAGE = ROOT / "integrations" / "openclaw"
OPENCLAW = PACKAGE / "node_modules" / ".bin" / "openclaw"
EVIDENCE = Path(__file__).resolve().parents[1]
MODEL = "relaymind/relaymind-client-tools"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Gateway:
    """One gateway process lifetime: a fresh token, the same port across restarts."""

    def __init__(self, work: Path, port: int, turns: list):
        self.gateway = build_gateway(work, turns, external_turns=[])
        self.app = make_app(self.gateway, client_tools=ClientToolsChat(self.gateway.orchestrator))
        self.token = token_of(self.app)
        self.server = build_server(self.app, host="127.0.0.1", port=port)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started:
            assert time.monotonic() < deadline, "the gateway did not start"
            time.sleep(0.01)
        return self

    def runs(self) -> list:
        ids = [r[0] for r in self.gateway.store._conn.execute("SELECT run_id FROM runs")]
        return [
            {
                "run_id": run_id,
                "state": self.gateway.store.get_run(run_id).state,
                "events": [
                    {"type": e.event_type.value, "payload": e.payload}
                    for e in self.gateway.store.events(run_id)
                ],
            }
            for run_id in ids
        ]

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(timeout=10)
        self.gateway.store.close()


def trim_payloads(gateway_runs: dict) -> dict:
    """Keep each event's small fields; a field over 2000 characters (the host's prompt) by size only."""
    for runs in gateway_runs.values():
        for run in runs:
            for event in run["events"]:
                payload = event["payload"]
                if not isinstance(payload, dict):
                    continue
                for key, value in list(payload.items()):
                    size = len(json.dumps(value, default=str))
                    if size > 2000:
                        payload[key] = {"omitted_json_chars": size}
    return gateway_runs


def summarize_wire(path: Path) -> None:
    """Keep the request shape, not the host's prompt text: roles and sizes per message."""
    exchanges = json.loads(path.read_text("utf-8"))
    for exchange in exchanges:
        body = exchange["request"]["body"]
        if isinstance(body, dict) and "messages" in body:
            body["messages"] = [
                {
                    "role": m.get("role"),
                    "keys": sorted(m),
                    "content_chars": len(m["content"]) if isinstance(m.get("content"), str) else None,
                }
                for m in body["messages"]
            ]
            body["tools"] = [
                {"keys": sorted(t), "function_keys": sorted(t["function"]), "name": t["function"]["name"]}
                for t in body.get("tools", [])
            ]
            exchange["request"]["body_summarized"] = True
    path.write_text(json.dumps(exchanges, indent=2), "utf-8")


def main(scratch: Path) -> int:
    raw = scratch / "raw"
    home = scratch / "home"
    for d in (raw, home, scratch / "gw1", scratch / "gw2", scratch / "pack"):
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
    )
    steps: list[dict] = []
    tokens: list[str] = []
    proxy_out = raw / "wire.json"

    def wire_count() -> int:
        time.sleep(0.5)  # the proxy flushes every 200 ms
        try:
            return len(json.loads(proxy_out.read_text("utf-8")))
        except (OSError, ValueError):
            return 0

    def run(name, argv, *, stdin=None, cwd=PACKAGE, expect=None, timeout=180):
        wire_seen = wire_count()
        started = time.monotonic()
        try:
            done = subprocess.run(
                argv,
                cwd=cwd,
                env=env,
                input=stdin,
                stdin=None if stdin else subprocess.DEVNULL,
                capture_output=True, text=True, timeout=timeout
            )
            code, out, err = done.returncode, done.stdout, done.stderr
        except subprocess.TimeoutExpired as e:
            code, out, err = None, str(e.stdout or ""), str(e.stderr or "") + "\ntimed out"
        step = {
            "name": name,
            "wire_exchanges": [wire_seen, wire_count()],
            "argv": [str(a) for a in argv],
            "stdin": "<the gateway token>" if stdin else None,
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

    oc = [str(OPENCLAW)]
    # 1. Pack.
    run("npm run build", ["npm", "run", "build"])
    pack = run("npm pack", ["npm", "pack", "--json", "--pack-destination", str(scratch / "pack")])
    tgz = scratch / "pack" / json.loads(pack["stdout"])[0]["filename"]
    gw_port, proxy_port = free_port(), free_port()
    proxy = subprocess.Popen(
        [
            "node",
            str(EVIDENCE / "scaffold" / "record_proxy.mjs"),
            f"http://127.0.0.1:{gw_port}",
            str(proxy_port),
            str(proxy_out),
        ],
        env=env,
        stdout=subprocess.PIPE,
        text=True,
    )
    proxy.stdout.readline()
    base_url = f"http://127.0.0.1:{proxy_port}/v1"
    gateway_runs: dict[str, list] = {}
    state_checks: dict[str, object] = {}

    def token_in_state_db() -> bool | None:
        found = None
        for db in sorted(home.rglob("openclaw.sqlite")):
            try:
                with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
                    tables = [
                        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
                    ]
                    for table in tables:
                        rows = conn.execute(f'SELECT * FROM "{table}"').fetchall()
                        blob = json.dumps(rows, default=str)
                        found = bool(found) or any(t in blob for t in tokens)
            except sqlite3.DatabaseError as error:
                print(f"{db}: {error}", flush=True)
        return found

    def files_holding_token() -> list[str]:
        """Host files (relative to the isolated home) whose bytes hold a gateway token."""
        held = []
        for f in sorted(home.rglob("*")):
            if f.is_file():
                data = f.read_bytes()
                if any(t.encode() in data for t in tokens):
                    held.append(str(f.relative_to(home)))
        return held

    try:
        # 2. Install, configure, enable, inspect.
        run("install without --accept-capabilities", oc + ["plugins", "install", f"npm-pack:{tgz}", "--force"], expect=1)
        run("install", oc + ["plugins", "install", f"npm-pack:{tgz}", "--force", "--accept-capabilities"], expect=0)
        run("config set baseUrl", oc + ["config", "set", "plugins.entries.relaymind.config.baseUrl", base_url], expect=0)
        run("inspect before enable", oc + ["plugins", "inspect", "relaymind", "--runtime", "--json"])
        run("enable", oc + ["plugins", "enable", "relaymind"], expect=0)
        run("inspect", oc + ["plugins", "inspect", "relaymind", "--runtime", "--json"], expect=0)
        run(
            "config set executionMode parallel_demo",
            oc + ["config", "set", "plugins.entries.relaymind.config.executionMode", "parallel_demo"],
            expect=1,
        )
        with Gateway(scratch / "gw1", gw_port, [text_turn("Hello from RelayMind.")] * 4) as g1:
            tokens.append(g1.token)
            run("models list before token", oc + ["models", "list", "--refresh", "--json"])
            run(
                "paste-api-key",
                oc + ["models", "auth", "paste-api-key", "--provider", "relaymind"],
                stdin=g1.token + "\n",
                expect=0,
            )
            state_checks["after_paste"] = {"token_in_state_db": token_in_state_db(), "files_holding_token": files_holding_token()}
            run("models list without --refresh", oc + ["models", "list", "--json"])
            # Before any catalog refresh: the cached catalog has no relaymind row, so
            # the host can find the model only through resolveDynamicModel (Q7).
            run(
                "agent exec text before any catalog refresh",
                oc + ["agent", "exec", "--config", str(config), "--json", "--cwd", str(scratch),
                      "--model", MODEL, "Say hello"],
                expect=0,
            )
            run("models list --refresh", oc + ["models", "list", "--refresh", "--json"], expect=0)
            run(
                "config set limits",
                oc + ["config", "set", "plugins.entries.relaymind.config.limits",
                      '{"externalContextLimit":128000,"maxCompletionTokens":512}', "--strict-json"],
                expect=0,
            )
            run("models list --refresh with limits", oc + ["models", "list", "--refresh", "--json"], expect=0)
            run(
                "agent exec text with limits",
                oc + ["agent", "exec", "--config", str(config), "--json", "--cwd", str(scratch),
                      "--model", MODEL, "Say hello"],
                expect=0,
            )
            gateway_runs["gateway1"] = g1.runs()
        # 3. Restart: a new token on the same port; the stored one is stale.
        with Gateway(
            scratch / "gw2", gw_port, [text_turn("Hello again after the restart.")] * 4
        ) as g2:
            tokens.append(g2.token)
            run("models list --refresh with the stale token", oc + ["models", "list", "--refresh", "--json"])
            run(
                "agent exec with the stale token",
                oc + ["agent", "exec", "--config", str(config), "--json", "--cwd", str(scratch),
                      "--model", MODEL, "Say hello"],
            )
            run(
                "paste-api-key (new token)",
                oc + ["models", "auth", "paste-api-key", "--provider", "relaymind"],
                stdin=g2.token + "\n",
                expect=0,
            )
            run("models list --refresh after recovery", oc + ["models", "list", "--refresh", "--json"], expect=0)
            run(
                "agent exec after recovery",
                oc + ["agent", "exec", "--config", str(config), "--json", "--cwd", str(scratch),
                      "--model", MODEL, "Say hello"],
                expect=0,
            )
            gateway_runs["gateway2"] = g2.runs()
        # 4. Removal and what it leaves behind (Q12).
        run("disable", oc + ["plugins", "disable", "relaymind"], expect=0)
        run("uninstall without --force", oc + ["plugins", "uninstall", "relaymind"], expect=1)
        run("uninstall --force", oc + ["plugins", "uninstall", "relaymind", "--force"], expect=0)
        cfg = json.loads(config.read_text("utf-8"))
        state_checks["after_uninstall"] = {
            "plugins.entries.relaymind": cfg.get("plugins", {}).get("entries", {}).get("relaymind"),
            "auth.profiles": sorted(cfg.get("auth", {}).get("profiles", {})),
            "token_in_state_db": token_in_state_db(), "files_holding_token": files_holding_token(),
        }
        run("models auth list", oc + ["models", "auth", "list"])
        run("models auth logout", oc + ["models", "auth", "logout", "relaymind:manual", "--yes"], expect=0)
        run("config unset plugins.entries.relaymind", oc + ["config", "unset", "plugins.entries.relaymind"], expect=0)
        cfg = json.loads(config.read_text("utf-8"))
        state_checks["after_logout_and_unset"] = {
            "plugins.entries.relaymind": cfg.get("plugins", {}).get("entries", {}).get("relaymind"),
            "auth.profiles": sorted(cfg.get("auth", {}).get("profiles", {})),
            "token_in_state_db": token_in_state_db(), "files_holding_token": files_holding_token(),
        }
    finally:
        proxy.terminate()
        proxy.wait(timeout=10)

    (raw / "steps.json").write_text(json.dumps(steps, indent=2), "utf-8")
    (raw / "gateway-runs.json").write_text(
        json.dumps(trim_payloads(gateway_runs), indent=2, default=str), "utf-8"
    )
    (raw / "state-checks.json").write_text(json.dumps(state_checks, indent=2), "utf-8")

    # Sanitize into the evidence folder.
    summarize_wire(proxy_out)
    replacements = [(t, "<token>") for t in tokens] + [
        (socket.gethostname(), "<hostname>"),
        (str(scratch), "<scratch>"),
        (str(ROOT), "<repo>"),
        (str(Path.home()), "<home>"),
        (f"127.0.0.1:{gw_port}", "127.0.0.1:<gateway-port>"),
        (f"127.0.0.1:{proxy_port}", "127.0.0.1:<port>"),
    ]
    for name in ("steps.json", "wire.json", "gateway-runs.json", "state-checks.json"):
        text = (raw / name).read_text("utf-8")
        for old, new in replacements:
            text = text.replace(old, new)
        text = re.sub(r"(\\u001b|\x1b)\[[0-9;]*m", "", text)
        for t in tokens:
            assert t not in text, f"the token reached {name}"
        (EVIDENCE / name).write_text(text if text.endswith("\n") else text + "\n", "utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1]).resolve()))
