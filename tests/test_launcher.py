"""The operator launcher (#87; RM12, RM17, RM18, RM19, RM21, RM23, spec 0.21 section 14.1).

All model output comes from doubles; the local artifact is a tiny
synthetic GGUF file, and nothing here starts a real model server.
"""

import asyncio
import dataclasses
import json
import re
import tomllib

import httpx
import pytest

from api_helpers import call
from gateway_helpers import STAGING_OK, build_gateway, clarify_turn
from helpers import (
    ACCEPTED_PAYLOAD,
    BASELINE,
    REPO_ROOT,
    ScriptedAdapter,
    delta_chunk,
    sse_response,
)
from launcher_helpers import toml_path, write_config
from relaymind import launcher
from relaymind.adapters.chat_completions import ChatCompletionsAdapter
from relaymind.adapters.llamacpp import REQUEST_TIMEOUT_SECONDS, LlamaCppAdapter, LlamaCppConfig
from relaymind.launcher import LaunchError, build_app, load_config, main
from relaymind.orchestrator.budgets import LocalBudgets
from relaymind.orchestrator.local_loop import LocalOrchestrator
from relaymind.orchestrator.local_status import LocalStatus
from relaymind.orchestrator.performance import PerformanceSettings
from relaymind.orchestrator.route_decision import ExecutionMode
from relaymind.orchestrator.run_state import RunState
from relaymind.selection import SelectionSettings
from relaymind.store import RunStore, RunStoreError
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


@pytest.mark.parametrize("step", ["recover_after_restart", "replace", "create_app", "Launched"])
def test_store_is_closed_when_a_later_startup_step_fails(tmp_path, monkeypatch, step):
    """RM17, section 14.1: every failed startup after opening the store closes it."""
    config = load_config(write_config(tmp_path, "\n[ui]\nmode = 'double'\n"))
    stores = []

    def open_store(path):
        store = RunStore(path)
        stores.append(store)
        return store

    failure = RuntimeError("startup failed")

    def boom(*args, **kwargs):
        raise failure

    monkeypatch.setattr(launcher, "RunStore", open_store)
    target = LocalOrchestrator if step == "recover_after_restart" else launcher
    monkeypatch.setattr(target, step, boom)
    try:
        with pytest.raises(RuntimeError) as raised:
            build_app(config, **doubles())
        assert raised.value is failure
        (store,) = stores
        with pytest.raises(RunStoreError, match="closed"):
            store.events("unused-run")
    finally:
        for store in stores:
            store.close()


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


def test_enabling_the_workflow_makes_parallel_demo_ready(tmp_path):
    """RM15, RM21, spec 0.25 section 7.4: enabled_workflows makes parallel_demo ready."""
    extra = (
        EXTERNAL
        + "\n[policy]\nenabled_workflows = ['health-and-diagnostic-checklist']\n"
        + "allowed_modes = ['parallel_demo']\n"
    )
    external = ScriptedAdapter(turns=[], locality="external")
    built = build_app(
        load_config(write_config(tmp_path, extra)),
        local_adapter=ScriptedAdapter(turns=[]),
        external_adapter=external,
    )
    response = call(built.app, "GET", "/readyz")
    assert response.status_code == 200
    assert response.json()["modes"]["parallel_demo"] == {"ready": True, "reason": None}
    # Without the workflow named, the same gateway reports it not ready.
    (tmp_path / "bare").mkdir()
    bare = build_app(
        load_config(
            write_config(
                tmp_path / "bare", EXTERNAL + "\n[policy]\nallowed_modes = ['parallel_demo']\n"
            )
        ),
        local_adapter=ScriptedAdapter(turns=[]),
        external_adapter=ScriptedAdapter(turns=[], locality="external"),
    )
    assert call(bare.app, "GET", "/readyz").status_code == 503


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


@pytest.mark.parametrize(
    "extra", [["--api-key", "sk-x"], ["--api-key=sk-x"], ["sk-x"], ["--sk-x"], ["--help=sk-x"]]
)
@pytest.mark.parametrize("position", ["after", "before", "alone"])
def test_credentials_are_never_a_command_line_option(tmp_path, capsys, caplog, extra, position):
    """RM12, section 14.1: refused values are never echoed, in any argument position."""
    command = ["serve", "--config", str(write_config(tmp_path))]
    argv = {"after": command + extra, "before": extra + command, "alone": extra}[position]
    with pytest.raises(SystemExit) as raised:
        main(argv)
    assert raised.value.code == 2
    captured = capsys.readouterr()
    assert "sk-x" not in captured.out + captured.err + caplog.text
    assert "--config" in captured.err
    assert not (tmp_path / "runs.sqlite3").exists()


