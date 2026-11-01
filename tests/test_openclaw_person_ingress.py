"""RM37/RM39 AC28/AC30: fixture person ingress never invents network identity."""

import importlib.util
import json
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
    assert capture["login_progress"] == []


def test_pinned_portal_icon_name_proceed_is_unique_and_ambiguity_is_refused():
    """RM39/AC30: click the actual role control, refusing ambiguous submit buttons."""
    from playwright.sync_api import Error, sync_playwright

    api = module()
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch()
        except Error as error:
            if "Executable doesn't exist" in str(error):
                pytest.skip("Chromium is installed in the core browser check")
            raise
        try:
            page = browser.new_page()
            page.set_default_timeout(1000)
            page.set_content(
                '<form onsubmit="event.preventDefault(); '
                "document.querySelector('#stage').textContent='password'\">"
                "<button type='submit'><span>&#xf058;</span> Proceed</button>"
                "</form><div id='stage'>username</div>"
            )
            assert page.get_by_role("button", name="Proceed", exact=True).count() == 0
            assert page.get_by_role("button", name="Proceed").count() == 1
            api.click_proceed(page)
            assert page.locator("#stage").inner_text() == "password"
            page.set_content(
                "<button onclick=\"document.body.dataset.clicked='yes'\">"
                "<span>&#xf058;</span> Proceed</button>"
                "<button onclick=\"document.body.dataset.clicked='yes'\">Proceed again</button>"
            )
            with pytest.raises(Error, match="strict mode violation"):
                api.click_proceed(page)
            assert page.evaluate("() => document.body.dataset.clicked") is None
        finally:
            browser.close()


@pytest.mark.skipif(sys.platform != "linux", reason="the opt-in Ubuntu runner owns a deadline")
def test_failed_login_subphase_keeps_partial_denominator_without_secret_diagnostics(
    tmp_path, monkeypatch
):
    """RM16/RM39: a portal failure records its safe substep without a person claim."""
    api = module()
    monkeypatch.setattr(api, "discover_network", lambda: ["10.1.2.3"])
    secrets = (
        "credential-marker",
        "opaque-sandbox-id",
        "opaque-cookie-id",
        "query-marker",
        "fragment-marker",
    )

    def fail(_work, _address, capture):
        capture["phase"] = "account_a_login"
        capture["steps"][0]["status"] = "passed"
        api.observe_login_subphase(capture, "account_a", "navigation", lambda: 200)

        def rejected():
            raise RuntimeError(
                "https://rm-person.test/auth/sandbox/opaque-sandbox-id?query-marker"
                "#fragment-marker RMPERSON_SANDBOX_ID_opaque-cookie-id credential-marker"
            )

        api.observe_login_subphase(capture, "account_a", "proceed_click", rejected)

    monkeypatch.setattr(api, "_run_private_trial", fail)
    output = tmp_path / "capture.json"
    assert api.run_qualification(tmp_path / "work", output) == 2
    raw = output.read_text()
    assert all(secret not in raw for secret in secrets)
    capture = json.loads(raw)
    assert capture["status"] == "failed"
    assert capture["phase"] == "account_a_login"
    assert capture["checks"] == {
        "person_composition": False,
        "production_adapter_qualified": False,
    }
    assert len(capture["steps"]) == len(api.STEPS) == 12
    assert capture["steps"][0]["status"] == "passed"
    assert capture["steps"][2]["status"] == "failed"
    assert capture["login_progress"][0]["subphase"] == "navigation"
    assert capture["login_progress"][0]["status"] == "passed"
    assert capture["login_progress"][1]["subphase"] == "proceed_click"
    assert capture["login_progress"][1]["status"] == "failed"
    assert all(
        item["account"] == "account_a"
        and isinstance(item["observed_ms"], (int, float))
        and item["observed_ms"] >= 0
        for item in capture["login_progress"]
    )


@pytest.mark.parametrize(
    ("account", "subphase"),
    [
        ("account_a/opaque", "navigation"),
        ("account_a", "auth_sandbox/opaque"),
    ],
)
def test_login_progress_rejects_unclassified_names(account, subphase):
    """RM12/RM39: dynamic route and account data cannot enter diagnostics."""
    api = module()
    capture = api.unavailable_capture("private_network_unavailable")
    with pytest.raises(ValueError, match="login_subphase_invalid"):
        api.observe_login_subphase(
            capture, account, subphase, lambda: pytest.fail("invalid action ran")
        )
    assert capture["login_progress"] == []


def test_person_rpc_failure_projection_correlates_only_fixed_event_classes(tmp_path):
    """RM12/RM39: native handler diagnostics keep no private event content."""
    api = module()
    event_file = tmp_path / "native-events.jsonl"
    secret = "secret-cookie-RMPERSON_SANDBOX_ID_opaque"
    event_file.write_text(
        json.dumps(
            dict(
                requestId="other-request",
                phase="probe_failed",
                reason="inventory_unavailable",
                cookie=secret,
            )
        )
        + "\n"
        + json.dumps(
            dict(
                requestId="wanted-request",
                connectionId="native-connection",
                phase="guard_denied",
                reason="initial_authority_retired",
                url="https://rm-person.test/auth/sandbox/opaque?secret=1",
                cookie=secret,
            )
        )
        + "\n"
    )
    projected = api.project_person_rpc_failure(event_file, "wanted-request")
    assert projected == {
        "status": "handler_event_present",
        "observations": [{"phase": "guard_denied", "reason": "initial_authority_retired"}],
    }
    assert secret not in json.dumps(projected)
    assert "opaque" not in json.dumps(projected)
    assert api.project_person_rpc_failure(event_file, "missing-request") == {
        "status": "handler_event_absent",
        "observations": [],
    }


