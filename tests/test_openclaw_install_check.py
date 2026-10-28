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
import shutil
import socket
import sys
from pathlib import Path
from types import SimpleNamespace

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
    if not config_path.exists():
        return {}
    lines = config_path.read_text().splitlines()
    return json.loads("\n".join(line for line in lines if not line.startswith("//")))

def save(config):
    config_path.parent.mkdir(parents=True, exist_ok=True)
    # STUB_JSON5: a config the host may write that strict JSON cannot read.
    comment = "// written by the stub\n" if os.environ.get("STUB_JSON5") else ""
    config_path.write_text(comment + json.dumps(config))

def db():
    (state / "state").mkdir(exist_ok=True)
    conn = sqlite3.connect(state / "state" / "openclaw.sqlite")
    # Many SQLite builds keep deleted rows in free space and WAL frames.
    conn.execute("pragma secure_delete=0")
    conn.execute("pragma journal_mode=wal")
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
elif args[:2] == ["models", "list"] and os.environ.get("OPENCLAW_GATEWAY_PORT"):
    # The pinned host treats an explicit port as a Gateway target and never
    # falls back to the local catalog (Linux VPS functional check, 28 October 2026).
    print("gateway models.list requires credentials before opening a websocket",
          file=sys.stderr)
    raise SystemExit(1)
elif args[:2] == ["models", "list"]:
    base = entries(config)["relaymind"]["config"]["baseUrl"]
    served = call(base, stored_key(), "/models")
    print("stub key " + stored_key(), file=sys.stderr)
    models = [{"key": "relaymind/" + m["id"]} for m in served["data"]]
    print(json.dumps({"count": len(models), "models": models}))
elif args[:2] == ["agent", "exec"]:
    prompt = args[2]
    if "--state-dir" in args:
        # The host uses an existing state directory and never deletes it.
        if not Path(args[args.index("--state-dir") + 1]).is_dir():
            print("State directory does not exist", file=sys.stderr)
            raise SystemExit(1)
    elif os.environ.get("STUB_EXEC_CLEANUP_LOCKED"):
        # The pinned host on Windows (GitHub runner, 28 October 2026): the turn
        # answers, then deleting its own temporary state directory fails on a
        # SQLite file it still holds, and the run reports ok false, exit 1.
        error = {"message": "Agent exec cleanup failed: EBUSY: resource busy or locked",
                 "kind": "cleanup"}
        print(json.dumps({"ok": False, "status": "error", "error": error}))
        raise SystemExit(1)
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


def test_the_home_relative_form_the_host_prints_is_a_placeholder_too(tmp_path):
    """RM12: the host prints paths under the user's home as ``~/...`` (its skill
    locations); those are placeholders like the absolute forms (PR #182 review)."""
    ctx = SimpleNamespace(home=tmp_path / "host", tarball=tmp_path / "a" / "x.tgz", token="t0k")
    home = Path.home()
    repo = install_check.ROOT
    lines = [f"{repo}/skills/a/SKILL.md", f"{home}/elsewhere/b"]
    if repo.is_relative_to(home):
        lines.append(f"~/{repo.relative_to(home)}/skills/a/SKILL.md")
    lines.append("~/elsewhere/b")
    text = install_check._sanitize("\n".join(lines), ctx)
    assert "~/" not in text
    assert str(home) not in text
    for line in text.splitlines():
        assert line in ("<repo>/skills/a/SKILL.md", "<home>/elsewhere/b"), line


@pytest.mark.skipif(sys.platform == "win32", reason="a backslash cannot be in a Windows name")
def test_the_json_form_of_a_resolved_path_is_a_placeholder_too(tmp_path):
    """RM12: the export is JSON, so a path the host printed in its resolved form
    (a symlink or a Windows short name resolved) is replaced in its JSON-escaped
    form too, not only the given path's (PR #182 review)."""
    real = tmp_path / "real\\host"
    real.mkdir()
    link = tmp_path / "host"
    link.symlink_to(real, target_is_directory=True)
    ctx = SimpleNamespace(home=link, tarball=tmp_path / "a" / "x.tgz", token="t0k")
    escaped = json.dumps(f"{link.resolve()}/state/x")
    assert install_check._sanitize(escaped, ctx) == '"<host-home>/state/x"'


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
    assert default["platform_qualification"] is False
    labeled = run(stub_host, tarball, tmp_path, label="GitHub runner ubuntu-latest")
    assert labeled["evidence"].startswith("GitHub runner ubuntu-latest")
    assert "not live model evidence" in labeled["evidence"]
    assert "not a supported-platform qualification" in labeled["evidence"]
    assert labeled["platform_qualification"] is False