def test_config_path_is_not_echoed_when_serve_is_missing(capsys):
    """RM12, section 14.1: a rejected config path is also an untrusted argument."""
    with pytest.raises(SystemExit) as raised:
        main(["--config", "private-config.toml"])
    assert raised.value.code == 2
    captured = capsys.readouterr()
    assert "private-config.toml" not in captured.out + captured.err
    assert "--config" in captured.err


@pytest.mark.parametrize("argv", [["--help"], ["serve", "--help"]])
def test_command_help_is_available_without_starting_the_gateway(capsys, argv):
    """RM19, section 14.1: help succeeds without configuration or opening the server."""
    with pytest.raises(SystemExit) as raised:
        main(argv)
    assert raised.value.code == 0
    captured = capsys.readouterr()
    assert "usage:" in captured.out and "serve" in captured.out
    assert captured.err == ""


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


# -- the [budgets] table (#142; RM03, RM17, RM19, sections 11 and 14.1) -----------------

BUDGET_FIELDS = tuple(f.name for f in dataclasses.fields(LocalBudgets))


def test_budget_keys_are_the_local_budgets_fields():
    """Section 14.1: the table's keys are the section 11 limit names, derived from one list."""
    assert set(launcher._TABLES["budgets"]) == set(BUDGET_FIELDS)
    assert "active_seconds" in BUDGET_FIELDS and "external_seconds" in BUDGET_FIELDS


def test_without_the_table_the_budget_defaults_apply(tmp_path):
    """RM03, section 14.1: a file without [budgets] keeps the section 11 defaults."""
    config = load_config(write_config(tmp_path))
    assert config.budgets == LocalBudgets()
    built = build_app(config, **doubles())
    assert built.orchestrator.budgets == LocalBudgets()


def test_every_budget_key_reaches_local_budgets(tmp_path):
    """RM03, section 14.1: each key sets its limit; the others keep the default."""
    values = {
        "routing_generations": 2,
        "local_generations": 5,
        "tool_calls": 3,
        "repair_generations": 2,
        "active_seconds": 12,
        "routing_input_tokens": 4096,
        "routing_output_tokens": 256,
        "run_active_seconds": 90.5,
        "run_generations": 10,
        "run_tool_executions": 7,
        "waiting_seconds": 120,
        "clarification_rounds": 1,
        "external_generations": 4,
        "external_seconds": 45.5,
    }
    assert set(values) == set(BUDGET_FIELDS)
    lines = "".join(f"{key} = {value}\n" for key, value in values.items())
    config = load_config(write_config(tmp_path, f"\n[budgets]\n{lines}"))
    assert config.budgets == LocalBudgets(**values)
    one = tmp_path / "one"
    one.mkdir()
    single = load_config(write_config(one, "\n[budgets]\nactive_seconds = 9\n"))
    assert single.budgets == dataclasses.replace(LocalBudgets(), active_seconds=9)


@pytest.mark.parametrize(
    "line, word",
    [
        ("active_seconds = 'fast'", "active_seconds"),
        ("local_generations = true", "local_generations"),
        ("active_seconds = false", "active_seconds"),
        ("local_generations = 2.5", "local_generations"),
        ("tool_calls = 1.0", "tool_calls"),
        ("active_seconds = inf", "active_seconds"),
        ("active_seconds = nan", "active_seconds"),
        ("external_seconds = -inf", "external_seconds"),
        ("active_seconds = 0", "active_seconds"),
        ("active_seconds = -1", "active_seconds"),
        ("local_generations = 0", "local_generations"),
        ("run_generations = 0", "run_generations"),
        ("routing_input_tokens = 0", "routing_input_tokens"),
        ("tool_calls = -1", "tool_calls"),
        ("repair_generations = 4", "repair"),
        ("local_generations = 1\nrepair_generations = 2", "repair"),
        ("surprise = 1", "surprise"),
    ],
)
def test_a_bad_budget_value_is_one_startup_message(tmp_path, line, word):
    """RM03, section 14.1: a wrong type or range is exit status 2 with one message."""
    config = write_config(tmp_path, f"\n[budgets]\n{line}\n")
    with pytest.raises(LaunchError, match=r"\[budgets\].*" + word) as raised:
        load_config(config)
    assert "\n" not in str(raised.value)
    out, err = [], []
    assert main(["serve", "--config", str(config)], stdout=out.append, stderr=err.append) == 2
    assert out == [] and len(err) == 1 and "[budgets]" in err[0]


