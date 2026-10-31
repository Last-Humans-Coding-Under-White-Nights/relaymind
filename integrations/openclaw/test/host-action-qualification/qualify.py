"""RM37/RM39 AC28/AC30: test-only real person→production factory→real gateway composition.

This does not add a production qualification flag or enable the shipped registry.
"""

import asyncio
import hashlib
import importlib.util
import json
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests"), str(ROOT / "scripts")]
CHECKS = (
    "choice",
    "replay",
    "correction",
    "inspect",
    "delete",
    "reset",
    "foreign_denied",
    "unregistered_denied",
    "revocation_denied",
)


def person_module():
    spec = importlib.util.spec_from_file_location(
        "native_person_harness", Path(__file__).parents[1] / "host-person-qualification/qualify.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def composed_pass(capture):
    """Correlate raw native receipts; complete check flags alone are never proof."""
    try:
        value = capture["composed_actions"]
        if not person_module().person_composition_pass(capture):
            return False
        if (
            value["production_registry_enabled"] is not False
            or value["qualification_constructor"] != "explicit-test-only"
        ):
            return False
        revision = value["implementation_revision"]
        if (
            not isinstance(revision, str)
            or len(revision) != 64
            or any(c not in "0123456789abcdef" for c in revision)
        ):
            return False
        if set(value["checks"]) != set(CHECKS) or not all(
            value["checks"][name] is True for name in CHECKS
        ):
            return False
        account = capture["accounts"][0]
        raw = {
            event["request_id"]: event
            for event in capture["events"]
            if event.get("kind") == "native_rpc"
        }
        receipts = value["receipts"]
        names = ("recommend", "choice", "replay", "correction", "inspect", "delete", "reset")
        ids = [receipts[name]["request_id"] for name in names]
        if len(set(ids)) != len(ids):
            return False
        for name in names:
            receipt = receipts[name]
            rpc = raw[receipt["request_id"]]
            dispatch = [
                event
                for event in value["events"]
                if event["kind"] == "dispatch" and event["requestId"] == receipt["request_id"]
            ]
            if len(dispatch) != 1:
                return False
            event = dispatch[0]
            if (
                rpc["ok"] is not True
                or rpc["method"] != "relaymind.recommendations"
                or rpc["socket_id"] != account["socket_id"]
                or receipt["socket_id"] != account["socket_id"]
            ):
                return False
            if (
                event["profileId"],
                event["connectionId"],
                event["sessionId"],
                event["implementation_revision"],
                event["authenticatedControlUi"],
            ) != (
                account["profile_id"],
                account["connection_id"],
                account["native_session_id"],
                revision,
                True,
            ):
                return False
        chosen = receipts["choice"]["receipt"]
        if (
            chosen != receipts["replay"]["receipt"]
            or chosen["state"] != "chosen"
            or chosen["selected_action"]["scope"] != {"user_id": "u", "workspace_id": "w"}
        ):
            return False
        correction = receipts["correction"]["receipt"]
        inspected = [
            row
            for row in receipts["inspect"]["receipt"]["records"]
            if row["feedback_id"] == correction["feedback_id"]
        ]
        if (
            set(correction) != {"schema_version", "feedback_id", "feedback_revision"}
            or correction["schema_version"] != 1
            or len(inspected) != 1
            or inspected[0]["feedback"]["kind"] != "correction"
        ):
            return False
        if (
            receipts["delete"]["receipt"]["deleted"] is not True
            or str(receipts["reset"]["receipt"]["feedback_revision"]) != "3"
        ):
            return False
        for name in ("foreign", "unregistered"):
            receipt = receipts[name]
            rpc = raw[receipt["request_id"]]
            if (
                rpc["ok"] is not False
                or rpc["socket_id"] != capture["accounts"][1]["socket_id"]
                or rpc["error_code"] not in {"FORBIDDEN", "INVALID_REQUEST", "NOT_FOUND"}
            ):
                return False
        if any(
            event["kind"] in {"http_attempt", "http"} and event["path"].startswith("/relay/v1/")
            for event in value["negative_events"]
        ):
            return False
        retirement = value["retirement"]
        if [event["kind"] for event in retirement] != [
            "dispatch",
            "connection_retired",
            "result",
        ] or len({event["requestId"] for event in retirement}) != 1:
            return False
        if (
            retirement[1]["connectionId"] != account["connection_id"]
            or retirement[1]["aborted"] is not True
            or retirement[2]["ok"] is not False
            or retirement[2]["reason"] != "cancelled"
        ):
            return False
        return True
    except (KeyError, TypeError, ValueError, IndexError):
        return False


def private_json(path, value):
    path.write_bytes((json.dumps(value, ensure_ascii=True) + "\n").encode("utf-8"))
    path.chmod(0o600)


class ActionComposition:
    """Operator-only fixture bootstrap; mappings derive from real host receipts."""

    def __init__(self, base_url, application_token, action_token, hold, entered, release):
        self.base_url, self.application_token, self.action_token = (
            base_url,
            application_token,
            action_token,
        )
        self.hold, self.entered, self.release = hold, entered, release

    def configure(self, config, work, origin):
        self.work, self.origin = work, origin
        self.operator_file = work / "action-operator.json"
        self.event_file = work / "action-events.jsonl"
        self.event_file.write_bytes(b"")
        self.event_file.chmod(0o600)
        self.config = dict(
            baseUrl=self.base_url,
            recommendations=dict(
                controlUiOrigin=origin,
                applicationTokenEnv="RM_QUALIFICATION_APP_TOKEN",
                bindings=[],
            ),
        )
        # Empty mapping is intentionally unavailable until actual native receipts exist.
        private_json(self.operator_file, self.config)
        plugin = Path(__file__).resolve().parent
        config["plugins"]["allow"].append("relaymind-action-qualification")
        config["plugins"]["load"]["paths"].append(str(plugin))
        config["plugins"]["entries"]["relaymind-action-qualification"] = dict(
            enabled=True,
            config=dict(operatorFile=str(self.operator_file), eventFile=str(self.event_file)),
        )
        self.workspace = config["agents"]["defaults"]["workspace"]

    def failure_reasons(self):
        return {
            "composed_action_response_failed",
            "composed_empty_catalog",
            "composed_native_binding_failed",
            "composed_checks_failed",
            "composed_hold_not_observed",
            "composed_revocation_failed",
            "composed_revocation_not_observed",
        }

    def source_paths(self):
        return sorted(
            path
            for path in Path(__file__).parent.iterdir()
            if path.suffix in {".py", ".mjs", ".json"}
        )

    def environment(self):
        return {
            "RM_QUALIFICATION_APP_TOKEN": self.application_token,
            "RM_QUALIFICATION_ACTION_TOKEN": self.action_token,
        }

    def secrets(self):
        return [self.application_token, self.action_token]

    def persons_ready(self, rpc, pages, sessions, capture):
        account = capture["accounts"][0]
        native = next(
            event["payload"]
            for event in capture["events"]
            if event["id"] == account["rpc_evidence_id"]
        )
        self.workspace = native["workspaceDir"]
        self.config["recommendations"]["bindings"] = [
            dict(
                profileId=account["profile_id"],
                authenticatedUserId=account["native_person_email"],
                agentId="main",
                workspaceDir=self.workspace,
                userId="u",
                workspaceId="w",
                actionTokenEnv="RM_QUALIFICATION_ACTION_TOKEN",
            )
        ]
        private_json(self.operator_file, self.config)
        results, checks = {}, {}
        capture["composed_actions"] = {"checks": checks, "receipts": results}

        def call(input, key, index=0, target=None, expect=True):
            observed = rpc(
                pages[index],
                "relaymind.recommendations",
                dict(
                    sessions[index] if target is None else target,
                    input=input,
                    idempotencyKey="native-composed-" + key,
                ),
            )
            frame = observed["frame"]
            if frame.get("ok") is not expect:
                raise ValueError("composed_action_response_failed")
            if not expect:
                results[key] = dict(request_id=observed["id"], socket_id=observed["socket_id"])
                return {"ok": False, "error_code": (frame.get("error") or {}).get("code")}
            value = json.loads(frame["payload"]["receipt_json"])
            results[key] = dict(
                request_id=observed["id"], socket_id=observed["socket_id"], receipt=value
            )
            return value

        view = call(
            dict(
                kind="recommend",
                request=dict(
                    schema_version=1,
                    task_text="hello greeting",
                    requested_kinds=["skill"],
                    selection_scope="discovery",
                ),
            ),
            "recommend",
        )
        if not view["displayed_candidates"]:
            raise ValueError("composed_empty_catalog")
        asset = view["displayed_candidates"][0]["asset"]
        rid = view["recommendation_id"]
        choice = dict(
            kind="choose",
            recommendationId=rid,
            request=dict(
                schema_version=1,
                expected_version=str(view["state_version"]),
                choice=dict(
                    kind="asset", asset_id=asset["asset_id"], content_hash=asset["content_hash"]
                ),
            ),
        )
        chosen = call(choice, "choice")
        checks["choice"] = chosen["state"] == "chosen" and chosen["selected_action"]["scope"] == {
            "user_id": "u",
            "workspace_id": "w",
        }
        first_choice = dict(results["choice"])
        replay = call(choice, "choice")
        results["replay"] = dict(results["choice"])
        results["choice"] = first_choice
        checks["replay"] = replay == chosen
        correction = call(
            dict(
                kind="feedback",
                recommendationId=rid,
                request=dict(
                    schema_version=1,
                    event_id="native-composed-correction",
                    expected_feedback_revision="0",
                    feedback=dict(
                        kind="correction",
                        asset_id=asset["asset_id"],
                        content_hash=asset["content_hash"],
                    ),
                ),
            ),
            "correction",
        )
        inspected = call(dict(kind="listFeedback", page=dict(limit=10)), "inspect")
        matching = [
            item
            for item in inspected["records"]
            if item["feedback_id"] == correction["feedback_id"]
        ]
        checks["inspect"] = len(matching) == 1
        checks["correction"] = (
            len(matching) == 1 and matching[0]["feedback"]["kind"] == "correction"
        )
        deleted = call(
            dict(
                kind="deleteFeedback",
                feedbackId=correction["feedback_id"],
                expectedFeedbackRevision=str(correction["feedback_revision"]),
            ),
            "delete",
        )
        checks["delete"] = deleted["deleted"] is True
        reset = call(
            dict(kind="resetFeedback", expectedFeedbackRevision=str(deleted["feedback_revision"])),
            "reset",
        )
        checks["reset"] = str(reset["feedback_revision"]) == "3"
        before = len(self.event_file.read_bytes())
        foreign = call(
            dict(kind="listFeedback", page=dict(limit=1)),
            "foreign",
            index=1,
            target=sessions[0],
            expect=False,
        )
        unregistered = call(
            dict(kind="listFeedback", page=dict(limit=1)), "unregistered", index=1, expect=False
        )
        new_events = [
            json.loads(line) for line in self.event_file.read_bytes()[before:].splitlines()
        ]
        no_http = not any(
            event["kind"] in {"http_attempt", "http"} and event["path"].startswith("/relay/v1/")
            for event in new_events
        )
        checks["foreign_denied"] = (
            foreign["error_code"] in {"FORBIDDEN", "INVALID_REQUEST", "NOT_FOUND"} and no_http
        )
        checks["unregistered_denied"] = unregistered["error_code"] == "INVALID_REQUEST" and no_http
        events = [json.loads(line) for line in self.event_file.read_bytes().splitlines()]
        dispatches = [event for event in events if event["kind"] == "dispatch"]
        revisions = {event["implementation_revision"] for event in dispatches}
        if len(revisions) != 1 or any(
            event["authenticatedControlUi"] is not True for event in dispatches
        ):
            raise ValueError("composed_native_binding_failed")
        capture["composed_actions"] = dict(
            checks=checks,
            negative_events=new_events,
            receipts=results,
            events=events,
            implementation_revision=revisions.pop(),
            qualification_constructor="explicit-test-only",
            production_registry_enabled=False,
            fixture_scope={"user_id": "u", "workspace_id": "w"},
        )
        if not all(checks.values()):
            raise ValueError("composed_checks_failed")

    def before_retirement(self, page, session):
        self.hold.set()
        self.held_request = page.evaluate(
            "params => window.__rmPersonWire.startRpc('relaymind.recommendations', params)",
            dict(
                session,
                input=dict(kind="listFeedback", page=dict(limit=1)),
                idempotencyKey="native-composed-retirement",
            ),
        )
        if not self.entered.wait(3):
            raise ValueError("composed_hold_not_observed")

    def after_retirement(self, capture):
        import time

        self.release.set()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            events = [json.loads(line) for line in self.event_file.read_bytes().splitlines()]
            relevant = [event for event in events if event.get("requestId") == self.held_request]
            retired = next(
                (event for event in relevant if event["kind"] == "connection_retired"), None
            )
            result = next((event for event in relevant if event["kind"] == "result"), None)
            if result is not None:
                passed = (
                    retired is not None
                    and retired["aborted"] is True
                    and result["ok"] is False
                    and result["reason"] == "cancelled"
                )
                capture["composed_actions"]["checks"]["revocation_denied"] = passed
                capture["composed_actions"]["retirement"] = relevant
                capture["composed_actions"]["held_backend_entered"] = self.entered.is_set()
                if not passed:
                    raise ValueError("composed_revocation_failed")
                return
            time.sleep(0.01)
        raise ValueError("composed_revocation_not_observed")


def main(argv=None):
    import argparse
    import threading

    from api_helpers import RunningServer
    from recommendation_helpers import setup
    from relaymind.recommendations.registrations import (
        ActionRegistrations,
        Registration,
        RegistrationSnapshot,
    )
    from relaymind.store.asset_feedback import AssetFeedbackStore

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    person = person_module()
    backend = args.work_dir.parent / (args.work_dir.name + "-gateway")
    backend.mkdir(mode=0o700, exist_ok=False)
    app, service, runs, _ = setup(backend)
    hold, entered, release = threading.Event(), threading.Event(), threading.Event()

    @app.middleware("http")
    async def hold_private_read(request, call_next):
        if hold.is_set() and request.method == "GET" and request.url.path == "/relay/v1/feedback":
            entered.set()
            await asyncio.to_thread(release.wait, 5)
        return await call_next(request)

    token = secrets.token_urlsafe(48)
    app.state.application_token = secrets.token_urlsafe(48)
    registration = ActionRegistrations(
        runs,
        qualification=lambda *claim: (
            claim
            == (
                "native-composition-fixture",
                "disposable-native-host",
                "actual-person-session",
                "openclaw-native-2026.10.7.v1",
            )
        ),
    )
    registration.replace(
        RegistrationSnapshot(
            (
                Registration(
                    "native-composition-fixture",
                    "u",
                    "w",
                    hashlib.sha256(token.encode()).hexdigest(),
                    True,
                    "disposable-native-host",
                    "actual-person-session",
                    "openclaw-native-2026.10.7.v1",
                ),
            )
        )
    )
    app.state.action_registrations = registration
    app.state.feedback_store = AssetFeedbackStore(
        service.store, service.provider.store, registration, policy=service.provider.policy
    )
    asyncio.run(service.refresh_catalog())
    try:
        with RunningServer(app) as server:
            extension = ActionComposition(
                server.base_url, app.state.application_token, token, hold, entered, release
            )
            code = person.run_qualification(args.work_dir, args.out, extension=extension)
        capture = json.loads(args.out.read_bytes())
        capture["checks"]["composed_actions"] = composed_pass(capture)
        person.write_capture(args.out, capture)
        return 0 if code == 0 and capture["checks"]["composed_actions"] else 2
    finally:
        release.set()
        asyncio.run(service.stop())
        runs.close()


if __name__ == "__main__":
    raise SystemExit(main())