@pytest.mark.parametrize(
    ("line", "expected_status"),
    [
        (
            '{"requestId":"wanted","phase":"guard_denied","reason":"private-secret"}',
            "event_file_invalid",
        ),
        ('{"requestId":"wanted","phase":["private-secret"],"reason":null}', "event_file_invalid"),
        (
            '{"requestId":"wanted","phase":"guard_denied","reason":["private-secret"]}',
            "event_file_invalid",
        ),
        ("not-json-private-secret", "event_file_invalid"),
    ],
)
def test_person_rpc_failure_projection_refuses_unclassified_event_data(
    tmp_path, line, expected_status
):
    """RM12: unknown reasons and malformed records cannot become capture fields."""
    api = module()
    event_file = tmp_path / "native-events.jsonl"
    event_file.write_text(line + "\n")
    projected = api.project_person_rpc_failure(event_file, "wanted")
    assert projected == {"status": expected_status, "observations": []}
    assert "private-secret" not in json.dumps(projected)


def test_person_rpc_failure_projection_bounds_file_and_matching_rows(tmp_path):
    """RM12: private event diagnostics have fixed byte and row ceilings."""
    api = module()
    event_file = tmp_path / "native-events.jsonl"
    event_file.write_bytes(b"x" * (1024 * 1024 + 1))
    assert api.project_person_rpc_failure(event_file, "wanted") == {
        "status": "event_file_oversize",
        "observations": [],
    }
    event_file.write_text((json.dumps(dict(requestId="wanted", phase="prepared")) + "\n") * 5)
    assert api.project_person_rpc_failure(event_file, "wanted") == {
        "status": "event_file_invalid",
        "observations": [],
    }


@pytest.mark.skipif(sys.platform != "linux", reason="the opt-in Ubuntu runner owns a deadline")
def test_failed_person_rpc_keeps_all_steps_false_checks_and_safe_projection(tmp_path, monkeypatch):
    """RM16/RM39: failure remains incomplete even with correlated handler evidence."""
    api = module()
    monkeypatch.setattr(api, "discover_network", lambda: ["10.1.2.3"])

    def fail(work, _address, capture):
        capture["phase"] = "account_a_person_rpc"
        for step in capture["steps"][:5]:
            step["status"] = "passed"
        event_file = work / "native-events.jsonl"
        event_file.write_text(
            json.dumps(
                dict(
                    requestId="request-a",
                    phase="guard_denied",
                    reason="admission_refused",
                    private="credential-marker",
                )
            )
            + "\n"
        )
        capture["person_rpc_failure"] = api.project_person_rpc_failure(event_file, "request-a")
        raise ValueError("native_person_rpc_denied")

    monkeypatch.setattr(api, "_run_private_trial", fail)
    output = tmp_path / "capture.json"
    assert api.run_qualification(tmp_path / "work", output) == 2
    raw = output.read_text()
    assert "credential-marker" not in raw
    capture = json.loads(raw)
    assert capture["reason"] == "native_person_rpc_denied"
    assert capture["checks"] == {
        "person_composition": False,
        "production_adapter_qualified": False,
    }
    assert len(capture["steps"]) == len(api.STEPS) == 12
    assert capture["steps"][5]["status"] == "failed"
    assert capture["person_rpc_failure"] == {
        "status": "handler_event_present",
        "observations": [{"phase": "guard_denied", "reason": "admission_refused"}],
    }


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
    capture["configured_session_policy"] = api.person_role_config(
        ["operator.read", "operator.write", "operator.approvals", "operator.questions"]
    )
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
                        authenticatedControlUi=True,
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
                retired=dict(
                    phase="connection_retired",
                    requestId="held",
                    connectionId="native-connection-1",
                    connection_aborted=True,
                ),
                denied=dict(
                    phase="guard_denied",
                    requestId="held",
                    connectionId="native-connection-1",
                    reason="post_await_authority_retired",
                ),
                native_sequence=["prepared", "connection_retired", "guard_denied"],
            ),
        ]
    )
    return capture


def test_complete_same_socket_create_and_person_receipts_pass_fixture_mechanics_only():
    api = module()
    assert api.person_composition_pass(complete_capture(api)) is True


@pytest.mark.parametrize("mutation", ["missing", "shared", "admin"])
def test_missing_or_broad_host_session_policy_never_qualifies(mutation):
    """RM39/AC30: a request denial must be backed by the configured person role."""
    api = module()
    capture = complete_capture(api)
    person = capture["configured_session_policy"]["definitions"]["person"]
    if mutation == "missing":
        del capture["configured_session_policy"]
    elif mutation == "shared":
        person["sessions"]["others"] = "write"
    else:
        person["scopes"].append("operator.admin")
    assert api.person_composition_pass(capture) is False


@pytest.mark.parametrize("attestation", [False, None])
def test_missing_or_false_native_control_ui_attestation_never_qualifies(attestation):
    """RM39/AC30: browser origin alone cannot attest a native Control UI client."""
    api = module()
    capture = complete_capture(api)
    receipt = next(event for event in capture["events"] if event["kind"] == "person_rpc")["payload"]
    if attestation is None:
        del receipt["authenticatedControlUi"]
    else:
        receipt["authenticatedControlUi"] = False
    assert api.person_composition_pass(capture) is False


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
        "missing_retirement",
        "false_abort",
        "foreign_retirement",
        "retirement_before_prepared",
        "admitted_held",
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
    elif mutation == "missing_retirement":
        del capture["events"][-1]["retired"]
    elif mutation == "false_abort":
        capture["events"][-1]["retired"]["connection_aborted"] = False
    elif mutation == "foreign_retirement":
        capture["events"][-1]["retired"]["connectionId"] = "other"
    elif mutation == "retirement_before_prepared":
        capture["events"][-1]["native_sequence"] = [
            "connection_retired",
            "prepared",
            "guard_denied",
        ]
    elif mutation == "admitted_held":
        capture["events"][-1]["native_sequence"].insert(2, "admitted")
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