def test_budgets_table_must_be_a_table(tmp_path):
    path = write_config(tmp_path, base=None)
    path.write_text("budgets = 5\n" + path.read_text())
    with pytest.raises(LaunchError, match="budgets must be a table"):
        load_config(path)


def test_the_configured_budgets_are_recorded_in_run_accepted(tmp_path):
    """RM03, RM17: run.accepted holds the limits in force; a raised limit is on the record."""
    extra = "\n[budgets]\nactive_seconds = 20\nlocal_generations = 4\nrun_generations = 9\n"
    built = build_app(
        load_config(write_config(tmp_path, extra)),
        local_adapter=ScriptedAdapter(turns=[clarify_turn()]),
    )
    outcome = asyncio.run(built.orchestrator.run_text("Which service is it?"))
    accepted = built.orchestrator.store.events(outcome.run_id)[0]
    assert accepted.event_type is EventType.RUN_ACCEPTED
    recorded = accepted.payload["budgets"]
    assert recorded["active_seconds"] == 20.0
    assert recorded["local_generations"] == 4
    assert recorded["run_generations"] == 9
    assert recorded == {
        **LocalBudgets(active_seconds=20, local_generations=4, run_generations=9).to_json_object(),
        # Spec 0.31 section 8: the selection time limit is recorded with the limits (RM28).
        "selection.timeout_seconds": 2.0,
        "placement.margin": 2,
        "placement.probe_after_seconds": 600,
    }
    # The default file records the section 11 defaults.
    plain = tmp_path / "plain"
    plain.mkdir()
    built = build_app(
        load_config(write_config(plain)), local_adapter=ScriptedAdapter(turns=[clarify_turn()])
    )
    outcome = asyncio.run(built.orchestrator.run_text("Which service is it?"))
    accepted = built.orchestrator.store.events(outcome.run_id)[0]
    assert accepted.payload["budgets"] == {
        **LocalBudgets().to_json_object(),
        "selection.timeout_seconds": 2.0,
        "placement.margin": 2,
        "placement.probe_after_seconds": 600,
    }


def _read_timeouts(built):
    """The read timeouts of the real local and external adapters a built gateway holds."""
    orchestrator = built.orchestrator
    return (
        orchestrator.model.adapter._client.timeout.read,
        orchestrator.external.adapter._client.timeout.read,
    )


def test_adapter_read_timeouts_follow_the_raised_budgets(tmp_path):
    """RM03: a raised active_seconds or external_seconds is not cut off by a hidden 30 s limit."""
    extra = EXTERNAL + "\n[budgets]\nactive_seconds = 90\nexternal_seconds = 120\n"
    built = build_app(load_config(write_config(tmp_path, extra)))
    try:
        assert _read_timeouts(built) == (90.0, 120.0)
    finally:
        built.orchestrator.store.close()


def test_the_local_read_timeout_floor_is_the_adapter_config_default():
    """Section 11.2, RM03: the launcher's floor is the field default, read from the module.

    The launcher does not read the default off the dataclass itself, so
    a later field(default=...) cannot turn the floor into a Field object.
    """
    default = {f.name: f.default for f in dataclasses.fields(LlamaCppConfig)}
    assert default["request_timeout_seconds"] == REQUEST_TIMEOUT_SECONDS == 30.0
    assert launcher.REQUEST_TIMEOUT_SECONDS is REQUEST_TIMEOUT_SECONDS


def test_adapter_read_timeouts_keep_the_30_second_floor(tmp_path):
    """Section 11.2: budgets at or under 30 s keep the 30 s adapter timeout."""
    built = build_app(load_config(write_config(tmp_path, EXTERNAL)))
    try:
        assert _read_timeouts(built) == (30.0, 30.0)
    finally:
        built.orchestrator.store.close()


def test_example_config_documents_the_budgets_table(tmp_path):
    """Section 14.1: the commented [budgets] block loads when uncommented and holds the defaults."""
    lines = (REPO_ROOT / "relaymind.example.toml").read_text(encoding="utf-8").splitlines()
    start = lines.index("# [budgets]")
    block = []
    for line in lines[start:]:
        if not line.startswith("# "):
            break
        block.append(line[2:])
    assert len(block) == 1 + len(BUDGET_FIELDS)
    config = load_config(write_config(tmp_path, "\n" + "\n".join(block) + "\n"))
    assert config.budgets == LocalBudgets()


