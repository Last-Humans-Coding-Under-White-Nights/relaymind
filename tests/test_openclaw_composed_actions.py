"""RM37/RM39 AC28/AC30: composed native proof cannot be inferred from person login."""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def module():
    path = ROOT / "integrations/openclaw/test/host-action-qualification/qualify.py"
    spec = importlib.util.spec_from_file_location("composed_action_qualification", path)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def test_person_only_and_incomplete_actions_never_qualify():
    api = module()
    assert api.composed_pass({"checks": {"person_composition": True}}) is False
    assert (
        api.composed_pass(
            {"checks": {"person_composition": True}, "composed_actions": {"passed": True}}
        )
        is False
    )


@pytest.mark.parametrize(
    "missing",
    [
        "choice",
        "replay",
        "correction",
        "inspect",
        "delete",
        "reset",
        "foreign_denied",
        "unregistered_denied",
    ],
)
def test_every_action_and_negative_control_is_required(missing):
    api = module()
    report = complete_fixture(api)
    assert api.composed_pass(report)
    report["composed_actions"]["checks"][missing] = False
    assert not api.composed_pass(report)


def test_self_asserted_complete_flags_without_native_receipts_never_qualify():
    api = module()
    forged = {
        "checks": {"person_composition": True},
        "composed_actions": {
            "checks": {name: True for name in api.CHECKS},
            "implementation_revision": "a" * 64,
        },
    }
    assert not api.composed_pass(forged)


def complete_fixture(api):
    """Controlled original receipts for hostile offline controls; not native qualification."""
    from test_openclaw_person_ingress import complete_capture

    capture = complete_capture(api.person_module())
    account = capture["accounts"][0]
    chosen = dict(state="chosen", selected_action=dict(scope={"user_id": "u", "workspace_id": "w"}))
    correction = dict(feedback_id="private-1", feedback=dict(kind="correction"))
    values = dict(
        recommend={},
        choice=chosen,
        replay=chosen,
        correction=correction,
        inspect=dict(records=[correction]),
        delete=dict(deleted=True),
        reset=dict(feedback_revision="3"),
    )
    receipts, events = {}, []
    for name, value in values.items():
        request_id = "action-" + name
        receipts[name] = dict(request_id=request_id, socket_id=account["socket_id"], receipt=value)
        capture["events"].append(
            dict(
                id="request:" + request_id,
                kind="native_rpc",
                method="relaymind.recommendations",
                request_id=request_id,
                socket_id=account["socket_id"],
                ok=True,
            )
        )
        events.append(
            dict(
                kind="dispatch",
                requestId=request_id,
                profileId=account["profile_id"],
                connectionId=account["connection_id"],
                sessionId=account["native_session_id"],
                implementation_revision="a" * 64,
                authenticatedControlUi=True,
            )
        )
    for name in ("foreign", "unregistered"):
        request_id = "deny-" + name
        receipts[name] = dict(request_id=request_id, socket_id=capture["accounts"][1]["socket_id"])
        capture["events"].append(
            dict(
                id="request:" + request_id,
                kind="native_rpc",
                method="relaymind.recommendations",
                request_id=request_id,
                socket_id=capture["accounts"][1]["socket_id"],
                ok=False,
                error_code="INVALID_REQUEST",
            )
        )
    capture["composed_actions"] = dict(
        checks={name: True for name in api.CHECKS},
        receipts=receipts,
        events=events,
        negative_events=[],
        implementation_revision="a" * 64,
        production_registry_enabled=False,
        qualification_constructor="explicit-test-only",
        retirement=[
            dict(kind="dispatch", requestId="held-action"),
            dict(
                kind="connection_retired",
                requestId="held-action",
                connectionId=account["connection_id"],
                aborted=True,
            ),
            dict(kind="result", requestId="held-action", ok=False, reason="cancelled"),
        ],
    )
    return capture


@pytest.mark.parametrize(
    "change",
    [
        "socket",
        "profile",
        "session",
        "revision",
        "missing_rpc",
        "cancel_success",
        "foreign_http",
        "foreign_attempt",
        "duplicate",
    ],
)
def test_unbound_native_originals_and_false_revocation_are_rejected(change):
    api = module()
    value = complete_fixture(api)
    assert api.composed_pass(value)
    composed = value["composed_actions"]
    if change == "socket":
        composed["receipts"]["choice"]["socket_id"] = "foreign"
    elif change == "profile":
        composed["events"][0]["profileId"] = "foreign"
    elif change == "session":
        composed["events"][0]["sessionId"] = "foreign"
    elif change == "revision":
        composed["events"][0]["implementation_revision"] = "b" * 64
    elif change == "missing_rpc":
        value["events"] = [
            event for event in value["events"] if event.get("request_id") != "action-choice"
        ]
    elif change == "cancel_success":
        composed["retirement"][-1]["ok"] = True
    elif change == "foreign_attempt":
        composed["negative_events"].append(
            dict(kind="http_attempt", method="GET", path="/relay/v1/feedback")
        )
    elif change == "foreign_http":
        composed["negative_events"].append(dict(kind="http", path="/relay/v1/feedback"))
    elif change == "duplicate":
        composed["receipts"]["replay"]["request_id"] = composed["receipts"]["choice"]["request_id"]
    assert not api.composed_pass(value)


def test_operator_mapping_uses_host_observed_workspace_not_global_default(tmp_path):
    """RM39: the actual current profile's runtime directory is not guessed from config."""
    import json
    import threading

    api = module()
    events = [threading.Event() for _ in range(3)]
    extension = api.ActionComposition(
        "http://127.0.0.1:12345", "app-secret", "action-secret", *events
    )
    config = dict(
        plugins=dict(allow=[], load=dict(paths=[]), entries={}),
        agents=dict(defaults=dict(workspace="/global/default")),
    )
    extension.configure(config, tmp_path, "https://fixture.test")
    assert json.loads(extension.operator_file.read_bytes())["recommendations"]["bindings"] == []
    capture = complete_fixture(api)
    for event in capture["events"]:
        if event.get("kind") == "person_rpc":
            event["payload"]["workspaceDir"] = "/actual/current/profile/workspace"

    def stop_before_rpc(*args):
        raise RuntimeError("controlled-stop-before-network")

    with pytest.raises(RuntimeError, match="controlled-stop-before-network"):
        extension.persons_ready(stop_before_rpc, [None, None], [{}, {}], capture)
    binding = json.loads(extension.operator_file.read_bytes())["recommendations"]["bindings"][0]
    assert binding["workspaceDir"] == "/actual/current/profile/workspace"
    assert binding["profileId"] == capture["accounts"][0]["profile_id"]
    assert binding["authenticatedUserId"] == capture["accounts"][0]["native_person_email"]