def test_held_native_failure_projection_is_bounded_and_closed(tmp_path):
    """RM39/AC30: a failed held control retains fixed facts without private payloads."""
    api = module()
    events = tmp_path / "native-events.jsonl"
    rows = [
        dict(phase="prepared", requestId="held", connectionId="connection-a", token="secret"),
        dict(
            phase="connection_retired",
            requestId="held",
            connectionId="connection-a",
            connection_aborted=True,
            url="https://private.invalid/secret",
        ),
        dict(
            phase="guard_denied",
            requestId="held",
            connectionId="connection-a",
            reason="post_await_authority_retired",
        ),
        dict(phase="admitted", requestId="other", connectionId="connection-b"),
    ]
    events.write_text("".join(json.dumps(row) + "\n" for row in rows))
    projection = api.project_held_native_events(events, "held")
    assert projection == {
        "status": "handler_event_present",
        "observations": [
            {"phase": "prepared", "reason": None},
            {"phase": "connection_retired", "connection_aborted": True},
            {"phase": "guard_denied", "reason": "post_await_authority_retired"},
        ],
    }
    assert "secret" not in repr(projection)
    assert "connection-a" not in repr(projection)
    events.write_text(json.dumps(dict(phase="unknown", requestId="held")) + "\n")
    assert api.project_held_native_events(events, "held") == {
        "status": "event_file_invalid",
        "observations": [],
    }


def test_each_account_session_has_its_own_key_and_label():
    """RM37/AC28: the host refuses a second session that reuses a label."""
    m = module()
    first = m.session_create_params()
    second = m.session_create_params()
    assert first["agentId"] == second["agentId"] == "main"
    assert first["key"] != second["key"]
    assert first["label"] != second["label"]
    assert first["key"].startswith("agent:main:person-")
    assert 0 < len(first["label"]) <= 512


def test_disposable_host_configures_person_session_boundary():
    """RM39/AC30: native accounts cannot target another person's session."""
    scopes = ["operator.read", "operator.write", "operator.approvals", "operator.questions"]
    assert module().person_role_config(scopes) == {
        "default": "person",
        "definitions": {
            "person": {
                "sessions": {"others": "none"},
                "agents": ["main"],
                "scopes": scopes,
            }
        },
    }


def test_native_event_reader_skips_only_a_partial_trailing_line(tmp_path):
    """RM39/AC30: a line the host is still appending is retried, never parsed."""
    api = module()
    events = tmp_path / "native-events.jsonl"
    complete = json.dumps(dict(phase="prepared", requestId="r1")).encode() + b"\n"
    events.write_bytes(complete + b'{"phase":"admitted","requestId":"r')
    assert api.read_native_events(events) == [dict(phase="prepared", requestId="r1")]
    # A multi-byte character cut in the middle of the trailing write.
    events.write_bytes(complete + '{"phase":"正'.encode()[:-1])
    assert api.read_native_events(events) == [dict(phase="prepared", requestId="r1")]
    events.write_bytes(b"")
    assert api.read_native_events(events) == []


@pytest.mark.parametrize("line", [b"{not json}\n", b"[1]\n", b"\xff\xfe\n"])
def test_native_event_reader_refuses_a_corrupt_complete_line(tmp_path, line):
    api = module()
    events = tmp_path / "native-events.jsonl"
    events.write_bytes(line)
    with pytest.raises(ValueError, match="native_event_invalid"):
        api.read_native_events(events)


def test_native_event_reader_keeps_its_bound(tmp_path):
    api = module()
    events = tmp_path / "native-events.jsonl"
    events.write_bytes(b"{}\n" * (400 * 1024))
    with pytest.raises(ValueError, match="native_event_bound"):
        api.read_native_events(events)


def test_every_raised_reason_is_exported_as_itself():
    """RM39/AC30: no named harness failure collapses into qualification_failed."""
    import re

    api = module()
    source = (ROOT / "integrations/openclaw/test/host-person-qualification/qualify.py").read_text()
    raised = set(re.findall(r'raise \w+\("([a-z_]+)"\)', source))
    assert "artifact_deadline" in raised
    assert raised <= api.SAFE_REASONS


def linux_runner(monkeypatch, api):
    import platform

    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(api, "discover_network", lambda: ["10.1.2.3"])


@pytest.mark.parametrize(
    "reason", ["artifact_deadline", "network_inventory_invalid", "qualification_port"]
)
def test_named_setup_failures_keep_their_reason(tmp_path, monkeypatch, reason):
    import signal

    if not hasattr(signal, "setitimer"):
        pytest.skip("the opt-in Ubuntu qualification uses a process deadline")
    api = module()
    linux_runner(monkeypatch, api)

    def fail(_work, _address, capture):
        capture["phase"] = "artifact_setup"
        raise TimeoutError(reason)

    monkeypatch.setattr(api, "_run_private_trial", fail)
    output = tmp_path / "capture.json"
    assert api.run_qualification(tmp_path / "work", output) == 2
    capture = json.loads(output.read_bytes())
    assert capture["reason"] == reason
    assert isinstance(capture["observed_ms"], (int, float)) and capture["observed_ms"] >= 0


