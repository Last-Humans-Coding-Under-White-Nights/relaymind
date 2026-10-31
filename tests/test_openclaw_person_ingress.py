"""RM37/RM39 AC28/AC30: fixture person ingress never invents network identity."""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def module():
    path = ROOT / "integrations/openclaw/test/host-person-qualification/qualify.py"
    if not path.exists():
        pytest.fail("person-ingress qualification harness missing")
    spec = importlib.util.spec_from_file_location("person_ingress_qualification", path)
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = loaded
    spec.loader.exec_module(loaded)
    return loaded


def interface(address, *, flags=None):
    return dict(
        ifname="fixture0",
        flags=["UP"] if flags is None else flags,
        addr_info=[dict(family="inet", local=address)],
    )


@pytest.mark.parametrize("address", ["10.1.2.3", "172.16.0.1", "172.31.255.254", "192.168.1.2"])
def test_only_actual_up_rfc1918_addresses_are_network_candidates(address):
    assert module().eligible_private_ipv4([interface(address)]) == [address]


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "0.0.0.0",
        "80.71.236.197",
        "169.254.1.1",
        "192.0.2.1",
        "100.64.1.1",
        "172.32.0.1",
        "10.1.2.999",
        "010.1.2.3",
        "::1",
    ],
)
def test_loopback_public_linklocal_documentation_and_invalid_addresses_refuse(address):
    assert module().eligible_private_ipv4([interface(address)]) == []


def test_down_or_loopback_interfaces_cannot_qualify_by_address_text():
    api = module()
    assert api.eligible_private_ipv4([interface("10.1.2.3", flags=[])]) == []
    assert api.eligible_private_ipv4([interface("10.1.2.3", flags=["UP", "LOOPBACK"])]) == []


def test_setup_unavailable_retains_all_planned_steps_and_no_person_claim():
    capture = module().unavailable_capture("private_network_unavailable")
    assert capture["status"] == "setup_unavailable"
    assert capture["reason"] == "private_network_unavailable"
    assert all(step["status"] == "not_run" for step in capture["steps"])
    assert capture["checks"]["person_composition"] is False
    assert capture["checks"]["production_adapter_qualified"] is False


def test_capture_cannot_promote_login_without_native_profile_and_scoped_rpc():
    api = module()
    capture = api.unavailable_capture("private_network_unavailable")
    capture["status"] = "completed"
    capture["checks"]["person_composition"] = True
    assert api.person_composition_pass(capture) is False


def test_changed_download_cannot_become_executable(tmp_path):
    artifact = tmp_path / "authcrunch.tar.gz"
    artifact.write_bytes(b"not the pinned release")
    with pytest.raises(ValueError, match="artifact_digest"):
        module().install_authcrunch(artifact, tmp_path / "install")
    assert not (tmp_path / "install").exists()


@pytest.mark.parametrize("address", ["0.0.0.0", "127.0.0.1", "80.71.236.197", "192.0.2.1"])
def test_proxy_configuration_cannot_open_a_public_or_loopback_portal(tmp_path, address):
    with pytest.raises(ValueError, match="private_network_required"):
        module().proxy_configuration(address, 38443, 38001, [], "signing-key", tmp_path)


def mechanical_capture(api):
    capture = api.unavailable_capture("private_network_unavailable")
    capture.update(
        status="completed",
        reason=None,
        steps=[dict(name=name, status="passed") for name in api.STEPS],
        network=dict(bind_ip="10.1.2.3", peer_ip="10.1.2.3", upstream="127.0.0.1"),
    )
    capture["accounts"] = [
        dict(
            login_verified=True,
            native_device_paired=True,
            scoped_rpc_admitted=True,
            proxy_email=f"account{i}@example.test",
            native_person_email=f"account{i}@example.test",
            profile_id=f"native-profile-{i}",
            native_session_id=f"native-session-{i}",
            lifecycle_revision=f"native-life-{i}",
            device_id=f"native-device-{i}",
            connection_id=f"native-connection-{i}",
            login_evidence_id=f"login-{i}",
            rpc_evidence_id=f"request-{i}",
        )
        for i in (1, 2)
    ]
    return capture


def test_populated_success_flags_without_correlated_original_events_still_refuse():
    api = module()
    assert api.person_composition_pass(mechanical_capture(api)) is False


def test_correlated_mechanical_fixture_is_not_production_or_human_qualification():
    api = module()
    capture = complete_capture(api)
    assert api.person_composition_pass(capture) is True
    assert capture["checks"]["production_adapter_qualified"] is False
    next(event for event in capture["events"] if event["kind"] == "person_rpc")["payload"][
        "profileId"
    ] = "foreign"
    assert api.person_composition_pass(capture) is False


