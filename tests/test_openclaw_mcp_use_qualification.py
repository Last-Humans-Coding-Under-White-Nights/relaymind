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


def test_empty_arguments_object_is_a_valid_binding():
    """RM39/AC35: a tool with no arguments has a present, empty arguments object."""
    binding = trial()["binding"] | {"arguments": {}}
    approval = dict(binding)
    assert module().binding_matches(binding, approval)
    assert not module().binding_matches(binding, approval | {"arguments": None})
    assert not module().binding_matches(
        binding, {key: value for key, value in approval.items() if key != "arguments"}
    )


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


def test_read_jsonl_annotation_admits_string_records():
    """RM39/AC35: sandbox files hold JSON string primitives, not only objects."""
    from typing import get_type_hints

    assert get_type_hints(module("qualify").read_jsonl)["return"] == list[dict | str]


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


@pytest.mark.parametrize(
    "case", ["schema_change", "server_removal", "session_reset", "arguments_changed"]
)
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
    assert module().negative_control_pass(value)
    changed = copy.deepcopy(value)
    changed["server_events"] = [dict(kind="call")]
    assert not module().stale_case_pass(changed)
    value["rejection"]["code"] = "INVALID_ARGUMENTS"
    assert not module().stale_case_pass(value)
    value["status"] = "not_run"
    assert not module().stale_case_pass(value)


@pytest.mark.parametrize(
    "case",
    [
        "policy_denied",
        "disconnected",
        "foreign_requester",
        "approval_replay",
        "cancelled",
        "timeout",
        "choice_expired",
        "registration_revoked",
    ],
)
def test_each_negative_control_needs_its_own_rejection(case):
    """RM39/AC35: a stale-schema refusal cannot stand in for another control."""
    api = module()
    value = dict(
        name=case,
        status="observed",
        server_events=[],
        before={"a": []},
        after={"a": []},
        rejection={"code": "STALE_BINDING", "phase": "final_io"},
    )
    assert not api.stale_case_pass(value)
    assert not api.negative_control_pass(value)
    code, phase = api.CASE_REJECTIONS[case]
    value["rejection"] = {"code": code, "phase": phase}
    assert api.negative_control_pass(value)
    assert not api.stale_case_pass(value)


def test_every_negative_control_has_an_explicit_rejection():
    api = module()
    assert set(api.CASE_REJECTIONS) == set(api.NEGATIVE_CASES)
    assert set(api.STALE_CASES) < set(api.NEGATIVE_CASES)
    assert set(api.STALE_CASES) == {
        "schema_change",
        "server_removal",
        "session_reset",
        "arguments_changed",
    }


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
        inventories=[
            # A session can be inventoried before its MCP runtime connects.
            effective_event("empty", [], notices=["mcp-not-yet-connected"]),
            dict(method="mcp.app.listTools", status="refused", inventory="none", tool_names=[]),
            effective_event("listed", list(COLLIDING_ROWS)),
        ],
        sandbox_files={"alpha.one": [], "alpha?one": []},
    )
    return capture


# Projection of the pinned host's grouped tools.effective rows (probe.mjs).
COLLIDING_ROWS = (
    dict(name="alpha-one__append", server="alpha.one", original="append"),
    dict(name="alpha-one-2__append", server="alpha?one", original="append"),
)


def effective_event(inventory, mcp_tools, *, core=("read",), notices=()):
    names = [*core, *(row["name"] for row in mcp_tools)] if inventory == "listed" else []
    return dict(
        method="tools.effective",
        status="ok",
        inventory=inventory,
        tool_names=names,
        mcp_tools=[dict(row) for row in mcp_tools],
        notices=list(notices),
    )


