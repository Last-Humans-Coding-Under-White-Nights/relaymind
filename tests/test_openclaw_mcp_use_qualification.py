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
    call = dict(kind="call", **binding, result="alpha.one:1", counter=1)
    return dict(
        binding=binding,
        before={"alpha.one": [], "alpha?one": []},
        pending={"alpha.one": [], "alpha?one": []},
        after={"alpha.one": ["x"] if decision == "allow-once" else [], "alpha?one": []},
        server_events=[call] if decision == "allow-once" else [],
        approval=dict(**binding, id="approval-1", decision=decision, driver="automated_ui"),
        ui_events=[dict(approval_id="approval-1", action=decision, driver="automated_ui")],
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
    value["server_events"][0][field] = "foreign"
    assert not module().effect_matches(value, "allow-once")


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
    assert capture["mcp_use"]["approval_driver"] == "automated_ui"
    assert not module().mcp_use_pass(capture)


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


def test_gateway_started_event_alone_is_execution():
    assert not module().zero_gateway_execution([{"events": [{"type": "tool.started"}]}])


def test_existing_person_and_registration_proof_is_regraded_without_altering_it():
    from test_openclaw_ordinary_channel import fixture
    from test_openclaw_ordinary_channel import module as ordinary_module

    original = fixture(ordinary_module())
    capture = {"ordinary_capture": original}
    api = module()
    assert api.grade_capture(capture)["genuine_person_ingress"]
    assert api.grade_capture(capture)["current_registered_scope"]
    original["accounts"][0]["native_person_email"] = "foreign@example.test"
    assert not api.grade_capture(capture)["genuine_person_ingress"]
    assert not api.grade_capture(capture)["current_registered_scope"]


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


@pytest.mark.parametrize("decision", ["allow-once", "deny"])
def test_browser_driver_clicks_only_the_bound_native_card_and_labels_automation(decision):
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
                b.onclick = () => window.clicked.push(b.textContent);
              });
            }""")
            api = module("approval_ui")
            with pytest.raises(ValueError, match="approval_binding_unavailable"):
                api.click_native_decision(page, "foreign-id", "s", "alpha.one append", decision)
            assert page.evaluate("window.clicked") == []
            event = api.click_native_decision(page, "native-1", "s", "alpha.one append", decision)
            assert event == dict(approval_id="native-1", action=decision, driver="automated_ui")
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
    from openclaw_install_check import host_environment

    _, config = configured_extension(tmp_path)
    # CLI discovery needs no provider or person login; preserve that limitation.
    config = {"mcp": config["mcp"]}
    env = host_environment(tmp_path)
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