def test_requested_qualification_exits_nonzero_with_complete_unavailable_capture(
    tmp_path, monkeypatch
):
    import platform

    api = module()
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(api, "discover_network", lambda: [])
    output = tmp_path / "capture.json"
    work = tmp_path / "sandbox"
    assert api.run_qualification(work, output) == 2
    import json

    capture = json.loads(output.read_bytes())
    assert capture["status"] == "setup_unavailable"
    assert capture["reason"] == "private_network_unavailable"
    assert not work.exists()
    assert [step["name"] for step in capture["steps"]] == list(api.STEPS)
    assert all(step["status"] == "not_run" for step in capture["steps"])


@pytest.mark.skipif(
    sys.platform != "linux", reason="the opt-in Ubuntu runner owns a process deadline"
)
def test_credential_guard_failure_never_exports_the_rejected_payload(tmp_path, monkeypatch):
    api = module()
    monkeypatch.setattr(api, "discover_network", lambda: ["10.1.2.3"])

    def unsafe(_work, _address, capture):
        capture["events"] = [dict(password="fixture-secret-never-export")]
        capture["steps"][0]["status"] = "passed"
        raise ValueError("credential_export_refused")

    monkeypatch.setattr(api, "_run_private_trial", unsafe)
    output = tmp_path / "capture.json"
    assert api.run_qualification(tmp_path / "work", output) == 2
    raw = output.read_bytes()
    assert b"fixture-secret-never-export" not in raw
    import json

    capture = json.loads(raw)
    assert capture["steps"][0]["status"] == "passed"
    assert capture["checks"]["person_composition"] is False


@pytest.mark.skipif(
    sys.platform != "linux", reason="the opt-in Ubuntu runner owns a process deadline"
)
def test_requested_qualification_has_one_overall_deadline(tmp_path, monkeypatch):
    import signal
    import time

    if not hasattr(signal, "setitimer"):
        pytest.skip("the opt-in Ubuntu qualification uses a process deadline")
    api = module()
    monkeypatch.setattr(api, "discover_network", lambda: ["10.1.2.3"])
    monkeypatch.setattr(api, "TOTAL_SECONDS", 0.01, raising=False)
    monkeypatch.setattr(api, "_run_private_trial", lambda *_: time.sleep(0.05))
    output = tmp_path / "capture.json"
    assert api.run_qualification(tmp_path / "work", output) == 2
    import json

    assert json.loads(output.read_bytes())["reason"] == "overall_deadline_exceeded"


@pytest.mark.parametrize("system", ["Darwin", "Windows"])
def test_opt_in_person_runner_refuses_other_platforms_before_setup(tmp_path, monkeypatch, system):
    import platform

    api = module()
    monkeypatch.setattr(platform, "system", lambda: system)
    monkeypatch.setattr(api, "discover_network", lambda: ["10.1.2.3"])
    monkeypatch.setattr(
        api, "_run_private_trial", lambda *_: pytest.fail("wrong-platform setup started")
    )
    output = tmp_path / "capture.json"
    assert api.run_qualification(tmp_path / "work", output) == 2
    import json

    assert json.loads(output.read_bytes())["reason"] == "linux_runner_required"
    assert not (tmp_path / "work").exists()


def test_complete_login_claim_still_needs_bound_denial_observations():
    api = module()
    capture = mechanical_capture(api)
    capture["events"] = []
    for account in capture["accounts"]:
        capture["events"].extend(
            [
                dict(
                    id=account["login_evidence_id"],
                    kind="authenticated_proxy",
                    email=account["proxy_email"],
                    peer_ip="10.1.2.3",
                ),
                dict(
                    id=account["rpc_evidence_id"],
                    kind="person_rpc",
                    ok=True,
                    payload=dict(
                        requestId=account["rpc_evidence_id"],
                        profileId=account["profile_id"],
                        authenticatedUserId=account["native_person_email"],
                        sessionId=account["native_session_id"],
                        lifecycleRevision=account["lifecycle_revision"],
                        deviceId=account["device_id"],
                        connectionId=account["connection_id"],
                    ),
                ),
            ]
        )
    assert api.person_composition_pass(capture) is False