# -- [external_model] request_options (#120; RM12, spec 0.31 sections 6 and 14.1) -----------------


def options_config(options_toml: str) -> str:
    return EXTERNAL + f"\n[external_model.request_options]\n{options_toml}\n"


def test_request_options_reach_the_adapter_config_verbatim(tmp_path):
    """Spec 0.31 section 14.1: a table of provider request fields, kept as written."""
    extra = options_config("seed = 7\nthinking = { type = 'disabled' }\nlogprobs = false")
    config = load_config(write_config(tmp_path, extra))
    assert dict(config.external.request_options) == {
        "seed": 7,
        "thinking": {"type": "disabled"},
        "logprobs": False,
    }
    built = build_app(config)
    try:
        assert built.orchestrator.external.adapter.request_options == {
            "seed": 7,
            "thinking": {"type": "disabled"},
            "logprobs": False,
        }
    finally:
        built.orchestrator.store.close()


def test_without_request_options_the_config_holds_an_empty_table(tmp_path):
    """Spec 0.33 section 8: an empty table declares nothing, so model.started records nothing."""
    config = load_config(write_config(tmp_path, EXTERNAL))
    assert dict(config.external.request_options) == {}


def test_request_options_are_sent_to_the_provider_and_recorded(tmp_path, monkeypatch):
    """Spec 0.31 sections 6 and 8: the body carries them and model.started records them."""
    monkeypatch.setenv("TEST_EXTERNAL_KEY", "sk-test-123")
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return sse_response(
            [delta_chunk({"content": STAGING_OK}), delta_chunk({}, finish_reason="stop")]
        )

    extra = options_config("thinking = { type = 'disabled' }")
    config = load_config(write_config(tmp_path, extra))
    adapter = ChatCompletionsAdapter(config.external, transport=httpx.MockTransport(handler))
    built = build_app(config, local_adapter=ScriptedAdapter(turns=[]), external_adapter=adapter)
    try:
        outcome = asyncio.run(
            built.orchestrator.run_text(
                "Is staging-api healthy right now?", execution_mode="external_only"
            )
        )
        assert bodies and all(b["thinking"] == {"type": "disabled"} for b in bodies)
        events = built.orchestrator.store.events(outcome.run_id)
        started = [e for e in events if e.event_type is EventType.MODEL_STARTED]
        assert started and all(
            e.payload["request_options"] == {"thinking": {"type": "disabled"}} for e in started
        )
    finally:
        built.orchestrator.store.close()


@pytest.mark.parametrize(
    "key",
    [
        "api_key",
        "API_KEY",
        "Authorization",
        "authorization",
        "headers",
        "extra_headers",
        "Extra-Headers",
        "model",
        "messages",
        "tools",
        "stream",
        "stream_options",
    ],
)
def test_a_credential_header_or_override_key_stops_startup_without_its_value(tmp_path, key):
    """RM12, spec 0.31 section 14.1: the message names the key, never the value."""
    extra = options_config(f"{key} = 'sk-secret-value-123'")
    with pytest.raises(LaunchError, match="request_options") as raised:
        load_config(write_config(tmp_path, extra))
    assert key in str(raised.value)
    assert "sk-secret-value-123" not in str(raised.value)
    out, err = [], []
    assert (
        main(
            ["serve", "--config", str(tmp_path / "relaymind.toml")],
            stdout=out.append,
            stderr=err.append,
        )
        == 2
    )
    assert out == [] and len(err) == 1 and "sk-secret-value-123" not in err[0]


@pytest.mark.parametrize("key", ["max_tokens", "temperature", "top_p", "Max-Tokens"])
def test_a_sampling_option_key_is_refused_so_the_trace_matches_the_body(tmp_path, key):
    """RM16, spec 0.31 section 8: a request sets these, so a declared one would be replaced."""
    extra = options_config(f"{key} = 100")
    with pytest.raises(LaunchError, match="request_options") as raised:
        load_config(write_config(tmp_path, extra))
    assert key in str(raised.value)


def test_a_nested_credential_key_is_refused_too(tmp_path):
    """A header or credential key one level down is still a credential (RM12)."""
    extra = options_config("extra_body = { headers = { Authorization = 'Bearer sk-secret-9' } }")
    with pytest.raises(LaunchError, match="request_options") as raised:
        load_config(write_config(tmp_path, extra))
    assert "sk-secret-9" not in str(raised.value)