@pytest.mark.parametrize(
    ("machine", "candidates", "reason"),
    [
        ("aarch64", ["10.1.2.3"], "linux_runner_required"),
        ("x86_64", [], "private_network_unavailable"),
    ],
)
def test_early_refusals_record_observed_duration(
    tmp_path, monkeypatch, machine, candidates, reason
):
    import platform

    api = module()
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(platform, "machine", lambda: machine)
    monkeypatch.setattr(api, "discover_network", lambda: candidates)
    monkeypatch.setattr(api, "_run_private_trial", lambda *_: pytest.fail("refused setup started"))
    output = tmp_path / "capture.json"
    assert api.run_qualification(tmp_path / "work", output) == 2
    capture = json.loads(output.read_bytes())
    assert capture["reason"] == reason
    assert isinstance(capture["observed_ms"], (int, float)) and capture["observed_ms"] >= 0
    assert not (tmp_path / "work").exists()


def test_linux_aarch64_runner_is_refused_before_setup(tmp_path, monkeypatch):
    import platform

    api = module()
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(platform, "machine", lambda: "aarch64")
    monkeypatch.setattr(api, "discover_network", lambda: pytest.fail("network probed"))
    monkeypatch.setattr(
        api, "_run_private_trial", lambda *_: pytest.fail("wrong-architecture setup started")
    )
    output = tmp_path / "capture.json"
    assert api.run_qualification(tmp_path / "work", output) == 2
    assert json.loads(output.read_bytes())["reason"] == "linux_runner_required"
    assert not (tmp_path / "work").exists()


def test_caller_alarm_in_use_is_refused_and_left_untouched(tmp_path, monkeypatch):
    import signal

    if not hasattr(signal, "setitimer"):
        pytest.skip("the opt-in Ubuntu qualification uses a process deadline")
    api = module()
    linux_runner(monkeypatch, api)
    monkeypatch.setattr(signal, "getitimer", lambda _which: (5.0, 0.0))
    monkeypatch.setattr(signal, "setitimer", lambda *_: pytest.fail("caller alarm was replaced"))
    monkeypatch.setattr(
        signal, "signal", lambda *_: pytest.fail("caller alarm handler was replaced")
    )
    monkeypatch.setattr(api, "_run_private_trial", lambda *_: pytest.fail("setup started"))
    output = tmp_path / "capture.json"
    assert api.run_qualification(tmp_path / "work", output) == 2
    capture = json.loads(output.read_bytes())
    assert capture["reason"] == "caller_alarm_in_use"
    assert capture["checks"]["person_composition"] is False


def test_gateway_and_proxy_ports_are_always_distinct(monkeypatch):
    api = module()
    issued = iter([5000, 5000, 5000, 6000])
    monkeypatch.setattr(api, "_free_port", lambda _address: next(issued))
    assert api._distinct_ports("10.1.2.3") == (5000, 6000)


def test_port_allocation_that_never_differs_is_refused(monkeypatch):
    api = module()
    monkeypatch.setattr(api, "_free_port", lambda _address: 5000)
    with pytest.raises(ValueError, match="qualification_port"):
        api._distinct_ports("10.1.2.3")


@pytest.mark.parametrize(
    "error", [ConnectionRefusedError(), TimeoutError(), OSError("unreachable")]
)
def test_dropped_or_refused_connection_means_no_listener(monkeypatch, error):
    """RM19: a blackholed or filtered port is unreachable, not a harness crash."""
    import socket

    api = module()

    def connect(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(socket, "create_connection", connect)
    assert api._unreachable("127.0.0.1", 5000) is True


def test_an_accepting_listener_is_reachable():
    import socket

    api = module()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        assert api._unreachable("127.0.0.1", listener.getsockname()[1]) is False


def test_network_peer_step_is_marked_once_after_every_account():
    import ast

    source = (ROOT / "integrations/openclaw/test/host-person-qualification/qualify.py").read_text()
    trial = next(
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef) and node.name == "_run_private_trial"
    )

    def marks(node):
        return [
            call
            for call in ast.walk(node)
            if isinstance(call, ast.Call)
            and getattr(call.func, "id", None) == "mark"
            and call.args
            and getattr(call.args[0], "value", None) == "network_peer"
        ]

    account_loop = next(
        node
        for node in ast.walk(trial)
        if isinstance(node, ast.For) and ast.unparse(node.iter) == "enumerate(accounts)"
    )
    assert len(marks(trial)) == 1
    assert marks(account_loop) == []


def test_every_browser_context_is_closed_even_if_one_close_fails():
    api = module()
    closed = []

    class Context:
        def __init__(self, name, fail=False):
            self.name, self.fail = name, fail

        def close(self):
            closed.append(self.name)
            if self.fail:
                raise RuntimeError("already closed")

    api._close_contexts([Context("a", fail=True), Context("b")])
    assert closed == ["a", "b"]


# --- #228 harness hardening (review of #224) -------------------------------------


@pytest.mark.parametrize(
    "frame, code",
    [
        ({"error": {"code": "FORBIDDEN"}}, "FORBIDDEN"),
        ({"error": "FORBIDDEN"}, None),
        ({"error": ["FORBIDDEN"]}, None),
        ({"error": None}, None),
        ({}, None),
        ({"error": {"code": "not a code!"}}, None),
        ({"error": {"code": "X" * 65}}, None),
    ],
)
def test_native_error_code_reads_only_a_mapping_error(frame, code):
    assert module().native_error_code(frame) == code