@pytest.mark.parametrize(
    "inventories",
    [
        pytest.param([effective_event("empty", [])], id="empty"),
        pytest.param([effective_event("malformed", [])], id="malformed"),
        pytest.param(
            [effective_event("listed", list(COLLIDING_ROWS)), effective_event("malformed", [])],
            id="any_malformed",
        ),
        pytest.param([effective_event("listed", [])], id="core_tools_only"),
        pytest.param([effective_event("listed", [COLLIDING_ROWS[0]])], id="one_colliding_tool"),
        pytest.param(
            [
                effective_event(
                    "listed",
                    [COLLIDING_ROWS[0], {**COLLIDING_ROWS[1], "name": "alpha-one__append"}],
                )
            ],
            id="same_exposed_name",
        ),
        pytest.param(
            [
                effective_event(
                    "listed", [COLLIDING_ROWS[0], {**COLLIDING_ROWS[1], "original": "other"}]
                )
            ],
            id="wrong_original",
        ),
        pytest.param(
            [
                effective_event(
                    "listed", [COLLIDING_ROWS[0], {**COLLIDING_ROWS[1], "denied_by_session": True}]
                )
            ],
            id="denied_by_session",
        ),
        pytest.param(
            [effective_event("listed", [*COLLIDING_ROWS, {**COLLIDING_ROWS[1], "name": "third"}])],
            id="duplicate_server_row",
        ),
        pytest.param(
            [{**effective_event("listed", list(COLLIDING_ROWS)), "tool_names": ["read"]}],
            id="names_not_listed",
        ),
        pytest.param(
            [{**effective_event("listed", list(COLLIDING_ROWS)), "status": "refused"}],
            id="refused",
        ),
        # The pre-fix projection: status ok, no inventory outcome, no mapping.
        pytest.param(
            [dict(method="tools.effective", status="ok", tool_names=[])], id="legacy_empty"
        ),
        pytest.param(
            [
                dict(
                    method="tools.effective",
                    status="ok",
                    tool_names=[r["name"] for r in COLLIDING_ROWS],
                )
            ],
            id="legacy_names_only",
        ),
        pytest.param(
            [
                dict(
                    method="mcp.app.listTools",
                    **{
                        k: v
                        for k, v in effective_event("listed", list(COLLIDING_ROWS)).items()
                        if k != "method"
                    },
                )
            ],
            id="other_method",
        ),
    ],
)
def test_discovery_requires_both_colliding_tools_in_the_session_inventory(inventories):
    """RM39/AC35: an empty, malformed or partial session inventory is not discovery."""
    api = module()
    capture = discovery_capture()
    assert api.fixture_discovery_pass(capture)
    capture["mcp_use"]["inventories"] = inventories
    assert not api.fixture_discovery_pass(capture)
    assert module("qualify").trial_outcome(capture) == 1


def test_recorded_ci_inventory_shape_is_not_discovery():
    """RM39/AC35: the six rows the pre-fix projection recorded on the real host."""
    capture = discovery_capture()
    capture["mcp_use"]["inventories"] = [
        dict(method=method, status=status, tool_names=[], monotonic_ns=str(n))
        for n in range(3)
        for method, status in (("tools.effective", "ok"), ("mcp.app.listTools", "refused"))
    ]
    assert not module().fixture_discovery_pass(capture)


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


@pytest.mark.parametrize("case", ["healthy", "environment", "fixture", "harness", "proof"])
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
        # Setup failed before discovery ran: not a fixture or proof failure.
        original = {"status": "failed", "reason": "qualification_failed"}
        exit_code, expected_status, expected_reason = 2, "failed", "person_setup_failed"
    elif case == "proof":
        # Healthy discovery, then the person proof failed (N3).
        original = dict(original, status="failed", reason="person_composition_unverified")
        exit_code, expected_status, expected_reason = 2, "failed", "person_proof_failed"

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


