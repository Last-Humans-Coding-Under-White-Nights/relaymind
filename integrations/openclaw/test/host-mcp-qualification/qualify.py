"""Disposable pinned-host MCP qualification (#190; RM08/RM09/RM12/RM16/RM33, AC24)."""

import json
import os
import socket
import tempfile
import time
from pathlib import Path


def roundtrip_pass(record: dict) -> bool:
    """Require independent actual-use evidence; names and listing never suffice (RM16)."""
    return (
        record.get("settled") is True
        and record.get("gateway_executions") == 0
        and record.get("answer") == record.get("marker")
        and record.get("marker") in record.get("tool_results", [])
        and record.get("tool") in record.get("offered", [])
        and record.get("calls")
        == [
            {
                "server": record.get("server"),
                "tool": "echo",
                "arguments": record.get("arguments"),
                "result": record.get("marker"),
            }
        ]
        and record.get("proposals")
        == [
            {
                "name": record.get("tool"),
                "arguments": record.get("arguments"),
            }
        ]
        and any(
            item.get("source") == "mcp"
            and item.get("exposed_name") == record.get("tool")
            and (item.get("mcp") or {}).get("serverName") == record.get("server")
            and (item.get("mcp") or {}).get("toolName") == "echo"
            for item in record.get("observations", [])
        )
    )


def write_export(out: Path, exported: dict, credentials: list[str]) -> None:
    """Refuse contaminated evidence before persisting it (RM12)."""
    serialized = json.dumps(exported, indent=2, ensure_ascii=False)
    if any(secret and secret in serialized for secret in credentials):
        raise ValueError("credential reached qualification export")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(serialized + "\n", encoding="utf-8")


def write_json_atomic(path: Path, value: dict) -> None:
    """Replace a control file in one step; a reader never sees it truncated (RM16)."""
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as stream:
        json.dump(value, stream)
    os.replace(stream.name, path)


def wait_for_listener(process, port: int, *, timeout: float = 90) -> None:
    """Fail at startup if the host exits or never listens, not at a later RPC (RM16)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("isolated host failed startup; see gateway-console.log")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                return
        except OSError:
            time.sleep(0.2)
    raise TimeoutError(f"isolated host did not start listening within {timeout:g} seconds")


def fixture_modules(directory: Path) -> list[Path]:
    """The fixture's plugin and server modules, in a filesystem-independent order."""
    return sorted(directory.glob("*.mjs"))


def result_answer(marker: str, messages: list[dict], *, issued: bool) -> str:
    """The double consumes only the current call's last result, never historical success."""
    if issued and messages and messages[-1]["role"] == "tool":
        if marker in (messages[-1]["content"] or ""):
            return marker
    return "tool unavailable"