def test_a_target_platform_run_is_recorded_as_the_platform_qualification(
    stub_host, tarball, tmp_path
):
    """RM33, AC24, section 12.5: a run on a target platform of section 1 is the
    platform qualification of the install lifecycle, still never live model evidence."""
    label = "macOS 27.0.1 arm64, Apple M1 (user's machine), Node v26.12.1"
    export = run(stub_host, tarball, tmp_path, label=label, target_platform=True)
    assert export["evidence"].startswith(label)
    assert "platform qualification" in export["evidence"]
    assert "not live model evidence" in export["evidence"]
    assert "not a supported-platform qualification" not in export["evidence"]
    assert export["platform_qualification"] is True
    keys = list(export)
    assert keys[keys.index("evidence") + 1] == "platform_qualification"


def test_a_target_platform_run_must_name_its_machine(tarball, tmp_path, monkeypatch, capsys):
    """RM33, AC24, section 12.5: a target-platform run without a label is refused
    before any host or gateway starts."""

    def no_run(*args, **kwargs):
        raise AssertionError("the install check must not start")

    monkeypatch.setattr(install_check, "run_install_check", no_run)
    out = tmp_path / "x.json"
    argv = ["--tarball", str(tarball), "--out", str(out), "--target-platform", "--", "openclaw"]
    with pytest.raises(SystemExit) as raised:
        install_check.main(argv)
    assert raised.value.code == 2
    assert "--target-platform needs --label" in capsys.readouterr().err
    assert not out.exists()


def test_the_main_entry_exits_by_the_result(stub_host, tarball, tmp_path, monkeypatch):
    out = tmp_path / "main.json"
    argv = ["--tarball", str(tarball), "--out", str(out), "--", *stub_host]
    assert install_check.main(argv) == 0
    assert json.loads(out.read_text("utf-8"))["passed"] is True
    monkeypatch.setenv("STUB_FAIL", "plugins enable")
    assert install_check.main(argv) == 1
    with pytest.raises(SystemExit):
        install_check.main(["--tarball", str(tarball), "--out", str(out)])


def test_the_main_entry_lists_only_the_steps_that_ran_and_failed(
    stub_host, tarball, tmp_path, monkeypatch, capsys
):
    """A step the lifecycle never reached is not_run, not failed (PR #182 review)."""
    out = tmp_path / "main.json"
    monkeypatch.setenv("STUB_FAIL", "plugins enable")
    assert install_check.main(["--tarball", str(tarball), "--out", str(out), "--", *stub_host]) == 1
    assert json.loads(capsys.readouterr().out)["failed"] == ["enable"]
    argv = ["--tarball", str(tarball), "--out", str(out), "--", "openclaw-not-installed-here"]
    assert install_check.main(argv) == 1
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "not_run"
    assert summary["failed"] == []


def test_a_host_command_is_run_as_path_lookup_finds_it(stub_host, tarball, tmp_path, monkeypatch):
    """On Windows ``openclaw`` is ``openclaw.cmd`` on PATH, which only a PATHEXT
    lookup finds; the command is run as ``shutil.which`` resolves it and recorded
    as given (PR #182 review)."""
    which = shutil.which
    found = stub_host[0]
    monkeypatch.setattr(
        install_check.shutil, "which", lambda name: found if name == "openclaw" else which(name)
    )
    export = run(["openclaw", *stub_host[1:]], tarball, tmp_path)
    assert export["passed"] is True
    assert export["command"][0] == "openclaw"


def test_a_missing_artifact_is_refused_before_the_host_runs(stub_host, tmp_path):
    with pytest.raises(SystemExit, match="artifact"):
        install_check.main(
            ["--tarball", str(tmp_path / "nope.tgz"), "--out", str(tmp_path / "x.json"), "--"]
            + stub_host
        )
    assert not Path(tmp_path / "x.json").exists()


def test_a_removed_credential_left_in_sqlite_free_space_is_not_reported(
    stub_host, tarball, tmp_path
):
    """Only the rows a host can read count: a deleted row's bytes in free pages do not."""
    export = run(stub_host, tarball, tmp_path, host_home=tmp_path / "host")
    assert export["leftovers"]["after_removal"]["credential_in_host_state"] is False
    assert export["passed"] is True


