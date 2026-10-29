"""Characterize headless approval refusal, never a human allow/deny pass (#189; RM11, RM16)."""

import argparse
import json
import subprocess
from pathlib import Path

from qualify import HOST, install, qualification

from api_helpers import RunningServer, make_app, token_of
from gateway_helpers import build_gateway, text_turn, tool_turn
from relaymind.api.chat import ClientToolsChat


def run(host_home: Path, work: Path, out: Path) -> dict:
    """Use an existing disposable probe config as input to a fresh headless sandbox."""
    config = json.loads((host_home / "openclaw.json").read_text())
    work.mkdir(parents=True, exist_ok=False)
    for name in ("home", "state", "sandbox", "exec-state"):
        (work / name).mkdir()
    env = install.host_environment(work)
    env["HOME"] = str(work / "home")
    env["RELAYMIND_TEST_SANDBOX"] = str(work / "sandbox")
    config["agents"]["defaults"]["workspace"] = str(work / "sandbox")
    # Explicitly admit the sandbox plugin tools; keep native tool search enabled.
    config["tools"] = {"alsoAllow": ["relaymind_test_write", "relaymind_test_echo"]}
    gateway = build_gateway(
        work / "gateway",
        [
            tool_turn(
                "tool_call",
                id="relaymind_test_write",
                args={"name": "note.txt", "line": "sandbox qualification"},
            ),
            text_turn("The headless write was refused."),
        ],
        external_turns=[],
    )
    app = make_app(gateway, client_tools=ClientToolsChat(gateway.orchestrator))
    try:
        with RunningServer(app) as running:
            env["RELAYMIND_APP_TOKEN"] = token_of(app)
            config["plugins"]["entries"]["relaymind"]["config"]["baseUrl"] = running.base_url
            config_path = Path(env["OPENCLAW_CONFIG_PATH"])
            config_path.write_text(json.dumps(config))
            subprocess.run(
                HOST + ["models", "auth", "paste-api-key", "--provider", "relaymind"],
                env=env,
                input=token_of(app) + "\n",
                text=True,
                capture_output=True,
                timeout=90,
                check=True,
            )
            command = HOST + [
                "agent",
                "exec",
                "Write one sandbox line with relaymind_test_write.",
                "--model",
                "relaymind/relaymind-client-tools",
                "--json",
                "--config",
                str(config_path),
                "--cwd",
                str(work / "sandbox"),
                "--state-dir",
                str(work / "exec-state"),
            ]
            result = subprocess.run(command, env=env, text=True, capture_output=True, timeout=120)
            exported = {
                "kind": "openclaw-headless-approval-characterization",
                "evidence": "real headless host + gateway + model doubles; no human approval",
                "command": command,
                "tools_config": config["tools"],
                "exit_code": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "sandbox_files": [p.name for p in (work / "sandbox").iterdir()],
                "runs": qualification.run_records(gateway, qualification._run_ids(gateway)),
                "model_requests": [
                    {
                        "messages": [
                            {"role": m.role.value, "content": m.content} for m in r.messages
                        ],
                        "tools": [t.name for t in r.tool_schemas],
                    }
                    for r in gateway.adapter.requests
                ],
            }
            serialized = json.dumps(exported, indent=2)
            if token_of(app) in serialized:
                raise RuntimeError("credential reached export")
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(qualification.mask_machine_names(serialized) + "\n")
            return exported
    finally:
        gateway.store.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host-home", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    result = run(args.host_home.resolve(), args.work_dir.resolve(), args.out.resolve())
    print(json.dumps({"exit_code": result["exit_code"], "sandbox_files": result["sandbox_files"]}))
