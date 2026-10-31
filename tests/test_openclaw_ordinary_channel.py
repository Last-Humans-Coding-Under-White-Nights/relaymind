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
