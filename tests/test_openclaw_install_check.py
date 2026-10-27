"""The OpenClaw install check: the packaged plugin through a host's whole lifecycle (#145).

RM12, RM19, RM33; AC24; spec sections 7.5, 12.5, and 14.

``scripts/openclaw_install_check.py`` installs the release artifact into an
isolated host home, configures and enables it, inspects it, stores the
gateway token through the host's credential command, discovers the alias,
runs one text and one host-tool exchange against a doubles gateway, then
disables, uninstalls, and removes what uninstall leaves behind. Here the host
is a stub that keeps the real host's command forms and its observed behavior
(macOS probe and qualification, 27 October 2026): the stored key in a
SQLite state database, the call ID replayed without underscores, ``"ok":
true`` whatever happened, and the config entry and credential left after
uninstall. A real host run is an operator or CI step, never this suite.
"""

import json
import sys
from pathlib import Path

import pytest

from helpers import load_script_module

install_check = load_script_module("openclaw_install_check")

# The host stand-in. It keeps its config in OPENCLAW_CONFIG_PATH, its stored
# credentials in a SQLite file under OPENCLAW_STATE_DIR, and logs each call.
STUB_HOST = r"""
import json, os, sqlite3, sys, urllib.request
from pathlib import Path

args = sys.argv[1:]
state = Path(os.environ["OPENCLAW_STATE_DIR"])
state.mkdir(parents=True, exist_ok=True)
config_path = Path(os.environ["OPENCLAW_CONFIG_PATH"])
with open(state / "calls.jsonl", "a") as log:
    log.write(json.dumps({
        "args": args,
        "isolation": {k: os.environ.get(k) for k in
                      ("OPENCLAW_HOME", "OPENCLAW_STATE_DIR", "OPENCLAW_CONFIG_PATH")},
        "token_in_env": "RELAYMIND_APP_TOKEN" in os.environ,
    }) + "\n")
fail = os.environ.get("STUB_FAIL")
if fail and " ".join(args).startswith(fail):
    print("stub: refused " + fail, file=sys.stderr)
    raise SystemExit(1)

def load():
    return json.loads(config_path.read_text()) if config_path.exists() else {}

def save(config):
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config))

def db():
    (state / "state").mkdir(exist_ok=True)
    conn = sqlite3.connect(state / "state" / "openclaw.sqlite")
    conn.execute(
        "create table if not exists config_machine_state (key text primary key, value text)")
    return conn

def stored_key():
    row = db().execute(
        "select value from config_machine_state where key='authProfiles.store'").fetchone()
    return json.loads(row[0])["relaymind:manual"] if row else None

def entries(config):
    return config.setdefault("plugins", {}).setdefault("entries", {})

def opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))

def call(base, key, path, body=None):
    request = urllib.request.Request(
        base + path, data=None if body is None else json.dumps(body).encode(),
        method="GET" if body is None else "POST",
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    with opener().open(request, timeout=30) as response:
        return json.loads(response.read())

config = load()
if args == ["--version"]:
    print("OpenClaw 2026.10.7 (stub)")
elif args[:2] == ["config", "set"]:
    node = config
    *parents, last = args[2].split(".")
    for part in parents:
        node = node.setdefault(part, {})
    node[last] = args[3]
    save(config)
elif args[:2] == ["config", "unset"]:
    parts = args[2].split(".")
    node = config
    for part in parts[:-1]:
        node = node.get(part, {})
    node.pop(parts[-1], None)
    save(config)
elif args[:2] == ["plugins", "install"]:
    assert args[2].startswith("npm-pack:") and Path(args[2][len("npm-pack:"):]).is_file()
    if "--accept-capabilities" not in args:
        print("requires capability consent", file=sys.stderr)
        raise SystemExit(1)
    entries(config)["relaymind"] = {"enabled": False, "installed": True}
    save(config)
elif args[:2] in (["plugins", "enable"], ["plugins", "disable"]):
    entries(config).setdefault("relaymind", {})["enabled"] = args[1] == "enable"
    save(config)
elif args[:2] == ["plugins", "inspect"]:
    entry = entries(config).get("relaymind", {})
    status = os.environ.get("STUB_INSPECT_STATUS", "loaded")
    plugin = {"id": "relaymind", "status": status if entry.get("installed") else "missing",
              "enabled": entry.get("enabled"), "version": "0.1.0", "providerIds": ["relaymind"]}
    print(json.dumps({"plugin": plugin, "diagnostics": []}))
elif args[:3] == ["models", "auth", "paste-api-key"]:
    key = sys.stdin.readline().strip()
    conn = db()
    conn.execute("insert or replace into config_machine_state values ('authProfiles.store', ?)",
                 (json.dumps({"relaymind:manual": key}),))
    conn.commit()
    config.setdefault("auth", {}).setdefault("profiles", {})["relaymind:manual"] = {
        "provider": "relaymind", "mode": "api_key"}
    save(config)
    print("Auth profile: relaymind:manual (relaymind/api_key)")
elif args[:3] == ["models", "auth", "logout"]:
    if not os.environ.get("STUB_KEEP_CREDENTIAL"):
        conn = db()
        conn.execute("delete from config_machine_state where key='authProfiles.store'")
        conn.commit()
        config.get("auth", {}).get("profiles", {}).pop(args[3], None)
        save(config)
    print("Removed auth profile: " + args[3])
elif args[:2] == ["models", "list"]:
    base = entries(config)["relaymind"]["config"]["baseUrl"]
    served = call(base, stored_key(), "/models")
    print("stub key " + stored_key(), file=sys.stderr)
    models = [{"key": "relaymind/" + m["id"]} for m in served["data"]]
    print(json.dumps({"count": len(models), "models": models}))
elif args[:2] == ["agent", "exec"]:
    prompt = args[2]
    cwd = Path(args[args.index("--cwd") + 1])
    base = entries(config)["relaymind"]["config"]["baseUrl"]
    key = stored_key()
    read = {"type": "function", "function": {"name": "read", "parameters": {
        "type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}}
    messages = [{"role": "system", "content": "stub host"}, {"role": "user", "content": prompt}]
    body = {"model": "relaymind-client-tools", "messages": messages, "stream": False,
            "tools": [read], "tool_choice": "auto"}
    reply = call(base, key, "/chat/completions", body)["choices"][0]["message"]
    if reply.get("tool_calls") and not os.environ.get("STUB_SKIP_TOOL"):
        (tool_call,) = reply["tool_calls"]
        replayed = tool_call["id"].replace("_", "")
        path = cwd / json.loads(tool_call["function"]["arguments"])["path"]
        messages += [
            {"role": "assistant", "content": None, "tool_calls": [{**tool_call, "id": replayed}]},
            {"role": "tool", "tool_call_id": replayed, "content": path.read_text()},
        ]
        reply = call(base, key, "/chat/completions", body)["choices"][0]["message"]
    payloads = [{"text": reply.get("content") or ""}]
    print(json.dumps({"ok": True, "status": "ok", "payloads": payloads}))
elif args[:2] == ["plugins", "uninstall"]:
    if "--force" not in args:
        print("plugins uninstall requires confirmation input", file=sys.stderr)
        raise SystemExit(1)
    entries(config)["relaymind"] = {"enabled": False}
    save(config)
    print('Uninstalled plugin "relaymind".')
else:
    print("stub: unknown command " + " ".join(args), file=sys.stderr)
    raise SystemExit(2)
"""


