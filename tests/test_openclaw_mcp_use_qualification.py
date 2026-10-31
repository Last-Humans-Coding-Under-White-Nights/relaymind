"""RM39/AC35: missing native MCP use proof never becomes execution authority."""

import copy
import hashlib
import importlib.util
import json
import os
import subprocess
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = ROOT / "integrations/openclaw/test/host-mcp-use-qualification"


@pytest.fixture
def node_runtime():
    """Keep core CI usable without npm ci; the OpenClaw job requires it."""
    from test_openclaw_host_transport import REQUIRED, _missing, _node

    reason = _missing()
    if reason is not None:
        if REQUIRED:
            pytest.fail(f"RELAYMIND_OPENCLAW_NODE=1 but {reason}")
        pytest.skip(reason)
    return _node()


def module(name="grade"):
    spec = importlib.util.spec_from_file_location("mcp_use_" + name, DIRECTORY / (name + ".py"))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


TERMS = (
    "pinned_bytes",
    "genuine_person_ingress",
    "current_registered_scope",
    "retained_explicit_choice",
    "exact_native_binding",
    "live_session_runtime_requester_binding",
    "policy_before_and_after_approval",
    "currentness_at_final_io",
    "native_approval_receipt",
    "intended_effect_and_result",
    "zero_gateway_execution",
    "negative_controls",
    "preserved_baseline",
)


@pytest.mark.parametrize("term", TERMS)
def test_claimed_predicate_terms_cannot_replace_raw_evidence(term):
    """RM39/AC35: even all-true summaries cannot supply any missing proof term."""
    capture = {"checks": dict.fromkeys(TERMS, True), "status": "passed"}
    capture[term] = {"passed": True, "qualified": True}
    assert module().mcp_use_pass(capture) is False
    assert module().grade_capture(capture)[term] is False


@pytest.mark.parametrize("value", [None, [], {}, {"mcp_use": None}, {"runs": None}])
def test_incomplete_capture_is_blocked(value):
    assert module().mcp_use_pass(value) is False


