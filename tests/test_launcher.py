"""The operator launcher (#87; RM12, RM17, RM18, RM19, RM21, RM23, spec 0.21 section 14.1).

All model output comes from doubles; the local artifact is a tiny
synthetic GGUF file, and nothing here starts a real model server.
"""

import tomllib

import pytest

from api_helpers import call
from gateway_helpers import build_gateway
from helpers import ACCEPTED_PAYLOAD, BASELINE, REPO_ROOT, ScriptedAdapter
from launcher_helpers import toml_path, write_config
from relaymind.adapters.chat_completions import ChatCompletionsAdapter
from relaymind.adapters.llamacpp import LlamaCppAdapter
from relaymind.launcher import LaunchError, build_app, load_config, main
from relaymind.orchestrator.local_loop import LocalOrchestrator
from relaymind.orchestrator.run_state import RunState
from relaymind.store import RunStore
from relaymind.store.trace_event import EventType

EXTERNAL = (
    "\n[external_model]\nbase_url = 'https://example.invalid/v1'\n"
    "model = 'ext-1'\ncontext_limit = 16000\napi_key_env = 'TEST_EXTERNAL_KEY'\n"
)


def doubles():
    return {"local_adapter": ScriptedAdapter(turns=[]), "external_adapter": None}


def leave_unfinished_run(tmp_path):
    """A run a crashed process left in local_running, in the store the config names."""
    gateway = build_gateway(tmp_path, [], external_turns=[])
    machine = gateway.orchestrator.machine
    record = machine.create(accepted=(EventType.RUN_ACCEPTED, "gateway", ACCEPTED_PAYLOAD))
    for step in (RunState.ROUTING, RunState.LOCAL_RUNNING):
        record = machine.transition(record.run_id, record.version, step)
    gateway.store.close()
    return record.run_id


def test_defaults_and_declared_values(tmp_path):
    """Section 14.1: the documented defaults apply to omitted keys."""
    config = load_config(write_config(tmp_path))
    assert (config.host, config.port) == ("127.0.0.1", 8080)
    assert config.ui is None
    assert config.degraded_mode is False
    assert config.enabled_workflows == frozenset()
    assert config.external is None


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "example.com", "::"])
def test_public_bind_address_stops_startup_before_anything_opens(tmp_path, host):
    """RM19, section 7.1: a public address is refused, and no store file is created."""
    path = write_config(tmp_path, base=None)
    path.write_text(f"[server]\nhost = '{host}'\n" + path.read_text())
    with pytest.raises(LaunchError, match="refusing to bind"):
        build_app(load_config(path), **doubles())
    assert not (tmp_path / "runs.sqlite3").exists()


def test_manifest_hash_mismatch_stops_startup(tmp_path):
    """RM18, section 6: an artifact that differs from its manifest is refused."""
    path = write_config(tmp_path)
    (tmp_path / "tiny.gguf").write_bytes(b"GGUF" + b"\x00" * 32)
    with pytest.raises(LaunchError, match="sha256 mismatch"):
        build_app(load_config(path), **doubles())
    assert not (tmp_path / "runs.sqlite3").exists()


def test_missing_artifact_stops_startup(tmp_path):
    path = write_config(tmp_path)
    (tmp_path / "tiny.gguf").unlink()
    with pytest.raises(LaunchError, match="artifact file not found"):
        build_app(load_config(path), **doubles())


def test_invalid_skill_package_stops_startup(tmp_path):
    """RM06, section 4.2: an invalid skill root is an explicit startup error."""
    root = tmp_path / "skills"
    (root / "broken").mkdir(parents=True)
    (root / "broken" / "SKILL.md").write_text("no frontmatter here")
    path = write_config(tmp_path)
    path.write_text(path.read_text().replace(toml_path(REPO_ROOT / "skills"), toml_path(root)))
    with pytest.raises(LaunchError, match="broken"):
        build_app(load_config(path), **doubles())
    assert not (tmp_path / "runs.sqlite3").exists()


def test_missing_skill_root_stops_startup(tmp_path):
    path = write_config(tmp_path)
    path.write_text(
        path.read_text().replace(toml_path(REPO_ROOT / "skills"), toml_path(tmp_path / "nope"))
    )
    with pytest.raises(LaunchError):
        build_app(load_config(path), **doubles())