def person_result(payload):
    return {
        "id": "rpc-1",
        "socket_id": "s",
        "frame": {"id": "rpc-1", "ok": True, "payload": payload},
    }


PERSON = dict(
    requestId="rpc-1",
    authenticatedUserId="fixture@example.test",
    profileId="profile",
    sessionKey="key",
    sessionId="id",
    lifecycleRevision="life",
    deviceId="device",
    connectionId="connection",
)


def test_person_rpc_receipt_accepts_the_bound_payload():
    assert module().person_rpc_receipt(person_result(PERSON), "fixture@example.test") == PERSON


@pytest.mark.parametrize(
    "payload",
    [
        None,
        "receipt",
        [],
        {k: v for k, v in PERSON.items() if k != "requestId"},
        {k: v for k, v in PERSON.items() if k != "deviceId"},
        dict(PERSON, requestId="other"),
        dict(PERSON, authenticatedUserId="someone@example.test"),
        dict(PERSON, sessionId=7),
    ],
)
def test_malformed_person_rpc_receipts_are_identity_binding_failures(payload):
    with pytest.raises(ValueError, match="native_person_identity_binding"):
        module().person_rpc_receipt(person_result(payload), "fixture@example.test")


@pytest.mark.parametrize("payload", [None, "x", {"key": "k", "sessionId": "i"}, {"entry": None}])
def test_malformed_session_create_payloads_are_create_binding_failures(payload):
    observed = dict(id="c", socket_id="s", frame=dict(id="c", ok=True, payload=payload))
    with pytest.raises(ValueError, match="native_session_create_binding"):
        module().session_create_receipt(observed, "fixture@example.test")


def test_session_params_come_from_the_validated_create_receipt():
    observed = dict(
        id="c",
        socket_id="s",
        frame=dict(
            id="c",
            ok=True,
            payload=dict(
                key="native-key", sessionId="native-id", entry=dict(lifecycleRevision="l")
            ),
        ),
    )
    api = module()
    receipt = api.session_create_receipt(observed, "fixture@example.test")
    assert api.session_params(receipt) == dict(
        sessionKey="native-key", expectedSessionId="native-id", expectedLifecycleRevision="l"
    )


@pytest.mark.parametrize("projection", ["project_person_rpc_failure", "project_held_native_events"])
def test_event_projections_skip_blank_lines(tmp_path, projection):
    events = tmp_path / "events.jsonl"
    line = {"requestId": "wanted", "phase": "prepared", "reason": None, "connectionId": "c1"}
    events.write_bytes(b"\n" + json.dumps(line).encode() + b"\n\n  \n")
    projected = getattr(module(), projection)(events, "wanted")
    assert projected["status"] != "event_file_invalid"
    assert projected["observations"][0]["phase"] == "prepared"


@pytest.mark.parametrize("projection", ["project_person_rpc_failure", "project_held_native_events"])
@pytest.mark.parametrize("corrupt", [b"\x0b", b"\x0c", b"\x0b\x0c "])
def test_non_json_whitespace_lines_stay_corrupt(tmp_path, projection, corrupt):
    events = tmp_path / "events.jsonl"
    line = {"requestId": "wanted", "phase": "prepared", "reason": None, "connectionId": "c1"}
    events.write_bytes(json.dumps(line).encode() + b"\n" + corrupt + b"\n")
    assert getattr(module(), projection)(events, "wanted")["status"] == "event_file_invalid"


@pytest.mark.parametrize("field", ["phase", "reason"])
@pytest.mark.parametrize("value", [["prepared"], {"p": 1}])
def test_held_event_projection_rejects_non_string_phase_or_reason(tmp_path, field, value):
    events = tmp_path / "events.jsonl"
    line = {"requestId": "wanted", "phase": "prepared", "reason": None, "connectionId": "c1"}
    line[field] = value
    events.write_bytes(json.dumps(line).encode() + b"\n")
    assert module().project_held_native_events(events, "wanted")["status"] == "event_file_invalid"


@pytest.mark.parametrize("reply", ["peer", ["peer"], None, 7])
def test_non_mapping_peer_reply_is_a_peer_binding_failure(reply):
    class Page:
        def evaluate(self, *args):
            return reply

    with pytest.raises(ValueError, match="actual_peer_binding"):
        module().read_bound_peer(Page(), "127.0.0.1", "a@example.test")


def test_the_trial_reads_host_replies_only_through_the_checked_helpers():
    """#228: reverting any guarded call site in the trial must fail a test.

    The trial drives a real browser and host, so its wiring is checked on its
    source: every reply goes through the validating helpers, never raw indexing.
    """
    import inspect

    source = inspect.getsource(module()._run_private_trial)
    assert "session_params(created_receipt)" in source
    assert "person_rpc_receipt(result" in source
    assert source.count("native_error_code(") >= 2
    assert '["payload"]' not in source
    assert '.get("error")' not in source
    assert 'event.get("phase") in {' not in source


# --- #224 review (31 Oct 19:00): harness setup failures keep their reasons -------


@pytest.mark.parametrize("stdout", [b"not json", b"\xff\xfe", b"\xff", b'{"not": "a list"}'])
def test_unreadable_network_inventory_is_network_inventory_invalid(monkeypatch, stdout):
    import subprocess

    api = module()
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout, b"")
    )
    with pytest.raises(ValueError, match="network_inventory_invalid"):
        api.discover_network()


