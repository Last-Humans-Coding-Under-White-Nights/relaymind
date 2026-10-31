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