def test_ui_table_without_a_data_mode_stops_startup(tmp_path):
    """RM23, section 9: the data mode has no default."""
    with pytest.raises(LaunchError, match="mode"):
        load_config(write_config(tmp_path, "\n[ui]\narm_wait_seconds = 5\n"))


def test_unknown_data_mode_stops_startup(tmp_path):
    with pytest.raises(LaunchError, match="mode"):
        load_config(write_config(tmp_path, "\n[ui]\nmode = 'demo'\n"))


def test_unknown_workflow_stops_startup(tmp_path):
    """Section 4.4: an enabled workflow ID must be a registered one."""
    path = write_config(tmp_path, "\n[policy]\nenabled_workflows = ['made-up']\n")
    with pytest.raises(LaunchError, match="made-up"):
        build_app(load_config(path), **doubles())


@pytest.mark.parametrize(
    "extra, word",
    [
        ("\n[surprise]\nx = 1\n", "surprise"),
        ("\n[server]\nport = 'http'\n", "port"),
        ("\n[server]\nhostname = 'x'\n", "hostname"),
        (EXTERNAL + "api_key = 'sk-secret-value'\n", "api_key"),
        ("\n[policy]\nallowed_modes = ['sideways']\n", "sideways"),
    ],
)
def test_unknown_or_mistyped_settings_are_refused(tmp_path, extra, word):
    with pytest.raises(LaunchError, match=word) as raised:
        load_config(write_config(tmp_path, extra))
    assert "sk-secret-value" not in str(raised.value)


def test_unreadable_or_invalid_toml_is_a_launch_error(tmp_path):
    with pytest.raises(LaunchError):
        load_config(tmp_path / "missing.toml")
    bad = tmp_path / "bad.toml"
    bad.write_text("[server\n")
    with pytest.raises(LaunchError):
        load_config(bad)


def test_non_utf8_config_is_a_launch_error(tmp_path):
    """Section 14.1: an unreadable file is one explicit message, never a traceback."""
    bad = tmp_path / "bad.toml"
    bad.write_bytes("[server]\nhost = '127.0.0.1'\n".encode("utf-16"))
    with pytest.raises(LaunchError, match="cannot read"):
        load_config(bad)


@pytest.mark.parametrize(
    "extra, word",
    [
        ("\n[server]\nport = 70000\n", "port"),
        ("\n[server]\nport = 0\n", "port"),
        ("\n[native]\npoll_seconds = 0\n", "poll_seconds"),
        ("\n[native]\nheartbeat_seconds = -1\n", "heartbeat_seconds"),
        ("\n[chat]\nmax_completion_tokens = 0\n", "max_completion_tokens"),
        (EXTERNAL.replace("16000", "-5"), "context_limit"),
        ("\n[policy]\npermissions = ['demo.read', 'demo.mutatee']\n", "demo.mutatee"),
    ],
)
def test_out_of_range_or_unknown_values_are_refused(tmp_path, extra, word):
    """Section 14.1, RM03, RM08: a value that cannot work is a startup error, not a run-time one."""
    with pytest.raises(LaunchError, match=word):
        load_config(write_config(tmp_path, extra))


@pytest.mark.parametrize(
    "url", ["https://inference.example.com", "http://192.168.1.5:8033", "not a url"]
)
def test_local_model_must_be_a_loopback_server(tmp_path, url):
    """RM01, section 14.1: a remote 'local' model would make the local-first evidence false."""
    path = write_config(tmp_path)
    path.write_text(path.read_text() + f"base_url = {url!r}\n")
    with pytest.raises(LaunchError, match="base_url"):
        load_config(path)


def test_malformed_sandbox_seed_stops_startup(tmp_path):
    """Section 9, 14.1: a seed that is not JSON is a startup error before the store opens."""
    seed = tmp_path / "seed.json"
    seed.write_text("{not json")
    path = write_config(tmp_path)
    path.write_text(path.read_text().replace(toml_path(BASELINE), toml_path(seed)))
    with pytest.raises(LaunchError, match="sandbox"):
        build_app(load_config(path), **doubles())
    assert not (tmp_path / "runs.sqlite3").exists()