@pytest.fixture
def stub_host(tmp_path):
    path = tmp_path / "stub_openclaw.py"
    path.write_text(STUB_HOST, "utf-8")
    return [sys.executable, str(path)]


@pytest.fixture
def tarball(tmp_path):
    path = tmp_path / "artifact" / "relaymind-openclaw-provider-0.1.0.tgz"
    path.parent.mkdir()
    path.write_bytes(b"not a real tarball; the stub only checks that it exists")
    return path


def run(stub_host, tarball, tmp_path, **options):
    return install_check.run_install_check(stub_host, tarball, tmp_path / "export.json", **options)


def calls(home):
    lines = (home / "state" / "calls.jsonl").read_text("utf-8").splitlines()
    return [json.loads(line) for line in lines]


def test_every_step_runs_in_order_against_an_isolated_host_home(stub_host, tarball, tmp_path):
    """Section 12.5: the host's state is isolated; the documented command forms are used."""
    home = tmp_path / "host"
    export = run(stub_host, tarball, tmp_path, host_home=home)
    assert export["status"] == "completed"
    assert [s["name"] for s in export["steps"]] == list(install_check.STEPS)
    assert all(s["status"] == "passed" for s in export["steps"]), export["steps"]
    recorded = calls(home)
    argv = [c["args"] for c in recorded]
    assert argv[0] == ["--version"]
    install = next(a for a in argv if a[:2] == ["plugins", "install"])
    assert install[2] == f"npm-pack:{tarball}"
    assert {"--force", "--accept-capabilities"} <= set(install)
    assert ["plugins", "enable", "relaymind"] in argv
    assert ["plugins", "inspect", "relaymind", "--runtime", "--json"] in argv
    assert ["models", "auth", "paste-api-key", "--provider", "relaymind"] in argv
    assert ["models", "list", "--refresh", "--provider", "relaymind", "--json"] in argv
    assert ["plugins", "uninstall", "relaymind", "--force"] in argv
    assert ["config", "unset", "plugins.entries.relaymind"] in argv
    assert ["models", "auth", "logout", "relaymind:manual", "--yes"] in argv
    # Every call ran in one fresh host home, never the operator's ~/.openclaw.
    homes = {json.dumps(c["isolation"], sort_keys=True) for c in recorded}
    assert len(homes) == 1
    isolation = recorded[0]["isolation"]
    assert all(isolation.values())
    assert ".openclaw" not in json.dumps(isolation)
    # The stored credential, not the environment, carries the token (RM12, RM19).
    assert not any(c["token_in_env"] for c in recorded)
    assert export["passed"] is True
    assert export["host_version"] == "OpenClaw 2026.10.7 (stub)"