def test_pin_check_resolves_bytes_and_requires_the_complete_inventory(tmp_path):
    api = module()
    path = tmp_path / "host.js"
    path.write_bytes(b"exact pinned host bytes")
    inventory = [{"path": "host.js", "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}]
    assert api.source_bytes_match(inventory, root=tmp_path, required={"host.js"})
    assert not api.source_bytes_match([], root=tmp_path, required={"host.js"})
    path.write_bytes(b"changed host bytes")
    assert not api.source_bytes_match(inventory, root=tmp_path, required={"host.js"})
    assert not api.source_bytes_match(inventory * 2, root=tmp_path, required={"host.js"})


def test_source_paths_cannot_escape_the_checkout(tmp_path):
    api = module()
    assert not api.source_bytes_match(
        [{"path": "../outside", "sha256": "0" * 64}], root=tmp_path, required={"../outside"}
    )


def trial(decision="allow-once"):
    """Synthetic unit input only; this is not a native qualification receipt."""
    binding = dict(
        server="alpha.one",
        original="append",
        exposed="alpha-one__append",
        schema_hash="schema",
        session="session-a",
        requester="person-a",
        arguments={"text": "x"},
    )
    server_binding = {key: binding[key] for key in ("server", "original", "arguments")}
    call = dict(kind="call", call_id="call-1", **server_binding)
    effect = dict(
        kind="effect", call_id="call-1", **server_binding, result="alpha.one:1", counter=1
    )
    return dict(
        binding=binding,
        before={"alpha.one": [], "alpha?one": []},
        pending={"alpha.one": [], "alpha?one": []},
        after={"alpha.one": ["x"] if decision == "allow-once" else [], "alpha?one": []},
        server_events=[call, effect] if decision == "allow-once" else [],
        host_calls=[dict(binding)] if decision == "allow-once" else [],
        approval=dict(
            **binding,
            id="approval-1",
            decision=decision,
            driver="automated_ui",
            detail="alpha.one append",
        ),
        ui_events=[
            dict(
                approval_id="approval-1",
                action=decision,
                driver="automated_ui",
                observed=dict(id="approval-1", session="session-a", detail="alpha.one append"),
            )
        ],
        tool_results=[dict(**binding, content="alpha.one:1")] if decision == "allow-once" else [],
    )


@pytest.mark.parametrize("decision", ["allow-once", "deny"])
def test_effect_accounting_reads_server_events_files_and_result(decision):
    assert module().effect_matches(trial(decision), decision)


@pytest.mark.parametrize(
    "field", ["server", "original", "exposed", "schema_hash", "session", "requester", "arguments"]
)
def test_collision_schema_and_requester_misattribution_block_effect_credit(field):
    value = trial()
    value["host_calls"][0][field] = "foreign"
    assert not module().effect_matches(value, "allow-once")


@pytest.mark.parametrize(
    "field", ["call_id", "server", "original", "arguments", "counter", "result"]
)
def test_call_and_effect_must_join_exactly(field):
    """RM39/AC35 F3: effects cannot be borrowed from another request or server."""
    value = trial()
    value["server_events"][1][field] = "foreign"
    assert not module().effect_matches(value, "allow-once")


def test_effect_accounting_accepts_real_server_log(tmp_path, node_runtime):
    """RM39/AC35 F3: test-only client checks record compatibility, not host authority."""
    control, events, sandbox = (
        tmp_path / name for name in ("control.json", "events.jsonl", "sandbox.jsonl")
    )
    control.write_text(json.dumps(dict(server="alpha.one", revision=1)))
    result = subprocess.run(
        [
            node_runtime,
            "--input-type=module",
            "-e",
            """
        import {Client} from '@modelcontextprotocol/sdk/client/index.js';
        import {StdioClientTransport} from '@modelcontextprotocol/sdk/client/stdio.js';
        const client = new Client({name:'unit-only', version:'1'}, {capabilities:{}});
        await client.connect(new StdioClientTransport({command:'node',
          args:process.argv.slice(1)}));
        try {
          console.log(JSON.stringify(await client.callTool({name:'append', arguments:{text:'x'}})));
        }
        finally { await client.close(); }
        """,
            str(DIRECTORY / "server.mjs"),
            str(control),
            str(events),
            str(sandbox),
        ],
        cwd=ROOT / "integrations/openclaw",
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    value = trial()
    value["server_events"] = module("qualify").read_jsonl(events)
    value["after"]["alpha.one"] = module("qualify").read_jsonl(sandbox)
    value["tool_results"][0]["content"] = json.loads(result.stdout)["content"][0]["text"]
    assert module().effect_matches(value, "allow-once")
    assert not module().mcp_use_pass(value)


@pytest.mark.parametrize("part", ["approval", "tool_results", "ui_events", "server_events"])
def test_missing_raw_receipts_block_effect_credit(part):
    value = trial()
    del value[part]
    assert not module().effect_matches(value, "allow-once")


@pytest.mark.parametrize("phase", ["pending", "after"])
def test_early_duplicate_or_wrong_server_writes_block_effect_credit(phase):
    value = trial()
    value[phase]["alpha?one"].append("unapproved")
    assert not module().effect_matches(value, "allow-once")


def test_allow_once_is_exactly_once_and_deny_is_zero():
    allowed = trial()
    allowed["server_events"] *= 2
    allowed["after"]["alpha.one"].append("x")
    assert not module().effect_matches(allowed, "allow-once")
    denied = trial("deny")
    denied["server_events"] = trial()["server_events"]
    assert not module().effect_matches(denied, "deny")


def test_automated_clicks_are_never_human_acceptance():
    value = trial()
    value["approval"]["driver"] = "human"
    assert not module().effect_matches(value, "allow-once")
    capture = module("qualify").blocked_capture("exact_route_unqualified")
    assert capture["mcp_use"]["human_approval"] == "not_run"
    assert capture["mcp_use"]["approval_driver"] == "not_run"
    assert capture["mcp_use"]["approval_automation"] == "automated_ui"
    assert not module().mcp_use_pass(capture)


def test_human_accounting_requires_an_explicit_observed_interaction():
    """RM39/AC35 F9: human evidence has a slot but is never inferred from UI automation."""
    value = trial()
    value["approval"]["driver"] = "human"
    value["ui_events"][0]["driver"] = "human"
    assert not module().effect_matches(value, "allow-once")
    value["ui_events"][0]["human_observed"] = True
    assert module().effect_matches(value, "allow-once")
    assert not module().mcp_use_pass(value)


@pytest.mark.parametrize("field", ["id", "session", "detail"])
def test_effect_requires_the_observed_ui_binding(field):
    """RM39/AC35 F8: caller-supplied approval arguments cannot replace DOM identity."""
    value = trial()
    value["ui_events"][0]["observed"][field] = "foreign"
    assert not module().effect_matches(value, "allow-once")


@pytest.mark.parametrize("case", ["schema_change", "server_removal", "session_reset"])
def test_stale_trials_need_authority_rejection_and_zero_new_calls(case):
    value = dict(
        name=case,
        status="observed",
        server_events=[],
        before={"a": []},
        after={"a": []},
        rejection={"code": "STALE_BINDING", "phase": "final_io"},
    )
    assert module().stale_case_pass(value)
    changed = copy.deepcopy(value)
    changed["server_events"] = [dict(kind="call")]
    assert not module().stale_case_pass(changed)
    value["rejection"]["code"] = "INVALID_ARGUMENTS"
    assert not module().stale_case_pass(value)
    value["status"] = "not_run"
    assert not module().stale_case_pass(value)


def test_gateway_execution_is_derived_from_trace_and_requires_observed_runs():
    api = module()
    assert not api.zero_gateway_execution([])
    runs = [{"events": [{"type": "run.completed"}]}]
    assert api.zero_gateway_execution(runs)
    runs[0]["events"].append({"type": "tool.executed"})
    assert not api.zero_gateway_execution(runs)


@pytest.mark.parametrize("field", ["type", "event_type"])
def test_all_published_tool_events_prevent_zero_execution(field):
    """RM39/AC35 F2: terminal and proposed tool events cannot certify zero use."""
    from relaymind.store.trace_event import TRACE_EVENT_SCHEMA

    api = module()
    for name in TRACE_EVENT_SCHEMA["properties"]["event_type"]["enum"]:
        assert api.zero_gateway_execution([{"events": [{field: name}]}]) is (
            not name.startswith("tool.")
        )
    for name in ("tool.future", "unknown", None, 1):
        assert not api.zero_gateway_execution([{"events": [{field: name}]}])


def discovery_capture():
    """Synthetic discovery accounting input, never a native exact-use receipt."""
    capture = module("qualify").blocked_capture("exact_route_unqualified")
    capture.update(ordinary_capture={"status": "completed"}, ordinary_exit_code=0)
    capture["mcp_use"].update(
        probe_exit_code=0,
        server_events=[dict(kind="list", server=name) for name in ("alpha.one", "alpha?one")],
        inventories=[dict(method="tools.effective", status="ok", tool_names=["append"])],
        sandbox_files={"alpha.one": [], "alpha?one": []},
    )
    return capture


def test_trial_exit_distinguishes_blocked_environment_from_harness_failure():
    """RM39/AC35 F1: a failed prerequisite must make CI fail."""
    api = module("qualify")
    assert api.trial_outcome(api.blocked_capture("preflight_only")) == 0
    capture = discovery_capture()
    assert api.trial_outcome(capture) == 0
    for reason in (
        "linux_runner_required",
        "private_network_unavailable",
        "private_network_required",
    ):
        capture["ordinary_capture"] = {"status": "setup_unavailable", "reason": reason}
        capture["ordinary_exit_code"] = 2
        assert api.trial_outcome(capture) == 0
    capture["ordinary_capture"] = {"status": "failed", "reason": "qualification_failed"}
    assert api.trial_outcome(capture) == 1
    assert api.trial_outcome(api.blocked_capture("exact_route_unqualified")) == 1


@pytest.mark.parametrize("case", ["healthy", "environment", "fixture", "harness"])
def test_cli_publishes_the_trial_outcome_and_preserves_failure(tmp_path, monkeypatch, case):
    """RM39/AC35 F1/F4: only understood blocked outcomes exit zero."""
    from test_openclaw_ordinary_channel import module as ordinary_module

    api, ordinary = module("qualify"), ordinary_module()
    original = {"status": "completed", "mcp_use": discovery_capture()["mcp_use"]}
    exit_code = 0
    expected_status, expected_reason = "blocked", "exact_route_unqualified"
    if case == "environment":
        original = {"status": "setup_unavailable", "reason": "linux_runner_required"}
        exit_code, expected_reason = 2, "person_setup_unavailable"
    elif case == "fixture":
        original["mcp_use"]["server_events"] = []
        expected_status, expected_reason = "failed", "fixture_failed"
    elif case == "harness":
        original = {"status": "failed", "reason": "qualification_failed"}
        exit_code, expected_status, expected_reason = 2, "failed", "fixture_failed"

    output = tmp_path / "published.json"

    def ordinary_trial(argv):
        intermediate = Path(argv[argv.index("--out") + 1])
        assert intermediate != output
        assert not output.exists()
        intermediate.write_text(json.dumps(original))
        return exit_code

    monkeypatch.setattr(ordinary, "main", ordinary_trial)
    monkeypatch.setattr(api, "load", lambda *_: ordinary)
    archive = tmp_path / "provider.tgz"
    archive.write_bytes(b"fixture archive")
    code = api.main(
        ["--tarball", str(archive), "--work-dir", str(tmp_path / "work"), "--out", str(output)]
    )
    assert code == (1 if expected_status == "failed" else 0)
    capture = json.loads(output.read_bytes())
    assert (capture["status"], capture["reason"]) == (expected_status, expected_reason)
    assert capture["built_archive_sha256"] == hashlib.sha256(b"fixture archive").hexdigest()
    assert capture["functional_exact_use"] is False


def test_cli_missing_tarball_cannot_publish_a_blocked_capture(tmp_path):
    """RM39/AC35 F1: a setup error propagates to a nonzero CLI exit."""
    output = tmp_path / "capture.json"
    with pytest.raises(FileNotFoundError):
        module("qualify").main(
            [
                "--tarball",
                str(tmp_path / "absent.tgz"),
                "--work-dir",
                str(tmp_path / "work"),
                "--out",
                str(output),
            ]
        )
    assert not output.exists()


@pytest.mark.parametrize("broken", ["probe", "server", "inventory", "call", "effect", "sandbox"])
def test_fixture_discovery_failure_is_not_a_blocked_capability(broken):
    """RM39/AC35 F4: the raw discovery prerequisites must succeed."""
    api = module()
    capture = discovery_capture()
    assert api.fixture_discovery_pass(capture)
    value = capture["mcp_use"]
    if broken == "probe":
        value["probe_exit_code"] = 1
    elif broken == "server":
        value["server_events"].pop()
    elif broken == "inventory":
        value["inventories"] = []
    elif broken == "sandbox":
        value["sandbox_files"]["alpha.one"] = ["unapproved"]
    else:
        value["server_events"].append(dict(kind=broken, server="alpha.one"))
    assert not api.fixture_discovery_pass(capture)
    assert module("qualify").trial_outcome(capture) == 1


def test_gateway_started_event_alone_is_execution():
    assert not module().zero_gateway_execution([{"events": [{"type": "tool.started"}]}])


def test_existing_person_and_registration_proof_is_regraded_without_altering_it():
    from test_openclaw_ordinary_channel import fixture
    from test_openclaw_ordinary_channel import module as ordinary_module

    original = fixture(ordinary_module())
    capture = {"ordinary_capture": original}
    api = module()
    # RM39/AC35 F6: old or unrelated person receipts do not belong to this tier.
    assert not api.grade_capture(capture)["genuine_person_ingress"]
    assert not api.grade_capture(capture)["current_registered_scope"]
    original["environment"] = dict(
        checkout="a" * 40,
        fixture_plugin_sources=[
            dict(
                path=path.relative_to(ROOT).as_posix(),
                sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            )
            for path in DIRECTORY.iterdir()
            if path.suffix in {".py", ".mjs", ".json"}
        ],
    )
    assert api.grade_capture(capture)["genuine_person_ingress"]
    assert api.grade_capture(capture)["current_registered_scope"]
    saved = copy.deepcopy(original)
    original["environment"]["fixture_plugin_sources"][0]["sha256"] = "0" * 64
    assert not api.grade_capture(capture)["genuine_person_ingress"]
    original.clear()
    original.update(saved)
    original["accounts"][0]["native_person_email"] = "foreign@example.test"
    assert not api.grade_capture(capture)["genuine_person_ingress"]
    assert not api.grade_capture(capture)["current_registered_scope"]


def test_missing_dist_cannot_shrink_the_pin(tmp_path, monkeypatch):
    """RM39/AC35 F7: required build artifacts exist independently of dist contents."""
    api = module()
    monkeypatch.setattr(api, "ROOT", tmp_path)
    fixture_path = tmp_path / "integrations/openclaw/test/host-mcp-use-qualification/grade.py"
    monkeypatch.setattr(api, "__file__", str(fixture_path))
    assert "integrations/openclaw/dist/index.js" in api.required_sources()
    inventory = []
    for name in api.required_sources():
        path = tmp_path / name
        if "/dist/" not in name:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"source")
            inventory.append(dict(path=name, sha256=hashlib.sha256(b"source").hexdigest()))
    assert not api.source_bytes_match(inventory, root=tmp_path, required=api.required_sources())


def test_pinned_bytes_bind_the_installed_tarball(tmp_path, monkeypatch):
    """RM39/AC35 F7: equal source hashes do not excuse a different installed artifact."""
    api = module()
    monkeypatch.setattr(api, "ROOT", tmp_path)
    package = tmp_path / "integrations/openclaw/node_modules/openclaw/package.json"
    package.parent.mkdir(parents=True)
    package.write_text('{"version":"2026.10.7"}')
    name = package.relative_to(tmp_path).as_posix()
    monkeypatch.setattr(api, "required_sources", lambda: {name})
    capture = dict(
        source_inventory=[dict(path=name, sha256=hashlib.sha256(package.read_bytes()).hexdigest())],
        built_archive_sha256="a" * 64,
        ordinary_capture={"ordinary_actions": {"installation": {"archive_sha256": "b" * 64}}},
    )
    assert not api.grade_capture(capture)["pinned_bytes"]
    capture["ordinary_capture"]["ordinary_actions"]["installation"]["archive_sha256"] = "a" * 64
    assert api.grade_capture(capture)["pinned_bytes"]
    del capture["built_archive_sha256"]
    assert not api.grade_capture(capture)["pinned_bytes"]


def schema_observation():
    schema = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}
    raw = json.dumps(schema, separators=(",", ":"))
    from relaymind.canonical import canonical_digest

    return dict(
        server_event=dict(kind="list", server="alpha.one", original="append", schema=schema),
        tool=dict(
            mcp=dict(serverName="alpha.one", toolName="append"),
            exposed_name="alpha-one__append",
            schema=schema,
            schema_json=raw,
            schema_sha256=hashlib.sha256(raw.encode()).hexdigest(),
            canonical_schema_hash=canonical_digest(schema),
        ),
    )


@pytest.mark.parametrize("part", ["server", "original", "schema", "hash", "bytes", "missing"])
def test_raw_schema_and_both_hash_forms_are_required(part):
    value = schema_observation()
    api = module()
    assert api.schema_observation_matches(value)
    if part in {"server", "original"}:
        value["server_event"][part] = "foreign"
    elif part == "schema":
        value["tool"]["schema"] = {"type": "object"}
    elif part == "hash":
        value["tool"]["canonical_schema_hash"] = "0" * 64
    elif part == "bytes":
        value["tool"]["schema_json"] += " "
    else:
        del value["tool"]["schema"]
    assert not api.schema_observation_matches(value)


def test_no_run_capture_cli_is_bounded_and_does_not_claim_a_pass(tmp_path):
    """RM39/AC35: optional CI may return zero for a recorded blocked capability."""
    api = module("qualify")
    output = tmp_path / "capture.json"
    assert api.main(["--out", str(output), "--preflight-only"]) == 0
    capture = json.loads(output.read_text())
    assert capture["status"] == "blocked"
    assert capture["mcp_use"]["human_approval"] == "not_run"
    assert not module().mcp_use_pass(capture)


def test_final_capture_scan_rejects_secrets_added_after_the_person_guard(tmp_path):
    """RM12/RM39/AC35 F10: appended inventories are scanned with the original secret set."""
    from test_openclaw_ordinary_channel import module as ordinary_module

    api = module("qualify")
    extension, _ = configured_extension(tmp_path)
    person = ordinary_module().BASE.person_module()
    person.check_capture_secrets(
        {}, ["private-signing-key", "private-person-password"], extension=extension
    )
    output = tmp_path / "publish.json"
    capture = api.blocked_capture("preflight_only")
    api.write_capture(output, capture, extension.capture_forbidden_literals)
    clean = output.read_bytes()
    capture["mcp_use"]["inventories"].append({"tool_names": ["private-person-password"]})
    with pytest.raises(ValueError, match="credential_export_refused"):
        api.write_capture(output, capture, extension.capture_forbidden_literals)
    assert output.read_bytes() == clean
    assert b"private-person-password" not in output.read_bytes()


def test_inventory_projection_keeps_no_raw_params_or_payloads(node_runtime):
    """RM12/RM39/AC35 F10: inventory data cannot export a session key or config echo."""
    result = subprocess.run(
        [
            node_runtime,
            "--input-type=module",
            "-e",
            """
          import {observeDispatch} from './test/host-mcp-use-qualification/probe.mjs';
          const events = [];
          const dispatch = async () => ({ok:true,
            payload:{tools:[{name:'append', secret:'private-config'}]}});
          await observeDispatch(dispatch, event => events.push(event), 'request-1')(
            'tools.effective', {sessionKey:'private-session'}, {});
          console.log(JSON.stringify(events));
        """,
        ],
        cwd=ROOT / "integrations/openclaw",
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert "private-session" not in result.stdout
    assert "private-config" not in result.stdout
    events = json.loads(result.stdout)
    assert events == [
        dict(method=method, status="ok", tool_names=["append"])
        for method in ("tools.effective", "mcp.app.listTools")
    ]


@pytest.mark.parametrize("decision", ["allow-once", "deny"])
@pytest.mark.parametrize("change", ["none", "replace_on_show", "unresolved"])
def test_browser_driver_clicks_only_the_bound_native_card_and_labels_automation(decision, change):
    """RM39/AC35: DOM unit fixture, not a native-host approval observation."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            page.set_content(
                "<openclaw-exec-approval><button>Allow once</button>"
                "<button>Deny</button></openclaw-exec-approval>"
            )
            page.evaluate("""() => {
              const el = document.querySelector('openclaw-exec-approval');
              el.props = {canGrant: true, queue: [{id: 'native-1', kind: 'plugin',
                pluginDetail: 'alpha.one append', expiresAtMs: Date.now() + 60000,
                request: {sessionKey: 's', allowedDecisions: ['allow-once', 'deny']}}]};
              el.show = () => {};
              window.clicked = [];
              el.querySelectorAll('button').forEach(b => {
                b.onclick = () => { window.clicked.push(b.textContent); el.props.queue = []; };
              });
            }""")
            api = module("approval_ui")
            with pytest.raises(ValueError, match="approval_binding_unavailable"):
                api.click_native_decision(page, "foreign-id", "s", "alpha.one append", decision)
            assert page.evaluate("window.clicked") == []
            if change == "replace_on_show":
                page.evaluate("""() => {
                  const el = document.querySelector('openclaw-exec-approval');
                  el.show = () => { el.props.queue[0].id = 'foreign'; };
                }""")
                with pytest.raises(ValueError, match="approval_binding_unavailable"):
                    api.click_native_decision(page, "native-1", "s", "alpha.one append", decision)
                assert page.evaluate("window.clicked") == []
                return
            if change == "unresolved":
                page.evaluate("""() => document.querySelectorAll('button').forEach(b => {
                  b.onclick = () => window.clicked.push(b.textContent);
                })""")
                with pytest.raises(ValueError, match="approval_resolution_unobserved"):
                    api.click_native_decision(page, "native-1", "s", "alpha.one append", decision)
                return
            event = api.click_native_decision(page, "native-1", "s", "alpha.one append", decision)
            assert event == dict(
                approval_id="native-1",
                action=decision,
                driver="automated_ui",
                observed=dict(id="native-1", session="s", detail="alpha.one append"),
            )
            assert page.evaluate("window.clicked") == [
                "Allow once" if decision == "allow-once" else "Deny"
            ]
        finally:
            browser.close()


def configured_extension(tmp_path):
    from test_openclaw_ordinary_channel import module as ordinary_module

    ordinary = ordinary_module()
    archive = tmp_path / "fixture.tgz"
    archive.write_bytes(b"unused in configuration test")
    extension = module("qualify").composition_type(ordinary)(
        "http://127.0.0.1:12345/v1",
        "app-fixture",
        "action-fixture",
        *[threading.Event() for _ in range(3)],
        archive=archive,
        runs=None,
        skill_text="fixture",
        skill_asset={},
    )
    original = str(ROOT / "integrations/openclaw/test/host-person-qualification")
    config = dict(
        plugins=dict(
            allow=["relaymind-person-qualification"],
            load=dict(paths=[original]),
            entries={"relaymind-person-qualification": {"config": {}}},
        ),
        agents=dict(defaults=dict(workspace=str(tmp_path))),
        gateway=dict(controlUi={}),
    )
    extension.configure(config, tmp_path, "https://fixture.test")
    return extension, config


def test_configure_keeps_provider_policy_and_installs_two_colliding_host_owned_servers(tmp_path):
    extension, config = configured_extension(tmp_path)
    assert "tools" not in config
    assert set(config["mcp"]["servers"]) == {"alpha.one", "alpha?one"}
    assert str(DIRECTORY) in config["plugins"]["load"]["paths"]
    assert extension.config["recommendations"]["bindings"] == []
    assert "relaymind-action-qualification" not in config["plugins"]["allow"]


@pytest.mark.skipif(os.environ.get("RELAYMIND_OPENCLAW_NODE") != "1", reason="opt-in pinned host")
def test_pinned_host_launches_both_stdio_servers_without_any_mutation(tmp_path):
    """RM39/AC35 real host discovery only; no human or exact-use claim."""
    spec = importlib.util.spec_from_file_location(
        "mcp_use_install_check", ROOT / "scripts/openclaw_install_check.py"
    )
    install = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(install)

    _, config = configured_extension(tmp_path)
    # CLI discovery needs no provider or person login; preserve that limitation.
    config = {"mcp": config["mcp"]}
    env = install.host_environment(tmp_path)
    Path(env["OPENCLAW_CONFIG_PATH"]).write_text(json.dumps(config))
    result = subprocess.run(
        [
            "node",
            str(ROOT / "integrations/openclaw/node_modules/openclaw/openclaw.mjs"),
            "mcp",
            "probe",
            "--json",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    events = [
        event
        for i in range(2)
        for event in module("qualify").read_jsonl(tmp_path / f"mcp-events-{i}.jsonl")
    ]
    assert {e["server"] for e in events if e["kind"] == "list"} == {"alpha.one", "alpha?one"}
    assert all(e["kind"] not in {"call", "effect"} for e in events)
    assert not any(tmp_path.glob("mcp-sandbox-*.jsonl"))