def test_a_config_strict_json_cannot_read_is_never_reported_clean(
    stub_host, tarball, tmp_path, monkeypatch
):
    """Honest evidence: an unreadable config is unknown, and unknown never passes removal."""
    monkeypatch.setenv("STUB_JSON5", "1")
    export = run(stub_host, tarball, tmp_path)
    after = export["leftovers"]["after_removal"]
    assert after["config_entry"] is None
    assert after["auth_profile"] is None
    (step,) = [s for s in export["steps"] if s["name"] == "remove-credential"]
    assert step["status"] == "failed"
    assert export["passed"] is False


def test_a_missing_or_partial_config_is_unknown(tmp_path):
    assert install_check._profile_present(tmp_path / "absent.json") == (False, False)
    partial = tmp_path / "partial.json"
    partial.write_text('{"plugins": {"entries": {"relaymind"', "utf-8")
    assert install_check._profile_present(partial) == (None, None)
    odd = tmp_path / "odd.json"
    odd.write_text('{"plugins": [], "auth": "x"}', "utf-8")
    assert install_check._profile_present(odd) == (None, None)


def test_an_unreadable_sqlite_file_counts_as_holding_the_credential(tmp_path):
    """A host state file the check cannot read is never reported clean."""
    broken = tmp_path / "openclaw.sqlite"
    broken.write_bytes(b"SQLite format 3\x00" + b"\x00" * 50)
    assert install_check._credential_in(broken, "token-123") is True


def test_the_host_inherits_no_openclaw_gateway_profile_or_proxy(tmp_path, monkeypatch):
    """Section 12.5: no operator OpenClaw state, Gateway, or proxy reaches the host."""
    for name, value in {
        "OPENCLAW_PROFILE": "work",
        "OPENCLAW_GATEWAY_URL": "ws://127.0.0.1:18789",
        "OPENCLAW_GATEWAY_TOKEN": "operator-gateway-token",
        "OPENCLAW_GATEWAY_PORT": "18789",
        "ALL_PROXY": "socks5://proxy:1080",
        "all_proxy": "socks5://proxy:1080",
        "https_proxy": "http://proxy:3128",
        "RELAYMIND_APP_TOKEN": "x",
    }.items():
        monkeypatch.setenv(name, value)
    env = install_check.host_environment(tmp_path)
    for name in ("OPENCLAW_PROFILE", "OPENCLAW_GATEWAY_URL", "OPENCLAW_GATEWAY_TOKEN"):
        assert name not in env
    assert not {k for k in env if k.upper() in {"ALL_PROXY", "HTTPS_PROXY", "HTTP_PROXY"}}
    assert not any(k.startswith("RELAYMIND_") for k in env)
    # No OPENCLAW_GATEWAY_PORT either: the pinned host takes an explicit port
    # as a Gateway target, and `models list --refresh` then needs Gateway
    # credentials instead of refreshing the local catalog.
    assert {k for k in env if k.startswith("OPENCLAW_")} == {
        "OPENCLAW_HOME",
        "OPENCLAW_STATE_DIR",
        "OPENCLAW_CONFIG_PATH",
    }


def test_the_isolated_config_points_the_local_gateway_at_a_closed_port(
    stub_host, tarball, tmp_path
):
    """Section 12.5: before the install, gateway.port names a port nothing listens on.

    The host reads ``gateway.port`` from its config when no port is in the
    environment, so an operator's OpenClaw Gateway on the default 18789 is
    never reached, and the local catalog refresh still runs.
    """
    home = tmp_path / "host"
    export = run(stub_host, tarball, tmp_path, host_home=home)
    assert export["passed"] is True, export["steps"]
    argv = [c["args"] for c in calls(home)]
    port_set = next(i for i, a in enumerate(argv) if a[:3] == ["config", "set", "gateway.port"])
    install = next(i for i, a in enumerate(argv) if a[:2] == ["plugins", "install"])
    assert port_set < install
    port = int(argv[port_set][3])
    assert port != 18789
    with socket.socket() as probe, pytest.raises(OSError):
        probe.settimeout(1)
        probe.connect(("127.0.0.1", port))
    assert export["isolation"]["gateway_port"] == port