def test_a_value_that_is_not_json_is_refused(tmp_path):
    """Spec 0.31 section 14.1: a TOML date cannot be sent as a provider field."""
    extra = options_config("since = 2026-10-23")
    with pytest.raises(LaunchError, match="since") as raised:
        load_config(write_config(tmp_path, extra))
    assert "2026" not in str(raised.value)


def test_request_options_must_be_a_table(tmp_path):
    extra = EXTERNAL.replace("api_key_env", "request_options = 'off'\napi_key_env")
    with pytest.raises(LaunchError, match="request_options"):
        load_config(write_config(tmp_path, extra))


def test_example_config_documents_request_options(tmp_path):
    """Spec 0.31 section 14.1: the commented external_model block loads when uncommented."""
    lines = (REPO_ROOT / "relaymind.example.toml").read_text(encoding="utf-8").splitlines()
    block = []
    for line in lines[lines.index("# [external_model]") :]:
        if not line.startswith("#"):
            break
        if re.fullmatch(r"# (\[[a-z_.]+\]|[a-z_]+ = .*)", line):
            block.append(line[2:])
    config = load_config(write_config(tmp_path, "\n" + "\n".join(block) + "\n"))
    assert config.external.model == "your-model-id"
    assert dict(config.external.request_options) == {"thinking": {"type": "disabled"}}


# -- the [performance] table (#121, #140; RM31, RM32, spec 0.32 section 14.1) -----------------

PERFORMANCE_KEYS = {
    "answer_pass",
    "answer_stream",
    "calls_per_generation",
    "prefetch",
    "early_headers",
}


def test_performance_keys_are_the_settings_fields():
    """Section 14.1: the table's keys are the PerformanceSettings fields, from one list."""
    fields = {f.name for f in dataclasses.fields(PerformanceSettings)}
    assert set(launcher._TABLES["performance"]) == fields == PERFORMANCE_KEYS


def test_without_the_table_the_performance_defaults_apply(tmp_path):
    """RM31, RM32, section 14.1: a file without [performance] runs the baseline."""
    config = load_config(write_config(tmp_path))
    assert config.performance == PerformanceSettings()
    built = build_app(config, **doubles())
    assert built.orchestrator.performance == PerformanceSettings()


def test_every_performance_key_reaches_the_settings(tmp_path):
    """Section 14.1: each key sets its option; the others keep the default."""
    extra = (
        "\n[performance]\nanswer_pass = 'draft'\nanswer_stream = 'buffered'\n"
        "calls_per_generation = 'many'\nprefetch = true\nearly_headers = true\n"
    )
    config = load_config(write_config(tmp_path, extra))
    chosen = PerformanceSettings(
        answer_pass="draft", calls_per_generation="many", prefetch=True, early_headers=True
    )
    assert config.performance == chosen
    built = build_app(config, **doubles())
    assert built.orchestrator.performance == chosen
    one = tmp_path / "one"
    one.mkdir()
    single = load_config(write_config(one, "\n[performance]\nearly_headers = true\n"))
    assert single.performance == PerformanceSettings(early_headers=True)


@pytest.mark.parametrize(
    "line, word",
    [
        ("answer_pass = 'later'", "answer_pass"),
        ("answer_pass = true", "answer_pass"),
        ("answer_pass = 1", "answer_pass"),
        ("calls_per_generation = 'two'", "calls_per_generation"),
        ("calls_per_generation = 2", "calls_per_generation"),
        ("prefetch = 'yes'", "prefetch"),
        ("prefetch = 1", "prefetch"),
        ("early_headers = 'true'", "early_headers"),
        ("early_headers = 0", "early_headers"),
        ("answer_stream = 'chunked'", "answer_stream"),
        ("surprise = 1", "surprise"),
    ],
)
def test_a_bad_performance_value_is_one_startup_message(tmp_path, line, word):
    """RM32, section 14.1: a wrong performance value is exit 2."""
    config = write_config(tmp_path, f"\n[performance]\n{line}\n")
    with pytest.raises(LaunchError, match=r"\[performance\].*" + word) as raised:
        load_config(config)
    assert "\n" not in str(raised.value)
    out, err = [], []
    assert main(["serve", "--config", str(config)], stdout=out.append, stderr=err.append) == 2
    assert out == [] and len(err) == 1 and "[performance]" in err[0]