def test_the_text_and_tool_exchanges_go_through_the_gateway_and_the_host(
    stub_host, tarball, tmp_path
):
    """AC24, RM08, RM33: the host ran the tool once; the gateway proposed it and ran nothing."""
    export = run(stub_host, tarball, tmp_path)
    text, tool = export["exchanges"]["text"], export["exchanges"]["tool"]
    assert text["passed"] and tool["passed"]
    assert text["answer_in_host_output"] is True
    assert text["runs"] and all(r["state"] == "completed" for r in export["runs"])
    assert [p["tool_name"] for p in tool["proposed"]] == ["read"]
    # The host replays the call ID without underscores; the check still matches it.
    assert tool["replayed_call_id"] == "callread1"
    assert tool["result_reached_next_run"] is True
    assert tool["host_result_holds_the_file"] is True
    assert export["host_tool_executions_by_gateway"] == 0


def test_the_token_and_the_host_home_never_reach_the_export(stub_host, tarball, tmp_path):
    """RM12: the token is replaced; machine paths are placeholders."""
    export = run(stub_host, tarball, tmp_path)
    text = (tmp_path / "export.json").read_text("utf-8")
    assert export["token"] == "<not recorded>"
    # The stub prints the key it read; the export keeps only the placeholder.
    assert "stub key <not recorded>" in text
    assert str(tarball.parent) not in text
    # The checkout and the user's home are placeholders too (the gateway's skill
    # paths, the host's peer dependency link).
    assert str(install_check.ROOT) not in text
    assert str(Path.home()) not in text
    assert "<host-home>" in text
    assert export["artifact"]["filename"] == tarball.name
    assert len(export["artifact"]["sha256"]) == 64


def test_what_uninstall_leaves_is_recorded_and_the_removal_steps_clear_it(
    stub_host, tarball, tmp_path
):
    """The pinned host keeps the config entry and the credential after uninstall."""
    export = run(stub_host, tarball, tmp_path)
    after_uninstall = export["leftovers"]["after_uninstall"]
    assert after_uninstall == {
        "config_entry": True,
        "auth_profile": True,
        "credential_in_host_state": True,
    }
    assert export["leftovers"]["after_removal"] == {
        "config_entry": False,
        "auth_profile": False,
        "credential_in_host_state": False,
    }


def test_a_credential_left_after_removal_does_not_pass(stub_host, tarball, tmp_path, monkeypatch):
    monkeypatch.setenv("STUB_KEEP_CREDENTIAL", "1")
    export = run(stub_host, tarball, tmp_path)
    assert export["leftovers"]["after_removal"]["credential_in_host_state"] is True
    assert export["passed"] is False


