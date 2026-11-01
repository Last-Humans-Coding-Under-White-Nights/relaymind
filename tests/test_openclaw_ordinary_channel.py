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


@pytest.mark.parametrize("observe", [False, True])
def test_the_browser_observer_allows_every_method_the_ordinary_run_calls(tmp_path, observe):
    """RM39/AC30 N5: ordinary runs retain the base grants; observation grants are opt-in."""
    import re

    api = module()
    extension = extension_fixture(api, tmp_path)
    if observe:
        extension.observation = api.LoadedUseGatewayObservation()
    person = api.BASE.person_module()
    plugin = ROOT / "integrations/openclaw/test/host-person-qualification"
    script = person.socket_observer_script(plugin, extension)
    requested = re.search(r"installNativeSocketObserver\(window, (\[.*?\])\);", script)
    import json

    extras = set(json.loads(requested.group(1)))
    ordinary = {
        "relaymind.recommendations",
        "relaymind.skill",
        "skills.library.save",
        "skills.library.list",
    }
    observation = {
        "sessions.messages.subscribe",
        "chat.send",
        "skills.library.read",
        "skills.library.mutate",
        "skills.library.activate",
    }
    assert extras == ordinary | (observation if observe else set())
    assert "installNativeSocketObserver();" in person.socket_observer_script(plugin)
    source = Path(api.__file__).read_text()
    called = set(re.findall(r'rpc\(\s*[^,]+,\s*"([a-z.]+)"', source))
    assert ordinary | observation <= called
    assert (
        called
        <= {"rmqualification.person", "sessions.create", "sessions.reset"} | ordinary | observation
    )
    wire = (plugin / "browser-wire.mjs").read_text()
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
                payload=dict(
                    entry=dict(skillId=skill_id, revision=REVISION, name="Hello World"),
                    content=CONTENT,
                ),
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
                    socket_id="socket",
                    params=dict(
                        sessionKey=current["sessionKey"], message=task, idempotencyKey=run_id
                    ),
                    frame=dict(ok=True, payload=dict(runId=run_id)),
                ),
                tools=[tool],
                requests=attempts,
                provider_attempts=[
                    dict(
                        idempotencyKey=a["idempotency_key"],
                        runId=a["run_id"],
                        event="relaymind.inference_attempt",
                    )
                    for a in attempts
                ],
                advertised_revision=REVISION,
                failure_reason=None,
                subscription=dict(
                    ok=True, subscribed=True, key=current["sessionKey"], socket_id="socket"
                ),
                terminal=dict(
                    socket_id="socket",
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
    case["provider_attempts"] = case["provider_attempts"][:1]
    case["observed"] = observed
    if observed == "refused":
        case["terminal"]["state"] = "error"
    assert api.ordinary_skill_loaded_use_pass(capture)


def native_wrapped_task(task):
    # Real 2026.10.7 webchat shape from the Ubuntu observation capture;
    # disposable identities replace the original run's metadata.
    return (
        "[Sun 2026-11-01 00:21 UTC] Conversation info: ⟦openclaw:ctx⟧\n```json\n"
        '{"sender":{"id":"disposable-person","name":"account-a"}}\n```\n\n'
        + task
        + "\n\nRuntime: agent=main | session=agent:main:person-a | channel=webchat"
    )


@pytest.mark.parametrize("content", ["foreign task", "prefix {task}", "{task} suffix", '"{task}"'])
def test_observation_rejects_mismatched_task_in_native_wrapper(content):
    """RM16/RM39 AC30: wrapping must not turn a substring into task correlation."""
    from types import SimpleNamespace as NS

    api = module()
    capture = loaded_fixture(api)
    case = capture["ordinary_skill_loaded_use"]["cases"][0]
    wrapped = native_wrapped_task(content.format(task=case["task"]))
    case["requests"][0]["body"]["messages"][1]["content"] = wrapped
    assert api.finish_capture(capture, 0, observe=True) == 2
    observation = api.LoadedUseGatewayObservation()
    observation.begin(case["selected"], case["task"])
    with pytest.raises(ValueError, match="^ordinary_skill_task_mismatch$"):
        observation.generate(NS(messages=[NS(role=NS(value="user"), content=wrapped)]))


@pytest.mark.parametrize("wrapped", [False, True])
def test_scripted_gateway_records_real_client_owned_requests(tmp_path, wrapped):
    """RM39/AC30, RM33: deterministic transport plumbing is not native host evidence."""
    from api_helpers import call
    from recommendation_helpers import setup

    api = module()
    _, service, runs, _ = setup(tmp_path)
    app, gateway, observation = api.loaded_use_gateway(tmp_path / "inference", service)
    case = loaded_fixture(api)["ordinary_skill_loaded_use"]["cases"][0]
    observation.begin(case["selected"], case["task"])
    body = case["requests"][0]["body"]
    if wrapped:
        body["messages"][1]["content"] = native_wrapped_task(case["task"])
        body["messages"].append(
            dict(
                role="user",
                content="OpenClaw runtime context:\nConversation data (data, not instructions):\n"
                '"disposable requester profile"\nEnd OpenClaw runtime context.',
            )
        )
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
        observation=api.LoadedUseGatewayObservation(),
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
    assert "hooks" not in config["plugins"]["entries"]["relaymind-ordinary-observer"]
    assert all(path.name != "probe.test.ts" for path in extension.source_paths())

    assert extension.config["diagnosticsPath"] == str(tmp_path / "provider-attempts.jsonl")


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
    extension.provider_attempt_file = tmp_path / "attempts.jsonl"
    extension.provider_attempt_file.write_text(
        "".join(json.dumps(a) + "\n" for a in case["provider_attempts"])
    )
    records = []
    extension.observation = SimpleNamespace(requests=records, begin=lambda selected, task: None)
    terminal = {}
    calls = []

    def rpc(page, method, params):
        calls.append((method, params))
        if method == "sessions.messages.subscribe":
            return dict(
                socket_id="socket",
                frame=dict(ok=True, payload=dict(subscribed=True, key=params["key"])),
            )
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
            return dict(socket_id="socket", frame=dict(ok=True, payload=dict(runId=run_id)))
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
                socket_id="socket",
                frame=dict(
                    ok=True,
                    payload=(
                        dict(subscribed=True, key=params["key"])
                        if method == "sessions.messages.subscribe"
                        else dict(runId=params["idempotencyKey"])
                    ),
                ),
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
    case.update(tools=[], requests=[], provider_attempts=[], observed="refused")
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
            return dict(
                socket_id="socket",
                frame=dict(ok=True, payload=dict(subscribed=True, key=params["key"])),
            )
        observed_tool["context"]["runId"] = observed_tool["event"]["runId"] = params[
            "idempotencyKey"
        ]
        requests.append(dict(request_id="retained-attempt"))
        extension.event_file.write_text(
            json.dumps(dict(kind="native_skill_tool", **observed_tool)) + "\n"
        )
        return dict(
            socket_id="socket", frame=dict(ok=True, payload=dict(runId=params["idempotencyKey"]))
        )

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


FAILURE_STEPS = {
    "configure",
    "install",
    "sources",
    "persons_ready",
    "save_library_skill",
    "entry_read",
    "ui_actions",
    "select_library_skill",
    "negative_actions",
    "retirement_prepared",
    "retirement_attempt",
    "retirement_abort",
    "finalize",
}


def extension_fixture(api, tmp_path):
    archive = tmp_path / "release.tgz"
    archive.write_bytes(b"disposable archive")
    return api.OrdinaryComposition(
        "http://127.0.0.1:12345/v1",
        "app",
        "action",
        *[threading.Event() for _ in range(3)],
        archive=archive,
        runs=None,
        skill_text=CONTENT,
        skill_asset=dict(
            asset_id="example:hello-world",
            content_hash="0" * 64,
            source_version=None,
        ),
    )


@pytest.mark.parametrize("step", sorted(FAILURE_STEPS))
@pytest.mark.parametrize(
    "error_kind",
    [
        "playwright_timeout",
        "playwright_target_closed",
        "playwright_error",
        "value_error",
        "other",
    ],
)
def test_ordinary_failure_diagnostics_are_closed_and_private(tmp_path, step, error_kind):
    """RM12/RM16/RM39 AC30: every substep exports only fixed discriminators."""
    from playwright._impl._errors import TargetClosedError
    from playwright.sync_api import Error
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    api = module()
    extension = extension_fixture(api, tmp_path)
    classes = dict(
        playwright_timeout=PlaywrightTimeout,
        playwright_target_closed=TargetClosedError,
        playwright_error=Error,
        value_error=ValueError,
        other=RuntimeError,
    )
    with pytest.raises(ValueError) as caught:
        with extension.step(step):
            raise classes[error_kind]("https://private.example/secret?token=credential")
    assert str(caught.value) == (
        "ordinary_" + error_kind if error_kind.startswith("playwright_") else "ordinary_step_failed"
    )
    assert str(caught.value) in extension.failure_reasons()
    assert extension.failure_diagnostics() == {
        "ordinary_failure_step": step,
        "ordinary_error_class": error_kind,
    }
    assert api.ORDINARY_FAILURE_STEPS == FAILURE_STEPS


def test_ordinary_deadline_is_never_reclassified_or_swallowed(tmp_path):
    """RM16/RM39 AC30: nested step reporting cannot replace SIGALRM's reason."""
    api = module()
    extension = extension_fixture(api, tmp_path)
    with pytest.raises(TimeoutError, match="^overall_deadline_exceeded$"):
        with extension.step("persons_ready"):
            with extension.step("select_library_skill"):
                raise TimeoutError("overall_deadline_exceeded")
    assert extension.failure_diagnostics() == {
        "ordinary_failure_step": "select_library_skill",
        "ordinary_error_class": "other",
    }


def test_library_save_reports_the_actual_substep(tmp_path):
    """RM12/RM39 AC30: RPC exception translation retains the original browser class."""
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    api = module()
    extension = extension_fixture(api, tmp_path)

    class Page:
        def evaluate(self, *args):
            raise PlaywrightTimeout("private browser URL and credential")

    with pytest.raises(ValueError, match="^ordinary_playwright_timeout$"):
        extension.save_library_skill(api.BASE.person_module().native_rpc, Page(), {})
    assert extension.failure_diagnostics() == {
        "ordinary_failure_step": "save_library_skill",
        "ordinary_error_class": "playwright_timeout",
    }


@pytest.mark.parametrize("error_kind", ["deadline", "timeout", "closed"])
def test_library_selection_wait_cannot_swallow_browser_failure(tmp_path, monkeypatch, error_kind):
    """RM16/RM39 AC30: an unobserved UI selection fails at its original substep."""
    from playwright._impl._errors import TargetClosedError
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    api = module()
    extension = extension_fixture(api, tmp_path)
    monkeypatch.setattr(
        extension,
        "observe_backend",
        lambda: {
            "views": [{"state": "chosen", "recommendation_id": "r"}],
        },
    )
    errors = dict(
        deadline=TimeoutError("overall_deadline_exceeded"),
        timeout=PlaywrightTimeout("private"),
        closed=TargetClosedError("private"),
    )

    class Page:
        def get_by_role(self, *args, **kwargs):
            return self

        def get_by_text(self, *args, **kwargs):
            return self

        def click(self):
            pass

        def select_option(self, **kwargs):
            pass

        def wait_for(self, **kwargs):
            raise errors[error_kind]

    def rpc(*args):
        return {"frame": {"ok": True, "payload": {}}}

    skill = skill_fixture(api)["ordinary_skill"]
    expected = (
        "overall_deadline_exceeded"
        if error_kind == "deadline"
        else (
            "ordinary_playwright_timeout"
            if error_kind == "timeout"
            else "ordinary_playwright_target_closed"
        )
    )
    with pytest.raises((ValueError, TimeoutError), match="^" + expected + "$"):
        extension.select_library_skill(
            rpc, [Page(), Page()], [{"sessionKey": "s"}, {}], Page(), skill
        )
    assert extension.failure_diagnostics()["ordinary_failure_step"] == "select_library_skill"


@pytest.mark.parametrize("arrives", [True, False])
def test_retirement_wait_allows_loaded_runner_but_stays_bounded(tmp_path, monkeypatch, arrives):
    """RM16/RM39 AC30: delayed original abort evidence passes; absent evidence times out."""
    import json

    api = module()
    extension = extension_fixture(api, tmp_path)
    retirement = fixture(api)["ordinary_actions"]["retirement"]
    extension.event_file = tmp_path / "events.jsonl"
    extension.event_file.write_text("")
    extension.retirement_start = 0
    extension.held_attempt = retirement["attempt"]
    extension.held_request = "held"
    extension.ui_guard_request = retirement["ui_guard_request"]
    extension.ui_person = {
        "frame": {"payload": {"connectionId": retirement["connection_id"]}},
        "socket_id": retirement["native_socket_id"],
    }
    extension.entered.set()
    clock = [0.0]
    monkeypatch.setattr(api.time, "monotonic", lambda: clock[0])

    def sleep(seconds):
        clock[0] += seconds
        if arrives and clock[0] >= 4:
            extension.event_file.write_text(json.dumps(retirement["fetch_failures"][0]) + "\n")

    monkeypatch.setattr(api.time, "sleep", sleep)
    monkeypatch.setattr(extension, "native_events", lambda: [retirement["ui_retired"]])
    capture = {"ordinary_actions": {}}
    if arrives:
        extension.after_retirement(capture)
        assert (
            capture["ordinary_actions"]["retirement"]["fetch_failures"]
            == retirement["fetch_failures"]
        )
        assert 4 <= clock[0] < 10
    else:
        with pytest.raises(ValueError, match="^ordinary_abort_unobserved$"):
            extension.after_retirement(capture)
        assert 10 <= clock[0] < 10.1
        assert extension.failure_diagnostics() == {
            "ordinary_failure_step": "retirement_abort",
            "ordinary_error_class": "value_error",
        }


@pytest.mark.parametrize("stage", ["retirement_prepared", "retirement_attempt"])
@pytest.mark.parametrize("arrives", [True, False])
def test_retirement_preparation_waits_for_original_host_events(
    tmp_path,
    monkeypatch,
    stage,
    arrives,
):
    """RM16/RM39 AC30: each wait accepts late original evidence and fails without it."""
    import json

    api = module()
    extension = extension_fixture(api, tmp_path)
    extension.event_file = tmp_path / "events.jsonl"
    extension.event_file.write_text("")
    clock = [0.0]
    monkeypatch.setattr(api.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(api.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))

    class Page:
        def evaluate(self, expression, params):
            return "guard" if "rmqualification.person" in expression else "action"

    extension.ui_page = Page()
    monkeypatch.setattr(
        extension,
        "native_events",
        lambda: (
            [{"requestId": "guard", "phase": "prepared"}]
            if stage != "retirement_prepared" or (arrives and clock[0] >= 4)
            else []
        ),
    )

    class Entered:
        def wait(self, timeout):
            if stage == "retirement_attempt":
                clock[0] += min(4, timeout) if arrives else timeout
                if not arrives or timeout < 4:
                    return False
            extension.event_file.write_text(
                json.dumps(
                    {
                        "kind": "http_attempt",
                        "path": "/relay/v1/feedback",
                        "attempt_id": "original",
                    }
                )
                + "\n"
            )
            return True

    extension.entered = Entered()
    if arrives:
        extension.before_retirement(Page(), {})
        assert extension.held_attempt["attempt_id"] == "original"
        assert 4 <= clock[0] < 10
    else:
        expected = (
            "ordinary_abort_unobserved"
            if stage == "retirement_prepared"
            else "composed_hold_not_observed"
        )
        with pytest.raises(ValueError, match="^" + expected + "$"):
            extension.before_retirement(Page(), {})
        assert 10 <= clock[0] < 10.1
        assert extension.failure_diagnostics() == {
            "ordinary_failure_step": stage,
            "ordinary_error_class": "value_error",
        }


@pytest.mark.parametrize(
    "stage",
    [
        "save_library_skill",
        "entry_read",
        "ui_actions",
        "select_library_skill",
        "negative_actions",
        "cleanup_deadline",
    ],
)
def test_persons_ready_attributes_failures_at_the_actual_browser_boundary(
    tmp_path, monkeypatch, stage
):
    """RM12/RM16/RM39 AC30: real extension hook wiring retains the failing operation."""
    import json

    from playwright.sync_api import Error

    api = module()
    extension = extension_fixture(api, tmp_path)
    capture = fixture(api)
    person = next(
        e["payload"]
        for e in capture["events"]
        if e["id"] == capture["accounts"][0]["rpc_evidence_id"]
    )
    person["workspaceDir"] = str(tmp_path)
    extension.config = {"recommendations": {}}
    extension.host_config = {"plugins": {"allow": [], "entries": {}}}
    extension.host_config_path = tmp_path / "host.json"
    extension.installation = {}
    extension.origin = "https://fixture.test"
    extension.event_file = tmp_path / "events.jsonl"
    extension.event_file.write_text(
        "\n".join(
            json.dumps(e)
            for e in [
                {"kind": "http_attempt", "path": "/relay/v1/feedback", "attempt_id": "entry"},
                {"kind": "http", "attempt_id": "entry", "status": 200},
            ]
        )
        + "\n"
    )
    monkeypatch.setattr(
        extension,
        "observe_backend",
        lambda: {
            "views": [{"state": "chosen", "recommendation_id": "r"}],
        },
    )

    def fail():
        raise Error("private URL and credential")

    class Page:
        @property
        def context(self):
            return self

        def new_page(self):
            return self

        def goto(self, *args, **kwargs):
            assert kwargs["timeout"] == 10000
            if stage in {"ui_actions", "cleanup_deadline"}:
                fail()

        def evaluate(self, *args):
            return True

        def wait_for_function(self, *args, **kwargs):
            return True

        def close(self):
            if stage == "cleanup_deadline":
                raise TimeoutError("overall_deadline_exceeded")
            fail()  # Cleanup must not replace the original failure's class/step.

        def get_by_role(self, *args, **kwargs):
            if stage == "select_library_skill":
                fail()
            return self

        def get_by_text(self, *args, **kwargs):
            return self

        def click(self):
            pass

        def select_option(self, **kwargs):
            pass

        def wait_for(self, **kwargs):
            pass

    def rpc(page, method, params):
        if method == "skills.library.save":
            if stage == "save_library_skill":
                fail()
            payload = {"skillId": SKILL_ID, "revision": REVISION}
        elif method == "relaymind.recommendations":
            if stage == "entry_read" or (
                stage == "negative_actions" and params["idempotencyKey"] != "ordinary-entry-read"
            ):
                fail()
            payload = {}
        elif method == "rmqualification.person":
            payload = person
        else:
            payload = {}
        return {"frame": {"ok": True, "payload": payload}}

    monkeypatch.setattr(api, "run_ui", lambda *args: {})
    sessions = [{"sessionKey": "agent:main:person-a"}, {"sessionKey": "agent:main:person-b"}]
    expected = (
        "overall_deadline_exceeded" if stage == "cleanup_deadline" else "ordinary_playwright_error"
    )
    with pytest.raises((ValueError, TimeoutError), match="^" + expected + "$"):
        extension.persons_ready(rpc, [Page(), Page()], sessions, capture)
    assert extension.failure_diagnostics() == {
        "ordinary_failure_step": "ui_actions" if stage == "cleanup_deadline" else stage,
        "ordinary_error_class": "other" if stage == "cleanup_deadline" else "playwright_error",
        **({"ordinary_ui_action": "open"} if stage in {"ui_actions", "cleanup_deadline"} else {}),
    }


@pytest.mark.parametrize("observe", [False, True])
@pytest.mark.parametrize("outcome", ["not_loaded", "refused", "loaded_other_bytes"])
@pytest.mark.parametrize("wrapped", [False, True])
def test_observation_outcome_does_not_change_ordinary_exit(observe, outcome, wrapped):
    """RM39/AC30 F2: opt-in observations never change ordinary qualification."""
    api = module()
    capture = loaded_fixture(api)
    if wrapped:
        for item in capture["ordinary_skill_loaded_use"]["cases"]:
            for attempt in item["requests"]:
                attempt["body"]["messages"][1]["content"] = native_wrapped_task(item["task"])
    case = capture["ordinary_skill_loaded_use"]["cases"][0]
    case["observed"] = outcome
    if outcome == "loaded_other_bytes":
        case["tools"][0]["event"]["result"]["content"][0]["text"] = "other bytes"
        case["requests"][1]["body"]["messages"][-1]["content"] = "other bytes"
    else:
        case["tools"] = []
        case["requests"] = case["requests"][:1]
        case["provider_attempts"] = case["provider_attempts"][:1]
        if outcome == "refused":
            case["terminal"]["state"] = "error"
    assert api.finish_capture(capture, 0, observe=observe) == 0
    assert ("ordinary_skill_loaded_use_observation" in capture["checks"]) is observe
    assert api.finish_capture(capture, 2, observe=observe) == 2


def test_observation_is_opt_in_cli_and_separate_ci_step():
    """RM39/AC30 F2: ordinary CI retains its criterion; observation has its own artifact."""
    root = Path(__file__).resolve().parents[1]
    source = Path(module().__file__).read_text()
    assert 'parser.add_argument("--observe-loaded-use", action="store_true")' in source
    ci = (root / ".github/workflows/ci.yml").read_text()
    assert "--observe-loaded-use" in ci
    assert "openclaw-loaded-use-observation-ubuntu" in ci
    assert "continue-on-error" not in ci
    # The observation and its upload are the final steps of the existing job.
    observation = ci.index("      - name: Native loaded skill observation")
    assert observation > ci.index("      - name: Transport, plugin, and artifact tiers")
    assert observation > ci.index("      - name: Pinned host lifecycle")
    assert observation > ci.index("          name: openclaw-plugin-${{ matrix.os }}")
    assert ci[observation:].count("      - ") == 2
    assert "timeout-minutes: 6" in ci[observation:]


@pytest.mark.parametrize("tail", [b'{"kind":', b'{"kind":"late"}'])
def test_event_reader_rereads_only_unterminated_tail(tmp_path, monkeypatch, tail):
    """RM39/AC30 F1: one bounded reread completes a concurrent append."""
    api = module()
    path = tmp_path / "events"
    path.write_bytes(b'{"kind":"first"}\n' + tail)
    waits = []

    def wait(seconds):
        waits.append(seconds)
        path.write_bytes(b'{"kind":"first"}\n{"kind":"late"}\n')

    monkeypatch.setattr(api.time, "sleep", wait)
    assert api.read_observer_events(path) == [dict(kind="first"), dict(kind="late")]
    assert waits == [0.05]


@pytest.mark.parametrize("data", [b"bad\n", b"{}\nbad\n", b"bad\n{", b"\xff\n", b"[]\n"])
def test_event_reader_refuses_every_malformed_complete_record(tmp_path, data):
    """RM39/AC30 F1: malformed committed records cannot be skipped."""
    api = module()
    path = tmp_path / "events"
    path.write_bytes(data)
    with pytest.raises(ValueError, match="^ordinary_skill_native_event_invalid$"):
        api.read_observer_events(path)


def test_event_reader_never_retries_an_unfinished_tail_more_than_once(tmp_path, monkeypatch):
    """RM39/AC30 F1: a still-torn tail is ignored for this poll only."""
    api = module()
    path = tmp_path / "events"
    path.write_bytes(b'{}\n{"kind":')
    waits = []
    monkeypatch.setattr(api.time, "sleep", waits.append)
    assert api.read_observer_events(path) == [{}]
    assert waits == [0.05]


def test_other_advertised_revision_is_a_correlated_control_observation():
    """RM39/AC30 F3: a changed head is observed without claiming rejection."""
    api = module()
    capture = loaded_fixture(api)
    case = capture["ordinary_skill_loaded_use"]["cases"][1]
    case["advertised_revision"] = "b" * 64
    for attempt in case["requests"]:
        attempt["body"]["messages"][0]["content"] = attempt["body"]["messages"][0][
            "content"
        ].replace(REVISION, "b" * 64)
    case["tools"][0]["event"]["result"]["content"][0]["text"] = "new head"
    case["requests"][1]["body"]["messages"][-1]["content"] = "new head"
    case["observed"] = "loaded_other_bytes"
    assert api.ordinary_skill_loaded_use_pass(capture)
    case["advertised_revision"] = "invalid"
    assert not api.ordinary_skill_loaded_use_pass(capture)


def test_swapped_gateway_run_ids_fail_provider_bijection():
    """RM39/AC30 F4: independently recorded response IDs bind each HTTP attempt."""
    api = module()
    capture = loaded_fixture(api)
    first, second = [c["requests"][0] for c in capture["ordinary_skill_loaded_use"]["cases"][:2]]
    first["run_id"], second["run_id"] = second["run_id"], first["run_id"]
    assert not api.ordinary_skill_loaded_use_pass(capture)


def test_forged_advertised_name_cannot_replace_native_pin_name():
    """RM39/AC30 F5: consistent advertisement forgery cannot alter native evidence."""
    api = module()
    capture = loaded_fixture(api)
    case = capture["ordinary_skill_loaded_use"]["cases"][0]
    case["selected"]["name"] = "forged"
    case["tools"][0]["event"]["params"]["name"] = "forged"
    for attempt in case["requests"]:
        for message in attempt["body"]["messages"]:
            if message["role"] == "system":
                message["content"] = message["content"].replace("Hello World", "forged")
            for call in message.get("tool_calls", []):
                call["function"]["arguments"] = '{"name":"forged"}'
    assert not api.ordinary_skill_loaded_use_pass(capture)


@pytest.mark.parametrize("field", ["subscription", "chat", "terminal"])
def test_turn_socket_provenance_cannot_be_swapped(field):
    """RM39/AC30 F7: subscribe, send, and terminal share the native socket."""
    api = module()
    capture = loaded_fixture(api)
    capture["ordinary_skill_loaded_use"]["cases"][0][field]["socket_id"] = "foreign"
    assert not api.ordinary_skill_loaded_use_pass(capture)


def test_observation_failure_reasons_are_closed(tmp_path):
    """RM39/AC30 F10: harness failures retain useful fixed safe reasons."""
    api = module()
    assert {
        "ordinary_skill_generation_bound",
        "ordinary_skill_task_mismatch",
        "ordinary_skill_native_event_invalid",
    } <= extension_fixture(api, tmp_path).failure_reasons()


def turn_fixture(api, tmp_path, monkeypatch, *, deliver):
    """Drive the real collector around a terminal and asynchronous native hook."""
    import copy
    import json
    from types import SimpleNamespace

    extension = extension_fixture(api, tmp_path)
    extension.event_file = tmp_path / "events.jsonl"
    extension.event_file.write_text("")
    extension.provider_attempt_file = tmp_path / "attempts.jsonl"
    extension.provider_attempt_file.write_text("")
    case = loaded_fixture(api)["ordinary_skill_loaded_use"]["cases"][0]
    expected = copy.deepcopy(case)
    observation = api.LoadedUseGatewayObservation()
    extension.observation = observation
    clock = [0.0]
    monkeypatch.setattr(api.time, "monotonic", lambda: clock[0])

    def rpc(page, method, params):
        if method == "sessions.messages.subscribe":
            return dict(
                socket_id="socket",
                frame=dict(ok=True, payload=dict(subscribed=True, key=params["key"])),
            )
        if method == "rmqualification.person":
            return dict(frame=dict(ok=True, payload=case["current"]))
        host_id = params["idempotencyKey"]
        expected["terminal"]["runId"] = host_id
        expected["tools"][0]["event"]["runId"] = host_id
        expected["tools"][0]["context"]["runId"] = host_id
        observation.requests.extend(expected["requests"])
        extension.provider_attempt_file.write_text(
            "".join(json.dumps(a) + "\n" for a in expected["provider_attempts"])
        )
        foreign = copy.deepcopy(expected["tools"][0])
        foreign["context"]["runId"] = "previous-case"
        events = [dict(kind="native_skill_tool", **foreign)]
        if deliver:
            events.append(dict(kind="native_skill_tool", **expected["tools"][0]))
        extension.event_file.write_text("".join(json.dumps(e) + "\n" for e in events))
        return dict(socket_id="socket", frame=dict(ok=True, payload=dict(runId=host_id)))

    def wait(ms):
        clock[0] += ms / 1000

    page = SimpleNamespace(evaluate=lambda *a: [expected["terminal"]], wait_for_timeout=wait)
    return extension, case, rpc, page, expected


def test_missing_async_hook_stays_incomplete_and_preserves_requests(tmp_path, monkeypatch):
    """RM39/AC30 F6: a missing hook is not a truthful not_loaded control."""
    api = module()
    extension, case, rpc, page, _ = turn_fixture(api, tmp_path, monkeypatch, deliver=False)
    with pytest.raises(ValueError, match="^ordinary_skill_turn_timeout$"):
        extension.loaded_use_turn(rpc, page, {}, case)
    assert case["observed"] == "incomplete"
    assert len(case["requests"]) == 2
    assert case["tools"] == []


def test_late_foreign_hook_does_not_poison_next_case(tmp_path, monkeypatch):
    """RM39/AC30 F6/F7: collect by host run and retain socket receipts."""
    api = module()
    extension, case, rpc, page, expected = turn_fixture(api, tmp_path, monkeypatch, deliver=True)
    extension.loaded_use_turn(rpc, page, {}, case)
    assert case["tools"] == expected["tools"]
    assert case["subscription"] == expected["subscription"]
    assert case["provider_attempts"] == expected["provider_attempts"]
    assert case["observed"] == "loaded_pinned_revision"


def test_one_turn_request_overflow_does_not_starve_later_controls(tmp_path):
    """RM39/AC30 F9: each turn gets a fresh bounded HTTP window."""
    from api_helpers import call
    from recommendation_helpers import setup

    api = module()
    _, service, runs, _ = setup(tmp_path)
    app, gateway, observation = api.loaded_use_gateway(tmp_path / "inference", service)
    try:
        for case in loaded_fixture(api)["ordinary_skill_loaded_use"]["cases"]:
            observation.begin(case["selected"], case["task"])
            for i in range(5):
                response = call(
                    app,
                    "POST",
                    "/v1/chat/completions",
                    json_body=case["requests"][0]["body"],
                    headers={"Idempotency-Key": case["case"]},
                    raise_app_exceptions=False,
                )
                assert response.status_code == (429 if i == 4 else 200)
            assert observation.requests[-1]["status"] == 429
    finally:
        gateway.store.close()
        runs.close()


def test_scripted_model_does_not_write_native_selected_evidence():
    """RM39/AC30 F5: the model may propose an advertised name, never rewrite the pin."""
    import copy
    from types import SimpleNamespace as NS

    api = module()
    case = loaded_fixture(api)["ordinary_skill_loaded_use"]["cases"][0]
    before = copy.deepcopy(case["selected"])
    observation = api.LoadedUseGatewayObservation()
    observation.begin(case["selected"], case["task"])
    messages = [
        NS(role=NS(value=m["role"]), content=m["content"].replace("Hello World", "advertised"))
        for m in case["requests"][0]["body"]["messages"]
    ]
    observation.generate(NS(messages=messages, tool_schemas=[NS(name="skills_read")]))
    assert case["selected"] == before


def test_observation_fixture_error_is_not_silenced():
    """RM39/AC30 F2: separate CI fails a broken harness, never a truthful outcome."""
    api = module()
    capture = loaded_fixture(api)
    capture["ordinary_skill_loaded_use"]["cases"][1]["tools"] = []
    assert api.finish_capture(capture, 0, observe=True) == 2


def test_native_content_retention_is_bounded():
    """RM39/AC30 F11: retain at most the bounded native SKILL.md text."""
    api = module()
    assert api.bounded_skill_content("中文") == "中文"
    with pytest.raises(ValueError, match="^ordinary_skill_read_failed$"):
        api.bounded_skill_content("中" * (256 * 1024))


@pytest.mark.parametrize("pins", [[], [dict(skillId=SKILL_ID, revision="b" * 64, name="other")]])
def test_missing_native_pin_has_a_fixed_control_failure(pins):
    """RM39/AC30 F10: absent pinned revision never leaks StopIteration."""
    api = module()
    with pytest.raises(ValueError, match="^ordinary_skill_control_failed$"):
        api.selected_pin(
            dict(frame=dict(ok=True, payload=dict(session=dict(selections=pins)))),
            dict(skillId=SKILL_ID, revision=REVISION),
        )


@pytest.mark.parametrize("failure", ["ordinary_skill_turn_timeout", "not_text_content"])
def test_observation_continues_controls_after_a_turn_fixture_failure(
    tmp_path, monkeypatch, failure
):
    """RM39/AC30 F6/F9: one incomplete turn retains evidence without starving later cases."""
    api = module()
    capture = loaded_fixture(api)
    expected = capture["ordinary_skill_loaded_use"]["cases"]
    extension = extension_fixture(api, tmp_path)
    current = [0]
    visited = []

    def rpc(page, method, params):
        if method == "sessions.create":
            current[0] += 1
        case = expected[current[0]]
        selected = case["selected"]
        if method == "sessions.create":
            scope = case["before"]
            return dict(
                id="create",
                socket_id="socket",
                frame=dict(
                    id="create",
                    ok=True,
                    payload=dict(
                        key=scope["sessionKey"],
                        sessionId=scope["sessionId"],
                        entry=dict(lifecycleRevision=scope["lifecycleRevision"]),
                    ),
                ),
            )
        if method == "rmqualification.person":
            scope = (
                case["current"]
                if "expectedLifecycleRevision" in params
                and params["expectedLifecycleRevision"] == "L1"
                else case["before"]
            )
            return dict(frame=dict(ok=True, payload=scope))
        if method == "skills.library.read":
            return dict(frame=selected["read"])
        if method == "skills.library.list":
            return dict(
                frame=dict(
                    ok=True,
                    payload=dict(
                        session=dict(
                            selections=[
                                dict(
                                    skillId=selected["skillId"],
                                    revision=REVISION,
                                    name=selected["name"],
                                )
                            ]
                        )
                    ),
                )
            )
        if method == "skills.library.activate":
            return dict(frame=case["selection"])
        return dict(frame=case["intervention"]["frame"])

    def save(*args):
        case = expected[current[0]]
        case["selected"]["read"]["payload"]["entry"]["slug"] = "fixture"
        return dict(
            mapping=dict(skillId=case["selected"]["skillId"], revision=REVISION),
            content_sha256=case["selected"]["content_sha256"],
        )

    monkeypatch.setattr(extension, "save_library_skill", save)

    def turn(rpc, page, session, case):
        visited.append(case["case"])
        case["requests"] = [dict(status=429)]
        if case["case"] == "positive":
            if failure == "not_text_content":
                malformed = expected[0]
                malformed["tools"][0]["event"]["result"]["content"] = [{"type": "image"}]
                api.loaded_use_observation(malformed)
            raise ValueError(failure)

    monkeypatch.setattr(extension, "loaded_use_turn", turn)
    extension.observe_loaded_use(rpc, None, {}, capture)
    assert visited == ["positive", "stale", "deleted", "reset"]
    partial = capture["ordinary_skill_loaded_use"]["cases"][0]
    assert partial["observed"] == "incomplete"
    assert partial["failure_reason"] == failure
    assert partial["requests"] == [dict(status=429)]


def test_task_substring_does_not_bind_a_different_native_user_message():
    """RM39/AC30: a quoted or embedded task is not the dispatched task identity."""
    api = module()
    capture = loaded_fixture(api)
    case = capture["ordinary_skill_loaded_use"]["cases"][0]
    case["requests"][0]["body"]["messages"][1]["content"] = "Quoted task: " + case["task"]
    assert not api.ordinary_skill_loaded_use_pass(capture)


def test_backend_observation_times_out_while_a_real_transaction_owns_the_lock(
    tmp_path, monkeypatch
):
    """RM16/RM39 AC30: a blocked read fails closed without waiting for the gateway."""
    import time

    from recommendation_helpers import setup

    api = module()
    extension = extension_fixture(api, tmp_path)
    _, _, runs, _ = setup(tmp_path)
    extension.runs = runs
    monkeypatch.setattr(api, "ORDINARY_OBSERVE_SECONDS", 0.05, raising=False)
    entered, release = threading.Event(), threading.Event()

    def hold():
        with runs.sql_transaction():
            entered.set()
            release.wait(0.5)  # Also bounds the pre-fix regression test.

    holder = threading.Thread(target=hold)
    holder.start()
    assert entered.wait(1)
    started = time.monotonic()
    try:
        with pytest.raises(ValueError, match="^ordinary_observe_timeout$"):
            with extension.step("ui_actions"):
                extension.observe_backend()
        assert time.monotonic() - started < 0.4
        assert "ordinary_observe_timeout" in extension.failure_reasons()
        assert extension.failure_diagnostics() == {
            "ordinary_failure_step": "ui_actions",
            "ordinary_error_class": "value_error",
        }
    finally:
        release.set()
        holder.join(1)
        runs.close()


class OrdinaryUiPage:
    """Only the external browser boundary is doubled; run_ui owns sequencing."""

    def __init__(self, *, fail=None):
        self.target = None
        self.fail = fail
        self.actions = []
        self.dialog_handler = None
        self.dialog_accepted = False
        self.snapshot = dict(views=[], records=[], audits=[], events=[])

    def get_by_role(self, role, *, name, exact):
        assert exact is True
        self.target = name
        return self

    def get_by_label(self, name, *, exact):
        assert exact is True
        self.target = name
        return self

    def action(self, timeout, kind):
        assert timeout == 10000
        self.actions.append(self.target)
        if self.target == self.fail or (self.target, kind) == self.fail:
            from playwright.sync_api import TimeoutError as PlaywrightTimeout

            raise PlaywrightTimeout("private URL and credential")

    def click(self, *, timeout=None):
        self.action(timeout, "click")
        if self.target == "Recommend":
            self.snapshot["views"] = [{"state": "ready"}]
        elif self.target == "Choose 1":
            self.snapshot["views"] = [{"state": "chosen"}]
        elif self.target == "Correct to 1":
            self.snapshot["records"] = [{"feedback": {"kind": "correction"}}]
        elif self.target == "Delete feedback":
            self.snapshot["records"] = []
            self.snapshot["audits"] = [{"operation": "delete"}]
        elif self.target == "Reset feedback":
            self.dialog_handler(self)
            assert self.dialog_accepted
            self.snapshot["audits"].append({"operation": "reset"})

    def fill(self, text, *, timeout=None):
        self.action(timeout, "fill")

    def wait_for(self, *, timeout=None):
        self.action(timeout, "wait")

    def once(self, event, callback):
        assert event == "dialog"
        self.dialog_handler = callback

    def accept(self):
        self.dialog_accepted = True


def test_ui_actions_have_explicit_timeouts_and_preserve_reset_confirmation():
    """RM16/RM37/RM39 AC28/AC30: each DOM action is bounded, reset stays confirmed."""
    import copy

    api = module()
    page = OrdinaryUiPage()
    result = api.run_ui(page, {}, lambda: copy.deepcopy(page.snapshot))
    assert list(result["snapshots"]) == [
        "recommend",
        "choice",
        "correction",
        "inspect",
        "delete",
        "reset",
    ]
    assert page.actions == [
        "RelayMind recommendations",
        "Task",
        "Recommend",
        "Choose 1",
        "Correct to 1",
        "Inspect feedback",
        "Delete feedback",
        "Delete feedback",
        "Reset feedback",
    ]
    assert page.dialog_accepted


@pytest.mark.parametrize(
    ("target", "action"),
    [
        ("RelayMind recommendations", "open"),
        ("Task", "open"),
        ("Recommend", "recommend"),
        ("Choose 1", "choose"),
        ("Correct to 1", "correct"),
        ("Inspect feedback", "inspect"),
        (("Delete feedback", "wait"), "inspect"),
        (("Delete feedback", "click"), "delete"),
        ("Reset feedback", "reset"),
    ],
)
def test_ui_failure_records_the_current_closed_action(tmp_path, target, action):
    """RM12/RM16/RM39 AC30: browser failures retain the action, never browser text."""
    api = module()
    extension = extension_fixture(api, tmp_path)
    page = OrdinaryUiPage(fail=target)
    with pytest.raises(ValueError, match="^ordinary_playwright_timeout$"):
        with extension.step("ui_actions"):
            api.run_ui(page, {}, lambda: page.snapshot, extension.set_ui_action)
    assert extension.failure_diagnostics() == {
        "ordinary_failure_step": "ui_actions",
        "ordinary_error_class": "playwright_timeout",
        "ordinary_ui_action": action,
    }
    assert api.ORDINARY_UI_ACTIONS == {
        "open",
        "recommend",
        "choose",
        "correct",
        "inspect",
        "delete",
        "reset",
    }


def test_ui_action_refuses_private_unrecognized_values(tmp_path):
    """RM12/RM39 AC30: the diagnostic discriminator cannot carry arbitrary text."""
    api = module()
    extension = extension_fixture(api, tmp_path)
    with pytest.raises(ValueError, match="^ordinary_step_failed$"):
        extension.set_ui_action("private URL and credential")
    assert "ordinary_ui_action" not in extension.failure_diagnostics()


def test_backend_observation_returns_original_rows_and_sanitizes_worker_errors(tmp_path):
    """RM12/RM16/RM39 AC30: worker reads retain evidence and fail closed on storage errors."""
    from recommendation_helpers import setup

    api = module()
    extension = extension_fixture(api, tmp_path)
    _, _, runs, _ = setup(tmp_path)
    extension.runs = runs
    try:
        assert extension.observe_backend() == dict(views=[], records=[], audits=[], events=[])
    finally:
        runs.close()
    with pytest.raises(ValueError, match="^ordinary_step_failed$"):
        with extension.step("ui_actions"):
            extension.observe_backend()
    assert extension.failure_diagnostics()["ordinary_error_class"] == "other"


def test_real_dom_run_ui_accepts_the_native_reset_dialog():
    """RM37/RM39 AC28/AC30: DOM-only regression, not installed-host qualification."""
    from playwright.sync_api import sync_playwright

    api = module()
    actions = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            page.set_content("""
                <a href="#panel">RelayMind recommendations</a>
                <label>Task<input></label>
                <button onclick="snapshot.views = [{state:'ready'}]">Recommend</button>
                <button onclick="snapshot.views = [{state:'chosen'}]">Choose 1</button>
                <button onclick="snapshot.records = [{}]">Correct to 1</button>
                <button>Inspect feedback</button>
                <button onclick="snapshot.records = []; snapshot.audits.push({operation:'delete'})">
                    Delete feedback</button>
                <button onclick="if (confirm('Reset?')) snapshot.audits.push({operation:'reset'})">
                    Reset feedback</button>
                <script>window.snapshot = {views:[], records:[], audits:[], events:[]}</script>
            """)
            result = api.run_ui(page, {}, lambda: page.evaluate("snapshot"), actions.append)
            assert result["snapshots"]["reset"]["audits"] == [
                {"operation": "delete"},
                {"operation": "reset"},
            ]
            assert actions == [
                "open",
                "recommend",
                "choose",
                "correct",
                "inspect",
                "delete",
                "reset",
            ]
        finally:
            browser.close()


def test_flag_off_host_configuration_matches_base_harness(tmp_path):
    """RM39/AC30 N1: exact base b175f31 config, operator config, and environment."""
    api = module()
    extension = extension_fixture(api, tmp_path)
    config = dict(
        plugins=dict(allow=[], load=dict(paths=[]), entries={}),
        agents=dict(defaults=dict(workspace=str(tmp_path))),
        gateway=dict(controlUi={}),
    )
    extension.configure(config, tmp_path, "https://fixture.test")
    # Captured from the base harness before loaded-use observation was added.
    assert config == {
        "agents": {"defaults": {"workspace": str(tmp_path)}},
        "gateway": {"controlUi": {"experimental": {"customPlugins": True}}},
        "plugins": {
            "allow": ["relaymind-ordinary-observer"],
            "load": {
                "paths": [
                    str(ROOT / "integrations/openclaw/test/host-ordinary-qualification/observer")
                ]
            },
            "entries": {
                "relaymind-ordinary-observer": {
                    "enabled": True,
                    "config": {"eventFile": str(tmp_path / "action-events.jsonl")},
                }
            },
        },
    }
    assert extension.config == {
        "baseUrl": "http://127.0.0.1:12345/v1",
        "recommendations": {
            "controlUiOrigin": "https://fixture.test",
            "applicationTokenEnv": "RM_QUALIFICATION_APP_TOKEN",
            "bindings": [],
        },
    }
    assert extension.environment() == {
        "RM_QUALIFICATION_APP_TOKEN": "app",
        "RM_QUALIFICATION_ACTION_TOKEN": "action",
    }
    assert not (tmp_path / "provider-attempts.jsonl").exists()


@pytest.mark.parametrize("observe", [False, True])
def test_main_only_replaces_base_gateway_when_observation_is_requested(
    tmp_path, monkeypatch, observe
):
    """RM39/AC30 N1: flag-off serves the exact recommendation-only base app."""
    import json

    import api_helpers
    import recommendation_helpers

    api = module()
    setup = recommendation_helpers.setup
    base_apps = []

    def base_setup(*args, **kwargs):
        result = setup(*args, **kwargs)
        base_apps.append(result[0])
        return result

    monkeypatch.setattr(recommendation_helpers, "setup", base_setup)

    class Server:
        base_url = "http://127.0.0.1:12345"

        def __init__(self, app):
            assert (app is base_apps[0]) is (not observe)
            assert (
                any(getattr(route, "path", None) == "/v1/chat/completions" for route in app.routes)
                is observe
            )

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    monkeypatch.setattr(api_helpers, "RunningServer", Server)
    person = api.BASE.person_module()

    def run(work, out, *, extension):
        assert (extension.observation is not None) is observe
        out.write_text(json.dumps(loaded_fixture(api)))
        return 0

    monkeypatch.setattr(person, "run_qualification", run)
    monkeypatch.setattr(api.BASE, "person_module", lambda: person)
    archive = tmp_path / "release.tgz"
    archive.write_bytes(b"fixture")
    assert (
        api.main(
            [
                "--tarball",
                str(archive),
                "--work-dir",
                str(tmp_path / "host"),
                "--out",
                str(tmp_path / "capture.json"),
            ]
            + (["--observe-loaded-use"] if observe else [])
        )
        == 0
    )


def test_observer_source_inventory_includes_runtime_package_metadata(tmp_path):
    """RM39/AC30 N3: changing the host entry resolver must change source provenance."""
    api = module()
    extension = extension_fixture(api, tmp_path)
    observer = Path(api.__file__).parent / "observer"
    assert {p.name for p in extension.source_paths() if p.parent == observer} == {
        "probe.mjs",
        "openclaw.plugin.json",
        "package.json",
    }


@pytest.mark.parametrize("tail_length", [8, 11, 17])
def test_observer_window_resumes_from_consumed_bytes_after_torn_tail(
    tmp_path, monkeypatch, tail_length
):
    """RM39/AC30 N2: deferred tails cannot move a later window into the middle of JSON."""
    api = module()
    path = tmp_path / "events.jsonl"
    complete = '{"kind":"已读"}\n'.encode()
    pending = '{"kind":"后来"}\n'.encode()
    path.write_bytes(complete + pending[:tail_length])
    monkeypatch.setattr(api.time, "sleep", lambda _: None)
    events, offset = api.read_observer_events(path, with_offset=True)
    assert events == [{"kind": "已读"}]
    assert offset == len(complete)
    with path.open("ab") as writer:
        writer.write(pending[tail_length:] + b'{"kind":"next"}\n')
    events, end = api.read_observer_events(path, offset, with_offset=True)
    assert events == [{"kind": "后来"}, {"kind": "next"}]
    assert end == path.stat().st_size
    assert api.read_observer_events(path, end) == []


def test_retirement_window_keeps_deferred_tail_at_record_boundary(tmp_path, monkeypatch):
    """RM39/AC30 N2: the actual retirement window never starts inside a deferred record."""
    import json

    api = module()
    extension = extension_fixture(api, tmp_path)
    extension.event_file = tmp_path / "events.jsonl"
    complete = b'{"kind":"earlier"}\n'
    extension.event_file.write_bytes(complete + b'{"kind":"deferred"')
    monkeypatch.setattr(api.time, "sleep", lambda _: None)
    monkeypatch.setattr(
        extension, "native_events", lambda: [{"requestId": "guard", "phase": "prepared"}]
    )

    class Page:
        def evaluate(self, expression, params):
            return "guard" if "rmqualification.person" in expression else "action"

    class Entered:
        def wait(self, timeout):
            with extension.event_file.open("ab") as writer:
                writer.write(
                    b"}\n"
                    + json.dumps(
                        {
                            "kind": "http_attempt",
                            "path": "/relay/v1/feedback",
                            "attempt_id": "original",
                        }
                    ).encode()
                    + b"\n"
                )
            return True

    extension.ui_page = Page()
    extension.entered = Entered()
    extension.before_retirement(Page(), {})
    assert extension.retirement_start == len(complete)
    assert extension.held_attempt["attempt_id"] == "original"