@pytest.mark.parametrize(
    "budget, word",
    [("tool_calls = 0", "tool_calls"), ("run_tool_executions = 0", "run_tool_executions")],
)
def test_prefetch_that_does_not_fit_the_budgets_is_one_startup_message(tmp_path, budget, word):
    """RM29, section 14.1: a skill whose prefetch calls alone exceed a limit is exit 2."""
    extra = f"\n[performance]\nprefetch = true\n\n[budgets]\n{budget}\n"
    config = write_config(tmp_path, extra)
    out, err = [], []
    status = main(
        ["serve", "--config", str(config)], stdout=out.append, stderr=err.append, **doubles()
    )
    assert status == 2
    assert out == [] and len(err) == 1 and "\n" not in err[0]
    assert "[performance] prefetch" in err[0] and word in err[0]


def test_the_same_budgets_start_while_the_declarations_are_idle(tmp_path):
    """Section 14.1: with prefetch false the declarations are not checked against the limits."""
    config = write_config(tmp_path, "\n[budgets]\ntool_calls = 0\nrun_tool_executions = 0\n")
    assert build_app(load_config(config), **doubles()).orchestrator.performance.prefetch is False


def test_prefetch_within_the_budgets_starts(tmp_path):
    """RM29: the default limits hold every demo skill's declarations."""
    extra = "\n[performance]\nprefetch = true\ncalls_per_generation = 'many'\n"
    built = build_app(load_config(write_config(tmp_path, extra)), **doubles())
    assert built.orchestrator.performance.prefetch is True


def test_performance_table_must_be_a_table(tmp_path):
    path = write_config(tmp_path, base=None)
    path.write_text("performance = 5\n" + path.read_text())
    with pytest.raises(LaunchError, match="performance must be a table"):
        load_config(path)


def test_the_configured_performance_options_are_recorded_in_run_accepted(tmp_path):
    """RM22, spec 0.32 section 8: run.accepted names the options in force."""
    extra = "\n[performance]\nanswer_pass = 'draft'\nearly_headers = true\n"
    built = build_app(
        load_config(write_config(tmp_path, extra)),
        local_adapter=ScriptedAdapter(turns=[clarify_turn()]),
    )
    outcome = asyncio.run(built.orchestrator.run_text("Which service is it?"))
    accepted = built.orchestrator.store.events(outcome.run_id)[0]
    recorded = {key: accepted.payload[key] for key in PERFORMANCE_KEYS}
    assert recorded == PerformanceSettings(answer_pass="draft", early_headers=True).to_json_object()


def test_example_config_documents_the_performance_table(tmp_path):
    """Section 14.1: the commented [performance] block loads when uncommented, with the defaults."""
    lines = (REPO_ROOT / "relaymind.example.toml").read_text(encoding="utf-8").splitlines()
    start = lines.index("# [performance]")
    block = []
    for line in lines[start:]:
        if not line.startswith("# "):
            break
        block.append(line[2:])
    assert len(block) == 1 + len(PERFORMANCE_KEYS)
    config = load_config(write_config(tmp_path, "\n" + "\n".join(block) + "\n"))
    assert config.performance == PerformanceSettings()


# -- the [selection] table (#131, #132; RM28, spec 0.32 section 14.1) -------------------------

SELECTION_KEYS = {"chain", "timeout_seconds", "example_match_score", "example_match_margin"}


def test_selection_keys_are_the_settings_fields():
    """Section 14.1: the table's keys are the SelectionSettings fields, from one list."""
    fields = {f.name for f in dataclasses.fields(SelectionSettings)}
    assert set(launcher._TABLES["selection"]) == fields == SELECTION_KEYS


def test_without_the_table_the_baseline_chain_applies(tmp_path):
    """RM28, section 14.1: a file without [selection] runs model_router alone."""
    config = load_config(write_config(tmp_path))
    assert config.selection == SelectionSettings()
    built = build_app(config, **doubles())
    assert built.orchestrator.selection_chain.names == ("model_router",)


def test_every_selection_key_reaches_the_settings(tmp_path):
    extra = (
        "\n[selection]\nchain = ['example_match', 'model_router']\ntimeout_seconds = 0.5\n"
        "example_match_score = 0.7\nexample_match_margin = 0\n"
    )
    config = load_config(write_config(tmp_path, extra))
    assert config.selection == SelectionSettings(
        chain=("example_match", "model_router"),
        timeout_seconds=0.5,
        example_match_score=0.7,
        example_match_margin=0.0,
    )
    built = build_app(config, **doubles())
    assert built.orchestrator.selection_chain.names == ("example_match", "model_router")
    assert built.orchestrator.selection_chain.timeout_seconds == 0.5