@pytest.mark.parametrize("kind", ["not_a_database", "directory"])
def test_unusable_run_store_stops_startup(tmp_path, kind):
    """Section 5, 14.1: a store that cannot open is one explicit message."""
    store = tmp_path / "runs.sqlite3"
    if kind == "directory":
        store.mkdir()
    else:
        store.write_bytes(b"this is not a sqlite database" * 100)
    with pytest.raises(LaunchError, match="run store"):
        build_app(load_config(write_config(tmp_path)), **doubles())


def test_store_is_closed_when_a_later_startup_step_fails(tmp_path, monkeypatch):
    """Section 14.1: a refused start leaves no open store connection behind."""
    closed = []
    original = RunStore.close

    def spy(self):
        closed.append(1)
        original(self)

    def boom(self):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(RunStore, "close", spy)
    monkeypatch.setattr(LocalOrchestrator, "recover_after_restart", boom)
    with pytest.raises(RuntimeError):
        build_app(load_config(write_config(tmp_path)), **doubles())
    assert closed


def test_ipv6_loopback_address_is_printed_as_a_valid_url(tmp_path):
    """Section 14.1: the printed bind address is a URL a client can use."""
    path = write_config(tmp_path)
    path.write_text("[server]\nhost = '::1'\n" + path.read_text())
    stdout, served = [], []
    code = main(
        ["serve", "--config", str(path)],
        serve_fn=lambda app, *, host, port: served.append(host),
        stdout=stdout.append,
        stderr=stdout.append,
        **doubles(),
    )
    assert code == 0
    assert "Serving on http://[::1]:8080" in stdout


def test_app_serves_the_token_protected_endpoints_over_the_wired_gateway(tmp_path):
    """RM19: both aliases are served, and the generated token is required."""
    built = build_app(load_config(write_config(tmp_path)), **doubles())
    token = built.app.state.application_token
    assert len(token) >= 32
    assert call(built.app, "GET", "/v1/models", auth=False).status_code == 401
    ids = [m["id"] for m in call(built.app, "GET", "/v1/models").json()["data"]]
    assert ids == ["relaymind", "relaymind-client-tools"]
    assert call(built.app, "GET", "/ui", auth=False).status_code == 404


def test_ui_is_served_only_with_a_declared_mode(tmp_path):
    """RM23: the declared mode appears in the page."""
    built = build_app(load_config(write_config(tmp_path, "\n[ui]\nmode = 'double'\n")), **doubles())
    response = call(built.app, "GET", "/ui", auth=False)
    assert response.status_code == 200
    assert "DOUBLE" in response.text


def test_policy_reaches_the_orchestrator_and_the_api(tmp_path):
    """Section 11, 4.4, 7.1: degraded mode, workflows, and allowed modes are wired."""
    extra = (
        "\n[policy]\ndegraded_mode = true\n"
        "enabled_workflows = ['health-and-diagnostic-checklist']\n"
        "allowed_modes = ['auto', 'local_only']\n"
    )
    built = build_app(load_config(write_config(tmp_path, extra)), **doubles())
    assert built.orchestrator.degraded_mode is True
    assert built.orchestrator.enabled_workflows == {"health-and-diagnostic-checklist"}
    refused = call(
        built.app,
        "POST",
        "/v1/chat/completions",
        json_body={"model": "relaymind", "messages": [{"role": "user", "content": "hi"}]},
        headers={"X-RelayMind-Execution-Mode": "external_only"},
    )
    assert refused.status_code == 403


def test_real_adapters_are_built_from_the_manifest_and_config(tmp_path):
    """RM18: the local adapter uses the manifest's context size; the external one the file."""
    built = build_app(load_config(write_config(tmp_path, EXTERNAL)))
    local, external = built.orchestrator.model, built.orchestrator.external
    assert isinstance(local.adapter, LlamaCppAdapter)
    assert local.model_id == "tiny-double"
    assert local.adapter._config.context_limit == 4096
    assert local.adapter._config.chat_template_kwargs == {"enable_thinking": False}
    assert isinstance(external.adapter, ChatCompletionsAdapter)
    assert external.model_id == "ext-1"
    assert external.adapter._config.api_key_env == "TEST_EXTERNAL_KEY"


def test_without_an_external_table_no_external_model_exists(tmp_path):
    built = build_app(load_config(write_config(tmp_path)), **doubles())
    assert built.orchestrator.external is None


