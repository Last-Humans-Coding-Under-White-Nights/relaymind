"""RM37/RM39 AC28/AC30: ordinary proof needs original entry/UI/scope/abort evidence."""

import importlib.util
import threading
from pathlib import Path

import pytest

from test_openclaw_person_ingress import complete_capture

ROOT = Path(__file__).resolve().parents[1]


def module():
    path = ROOT / "integrations/openclaw/test/host-ordinary-qualification/qualify.py"
    spec = importlib.util.spec_from_file_location("ordinary_native_fixture", path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def test_flags_do_not_prove_an_ordinary_installed_channel():
    assert not module().ordinary_pass(
        {
            "checks": {"person_composition": True},
            "ordinary_actions": {"production_registry_enabled": True, "default_registration": True},
        }
    )


def test_startup_has_no_companion_constructor_or_fabricated_mapping(tmp_path):
    api = module()
    (tmp_path / "archive.tgz").write_bytes(b"owned fixture archive")
    extension = api.OrdinaryComposition(
        "http://127.0.0.1:12345/v1",
        "app-fixture",
        "action-fixture",
        *[threading.Event() for _ in range(3)],
        archive=tmp_path / "archive.tgz",
        runs=None,
        skill_text=CONTENT,
        skill_asset=dict(
            asset_id="example:hello-world", content_hash="0" * 64, source_version=None
        ),
    )
    config = dict(
        plugins=dict(allow=[], load=dict(paths=[]), entries={}),
        agents=dict(defaults=dict(workspace=str(tmp_path))),
        gateway=dict(controlUi={}),
    )
    extension.configure(config, tmp_path, "https://fixture.test")
    assert "relaymind-action-qualification" not in config["plugins"]["allow"]
    assert "relaymind" not in config["plugins"]["entries"]
    assert extension.config["recommendations"]["bindings"] == []


def fixture(api):
    capture = complete_capture(api.BASE.person_module())
    a, b = capture["accounts"]
    person = dict(
        profileId=a["profile_id"],
        authenticatedUserId=a["native_person_email"],
        sessionKey=a["session_key"],
        sessionId=a["native_session_id"],
        lifecycleRevision=a["lifecycle_revision"],
        connectionId="actual-ui-connection",
    )
    ui_person = dict(
        id="ui-person", socket_id="actual-ui-socket", frame=dict(ok=True, payload=person)
    )
    view = dict(
        state="chosen",
        recommendation_id="rid",
        selected_action=dict(
            adapter_id="ordinary-native-channel", scope=dict(user_id="u", workspace_id="w")
        ),
    )
    record = dict(feedback=dict(kind="correction"), recommendation_id="rid")
    snapshots = {
        name: dict(views=[view], records=[], audits=[])
        for name in ("choice", "correction", "delete", "reset")
    }
    snapshots["correction"]["records"] = [record]
    snapshots["delete"]["audits"] = [dict(operation="delete")]
    snapshots["reset"]["audits"] = [dict(operation="reset")]
    negatives = []
    for i, account in enumerate((a, b)):
        request_id = "negative-" + str(i)
        observed = dict(
            id=request_id,
            socket_id=b["socket_id"],
            frame=dict(ok=False, error=dict(code="FORBIDDEN" if i == 0 else "INVALID_REQUEST")),
        )
        target = dict(
            sessionKey=account["session_key"],
            expectedSessionId=account["native_session_id"],
            expectedLifecycleRevision=account["lifecycle_revision"],
        )
        negatives.append(dict(observed=observed, target=target))
        capture["events"].append(
            dict(
                id="request:" + request_id,
                kind="native_rpc",
                request_id=request_id,
                socket_id=b["socket_id"],
                method="relaymind.recommendations",
                ok=False,
            )
        )
    attempt = dict(
        kind="http_attempt",
        attempt_id="physical-1",
        method="GET",
        path="/relay/v1/feedback",
        monotonic_ns="100",
    )
    error = dict(
        kind="http_error",
        attempt_id="physical-1",
        path="/relay/v1/feedback",
        monotonic_ns="300",
        aborted=True,
    )
    prepared = dict(
        phase="prepared",
        requestId="ui-held-probe",
        connectionId=person["connectionId"],
        monotonic_ns="50",
    )
    retired = dict(
        phase="connection_retired",
        requestId="ui-held-probe",
        connectionId=person["connectionId"],
        monotonic_ns="200",
        connection_aborted=True,
    )
    denied = dict(
        phase="guard_denied",
        requestId="ui-held-probe",
        connectionId=person["connectionId"],
        monotonic_ns="400",
        reason="post_await_authority_retired",
    )
    capture["ordinary_actions"] = dict(
        installation=dict(exit_code=0, archive_sha256="a" * 64),
        default_registration=True,
        constructor_override=False,
        production_registry_enabled=True,
        ui_person=ui_person,
        ui=dict(snapshots=snapshots),
        negative_events=[],
        negative_receipts=negatives,
        retirement=dict(
            held_backend_entered=True,
            after_window=[attempt, error],
            attempt=attempt,
            fetch_failures=[error],
            ui_guard_events=[prepared, retired, denied],
            ui_guard_request="ui-held-probe",
            ui_retired=retired,
            connection_id=person["connectionId"],
            native_socket_id=ui_person["socket_id"],
        ),
    )
    paths = [
        ("/relay/v1/recommendations", "POST"),
        ("/relay/v1/recommendations/rid/choice", "POST"),
        ("/relay/v1/recommendations/rid/feedback", "POST"),
        ("/relay/v1/feedback", "GET"),
        ("/relay/v1/feedback/reset", "POST"),
    ]
    positives = []
    for i, (path, method) in enumerate(paths):
        positives.extend(
            [
                dict(
                    kind="http_attempt", attempt_id="positive-" + str(i), path=path, method=method
                ),
                dict(kind="http", attempt_id="positive-" + str(i), path=path, status=200),
            ]
        )
    capture["ordinary_actions"]["positive_events"] = positives
    return capture


@pytest.mark.parametrize(
    "change",
    [
        "prior_abort",
        "wrong_attempt",
        "early_abort",
        "wrong_ui_connection",
        "wrong_b_socket",
        "wrong_target",
        "multiple_attempts",
    ],
)
def test_prior_unrelated_or_mismatched_ordinary_evidence_cannot_pass(change):
    api = module()
    capture = fixture(api)
    assert api.ordinary_pass(capture)
    value = capture["ordinary_actions"]
    retirement = value["retirement"]
    if change == "prior_abort":
        retirement["after_window"] = []
    elif change == "wrong_attempt":
        retirement["fetch_failures"][0]["attempt_id"] = "unrelated"
    elif change == "early_abort":
        retirement["fetch_failures"][0]["monotonic_ns"] = "150"
    elif change == "wrong_ui_connection":
        retirement["connection_id"] = "base-original-instead-of-ui"
    elif change == "wrong_b_socket":
        value["negative_receipts"][0]["observed"]["socket_id"] = "foreign"
    elif change == "wrong_target":
        value["negative_receipts"][0]["target"]["sessionKey"] = "unrelated"
    elif change == "multiple_attempts":
        retirement["after_window"].append(dict(retirement["attempt"]))
    assert not api.ordinary_pass(capture)


def test_relative_release_archive_is_fixed_before_native_cli_changes_working_directory(
    tmp_path, monkeypatch
):
    """RM39: npm-pack input remains the actual release when the CLI runs in private work."""
    api = module()
    monkeypatch.chdir(tmp_path)
    archive = Path("release.tgz")
    archive.write_bytes(b"owned fixture archive")
    extension = api.OrdinaryComposition(
        "http://127.0.0.1:12345/v1",
        "app",
        "action",
        *[threading.Event() for _ in range(3)],
        archive=archive,
        runs=None,
        skill_text=CONTENT,
        skill_asset=dict(
            asset_id="example:hello-world", content_hash="0" * 64, source_version=None
        ),
    )
    assert extension.archive == tmp_path / "release.tgz"
    assert extension.archive.is_absolute()


# --- #197 exact Skill Library selection (RM39/AC30; spec 7.6.1) -----------------

SKILL_ID = "0b6b8e2a-3a1f-4c2e-9d7b-1f2e3d4c5b6a"
REVISION = "a" * 64
CONTENT = "---\nname: Hello World\ndescription: Greeting skill\n---\nSay hello."


def skill_fixture(api):
    import hashlib

    capture = fixture(api)
    digest = hashlib.sha256(CONTENT.encode("utf-8")).hexdigest()
    capture["ordinary_skill"] = dict(
        library_save=dict(ok=True, skill_id=SKILL_ID, revision=REVISION),
        content_sha256=digest,
        mapping=dict(
            assetId="example:hello-world",
            contentHash=digest,
            sourceVersion=None,
            skillId=SKILL_ID,
            revision=REVISION,
        ),
        receipt=dict(
            ok=True,
            payload=dict(
                skillId=SKILL_ID,
                revision=REVISION,
                sessionActivation="next-turn",
                instructions="not_observed",
            ),
        ),
        ui_status="selected",
        negatives=[
            dict(case="unmapped_skill", ok=False, error_code="INVALID_REQUEST"),
            dict(case="foreign_person", ok=False, error_code="FORBIDDEN"),
        ],
    )
    return capture


def test_exact_skill_selection_evidence_passes():
    api = module()
    assert api.ordinary_skill_pass(skill_fixture(api))


@pytest.mark.parametrize(
    "change",
    [
        "no_ordinary",
        "save_failed",
        "bad_skill_id",
        "bad_revision",
        "mapping_other_skill",
        "hash_mismatch",
        "receipt_claims_use",
        "receipt_failed",
        "ui_unavailable",
        "negative_admitted",
        "negative_missing",
    ],
)
def test_incomplete_or_contradictory_skill_evidence_cannot_pass(change):
    api = module()
    capture = skill_fixture(api)
    value = capture["ordinary_skill"]
    if change == "no_ordinary":
        capture["checks"]["person_composition"] = False
        capture.pop("ordinary_actions")
    elif change == "save_failed":
        value["library_save"]["ok"] = False
    elif change == "bad_skill_id":
        value["library_save"]["skill_id"] = value["mapping"]["skillId"] = "not-an-id"
    elif change == "bad_revision":
        value["library_save"]["revision"] = value["mapping"]["revision"] = "b" * 10
    elif change == "mapping_other_skill":
        value["mapping"]["skillId"] = "1b6b8e2a-3a1f-4c2e-9d7b-1f2e3d4c5b6a"
    elif change == "hash_mismatch":
        value["mapping"]["contentHash"] = "0" * 64
    elif change == "receipt_claims_use":
        value["receipt"]["payload"]["instructions"] = "observed"
    elif change == "receipt_failed":
        value["receipt"]["ok"] = False
    elif change == "ui_unavailable":
        value["ui_status"] = "unavailable"
    elif change == "negative_admitted":
        value["negatives"][0]["ok"] = True
    elif change == "negative_missing":
        value["negatives"].pop()
    assert not api.ordinary_skill_pass(capture)


def test_the_browser_observer_allows_every_method_the_ordinary_run_calls():
    """#197: a method missing from the observer allowlist never reaches the host."""
    import re

    root = Path(__file__).resolve().parents[1] / "integrations/openclaw/test"
    ordinary = (root / "host-ordinary-qualification/qualify.py").read_text(encoding="utf-8")
    person = (root / "host-person-qualification/qualify.py").read_text(encoding="utf-8")
    wire = (root / "host-person-qualification/browser-wire.mjs").read_text(encoding="utf-8")
    called = set(re.findall(r'rpc\(\s*[^,]+,\s*"([a-z.]+)"', ordinary))
    assert {"skills.library.save", "skills.library.list", "relaymind.recommendations"} <= called
    requested = re.search(r"installNativeSocketObserver\(window, \[(.*?)\]\)", person, re.S)
    extras = set(re.findall(r'"([a-z.]+)"', requested.group(1)))
    base = {"rmqualification.person", "sessions.create", "sessions.reset"}
    assert called <= base | extras
    allowed = re.search(r"some\(method => !\[(.*?)\]\.includes", wire).group(1)
    assert extras <= set(re.findall(r'"([a-z.]+)"', allowed))


def loaded_fixture(api):
    """RM39/AC30: retained byte witnesses, never a producer-supplied pass flag."""
    import copy
    import hashlib

    capture = skill_fixture(api)
    cases = []
    for index, name in enumerate(("positive", "stale", "deleted", "reset")):
        scope = dict(
            sessionKey=f"session-{name}", sessionId=f"session-id-{name}", lifecycleRevision="L0"
        )
        if name == "positive":
            person = capture["ordinary_actions"]["ui_person"]["frame"]["payload"]
            scope = {
                field: person[field] for field in ("sessionKey", "sessionId", "lifecycleRevision")
            }
        current = dict(scope, lifecycleRevision="L1") if name == "reset" else dict(scope)
        skill_id = SKILL_ID if index == 0 else f"{index}b6b8e2a-3a1f-4c2e-9d7b-1f2e3d4c5b6a"
        selected = dict(
            skillId=skill_id,
            revision=REVISION,
            content=CONTENT,
            content_sha256=hashlib.sha256(CONTENT.encode()).hexdigest(),
            name="Hello World",
            read=dict(
                ok=True,
                payload=dict(entry=dict(skillId=skill_id, revision=REVISION), content=CONTENT),
            ),
        )
        run_id, task = f"host-{name}", f"task-{name}"
        call_id = f"call{name}"
        request = dict(
            model="relaymind-client-tools",
            messages=[
                dict(
                    role="system",
                    content=(
                        "<available_skills><skill><name>Hello World</name>"
                        f"<location>/skill-library/{skill_id}/revisions/{REVISION}/SKILL.md"
                        "</location></skill></available_skills>"
                    ),
                ),
                dict(role="user", content=task),
            ],
        )
        receiving = copy.deepcopy(request)
        receiving["messages"].extend(
            [
                dict(
                    role="assistant",
                    content=None,
                    tool_calls=[
                        dict(
                            id=call_id,
                            type="function",
                            function=dict(name="skills_read", arguments='{"name":"Hello World"}'),
                        )
                    ],
                ),
                dict(role="tool", tool_call_id=call_id, content=CONTENT),
            ]
        )
        attempts = [
            dict(
                request_id=f"request-{name}-{i}",
                idempotency_key=f"key-{name}-{i}",
                body=body,
                run_id=f"relay-{name}-{i}",
                status=200,
            )
            for i, body in enumerate((request, receiving))
        ]
        tool = dict(
            context=dict(current, runId=run_id, toolCallId=call_id),
            event=dict(
                toolName="skills_read",
                toolCallId=call_id,
                runId=run_id,
                params=dict(name="Hello World"),
                result=dict(content=[dict(type="text", text=CONTENT)]),
            ),
        )
        intervention = None
        if name == "stale":
            intervention = dict(
                method="skills.library.save",
                params=dict(
                    skillId=skill_id, expectedRevision=REVISION, content=CONTENT + "\nChanged."
                ),
                frame=dict(ok=True, payload=dict(entry=dict(skillId=skill_id, revision="b" * 64))),
            )
        elif name == "deleted":
            intervention = dict(
                method="skills.library.mutate",
                params=dict(skillId=skill_id, expectedRevision=REVISION, action="remove"),
                frame=dict(
                    ok=True,
                    payload=dict(
                        state="removed",
                        entry=dict(skillId=skill_id, revision=REVISION, removed=True),
                    ),
                ),
            )
        elif name == "reset":
            intervention = dict(
                method="sessions.reset",
                params=dict(
                    key=scope["sessionKey"], expectedSessionId=scope["sessionId"], reason="reset"
                ),
                frame=dict(
                    ok=True,
                    payload=dict(
                        entry=dict(sessionId=current["sessionId"], lifecycleRevision="L1")
                    ),
                ),
            )
        cases.append(
            dict(
                case=name,
                task=task,
                before=scope,
                current=current,
                selected=selected,
                selection=dict(
                    ok=True,
                    payload=dict(
                        skillId=skill_id,
                        revision=REVISION,
                        sessionActivation="next-turn",
                        instructions="not_observed",
                    ),
                ),
                intervention=intervention,
                chat=dict(
                    params=dict(
                        sessionKey=current["sessionKey"], message=task, idempotencyKey=run_id
                    ),
                    frame=dict(ok=True, payload=dict(runId=run_id)),
                ),
                tools=[tool],
                requests=attempts,
                terminal=dict(
                    runId=run_id,
                    sessionKey=current["sessionKey"],
                    state="final",
                    message=dict(
                        role="assistant",
                        content=[dict(type="text", text="Native skill observation complete.")],
                    ),
                ),
                observed="loaded_pinned_revision",
            )
        )
    for case in cases[1:]:
        case["selection"] = dict(
            ok=True,
            payload=dict(
                sessionKey=case["before"]["sessionKey"],
                sessionActivation="next-turn",
                selections=[
                    dict(skillId=case["selected"]["skillId"], revision=REVISION, name="Hello World")
                ],
            ),
        )
    capture["ordinary_skill_loaded_use"] = dict(
        tier="deterministic_native_observation", qualification="unavailable", cases=cases
    )
    return capture


def test_loaded_byte_round_trip_is_observation_only():
    """RM39/AC30: even retained stale/deleted pins do not qualify loaded use."""
    api = module()
    assert api.ordinary_skill_loaded_use_pass(loaded_fixture(api))


@pytest.mark.parametrize(
    "path",
    [
        "ordinary_actions",
        "ordinary_skill",
        "ordinary_skill_loaded_use.cases",
        "ordinary_skill_loaded_use.cases.0.selected.content",
        "ordinary_skill_loaded_use.cases.0.selected.content_sha256",
        "ordinary_skill_loaded_use.cases.0.selected.revision",
        "ordinary_skill_loaded_use.cases.0.selected.read",
        "ordinary_skill_loaded_use.cases.0.selection",
        "ordinary_skill_loaded_use.cases.0.before.lifecycleRevision",
        "ordinary_skill_loaded_use.cases.0.current.sessionId",
        "ordinary_skill_loaded_use.cases.0.chat",
        "ordinary_skill_loaded_use.cases.0.tools.0.event.result",
        "ordinary_skill_loaded_use.cases.0.tools.0.event.params",
        "ordinary_skill_loaded_use.cases.0.tools.0.context.runId",
        "ordinary_skill_loaded_use.cases.0.requests.1.run_id",
        "ordinary_skill_loaded_use.cases.0.requests.1.idempotency_key",
        "ordinary_skill_loaded_use.cases.0.requests.1.body.messages.3",
        "ordinary_skill_loaded_use.cases.0.terminal",
        "ordinary_skill_loaded_use.cases.1.intervention",
        "ordinary_skill_loaded_use.cases.2.intervention",
        "ordinary_skill_loaded_use.cases.3.intervention",
    ],
)
def test_loaded_use_missing_proof_fails(path):
    """RM39/AC30: each independent proof is mandatory."""
    api = module()
    capture = loaded_fixture(api)
    parts = path.split(".")
    value = capture
    for part in parts[:-1]:
        value = value[int(part)] if isinstance(value, list) else value[part]
    del value[int(parts[-1]) if isinstance(value, list) else parts[-1]]
    assert not api.ordinary_skill_loaded_use_pass(capture)


@pytest.mark.parametrize(
    "change",
    [
        "duplicate_case",
        "duplicate_session",
        "duplicate_run",
        "empty_attempts",
        "no_receiving_request",
        "catalog_only",
        "changed_bytes",
        "changed_digest",
        "wrong_tool",
        "wrong_call",
        "wrong_name",
        "wrong_session",
        "wrong_lifecycle",
        "wrong_host_run",
        "wrong_terminal",
        "failed_terminal",
        "changed_revision",
        "changed_skill",
        "false_observation",
        "qualified",
        "control_claims_pass",
        "stale_no_change",
        "reset_no_change",
        "failed_intervention",
        "unrelated_task",
        "unrelated_proposal",
    ],
)
def test_loaded_use_mutated_proof_fails(change):
    """RM39/AC30: correlation and actual UTF-8 bytes must survive hostile mutations."""
    api = module()
    capture = loaded_fixture(api)
    value = capture["ordinary_skill_loaded_use"]
    case = value["cases"][0]
    if change == "duplicate_case":
        value["cases"].append(value["cases"][1])
    elif change == "duplicate_session":
        value["cases"][1]["before"] = case["before"]
    elif change == "duplicate_run":
        value["cases"][1]["requests"][0]["run_id"] = case["requests"][0]["run_id"]
    elif change == "empty_attempts":
        case["requests"] = []
    elif change == "no_receiving_request":
        case["requests"].pop()
    elif change == "catalog_only":
        case["requests"][1]["body"]["messages"][-1]["role"] = "system"
    elif change == "changed_bytes":
        case["tools"][0]["event"]["result"]["content"][0]["text"] += " altered"
    elif change == "changed_digest":
        case["selected"]["content_sha256"] = "0" * 64
    elif change == "wrong_tool":
        case["tools"][0]["event"]["toolName"] = "read"
    elif change == "wrong_call":
        case["requests"][1]["body"]["messages"][-1]["tool_call_id"] = "foreign"
    elif change == "wrong_name":
        case["tools"][0]["event"]["params"]["name"] = "foreign"
    elif change == "wrong_session":
        case["tools"][0]["context"]["sessionKey"] = "foreign"
    elif change == "wrong_lifecycle":
        case["current"]["lifecycleRevision"] = "foreign"
    elif change == "wrong_host_run":
        case["chat"]["frame"]["payload"]["runId"] = "foreign"
    elif change == "wrong_terminal":
        case["terminal"]["runId"] = "foreign"
    elif change == "failed_terminal":
        case["terminal"]["state"] = "error"
    elif change == "changed_revision":
        case["selected"]["revision"] = "b" * 64
    elif change == "changed_skill":
        case["selected"]["skillId"] = "foreign"
    elif change == "false_observation":
        case["observed"] = "refused"
    elif change == "qualified":
        value["qualification"] = "qualified"
    elif change == "control_claims_pass":
        value["cases"][1]["passed"] = True
    elif change == "stale_no_change":
        value["cases"][1]["intervention"]["frame"]["payload"]["entry"]["revision"] = REVISION
    elif change == "reset_no_change":
        value["cases"][3]["current"]["lifecycleRevision"] = "L0"
    elif change == "failed_intervention":
        value["cases"][2]["intervention"]["frame"]["ok"] = False
    elif change == "unrelated_task":
        case["requests"][1]["body"]["messages"][1]["content"] = "foreign"
    elif change == "unrelated_proposal":
        case["requests"][1]["body"]["messages"][2]["tool_calls"][0]["function"]["name"] = "foreign"
    assert not api.ordinary_skill_loaded_use_pass(capture)


@pytest.mark.parametrize("observed", ["not_loaded", "refused"])
def test_controls_are_observations_without_rejection_requirement(observed):
    """RM39/AC30: a completed control may refuse or omit loading; neither qualifies it."""
    api = module()
    capture = loaded_fixture(api)
    case = capture["ordinary_skill_loaded_use"]["cases"][3]
    case["tools"] = []
    case["requests"] = case["requests"][:1]
    case["observed"] = observed
    if observed == "refused":
        case["terminal"]["state"] = "error"
    assert api.ordinary_skill_loaded_use_pass(capture)


def test_scripted_gateway_records_real_client_owned_requests(tmp_path):
    """RM39/AC30, RM33: deterministic transport plumbing is not native host evidence."""
    from api_helpers import call
    from recommendation_helpers import setup

    api = module()
    _, service, runs, _ = setup(tmp_path)
    app, gateway, observation = api.loaded_use_gateway(tmp_path / "inference", service)
    case = loaded_fixture(api)["ordinary_skill_loaded_use"]["cases"][0]
    observation.begin(case["selected"], case["task"])
    body = case["requests"][0]["body"]
    body["tools"] = [
        dict(
            type="function",
            function=dict(
                name="skills_read",
                description="Read installed skill",
                parameters=dict(
                    type="object", properties=dict(name=dict(type="string")), required=["name"]
                ),
            ),
        )
    ]
    try:
        first = call(
            app,
            "POST",
            "/v1/chat/completions",
            json_body=body,
            headers={"Idempotency-Key": "first"},
        )
        assert first.status_code == 200, first.text
        proposal = first.json()["choices"][0]["message"]
        assert proposal["tool_calls"][0]["function"]["name"] == "skills_read"
        assert __import__("json").loads(proposal["tool_calls"][0]["function"]["arguments"]) == {
            "name": "Hello World"
        }
        body["messages"].extend(
            [
                proposal,
                dict(role="tool", tool_call_id=proposal["tool_calls"][0]["id"], content=CONTENT),
            ]
        )
        second = call(
            app,
            "POST",
            "/v1/chat/completions",
            json_body=body,
            headers={"Idempotency-Key": "second"},
        )
        assert second.status_code == 200, second.text
        assert second.json()["choices"][0]["message"]["content"] == api.LOADED_USE_FINAL
        assert len(observation.requests) == 2
        assert observation.requests[1]["body"]["messages"][-1]["content"] == CONTENT
        assert [r["run_id"] for r in observation.requests] == [
            first.headers["X-RelayMind-Run-Id"],
            second.headers["X-RelayMind-Run-Id"],
        ]
        assert observation.requests[0]["run_id"] != observation.requests[1]["run_id"]
        third = call(
            app,
            "POST",
            "/v1/chat/completions",
            json_body=body,
            headers={"Idempotency-Key": "third"},
            raise_app_exceptions=False,
        )
        assert third.status_code >= 400
    finally:
        gateway.store.close()
        runs.close()


def test_host_uses_packaged_provider_and_only_native_skill_read(tmp_path):
    """RM39/AC30: fixture permits native read without a companion provider."""
    api = module()
    archive = tmp_path / "release.tgz"
    archive.write_bytes(b"fixture")
    extension = api.OrdinaryComposition(
        "http://127.0.0.1:12345/v1",
        "app",
        "action",
        *[threading.Event() for _ in range(3)],
        archive=archive,
        runs=None,
        skill_text=CONTENT,
        skill_asset={},
    )
    config = dict(
        plugins=dict(allow=[], load=dict(paths=[]), entries={}),
        agents=dict(defaults=dict(workspace=str(tmp_path))),
        gateway=dict(controlUi={}),
    )
    extension.configure(config, tmp_path, "https://fixture.test")
    assert config["agents"]["defaults"]["model"]["primary"] == "relaymind/relaymind-client-tools"
    assert config["tools"]["allow"] == ["skills_read"]
    assert extension.environment()["RELAYMIND_APP_TOKEN"] == "app"


def test_control_native_activation_is_retained_as_native_not_bridge_receipt():
    """RM39/AC30: direct activation is isolated control setup, never adapter qualification."""
    api = module()
    capture = loaded_fixture(api)
    for case in capture["ordinary_skill_loaded_use"]["cases"][1:]:
        case["selection"] = dict(
            ok=True,
            payload=dict(
                sessionKey=case["before"]["sessionKey"],
                sessionActivation="next-turn",
                selections=[
                    dict(skillId=case["selected"]["skillId"], revision=REVISION, name="Hello World")
                ],
            ),
        )
    assert api.ordinary_skill_loaded_use_pass(capture)
    capture["ordinary_skill_loaded_use"]["cases"][1]["selection"]["payload"]["selections"][0][
        "revision"
    ] = "b" * 64
    assert not api.ordinary_skill_loaded_use_pass(capture)


def test_native_turn_collects_socket_terminal_tool_hook_and_gateway_window(tmp_path):
    """RM39/AC30: the turn collector retains boundary observations, not generated tool bytes."""
    import copy
    import json
    from types import SimpleNamespace

    api = module()
    case = loaded_fixture(api)["ordinary_skill_loaded_use"]["cases"][0]
    archive = tmp_path / "release.tgz"
    archive.write_bytes(b"fixture")
    extension = api.OrdinaryComposition(
        "http://127.0.0.1:1/v1",
        "app",
        "action",
        *[threading.Event() for _ in range(3)],
        archive=archive,
        runs=None,
        skill_text=CONTENT,
        skill_asset={},
    )
    extension.event_file = tmp_path / "events.jsonl"
    extension.event_file.write_text("")
    records = []
    extension.observation = SimpleNamespace(requests=records, begin=lambda selected, task: None)
    terminal = {}
    calls = []

    def rpc(page, method, params):
        calls.append((method, params))
        if method == "sessions.messages.subscribe":
            return dict(frame=dict(ok=True, payload=dict(subscribed=True, key=params["key"])))
        if method == "chat.send":
            assert calls[0][0] == "sessions.messages.subscribe"
            run_id = params["idempotencyKey"]
            tool = copy.deepcopy(case["tools"][0])
            tool["context"]["runId"] = tool["event"]["runId"] = run_id
            extension.event_file.write_text(
                json.dumps(dict(kind="native_skill_tool", **tool)) + "\n"
            )
            records.extend(copy.deepcopy(case["requests"]))
            terminal.update(case["terminal"], runId=run_id)
            return dict(frame=dict(ok=True, payload=dict(runId=run_id)))
        assert method == "rmqualification.person"
        return dict(frame=dict(ok=True, payload=case["current"]))

    page = SimpleNamespace(evaluate=lambda *args: [terminal], wait_for_timeout=lambda ms: None)
    result = copy.deepcopy(case)
    result.update(tools=[], requests=[], terminal={}, chat={})
    params = dict(
        sessionKey=case["current"]["sessionKey"],
        expectedSessionId=case["current"]["sessionId"],
        expectedLifecycleRevision=case["current"]["lifecycleRevision"],
    )
    extension.loaded_use_turn(rpc, page, params, result)
    assert result["observed"] == "loaded_pinned_revision"
    assert result["tools"][0]["event"]["result"]["content"][0]["text"] == CONTENT
    assert result["requests"] == records
    assert [method for method, _ in calls] == [
        "sessions.messages.subscribe",
        "chat.send",
        "rmqualification.person",
    ]


def test_native_turn_wait_is_bounded(tmp_path):
    """RM39/AC30: an unobserved terminal result cannot become a synthetic success."""
    from types import SimpleNamespace

    api = module()
    archive = tmp_path / "release.tgz"
    archive.write_bytes(b"fixture")
    extension = api.OrdinaryComposition(
        "http://127.0.0.1:1/v1",
        "app",
        "action",
        *[threading.Event() for _ in range(3)],
        archive=archive,
        runs=None,
        skill_text=CONTENT,
        skill_asset={},
    )
    extension.event_file = tmp_path / "events.jsonl"
    extension.event_file.write_text("")
    extension.observation = SimpleNamespace(requests=[], begin=lambda *args: None)
    case = loaded_fixture(api)["ordinary_skill_loaded_use"]["cases"][0]
    with pytest.raises(ValueError, match="ordinary_skill_turn_timeout"):
        extension.loaded_use_turn(
            lambda page, method, params: dict(
                frame=dict(
                    ok=True,
                    payload=(
                        dict(subscribed=True, key=params["key"])
                        if method == "sessions.messages.subscribe"
                        else dict(runId=params["idempotencyKey"])
                    ),
                )
            ),
            SimpleNamespace(evaluate=lambda *args: []),
            {},
            case,
            timeout=0,
        )


@pytest.mark.parametrize(
    "change", ["host_run", "session", "call", "missing_result", "missing_receiving", "wrong_role"]
)
def test_controls_cannot_relabel_broken_correlation_as_an_observation(change):
    """RM39/AC30: an observation label never repairs a missing native/request witness."""
    api = module()
    capture = loaded_fixture(api)
    case = capture["ordinary_skill_loaded_use"]["cases"][1]
    if change == "host_run":
        case["tools"][0]["context"]["runId"] = "unrelated"
    elif change == "session":
        case["tools"][0]["context"]["sessionId"] = "unrelated"
    elif change == "call":
        case["requests"][1]["body"]["messages"][-1]["tool_call_id"] = "unrelated"
    elif change == "missing_result":
        case["tools"] = []
    elif change == "missing_receiving":
        case["requests"].pop()
    else:
        case["requests"][1]["body"]["messages"][-1]["role"] = "system"
    case["observed"] = "not_loaded" if change == "missing_result" else "loaded_other_bytes"
    assert not api.ordinary_skill_loaded_use_pass(capture)


def test_positive_turn_must_belong_to_original_bridge_session():
    """RM39/AC30: another internally consistent native session is not the selected session."""
    api = module()
    capture = loaded_fixture(api)
    case = capture["ordinary_skill_loaded_use"]["cases"][0]
    case["before"]["lifecycleRevision"] = case["current"]["lifecycleRevision"] = "another-life"
    assert not api.ordinary_skill_loaded_use_pass(capture)


def test_control_refusal_before_inference_is_observed_without_inventing_a_gateway_request():
    """RM39/AC30: a native terminal refusal may legitimately precede any model request."""
    api = module()
    capture = loaded_fixture(api)
    case = capture["ordinary_skill_loaded_use"]["cases"][3]
    case.update(tools=[], requests=[], observed="refused")
    case["terminal"]["state"] = "error"
    assert api.ordinary_skill_loaded_use_pass(capture)


def test_timeout_retains_received_gateway_attempts(tmp_path):
    """RM16/RM39: an incomplete native turn must not discard observed inference."""
    import json
    from types import SimpleNamespace

    api = module()
    archive = tmp_path / "release.tgz"
    archive.write_bytes(b"fixture")
    extension = api.OrdinaryComposition(
        "http://127.0.0.1:1/v1",
        "app",
        "action",
        *[threading.Event() for _ in range(3)],
        archive=archive,
        runs=None,
        skill_text=CONTENT,
        skill_asset={},
    )
    extension.event_file = tmp_path / "events.jsonl"
    extension.event_file.write_text("")
    requests = []
    extension.observation = SimpleNamespace(requests=requests, begin=lambda *args: None)
    case = loaded_fixture(api)["ordinary_skill_loaded_use"]["cases"][0]
    observed_tool = case["tools"][0]

    def send(page, method, params):
        if method == "sessions.messages.subscribe":
            return dict(frame=dict(ok=True, payload=dict(subscribed=True, key=params["key"])))
        requests.append(dict(request_id="retained-attempt"))
        extension.event_file.write_text(
            json.dumps(dict(kind="native_skill_tool", **observed_tool)) + "\n"
        )
        return dict(frame=dict(ok=True, payload=dict(runId=params["idempotencyKey"])))

    with pytest.raises(ValueError, match="ordinary_skill_turn_timeout"):
        extension.loaded_use_turn(
            send, SimpleNamespace(evaluate=lambda *args: []), {}, case, timeout=0
        )
    assert case["requests"] == requests
    assert case["tools"] == [observed_tool]
    assert case["observed"] == "incomplete"


def test_deleted_control_requires_native_removal_receipt():
    """RM39/AC30: dispatching remove is not evidence that removal happened."""
    api = module()
    capture = loaded_fixture(api)
    capture["ordinary_skill_loaded_use"]["cases"][2]["intervention"]["frame"]["payload"] = {}
    assert not api.ordinary_skill_loaded_use_pass(capture)


def test_native_turn_refuses_to_run_when_session_subscription_is_denied(tmp_path):
    """RM39/AC30: Control UI session-scoped events need an actual native subscription."""
    from types import SimpleNamespace

    api = module()
    archive = tmp_path / "release.tgz"
    archive.write_bytes(b"fixture")
    extension = api.OrdinaryComposition(
        "http://127.0.0.1:1/v1",
        "app",
        "action",
        *[threading.Event() for _ in range(3)],
        archive=archive,
        runs=None,
        skill_text=CONTENT,
        skill_asset={},
    )
    extension.event_file = tmp_path / "events.jsonl"
    extension.event_file.write_text("")
    extension.observation = SimpleNamespace(requests=[], begin=lambda *args: None)
    calls = []

    def denied(page, method, params):
        calls.append(method)
        return dict(frame=dict(ok=False, error=dict(code="FORBIDDEN")))

    with pytest.raises(ValueError, match="ordinary_skill_subscription_failed"):
        extension.loaded_use_turn(
            denied, None, {}, loaded_fixture(api)["ordinary_skill_loaded_use"]["cases"][0]
        )
    assert calls == ["sessions.messages.subscribe"]