@pytest.mark.parametrize(
    "line, word",
    [
        ("chain = []", "chain"),
        ("chain = 'model_router'", "chain"),
        ("chain = ['model_router', 'model_router']", "chain"),
        ("chain = ['oracle']", "chain"),
        ("chain = [1]", "chain"),
        ("chain = [{ transport = 'http', url = 'http://127.0.0.1:9000/select' }]", "name, version"),
        ("timeout_seconds = 0", "timeout_seconds"),
        ("timeout_seconds = -1", "timeout_seconds"),
        ("timeout_seconds = inf", "timeout_seconds"),
        ("timeout_seconds = true", "timeout_seconds"),
        ("timeout_seconds = '2'", "timeout_seconds"),
        ("example_match_score = 1.5", "example_match_score"),
        ("example_match_score = -0.1", "example_match_score"),
        ("example_match_score = nan", "example_match_score"),
        ("example_match_score = true", "example_match_score"),
        ("example_match_margin = '0.1'", "example_match_margin"),
        ("example_match_margin = 2", "example_match_margin"),
        ("surprise = 1", "surprise"),
    ],
)
def test_a_bad_selection_value_is_one_startup_message(tmp_path, line, word):
    """RM28, section 14.1: a wrong type or range, or an incomplete HTTP selector item, is exit 2."""
    config = write_config(tmp_path, f"\n[selection]\n{line}\n")
    with pytest.raises(LaunchError, match=r"\[selection\].*" + word) as raised:
        load_config(config)
    assert "\n" not in str(raised.value)
    out, err = [], []
    assert main(["serve", "--config", str(config)], stdout=out.append, stderr=err.append) == 2
    assert out == [] and len(err) == 1 and "[selection]" in err[0]


@pytest.mark.parametrize(
    "url",
    [
        "http://192.0.2.10:8051/select",
        "http://selector.example:8051/select",
        "https://127.0.0.1:8051/select",
        "http://127.0.0.1:8051/select?token=secret-value",
    ],
)
def test_a_non_loopback_http_selector_is_a_startup_error(tmp_path, monkeypatch, url):
    """RM12, RM28, spec 0.40 section 14.3: an endpoint outside plain loopback http stops
    startup with one message, before a socket, without echoing the URL."""
    import socket

    def forbidden(*args, **kwargs):
        pytest.fail("startup must not resolve or contact the selector")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    item = f"{{ transport = 'http', name = 'remote', version = '1', url = '{url}' }}"
    config = write_config(tmp_path, f"\n[selection]\nchain = [{item}, 'model_router']\n")
    with pytest.raises(LaunchError, match=r"\[selection\].*loopback") as raised:
        load_config(config)
    assert url not in str(raised.value) and "secret-value" not in str(raised.value)
    out, err, served = [], [], []
    code = main(
        ["serve", "--config", str(config)],
        serve_fn=lambda *a, **k: served.append(1),
        stdout=out.append,
        stderr=err.append,
        **doubles(),
    )
    assert code == 2
    assert served == [] and out == []
    (message,) = err
    assert "[selection]" in message and url not in message and "secret-value" not in message


def test_the_configured_selection_is_recorded_in_run_accepted(tmp_path):
    """RM22, RM28, section 8: the chain, the thresholds, and the time limit in budgets."""
    extra = "\n[selection]\ntimeout_seconds = 1.5\nexample_match_score = 0.25\n"
    built = build_app(
        load_config(write_config(tmp_path, extra)),
        local_adapter=ScriptedAdapter(turns=[clarify_turn()]),
    )
    outcome = asyncio.run(built.orchestrator.run_text("Which service is it?"))
    accepted = built.orchestrator.store.events(outcome.run_id)[0]
    assert accepted.payload["selection"] == ["model_router"]
    assert accepted.payload["example_match_score"] == 0.25
    assert accepted.payload["example_match_margin"] == 0.1
    assert accepted.payload["budgets"]["selection.timeout_seconds"] == 1.5


def test_example_config_documents_the_selection_table(tmp_path):
    """Section 14.1: the commented [selection] block loads when uncommented, with the defaults."""
    lines = (REPO_ROOT / "relaymind.example.toml").read_text(encoding="utf-8").splitlines()
    start = lines.index("# [selection]")
    block = []
    for line in lines[start:]:
        if not line.startswith("# "):
            break
        block.append(line[2:])
    assert len(block) == 1 + len(SELECTION_KEYS)
    config = load_config(write_config(tmp_path, "\n" + "\n".join(block) + "\n"))
    assert config.selection == SelectionSettings()