def test_a_host_that_stops_starting_after_install_still_lists_and_cleans_up(
    stub_host, tarball, tmp_path, monkeypatch
):
    """An OSError mid-run: the rest is not_run, and the cleanup steps still run."""
    real = install_check._run

    def flaky(command, args, stdin, env, timeout):
        if args[:2] == ["agent", "exec"]:
            raise OSError(7, "Argument list too long")
        return real(command, args, stdin, env, timeout)

    monkeypatch.setattr(install_check, "_run", flaky)
    export = run(stub_host, tarball, tmp_path)
    assert [s["name"] for s in export["steps"]] == list(install_check.STEPS)
    status = {s["name"]: s["status"] for s in export["steps"]}
    assert status["install"] == "passed"
    assert status["text-exchange"] == "not_run"
    text = next(s for s in export["steps"] if s["name"] == "text-exchange")
    assert "Argument list too long" in text["stderr"]
    assert status["tool-exchange"] == "not_run"
    for name in install_check.CLEANUP_STEPS:
        assert status[name] == "passed"
    assert export["status"] == "completed"
    assert export["passed"] is False


def test_the_export_is_written_when_the_temporary_home_cannot_be_removed(
    stub_host, tarball, tmp_path, monkeypatch
):
    """Windows may still hold a file in the host home; the evidence is written anyway."""
    real = shutil.rmtree

    def locked(path, *args, **kwargs):
        if "openclaw-install-check-" in str(path):
            raise PermissionError(13, "The process cannot access the file", str(path))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(install_check.shutil, "rmtree", locked)
    export = run(stub_host, tarball, tmp_path)
    assert export["passed"] is True
    assert json.loads((tmp_path / "export.json").read_text("utf-8"))["passed"] is True


def _message(role, content, tool_call_id=None):
    return SimpleNamespace(
        role=SimpleNamespace(value=role), content=content, tool_call_id=tool_call_id
    )


def _tool_runs(state):
    proposed = {
        "type": "tool.proposed",
        "payload": {"tool_name": "read", "proposal_id": "call_read_1"},
    }
    answer = {"type": "answer.delta", "payload": {"text": install_check.TOOL_ANSWER}}
    return [
        {"run_id": "r1", "state": "completed", "events": [proposed]},
        {"run_id": "r2", "state": state, "events": [answer]},
    ]


def test_a_tool_exchange_passes_only_on_completed_runs_with_the_gateway_answer():
    """The gateway trace decides: a failed run, or host text the gateway never sent, fails."""
    requests = [SimpleNamespace(messages=[_message("tool", install_check.NOTE_TEXT, "callread1")])]
    stdout = json.dumps({"ok": True, "payloads": [{"text": install_check.TOOL_ANSWER}]})
    assert install_check.tool_checks(stdout, _tool_runs("completed"), requests)["passed"] is True
    for state in ("failed", "cancelled"):
        checks = install_check.tool_checks(stdout, _tool_runs(state), requests)
        assert checks["passed"] is False, state
    no_answer = _tool_runs("completed")
    no_answer[1]["events"] = []
    checks = install_check.tool_checks(stdout, no_answer, requests)
    assert checks["gateway_answer"] == ""
    assert checks["passed"] is False


def test_each_exchange_runs_in_its_own_state_directory_inside_the_host_home(
    stub_host, tarball, tmp_path, monkeypatch
):
    """Section 12.5: the exchanges never depend on the host deleting its own temporary state.

    Without ``--state-dir`` the pinned host makes a temporary state directory
    for each ``agent exec`` and deletes it after the turn; on Windows that
    delete fails on a SQLite file the host still holds (EBUSY), so a turn the
    gateway answered ends ``ok: false``. Each exchange gets a fresh, empty
    state directory inside the isolated host home instead, which the host
    keeps and the check removes with the host home; the credential scan
    after removal covers it.
    """
    monkeypatch.setenv("STUB_EXEC_CLEANUP_LOCKED", "1")
    home = tmp_path / "host"
    export = run(stub_host, tarball, tmp_path, host_home=home)
    assert export["passed"] is True, export["steps"]
    execs = [c["args"] for c in calls(home) if c["args"][:2] == ["agent", "exec"]]
    assert len(execs) == 2
    dirs = [Path(a[a.index("--state-dir") + 1]) for a in execs]
    assert len(set(dirs)) == 2
    assert all(home in d.parents for d in dirs)
    assert all(home / "sandbox" not in d.parents for d in dirs)
    step = next(s for s in export["steps"] if s["name"] == "text-exchange")
    shown = step["argv"][step["argv"].index("--state-dir") + 1]
    assert shown.startswith(install_check.HOST_HOME)