def test_oversized_network_inventory_is_network_inventory_size(monkeypatch):
    import subprocess

    api = module()
    big = b"[" + b" " * (1024 * 1024) + b"]"
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, big, b"")
    )
    with pytest.raises(ValueError, match="network_inventory_size"):
        api.discover_network()


@pytest.mark.parametrize(
    "interfaces",
    [
        [None],
        [{"flags": 7}],
        [{"flags": ["UP"], "addr_info": [None]}],
        [{"flags": ["UP"], "addr_info": "x"}],
        ["eth0"],
    ],
)
def test_malformed_interface_records_are_network_inventory_invalid(interfaces):
    with pytest.raises(ValueError, match="network_inventory_invalid"):
        module().eligible_private_ipv4(interfaces)


@pytest.mark.parametrize(
    "observed",
    [
        None,
        {"id": "r", "frame": []},
        {"frame": {}},
        {"id": None, "socket_id": "s", "frame": {"id": None}},
        {"id": 1, "socket_id": "s", "frame": {"id": 1}},
        {"id": "r", "frame": {"id": "r"}},
        {"id": "", "socket_id": "s", "frame": {"id": ""}},
        {"id": "r", "socket_id": "s", "frame": {"id": "other"}},
    ],
)
def test_malformed_browser_replies_are_native_response_binding(observed):
    class Page:
        def evaluate(self, *args):
            return observed

    with pytest.raises(ValueError, match="^native_response_binding$"):
        module().native_rpc(Page(), "m", {})


@pytest.mark.parametrize("ok", [True, False])
def test_bound_browser_replies_are_accepted(ok):
    frame = {"id": "r", "ok": ok} if ok else {"id": "r", "ok": False, "error": {"code": "X"}}
    observed = {"id": "r", "socket_id": "s", "frame": frame}

    class Page:
        def evaluate(self, *args):
            return observed

    assert module().native_rpc(Page(), "m", {}) is observed


@pytest.mark.parametrize(
    "message, reason",
    [
        ("Page.evaluate: Error: native_rpc_timeout", "native_rpc_timeout"),
        ("Page.evaluate: Error: native_socket_retired", "native_socket_retired"),
        ("Page.evaluate: Target closed /secret/path", "native_rpc_transport"),
    ],
)
def test_rpc_transport_rejection_keeps_a_fixed_reason(message, reason):
    api = module()

    class Page:
        def evaluate(self, *args):
            raise RuntimeError(message)

    with pytest.raises(ValueError, match=f"^{reason}$"):
        api.native_rpc(Page(), "m", {})
    assert reason in api.SAFE_REASONS


def test_the_overall_deadline_passes_through_an_rpc():
    class Page:
        def evaluate(self, *args):
            raise TimeoutError("overall_deadline_exceeded")

    with pytest.raises(TimeoutError, match="overall_deadline_exceeded"):
        module().native_rpc(Page(), "m", {})


@pytest.mark.parametrize(
    "failure, reason",
    [
        ("missing", "network_inventory_unavailable"),
        ("exit", "network_inventory_unavailable"),
        ("timeout", "network_inventory_deadline"),
    ],
)
def test_network_discovery_command_failures_keep_their_reasons(monkeypatch, failure, reason):
    import subprocess

    api = module()

    def run(*args, **kwargs):
        if failure == "missing":
            raise FileNotFoundError("ip")
        if failure == "exit":
            raise subprocess.CalledProcessError(1, ["ip"])
        raise subprocess.TimeoutExpired(["ip"], 5)

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(ValueError, match=f"^{reason}$"):
        api.discover_network()
    assert reason in api.SAFE_REASONS


@pytest.mark.parametrize("failure", ["url_timeout", "socket_timeout"])
def test_download_network_timeouts_are_artifact_deadline(tmp_path, monkeypatch, failure):
    import urllib.error
    import urllib.request

    api = module()

    class Opener:
        def open(self, *args, **kwargs):
            if failure == "url_timeout":
                raise urllib.error.URLError(TimeoutError("timed out"))
            raise TimeoutError("timed out")

    monkeypatch.setattr(urllib.request, "build_opener", lambda *a: Opener())
    with pytest.raises(TimeoutError, match="^artifact_deadline$"):
        api.download_authcrunch(tmp_path / "authcrunch.tgz")
    assert not (tmp_path / "authcrunch.tgz").exists()


def _archive(tmp_path, members):
    import hashlib
    import io
    import tarfile

    path = tmp_path / "authcrunch.tgz"
    with tarfile.open(path, "w:gz") as package:
        for name, data, kind in members:
            info = tarfile.TarInfo(name)
            if kind == "dir":
                info.type = tarfile.DIRTYPE
                package.addfile(info)
            else:
                info.size = len(data)
                package.addfile(info, io.BytesIO(data))
    raw = path.read_bytes()
    return path, len(raw), hashlib.sha256(raw).hexdigest()


@pytest.mark.parametrize(
    "members, reason",
    [
        ([(f"f{i}", b"x", "file") for i in range(1001)], "artifact_members"),
        ([("other", b"x", "file")], "artifact_executable"),
        ([("bin/authcrunch", b"", "dir")], "artifact_executable"),
    ],
)
def test_release_archive_shape_failures_keep_their_reasons(tmp_path, monkeypatch, members, reason):
    api = module()
    archive, size, digest = _archive(tmp_path, members)
    monkeypatch.setattr(api, "AUTHCRUNCH_BYTES", size)
    monkeypatch.setattr(api, "AUTHCRUNCH_SHA256", digest)
    with pytest.raises(ValueError, match=reason):
        api.install_authcrunch(archive, tmp_path / "bin")