def test_recovery_runs_once_before_the_server_and_fails_the_unfinished_run(tmp_path, monkeypatch):
    """Section 5, RM17: unfinished runs end as interrupted before serving starts."""
    run_id = leave_unfinished_run(tmp_path)
    calls = []
    original = LocalOrchestrator.recover_after_restart

    def spy(self):
        calls.append(1)
        return original(self)

    monkeypatch.setattr(LocalOrchestrator, "recover_after_restart", spy)
    seen = {}

    def serve(app, *, host, port):
        seen["calls_at_serve"] = len(calls)
        store = RunStore(tmp_path / "runs.sqlite3")
        seen["record"] = store.get_run(run_id)
        store.close()

    out = []
    code = main(
        ["serve", "--config", str(write_config(tmp_path))],
        serve_fn=serve,
        stdout=out.append,
        stderr=out.append,
        **doubles(),
    )
    assert code == 0
    assert seen["calls_at_serve"] == 1
    assert len(calls) == 1
    assert (seen["record"].state, seen["record"].error_code) == ("failed", "interrupted")
    assert any("1 unfinished" in line for line in out)


def test_token_is_printed_once_and_nowhere_else(tmp_path, caplog):
    """RM12, section 14.1: one stdout line carries the token; no log or stderr line does."""
    stdout, stderr, served = [], [], []
    caplog.set_level("DEBUG")
    code = main(
        ["serve", "--config", str(write_config(tmp_path))],
        serve_fn=lambda app, *, host, port: served.append((app, host, port)),
        stdout=stdout.append,
        stderr=stderr.append,
        **doubles(),
    )
    assert code == 0
    ((app, host, port),) = served
    token = app.state.application_token
    carrying = [line for line in stdout if token in line]
    assert len(carrying) == 1
    assert carrying[0].startswith("Application token: ")
    assert not any(token in line for line in stderr)
    assert token not in caplog.text
    assert (host, port) == ("127.0.0.1", 8080)


def test_startup_error_exits_2_with_one_message_and_no_server(tmp_path):
    """Section 14.1: nothing is served after a refused start; no traceback."""
    path = write_config(tmp_path)
    path.write_text("[server]\nhost = '0.0.0.0'\n" + path.read_text())
    stdout, stderr, served = [], [], []
    code = main(
        ["serve", "--config", str(path)],
        serve_fn=lambda *a, **k: served.append(1),
        stdout=stdout.append,
        stderr=stderr.append,
        **doubles(),
    )
    assert code == 2
    assert served == [] and stdout == []
    (message,) = stderr
    assert message.startswith("relaymind: cannot start: ") and "refusing to bind" in message
    assert "Traceback" not in message


@pytest.mark.parametrize("extra", [["--api-key", "sk-x"], ["--api-key=sk-x"], ["sk-x"]])
def test_credentials_are_never_a_command_line_option(tmp_path, capsys, extra):
    """RM12, section 14.1: the command accepts only --config and never echoes a refused value."""
    with pytest.raises(SystemExit) as raised:
        main(["serve", "--config", str(write_config(tmp_path)), *extra])
    assert raised.value.code == 2
    captured = capsys.readouterr()
    assert "sk-x" not in captured.out
    assert "sk-x" not in captured.err
    assert "--config" in captured.err


def test_missing_external_credential_does_not_stop_startup_or_leak(tmp_path, monkeypatch):
    """Section 14.1: the adapter reports a missing credential at call time (RM16)."""
    monkeypatch.delenv("TEST_EXTERNAL_KEY", raising=False)
    built = build_app(load_config(write_config(tmp_path, EXTERNAL)))
    assert built.orchestrator.external is not None


def test_console_script_is_declared():
    """Section 14.1: the start command is the relaymind console script."""
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["scripts"]["relaymind"] == "relaymind.launcher:cli"


def test_documented_command_replaces_the_hand_written_script():
    """Done when: README, TESTING, and ROADMAP name the command."""
    for name in ("README.md", "docs/TESTING.md", "docs/ROADMAP.md"):
        assert "relaymind serve --config" in (REPO_ROOT / name).read_text(encoding="utf-8"), name


def test_example_config_is_valid():
    """The checked-in example loads, so the documented command starts from a sound file."""
    config = load_config(REPO_ROOT / "relaymind.example.toml")
    assert config.host == "127.0.0.1" and config.ui is not None