def test_cli_grades_discovery_once(tmp_path, monkeypatch):
    """RM39/AC35: the discovery check is computed once and reused for the outcome."""
    from test_openclaw_ordinary_channel import module as ordinary_module

    api, ordinary = module("qualify"), ordinary_module()
    calls = []
    original = api.GRADE.fixture_discovery_pass

    def counted(capture):
        calls.append(capture)
        return original(capture)

    monkeypatch.setattr(api.GRADE, "fixture_discovery_pass", counted)
    original_capture = {"status": "completed", "mcp_use": discovery_capture()["mcp_use"]}

    def ordinary_trial(argv):
        Path(argv[argv.index("--out") + 1]).write_text(json.dumps(original_capture))
        return 0

    monkeypatch.setattr(ordinary, "main", ordinary_trial)
    monkeypatch.setattr(api, "load", lambda *_: ordinary)
    archive = tmp_path / "provider.tgz"
    archive.write_bytes(b"fixture archive")
    output = tmp_path / "published.json"
    code = api.main(
        ["--tarball", str(archive), "--work-dir", str(tmp_path / "work"), "--out", str(output)]
    )
    assert code == 0
    assert len(calls) == 1
    assert json.loads(output.read_bytes())["fixture_discovery_pass"] is True


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
    # Outputs are derived from the source tree, which exists even without dist/.
    source = tmp_path / "integrations/openclaw/src/index.ts"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"export {}")
    (source.parent / "control-ui.ts").write_bytes(b'import { a } from "./ui-part.ts";')
    (source.parent / "ui-part.ts").write_bytes(b"export const a = 1;")
    (source.parent.parent / "tsconfig.control-ui.json").write_text(
        '{"include": ["src/control-ui.ts"]}'
    )
    assert "integrations/openclaw/dist/index.js" in api.required_sources()
    assert "integrations/openclaw/dist/control-ui/ui-part.js" in api.required_sources()
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
          // Pinned host shapes: grouped tools.effective, MCP tools/list for Apps.
          const dispatch = async method => method === 'tools.effective'
            ? {ok:true, payload:{agentId:'main', profile:'full', groups:[{id:'mcp',
                label:'MCP server tools', source:'mcp', tools:[{id:'alpha-one__append',
                label:'append', description:'private-config', rawDescription:'private-config',
                source:'mcp', pluginId:'bundle-mcp', mcpServer:'alpha.one',
                mcpToolName:'append'}]}],
                notices:[{id:'mcp-stale-catalog', severity:'info', message:'private-config'}]}}
            : {ok:true, payload:{tools:[{name:'append', secret:'private-config'}]}};
          await observeDispatch(dispatch, event => events.push(event))(
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
        dict(method="mcp.app.discover", status="ok"),
        dict(
            method="tools.effective",
            status="ok",
            inventory="listed",
            tool_names=["alpha-one__append"],
            mcp_tools=[dict(name="alpha-one__append", server="alpha.one", original="append")],
            notices=["mcp-stale-catalog"],
        ),
        dict(method="mcp.app.listTools", status="ok", inventory="listed", tool_names=["append"]),
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
    # mcp.app.discover acquires the person session's MCP runtime only with
    # MCP Apps enabled; no approval or tool policy changes.
    assert config["mcp"]["apps"] == {"enabled": True}
    assert set(config["mcp"]) == {"servers", "apps"}
    # The host's default openai model selects an unregistered harness, which
    # refuses mcp.app.discover; the shipped RelayMind provider selects the
    # built-in openclaw harness. A zero heartbeat starts no unrequested run.
    defaults = config["agents"]["defaults"]
    assert defaults["model"] == {"primary": "relaymind/relaymind-client-tools"}
    assert defaults["heartbeat"] == {"every": "0m"}
    assert str(DIRECTORY) in config["plugins"]["load"]["paths"]
    assert extension.config["recommendations"]["bindings"] == []
    assert "relaymind-action-qualification" not in config["plugins"]["allow"]


def test_collect_is_safe_when_setup_failed_before_configure(tmp_path):
    """RM39/AC35: an early setup failure must not turn reporting into AttributeError."""
    from test_openclaw_ordinary_channel import module as ordinary_module

    api = module("qualify")
    archive = tmp_path / "fixture.tgz"
    archive.write_bytes(b"unused")
    extension = api.composition_type(ordinary_module())(
        "http://127.0.0.1:12345/v1",
        "app-fixture",
        "action-fixture",
        *[threading.Event() for _ in range(3)],
        archive=archive,
        runs=None,
        skill_text="fixture",
        skill_asset={},
    )
    assert (extension.probe_exit, extension.mcp_work, extension.inventory_file) == (
        None,
        None,
        None,
    )
    capture = api.blocked_capture("exact_route_unqualified")
    extension.collect(capture)
    assert capture["mcp_use"]["probe_exit_code"] is None
    assert capture["mcp_use"]["inventories"] == []
    assert capture["mcp_use"]["sandbox_files"] == {}


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


def test_the_pin_never_requires_an_output_for_a_declaration_file():
    """RM39/AC35 N1: tsc emits no JavaScript for a .d.ts input."""
    required = module("grade").required_sources()
    assert not [path for path in required if path.endswith(".d.js")]
    package = ROOT / "integrations/openclaw"
    for source in (package / "src").rglob("*.ts"):
        output = (
            (package / "dist" / source.relative_to(package / "src").with_suffix(".js"))
            .relative_to(ROOT)
            .as_posix()
        )
        assert (output in required) == (not source.name.endswith(".d.ts"))


def test_the_mcp_capability_step_runs_after_every_regular_tier():
    """RM39/AC35 F1: an unmeasured tier can never skip the regular plugin tiers."""
    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    step = ci.index("      - name: Disposable MCP use capability fixture")
    assert step > ci.index("      - name: Transport, plugin, and artifact tiers")
    assert step > ci.index("      - name: Pinned host lifecycle")
    assert step > ci.index("          name: openclaw-plugin-${{ matrix.os }}")
    assert "continue-on-error" not in ci


def test_the_pin_covers_every_emitted_control_ui_module(node_runtime, tmp_path):
    """RM16/RM39; AC35: every browser-bundle module tsc emits is pinned.

    The panel's emitted modules import one another at runtime, so a module the
    pin omits could change without failing the source byte check.
    """
    from test_openclaw_host_transport import PACKAGE, run_tsc

    run_tsc("-p", str(PACKAGE / "tsconfig.control-ui.json"), "--outDir", str(tmp_path))
    emitted = {
        (PACKAGE / "dist/control-ui" / p.relative_to(tmp_path)).relative_to(ROOT).as_posix()
        for p in tmp_path.rglob("*.js")
    }
    assert "integrations/openclaw/dist/control-ui/recommendation-display.js" in emitted
    required = {p for p in module("grade").required_sources() if "/dist/control-ui/" in p}
    assert required == emitted