def test_release_archive_symlinked_destination_is_artifact_path(tmp_path, monkeypatch):
    api = module()
    archive, size, digest = _archive(tmp_path, [("bin/authcrunch", b"#!", "file")])
    monkeypatch.setattr(api, "AUTHCRUNCH_BYTES", size)
    monkeypatch.setattr(api, "AUTHCRUNCH_SHA256", digest)
    (tmp_path / "real").mkdir()
    (tmp_path / "bin").symlink_to(tmp_path / "real")
    with pytest.raises(ValueError, match="artifact_path"):
        api.install_authcrunch(archive, tmp_path / "bin")


@pytest.mark.parametrize(
    "ports, accounts, reason",
    [
        ((80, 9000), 2, "qualification_port"),
        ((9000, True), 2, "qualification_port"),
        ((9000, 9001), 1, "qualification_accounts"),
        ((9000, 9001), "bad-user", "qualification_accounts"),
        ((9000, 9001), "bad-email", "qualification_accounts"),
    ],
)
def test_proxy_configuration_bounds(tmp_path, ports, accounts, reason):
    good = [
        dict(username="alice", email="alice@example.test", password="p"),
        dict(username="bob", email="bob@example.test", password="p"),
    ]
    if accounts == 1:
        good = good[:1]
    elif accounts == "bad-user":
        good[0]["username"] = "Alice!"
    elif accounts == "bad-email":
        good[0]["email"] = "alice@elsewhere.example"
    with pytest.raises(ValueError, match=reason):
        module().proxy_configuration("10.0.0.5", *ports, good, "k" * 32, tmp_path)


@pytest.mark.parametrize("extra", [-1, 1])
def test_release_executable_that_differs_from_its_declared_size_is_artifact_expansion(
    tmp_path, monkeypatch, extra
):
    """#224 review: a member shorter or longer than its header never installs."""
    import io
    import tarfile

    api = module()
    archive, size, digest = _archive(tmp_path, [("bin/authcrunch", b"#!binary", "file")])
    monkeypatch.setattr(api, "AUTHCRUNCH_BYTES", size)
    monkeypatch.setattr(api, "AUTHCRUNCH_SHA256", digest)
    real = tarfile.TarFile.extractfile

    def extractfile(self, member):
        data = real(self, member).read()
        return io.BytesIO(data[:-1] if extra < 0 else data + b"x")

    monkeypatch.setattr(tarfile.TarFile, "extractfile", extractfile)
    with pytest.raises(ValueError, match="artifact_expansion"):
        api.install_authcrunch(archive, tmp_path / "bin")


# --- #231 review (ArcsinX, 1 Nov) ------------------------------------------------


@pytest.mark.parametrize(
    "result",
    [
        None,
        "receipt",
        {"id": "rpc-1"},
        {"id": "rpc-1", "frame": None},
        {"id": "rpc-1", "frame": "bad"},
        {"frame": {"payload": {}}},
    ],
)
def test_malformed_person_rpc_envelopes_are_identity_binding_failures(result):
    with pytest.raises(ValueError, match="^native_person_identity_binding$"):
        module().person_rpc_receipt(result, "fixture@example.test")


@pytest.mark.parametrize("frame", [None, "error", [], 7])
def test_a_non_mapping_frame_has_no_error_code(frame):
    assert module().native_error_code(frame) is None


@pytest.mark.parametrize("interfaces", [None, 7, "eth0", {"flags": []}])
def test_a_non_list_interface_inventory_is_network_inventory_invalid(interfaces):
    with pytest.raises(ValueError, match="^network_inventory_invalid$"):
        module().eligible_private_ipv4(interfaces)


@pytest.mark.parametrize("reason", ["native_socket_unavailable", "native_send_failed"])
def test_observer_refusals_before_sending_keep_their_reason(reason):
    api = module()

    class Page:
        def evaluate(self, *args):
            raise RuntimeError(f"Page.evaluate: Error: {reason}")

    with pytest.raises(ValueError, match=f"^{reason}$"):
        api.native_rpc(Page(), "m", {})
    assert reason in api.SAFE_REASONS


# --- #231 re-review (ArcsinX, 1 Nov) ---------------------------------------------


@pytest.mark.parametrize("blank", [b"\n", b"  \n", b"\r\n", b"\t\r\n"])
def test_native_event_reader_skips_whitespace_and_crlf_blank_lines(tmp_path, blank):
    events = tmp_path / "native-events.jsonl"
    events.write_bytes(blank + b'{"phase":"prepared"}\n' + blank)
    assert module().read_native_events(events) == [{"phase": "prepared"}]


@pytest.mark.parametrize("field", ["phase", "reason"])
@pytest.mark.parametrize("value", [["prepared"], {"p": 1}])
def test_person_rpc_failure_projection_rejects_non_string_phase_or_reason(tmp_path, field, value):
    events = tmp_path / "events.jsonl"
    line = {"requestId": "wanted", "phase": "prepared", "reason": None, "connectionId": "c1"}
    line[field] = value
    events.write_bytes(json.dumps(line).encode() + b"\n")
    assert module().project_person_rpc_failure(events, "wanted")["status"] == "event_file_invalid"


@pytest.mark.parametrize(
    "frame",
    [
        {"id": "rpc-1", "ok": False, "payload": PERSON},
        {"id": "rpc-1", "payload": PERSON},
        {"id": "other", "ok": True, "payload": PERSON},
    ],
)
def test_person_rpc_receipt_needs_an_ok_frame_bound_to_the_request(frame):
    result = {"id": "rpc-1", "socket_id": "s", "frame": frame}
    with pytest.raises(ValueError, match="^native_person_identity_binding$"):
        module().person_rpc_receipt(result, "fixture@example.test")


