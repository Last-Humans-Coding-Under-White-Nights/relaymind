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
    )
    assert extension.archive == tmp_path / "release.tgz"
    assert extension.archive.is_absolute()


FAILURE_STEPS = {
    "configure",
    "install",
    "sources",
    "persons_ready",
    "entry_read",
    "ui_actions",
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
            with extension.step("ui_actions"):
                raise TimeoutError("overall_deadline_exceeded")
    assert extension.failure_diagnostics() == {
        "ordinary_failure_step": "ui_actions",
        "ordinary_error_class": "other",
    }


def test_entry_rpc_reports_the_actual_substep(tmp_path):
    """RM12/RM39 AC30: RPC exception translation retains the original browser class."""
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    api = module()
    extension = extension_fixture(api, tmp_path)

    class Page:
        def evaluate(self, *args):
            raise PlaywrightTimeout("private browser URL and credential")

    with pytest.raises(ValueError, match="^ordinary_playwright_timeout$"):
        with extension.step("entry_read"):
            api.BASE.person_module().native_rpc(Page(), "relaymind.recommendations", {})
    assert extension.failure_diagnostics() == {
        "ordinary_failure_step": "entry_read",
        "ordinary_error_class": "playwright_timeout",
    }


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
        "entry_read",
        "ui_actions",
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
            if stage in {"ui_actions", "cleanup_deadline"}:
                fail()

        def evaluate(self, *args):
            return True

        def close(self):
            if stage == "cleanup_deadline":
                raise TimeoutError("overall_deadline_exceeded")
            fail()  # Cleanup must not replace the original failure's class/step.

    def rpc(page, method, params):
        if method == "relaymind.recommendations":
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
    }