def test_a_refused_install_stops_the_lifecycle_and_never_passes(
    stub_host, tarball, tmp_path, monkeypatch
):
    monkeypatch.setenv("STUB_FAIL", "plugins install")
    export = run(stub_host, tarball, tmp_path)
    status = {s["name"]: s["status"] for s in export["steps"]}
    assert status["install"] == "failed"
    assert export["steps"][[s["name"] for s in export["steps"]].index("install")]["exit_code"] == 1
    after = list(install_check.STEPS)[list(install_check.STEPS).index("install") + 1 :]
    assert {status[name] for name in after} == {"not_run"}
    assert export["passed"] is False


def test_a_failed_exchange_still_cleans_up(stub_host, tarball, tmp_path, monkeypatch):
    """The cleanup steps run once the plugin was installed, whatever failed in between."""
    monkeypatch.setenv("STUB_FAIL", "agent exec")
    export = run(stub_host, tarball, tmp_path)
    status = {s["name"]: s["status"] for s in export["steps"]}
    assert status["text-exchange"] == "failed"
    assert status["tool-exchange"] == "not_run"
    for name in install_check.CLEANUP_STEPS:
        assert status[name] == "passed"
    assert export["passed"] is False


def test_a_plugin_the_host_did_not_load_fails_inspection(stub_host, tarball, tmp_path, monkeypatch):
    monkeypatch.setenv("STUB_INSPECT_STATUS", "error")
    export = run(stub_host, tarball, tmp_path)
    (inspect,) = [s for s in export["steps"] if s["name"] == "inspect"]
    assert inspect["status"] == "failed"
    assert inspect["check"]["status"] == "error"
    assert export["passed"] is False


def test_a_host_that_says_ok_without_running_the_tool_does_not_pass(
    stub_host, tarball, tmp_path, monkeypatch
):
    """Section 12.5: the host's "ok": true is its statement, never task success."""
    monkeypatch.setenv("STUB_SKIP_TOOL", "1")
    export = run(stub_host, tarball, tmp_path)
    tool = export["exchanges"]["tool"]
    assert tool["host_report"]["ok"] is True
    assert tool["result_reached_next_run"] is False
    assert tool["passed"] is False
    (step,) = [s for s in export["steps"] if s["name"] == "tool-exchange"]
    assert step["status"] == "failed"
    assert export["passed"] is False


def test_a_missing_host_is_not_run_never_a_pass(tarball, tmp_path):
    export = run(["openclaw-not-installed-here"], tarball, tmp_path)
    assert export["status"] == "not_run"
    assert {s["status"] for s in export["steps"]} == {"not_run"}
    assert export["passed"] is False


def test_the_label_names_what_the_run_is(stub_host, tarball, tmp_path):
    """Section 12.5: a functional check is never reported as a platform qualification."""
    default = run(stub_host, tarball, tmp_path)
    assert "not a supported-platform qualification" in default["evidence"]
    assert "not live model evidence" in default["evidence"]
    labeled = run(stub_host, tarball, tmp_path, label="GitHub runner ubuntu-latest")
    assert labeled["evidence"].startswith("GitHub runner ubuntu-latest")
    assert "not live model evidence" in labeled["evidence"]


def test_the_main_entry_exits_by_the_result(stub_host, tarball, tmp_path, monkeypatch):
    out = tmp_path / "main.json"
    argv = ["--tarball", str(tarball), "--out", str(out), "--", *stub_host]
    assert install_check.main(argv) == 0
    assert json.loads(out.read_text("utf-8"))["passed"] is True
    monkeypatch.setenv("STUB_FAIL", "plugins enable")
    assert install_check.main(argv) == 1
    with pytest.raises(SystemExit):
        install_check.main(["--tarball", str(tarball), "--out", str(out)])


def test_a_missing_artifact_is_refused_before_the_host_runs(stub_host, tmp_path):
    with pytest.raises(SystemExit, match="artifact"):
        install_check.main(
            ["--tarball", str(tmp_path / "nope.tgz"), "--out", str(tmp_path / "x.json"), "--"]
            + stub_host
        )
    assert not Path(tmp_path / "x.json").exists()