# -- without a local model (spec 0.31 RM26, sections 7.4 and 14.1; #126) -------------------


def external_double():
    return ScriptedAdapter(turns=[], locality="external")


def no_local_config(tmp_path, extra=""):
    return write_config(tmp_path, EXTERNAL + extra, local=False)


def test_a_file_without_a_local_model_table_starts(tmp_path):
    """RM26, section 14.1: the table is optional; the gateway runs without a local model."""
    config = load_config(no_local_config(tmp_path))
    assert config.manifest is None and config.artifact is None
    # Section 14.1: the no-local defaults of allowed_modes and of the chain.
    assert config.chat.allowed_modes == frozenset({ExecutionMode.AUTO, ExecutionMode.EXTERNAL_ONLY})
    assert config.selection.chain == ("example_match",)

    built = build_app(config, external_adapter=external_double())
    try:
        assert built.orchestrator.model is None
        assert built.orchestrator.local_status == LocalStatus.absent()
        assert built.new_local_adapter is None
        assert call(built.app, "GET", "/readyz").json()["local_model"]["status"] == "absent"
    finally:
        built.orchestrator.store.close()


def test_a_file_with_neither_model_table_stops_startup(tmp_path):
    """Section 14.1: one model table at least."""
    with pytest.raises(LaunchError, match="neither"):
        load_config(write_config(tmp_path, local=False))


@pytest.mark.parametrize("modes", [["auto", "local_only"], ["parallel_demo"]])
def test_local_modes_without_a_local_model_stop_startup(tmp_path, modes):
    """Section 14.1: local_only and parallel_demo need a local_model table."""
    with pytest.raises(LaunchError, match=r"\[policy\] allowed_modes names .*local_model"):
        load_config(no_local_config(tmp_path, f"\n[policy]\nallowed_modes = {json.dumps(modes)}\n"))


def test_model_router_without_a_local_model_stops_startup(tmp_path):
    """Section 14.1: without a local model model_router is a startup error."""
    extra = "\n[selection]\nchain = ['example_match', 'model_router']\n"
    with pytest.raises(LaunchError, match=r"\[selection\] chain names model_router"):
        load_config(no_local_config(tmp_path, extra))


def test_without_a_local_model_the_declared_chain_and_modes_are_kept(tmp_path):
    extra = (
        "\n[policy]\nallowed_modes = ['external_only']\n[selection]\nchain = ['example_match']\n"
    )
    config = load_config(no_local_config(tmp_path, extra))
    assert config.chat.allowed_modes == frozenset({ExecutionMode.EXTERNAL_ONLY})
    assert config.selection.chain == ("example_match",)


def test_main_without_a_local_model_prints_the_class_and_sends_no_warm_up(tmp_path):
    """RM26: one line names the class and the cause; nothing is warmed up."""
    out, err = [], []
    status = main(
        ["serve", "--config", str(no_local_config(tmp_path))],
        serve_fn=lambda app, *, host, port: None,
        stdout=out.append,
        stderr=err.append,
        external_adapter=external_double(),
    )
    assert status == 0 and err == []
    assert "Local model: absent (no local model is configured);" in "\n".join(out)
    assert not any(line.startswith("Local warm-up") for line in out)
    assert any(line.startswith("Application token: ") for line in out)


def test_the_example_without_its_local_model_table_starts_without_a_local_model(tmp_path):
    """#126: the documented no-local variant of the example file loads and builds."""
    lines = (REPO_ROOT / "relaymind.example.toml").read_text(encoding="utf-8").splitlines()
    start = lines.index("[local_model]")
    kept = lines[:start] + lines[start + 4 :]
    external = kept.index("# [external_model]")
    for index in range(external, external + 5):
        assert kept[index].startswith("# ")
        kept[index] = kept[index][2:]
    path = tmp_path / "no-local.toml"
    path.write_text("\n".join(kept) + "\n", encoding="utf-8")

    config = load_config(path)
    assert config.manifest is None and config.external is not None
    assert config.selection.chain == ("example_match",)


def test_with_a_local_model_the_baseline_defaults_stay(tmp_path):
    """Spec 0.31: every default keeps the pre-0.31 behavior when a local model is configured."""
    config = load_config(write_config(tmp_path))
    assert config.chat.allowed_modes == frozenset(ExecutionMode)
    assert config.selection.chain == ("model_router",)
    assert config.detect is True