def test_a_playwright_timeout_is_native_rpc_timeout():
    playwright = pytest.importorskip("playwright.sync_api")

    class Page:
        def evaluate(self, *args):
            raise playwright.TimeoutError("Timeout 30000ms exceeded.")

    with pytest.raises(ValueError, match="^native_rpc_timeout$"):
        module().native_rpc(Page(), "m", {})


def test_workspace_resolution_failure_is_its_own_reason():
    assert "workspace_unavailable" in module().PERSON_RPC_EVENT_REASONS["probe_failed"]


def test_ordinary_failure_projection_survives_capture_writer(tmp_path, monkeypatch):
    """RM12/RM16/RM39 AC30: the exported capture retains fixed extension diagnostics."""
    import platform
    import signal

    from playwright.sync_api import Error

    from test_openclaw_ordinary_channel import extension_fixture
    from test_openclaw_ordinary_channel import module as ordinary_module

    if not hasattr(signal, "setitimer"):
        pytest.skip("SIGALRM is required")
    api = module()
    extension = extension_fixture(ordinary_module(), tmp_path)
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(api, "discover_network", lambda: ["10.1.2.3"])

    def trial(work, address, capture, *, extension):
        capture["phase"] = "account_b_person_rpc"
        with extension.step("ui_actions"):
            raise Error("Page.goto: Target page, context or browser has been closed; secret URL")

    monkeypatch.setattr(api, "_run_private_trial", trial)
    output = tmp_path / "capture.json"
    assert api.run_qualification(tmp_path / "work", output, extension=extension) == 2
    capture = json.loads(output.read_bytes())
    assert capture["reason"] == "ordinary_playwright_target_closed"
    assert capture["ordinary_failure_step"] == "ui_actions"
    assert capture["ordinary_error_class"] == "playwright_target_closed"
    assert "secret URL" not in output.read_text()
    assert capture["checks"]["person_composition"] is False


def test_overall_deadline_interrupts_an_ordinary_extension_wait(tmp_path, monkeypatch):
    """RM16/RM39 AC30: an ordinary wait remains inside the real SIGALRM budget."""
    import platform
    import signal
    import time

    from test_openclaw_ordinary_channel import extension_fixture
    from test_openclaw_ordinary_channel import module as ordinary_module

    if not hasattr(signal, "setitimer"):
        pytest.skip("SIGALRM is required")
    api = module()
    extension = extension_fixture(ordinary_module(), tmp_path)
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(api, "discover_network", lambda: ["10.1.2.3"])
    monkeypatch.setattr(api, "TOTAL_SECONDS", 0.01)
    previous_handler = signal.getsignal(signal.SIGALRM)

    def trial(work, address, capture, *, extension):
        with extension.step("retirement_prepared"):
            time.sleep(0.1)

    monkeypatch.setattr(api, "_run_private_trial", trial)
    output = tmp_path / "capture.json"
    assert api.run_qualification(tmp_path / "work", output, extension=extension) == 2
    capture = json.loads(output.read_bytes())
    assert capture["reason"] == "overall_deadline_exceeded"
    assert capture["ordinary_failure_step"] == "retirement_prepared"
    assert capture["checks"]["person_composition"] is False
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
    assert signal.getsignal(signal.SIGALRM) == previous_handler


# --- readiness after navigation (#231, CI run 41251465501) ------------------------


class _WirePage:
    def __init__(self, raise_with=None):
        self.calls, self.raise_with = [], raise_with

    def wait_for_function(self, expression, **options):
        self.calls.append((expression, options))
        if self.raise_with is not None:
            raise self.raise_with

    def evaluate(self, *args):  # a navigation destroys this context
        raise RuntimeError("Execution context was destroyed, most likely because of a navigation")


def test_wire_readiness_waits_across_navigations_with_a_bound():
    page = _WirePage()
    module().wait_for_wire_ready(page, 20, ValueError("x"))
    ((expression, options),) = page.calls
    assert "__rmPersonWire" in expression and "ready === true" in expression
    assert options["timeout"] == 20000 and options["polling"] == 100


def test_wire_readiness_timeout_raises_the_callers_fixed_failure():
    playwright = pytest.importorskip("playwright.sync_api")
    page = _WirePage(playwright.TimeoutError("Timeout 20000ms exceeded."))
    with pytest.raises(ValueError, match="^ordinary_entry_unavailable$"):
        module().wait_for_wire_ready(page, 20, ValueError("ordinary_entry_unavailable"))


def test_wire_readiness_keeps_the_overall_deadline_and_other_errors():
    page = _WirePage(TimeoutError("overall_deadline_exceeded"))
    with pytest.raises(TimeoutError, match="overall_deadline_exceeded"):
        module().wait_for_wire_ready(page, 20, ValueError("x"))
    page = _WirePage(RuntimeError("Target closed"))
    with pytest.raises(RuntimeError, match="Target closed"):
        module().wait_for_wire_ready(page, 20, ValueError("x"))


def test_no_harness_polls_wire_readiness_with_raw_evaluate():
    """A raw evaluate loop fails when the single-page UI navigates."""
    root = Path(__file__).resolve().parents[1] / "integrations/openclaw/test"
    for path in (
        root / "host-person-qualification/qualify.py",
        root / "host-ordinary-qualification/qualify.py",
    ):
        source = path.read_text(encoding="utf-8")
        assert 'evaluate("() => window.__rmPersonWire?.state().ready' not in source, path