def complete_capture(api):
    """Controlled receipts include actual create response and same-socket RPC identity."""
    capture = mechanical_capture(api)
    capture["events"] = []
    for i, account in enumerate(capture["accounts"], 1):
        account.update(
            session_key=f"native-key-{i}",
            socket_id=f"browser-socket-{i}",
            create_evidence_id=f"create-{i}",
        )
        capture["events"].extend(
            [
                dict(
                    id=account["login_evidence_id"],
                    kind="authenticated_proxy",
                    email=account["proxy_email"],
                    peer_ip="10.1.2.3",
                ),
                dict(
                    id="request:" + account["create_evidence_id"],
                    kind="native_rpc",
                    method="sessions.create",
                    request_id=account["create_evidence_id"],
                    socket_id=account["socket_id"],
                    ok=True,
                    error_code=None,
                ),
                dict(
                    id=account["create_evidence_id"],
                    kind="session_created",
                    request_id=account["create_evidence_id"],
                    socket_id=account["socket_id"],
                    account_email=account["proxy_email"],
                    session_key=account["session_key"],
                    session_id=account["native_session_id"],
                    lifecycle_revision=account["lifecycle_revision"],
                    ok=True,
                ),
                dict(
                    id="request:" + account["rpc_evidence_id"],
                    kind="native_rpc",
                    method="rmqualification.person",
                    request_id=account["rpc_evidence_id"],
                    socket_id=account["socket_id"],
                    ok=True,
                    error_code=None,
                ),
                dict(
                    id=account["rpc_evidence_id"],
                    kind="person_rpc",
                    ok=True,
                    socket_id=account["socket_id"],
                    payload=dict(
                        requestId=account["rpc_evidence_id"],
                        profileId=account["profile_id"],
                        authenticatedUserId=account["native_person_email"],
                        sessionKey=account["session_key"],
                        sessionId=account["native_session_id"],
                        lifecycleRevision=account["lifecycle_revision"],
                        deviceId=account["device_id"],
                        connectionId=account["connection_id"],
                    ),
                ),
            ]
        )
    capture["events"].extend(
        [
            dict(
                id="cross",
                kind="cross_account_denied",
                caller_connection_id="native-connection-2",
                target_session_key="native-key-1",
                target_session_id="native-session-1",
                target_lifecycle_revision="native-life-1",
                ok=False,
                error_code="FORBIDDEN",
            ),
            dict(
                id="retired",
                kind="connection_revocation",
                browser_context_closed=True,
                prepared=dict(
                    phase="prepared", requestId="held", connectionId="native-connection-1"
                ),
                denied=dict(
                    phase="guard_denied",
                    requestId="held",
                    connectionId="native-connection-1",
                    reason="post_await_authority_retired",
                ),
            ),
        ]
    )
    return capture


def test_complete_same_socket_create_and_person_receipts_pass_fixture_mechanics_only():
    api = module()
    assert api.person_composition_pass(complete_capture(api)) is True


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_create",
        "missing_native_create",
        "missing_native_person",
        "key",
        "session",
        "lifecycle",
        "cross_socket",
        "account",
        "duplicate",
        "reassigned",
        "cross_target",
        "generic_denial",
        "wrong_denial_connection",
    ],
)
def test_incomplete_or_mismatched_create_and_retirement_receipts_never_qualify(mutation):
    api = module()
    capture = complete_capture(api)
    created = next(event for event in capture["events"] if event["kind"] == "session_created")
    if mutation == "missing_create":
        capture["events"].remove(created)
    elif mutation in {"missing_native_create", "missing_native_person"}:
        method = (
            "sessions.create" if mutation == "missing_native_create" else "rmqualification.person"
        )
        capture["events"].remove(
            next(
                event
                for event in capture["events"]
                if event["kind"] == "native_rpc" and event["method"] == method
            )
        )
    elif mutation in {"key", "session", "lifecycle", "cross_socket", "account"}:
        created[
            {
                "key": "session_key",
                "session": "session_id",
                "lifecycle": "lifecycle_revision",
                "cross_socket": "socket_id",
                "account": "account_email",
            }[mutation]
        ] = "wrong"
    elif mutation == "duplicate":
        capture["events"].append(dict(created, id="duplicate-create"))
    elif mutation == "reassigned":
        capture["accounts"][1]["create_evidence_id"] = created["id"]
    elif mutation == "cross_target":
        capture["events"][-2]["target_session_key"] = "wrong"
    elif mutation == "generic_denial":
        capture["events"][-1]["denied"]["reason"] = "fixture_hold_timeout"
    else:
        capture["events"][-1]["denied"]["connectionId"] = "other"
    assert api.person_composition_pass(capture) is False


def test_native_create_export_keeps_only_bounded_nonsecret_binding():
    api = module()
    observed = dict(
        id="native-create",
        socket_id="browser-socket",
        frame=dict(
            id="native-create",
            ok=True,
            payload=dict(
                key="native-key",
                sessionId="native-id",
                entry=dict(lifecycleRevision="native-life", secret="never-export"),
                token="never-export",
            ),
        ),
    )
    receipt = api.session_create_receipt(observed, "fixture@example.test")
    assert receipt == dict(
        id="native-create",
        request_id="native-create",
        kind="session_created",
        socket_id="browser-socket",
        account_email="fixture@example.test",
        ok=True,
        session_key="native-key",
        session_id="native-id",
        lifecycle_revision="native-life",
    )
    assert "never-export" not in repr(receipt)
    for changed in [dict(id="wrong"), dict(socket_id=""), dict(socket_id="a" * 1025)]:
        with pytest.raises(ValueError, match="native_session_create_binding"):
            api.session_create_receipt(observed | changed, "fixture@example.test")