def run(work: Path, out: Path) -> dict:
    """Use only host-managed MCP connections, with a real gateway and model doubles."""
    import hashlib
    import secrets
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[4]
    sys.path[:0] = [str(root / "scripts"), str(root / "tests"), str(root / "src")]
    import openclaw_install_check as install
    import openclaw_qualification as qualification

    from api_helpers import RunningServer, make_app, token_of
    from gateway_helpers import build_gateway, text_turn, tool_turn
    from relaymind.api.chat import ClientToolsChat

    package = root / "integrations/openclaw"
    fixture = Path(__file__).parent
    host_root = package / "node_modules/openclaw"
    host = ["node", str(host_root / "openclaw.mjs")]
    work.mkdir(parents=True, exist_ok=False)
    for directory in ("home", "state", "workspace"):
        (work / directory).mkdir()
    env = install.host_environment(work)
    env["HOME"] = str(work / "home")
    mcp_secret, host_secret = secrets.token_hex(32), secrets.token_hex(32)
    capture_path, events_path = work / "captures.jsonl", work / "mcp-events.jsonl"
    session = "agent:main:mcp-qualification"
    controls = {}
    for server, marker in (("alpha.one", "marker-alpha"), ("alpha?one", "marker-beta")):
        path = work / f"server-{len(controls)}.json"
        write_json_atomic(path, {"server": server, "marker": marker, "revision": 1})
        controls[server] = path
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    commands, exchanges = [], []
    mode = {"server": None, "pending": False, "issued": False, "marker": None, "arguments": {}}

    def jsonl(path):
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def on_generate(request):
        observed = [tool for capture in jsonl(capture_path) for tool in capture["tools"]]
        matching = [
            tool
            for tool in observed
            if tool.get("mcp", {})
            and tool["mcp"]["serverName"] == mode["server"]
            and tool["mcp"]["toolName"] == "echo"
            and tool["exposed_name"] in {t.name for t in request.tool_schemas}
        ]
        if mode["pending"] and matching:
            mode["pending"] = False
            mode["issued"] = True
            turn = tool_turn(matching[-1]["exposed_name"], **mode["arguments"])
        else:
            turn = text_turn(
                result_answer(
                    mode["marker"],
                    [{"role": m.role.value, "content": m.content} for m in request.messages],
                    issued=mode["issued"],
                )
            )
        gateway.adapter._turns.append(turn)

    gateway = build_gateway(work / "gateway", [], external_turns=[], on_generate=on_generate)
    app = make_app(gateway, client_tools=ClientToolsChat(gateway.orchestrator))
    process, log_file = None, None
    import platform

    exported = {
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
        "host_configuration": {
            "runtime": "embedded Gateway with the existing RelayMind provider",
            "initial_tools": {"profile": "coding", "toolSearch": False},
            "probe_conversation_access": True,
            "server_names": list(controls),
            "transport": "stdio",
            "credential_fields": ["RELAYMIND_MCP_TEST_CREDENTIAL"],
        },
        "kind": "openclaw-mcp-qualification",
        "requirements": ["RM08", "RM09", "RM12", "RM16", "RM33", "AC24"],
        "evidence": (
            "real embedded Gateway host + real RelayMind gateway + "
            "deterministic model doubles; no live model"
        ),
        "session": session,
        "integration_decision": {
            "same_instance_schema_provenance": "not_run",
            "current_availability_revision": "unsupported",
            "session_turn_binding": (
                "not_qualified: schema hook has no authoritative session/turn field"
            ),
            "required_host_change": (
                "supported immutable tool snapshot with authentic schema/provenance, "
                "session/requester/turn binding and a use-time revision/availability guard"
            ),
            "approvals": "not_run: read-only fixture; #189 headless refusal is separate evidence",
        },
    }

    def call(args, *, stdin=None):
        result = subprocess.run(
            host + args, env=env, input=stdin, text=True, capture_output=True, timeout=90
        )
        record = {
            "args": args,
            "exit_code": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
        commands.append(record)
        return record

    def rpc(method, params):
        return call(["gateway", "call", method, "--params", json.dumps(params), "--json"])

    def parsed(record):
        try:
            return json.loads(record["stdout"])
        except json.JSONDecodeError:
            return {}

    def send(name, server=None, arguments=None):
        mode.update(
            server=server,
            marker="marker-alpha" if server == "alpha.one" else "marker-beta",
            pending=server is not None,
            issued=False,
            arguments=arguments or {},
        )
        starts = {
            "requests": len(gateway.adapter.requests),
            "captures": len(jsonl(capture_path)),
            "events": len(jsonl(events_path)),
            "runs": len(qualification._run_ids(gateway)),
        }
        response = rpc(
            "chat.send",
            {
                "sessionKey": session,
                "message": f"Sandbox qualification {name}",
                "idempotencyKey": secrets.token_hex(12),
            },
        )
        settled, history = False, {}
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            history = parsed(rpc("chat.history", {"sessionKey": session, "limit": 100}))
            if history.get("sessionInfo", {}).get("hasActiveRun") is False:
                settled = True
                break
            time.sleep(0.2)
        requests = gateway.adapter.requests[starts["requests"] :]
        runs = qualification.run_records(gateway, qualification._run_ids(gateway)[starts["runs"] :])
        snapshots = jsonl(capture_path)[starts["captures"] :]
        observations = [t for s in snapshots for t in s["tools"]]
        matching = [t for t in observations if t.get("mcp") and t["mcp"]["serverName"] == server]
        tool_name = matching[-1]["exposed_name"] if matching else None
        messages = history.get("messages", [])
        final = next((m for m in reversed(messages) if m.get("role") == "assistant"), {})

        def text(m):
            return "".join(
                c.get("text", "") for c in m.get("content", []) if c.get("type") == "text"
            )

        record = {
            "name": name,
            "response": response,
            "history": history,
            "runs": runs,
            "capture_window": [starts["captures"], len(jsonl(capture_path))],
            "inventory": rpc("tools.effective", {"sessionKey": session}),
            "model_requests": [
                {
                    "tools": [t.name for t in r.tool_schemas],
                    "messages": [{"role": m.role.value, "content": m.content} for m in r.messages],
                }
                for r in requests
            ],
            "roundtrip": {
                "settled": settled,
                "server": server,
                "tool": tool_name,
                "arguments": arguments or {},
                "marker": mode["marker"],
                "calls": [
                    {k: e[k] for k in ("server", "tool", "arguments", "result")}
                    for e in jsonl(events_path)[starts["events"] :]
                    if e["kind"] == "call"
                ],
                "proposals": [
                    {"name": e["payload"]["tool_name"], "arguments": e["payload"]["arguments"]}
                    for r in runs
                    for e in r["events"]
                    if e["type"] == "tool.proposed"
                ],
                "offered": [t.name for r in requests for t in r.tool_schemas],
                "observations": observations,
                "tool_results": [text(m) for m in messages if m.get("role") == "toolResult"],
                "answer": text(final),
                "gateway_executions": qualification.tool_executions(runs),
            },
        }
        exchanges.append(record)
        print(
            json.dumps(
                {
                    "exchange": name,
                    "settled": settled,
                    "calls": record["roundtrip"]["calls"],
                    "answer": text(final),
                }
            ),
            flush=True,
        )
        return record

    def update_control(server, **changes):
        path = controls[server]
        value = json.loads(path.read_text(encoding="utf-8"))
        write_json_atomic(path, {**value, **changes})

    try:
        with RunningServer(app) as running:
            env["RELAYMIND_APP_TOKEN"] = token_of(app)
            config = {
                "gateway": {
                    "mode": "local",
                    "port": port,
                    "bind": "loopback",
                    "auth": {"mode": "token", "token": host_secret},
                },
                "logging": {"file": str(work / "host.log")},
                "agents": {
                    "defaults": {
                        "workspace": str(work / "workspace"),
                        "model": {"primary": "relaymind/relaymind-client-tools"},
                        "heartbeat": {"every": "0m"},
                    }
                },
                "tools": {"profile": "coding", "toolSearch": False},
                "plugins": {
                    "load": {"paths": [str(fixture)]},
                    "allow": ["relaymind-mcp-qualification"],
                    "entries": {
                        "relaymind-mcp-qualification": {
                            "enabled": True,
                            "hooks": {"allowConversationAccess": True},
                            "config": {
                                "baseUrl": running.base_url,
                                "capturePath": str(capture_path),
                            },
                        }
                    },
                },
                "mcp": {
                    "servers": {
                        server: {
                            "command": "node",
                            "args": [str(fixture / "server.mjs"), str(path), str(events_path)],
                            "env": {"RELAYMIND_MCP_TEST_CREDENTIAL": mcp_secret},
                        }
                        for server, path in controls.items()
                    }
                },
            }
            config_path = Path(env["OPENCLAW_CONFIG_PATH"])
            config_path.write_text(json.dumps(config), encoding="utf-8")
            call(
                ["models", "auth", "paste-api-key", "--provider", "relaymind"],
                stdin=token_of(app) + "\n",
            )
            exported["status_command"] = call(["mcp", "status", "--json"])
            exported["probe_command"] = call(["mcp", "probe", "--json"])
            log_file = (work / "gateway-console.log").open("w")
            process = subprocess.Popen(
                host + ["gateway", "run", "--compact"],
                env=env,
                stdout=log_file,
                stderr=subprocess.STDOUT,
            )
            wait_for_listener(process, port)
            exported["versions"] = qualification.versions()
            exported["mcp_sdk_version"] = json.loads(
                (package / "node_modules/@modelcontextprotocol/sdk/package.json").read_text(
                    encoding="utf-8"
                )
            )["version"]
            send("first_server", "alpha.one", {"text": "sandbox"})
            send("second_server", "alpha?one", {"text": "sandbox"})
            update_control("alpha.one", revision=2)
            exported["immediate_schema_change_inventory"] = rpc(
                "tools.effective", {"sessionKey": session}
            )
            time.sleep(1)
            send("changed_schema", "alpha.one", {"count": 2})
            update_control("alpha.one", disconnected=True)
            time.sleep(0.4)
            exported["immediate_disconnect_inventory"] = rpc(
                "tools.effective", {"sessionKey": session}
            )
            send("disconnected", "alpha.one", {"count": 2})
            config["tools"]["deny"] = ["bundle-mcp"]
            config_path.write_text(json.dumps(config), encoding="utf-8")
            exported["immediate_policy_inventory"] = rpc("tools.effective", {"sessionKey": session})
            time.sleep(1)
            send("policy_denied", "alpha?one", {"text": "sandbox"})
            config["tools"].pop("deny")
            config["mcp"]["servers"].pop("alpha?one")
            config_path.write_text(json.dumps(config), encoding="utf-8")
            exported["immediate_removal_inventory"] = rpc(
                "tools.effective", {"sessionKey": session}
            )
            time.sleep(1)
            send("removed", "alpha?one", {"text": "sandbox"})
            exported["status"] = "completed"
    except Exception as error:
        exported.update(status="failed", error=f"{type(error).__name__}: {error}")
    finally:
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        if log_file:
            log_file.close()
        exported["runs"] = qualification.run_records(gateway, qualification._run_ids(gateway))
        exported["gateway_executions"] = qualification.tool_executions(exported["runs"])
        gateway.store.close()
    exported["exchanges"] = exchanges
    exported["schema_captures"] = jsonl(capture_path)
    exported["mcp_events"] = jsonl(events_path)
    exported["commands"] = commands
    exported["host_log"] = (
        (work / "gateway-console.log").read_text(encoding="utf-8")
        if (work / "gateway-console.log").exists()
        else ""
    )
    source_paths = [
        *fixture_modules(fixture),
        fixture / "qualify.py",
        package / "dist/provider.js",
        fixture / "openclaw.plugin.json",
        fixture / "package.json",
        host_root / "openclaw.mjs",
        host_root / "dist/plugin-sdk/agent-harness-runtime.js",
        host_root / "dist/agent-bundle-mcp-materialize-DD5Z5Ssq.mjs",
        host_root / "dist/tools-effective-DRoSpE_Y.mjs",
        package / "package-lock.json",
        host_root / "package.json",
        host_root / "dist/config-contracts-Db6WHFol.d.ts",
        host_root / "dist/server-plugins-BN7J9yLN.mjs",
        root / "scripts/openclaw_qualification.py",
        root / "tests/gateway_helpers.py",
    ]
    exported["source_hashes"] = {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in source_paths
    }
    used = [e for e in exchanges if e["name"] in ("first_server", "second_server")]
    exported["checks"] = {
        "two_real_host_roundtrips": len(used) == 2
        and all(roundtrip_pass(e["roundtrip"]) for e in used)
    }
    if exported["checks"]["two_real_host_roundtrips"]:
        exported["integration_decision"]["same_instance_schema_provenance"] = "qualified_read_only"
    serialized = qualification.mask_machine_names(json.dumps(exported, default=str))
    serialized = serialized.replace(str(work), "<disposable>").replace(str(root), "<repo>")
    exported = json.loads(serialized)
    write_export(out, exported, [mcp_secret, host_secret, token_of(app)])
    return exported


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    result = run(args.work_dir.resolve(), args.out.resolve())
    print(json.dumps({"status": result["status"], "checks": result["checks"]}))
