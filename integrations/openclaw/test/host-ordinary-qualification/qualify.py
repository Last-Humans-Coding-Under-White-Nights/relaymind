"""RM37/RM39 AC28/AC30: ordinary installed entry, default Gateway registration, real person UI."""

import asyncio
import hashlib
import importlib.util
import json
import re
import secrets
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from queue import Queue

ROOT = Path(__file__).resolve().parents[4]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests"), str(ROOT / "scripts")]


def base_module():
    path = ROOT / "integrations/openclaw/test/host-action-qualification/qualify.py"
    spec = importlib.util.spec_from_file_location("native_action_component", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BASE = base_module()


ORDINARY_OBSERVE_SECONDS = 5
ORDINARY_UI_ACTIONS = frozenset(
    {"open", "recommend", "choose", "correct", "inspect", "delete", "reset"}
)


HOST_EVENT_WAIT_SECONDS = BASE.person_module().HOST_EVENT_WAIT_SECONDS
ORDINARY_FAILURE_STEPS = frozenset(
    {
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
)


def ordinary_error_class(error):
    # native_rpc translates browser errors into safe ValueErrors. Inspect its
    # original exception only for classification; never export exception text.
    original = error
    if isinstance(error, ValueError) and str(error).startswith("native_rpc_"):
        original = error.__context__ or error
    classes = type(original).__mro__
    browser_classes = {c.__name__ for c in classes if c.__module__.startswith("playwright.")}
    if "TimeoutError" in browser_classes:
        return "playwright_timeout"
    if "TargetClosedError" in browser_classes or (
        "Error" in browser_classes
        and (
            "Target closed" in str(original)
            or "Target page, context or browser has been closed" in str(original)
        )
    ):
        return "playwright_target_closed"
    if browser_classes:
        return "playwright_error"
    return "value_error" if isinstance(error, ValueError) else "other"


def ordinary_step(name):
    def decorate(method):
        @wraps(method)
        def run(self, *args, **kwargs):
            with self.step(name):
                return method(self, *args, **kwargs)

        return run

    return decorate


def private_json(path, data):
    path.write_bytes((json.dumps(data, ensure_ascii=True) + "\n").encode("utf-8"))
    path.chmod(0o600)


def ordinary_pass(capture):
    try:
        if not BASE.person_module().person_composition_pass(capture):
            return False
        value = capture["ordinary_actions"]
        if (
            value["installation"]["exit_code"] != 0
            or len(value["installation"]["archive_sha256"]) != 64
        ):
            return False
        if (
            value["default_registration"] is not True
            or value["constructor_override"] is not False
            or value.get("production_registry_enabled") is not True
        ):
            return False
        snapshots = value["ui"]["snapshots"]
        chosen = snapshots["choice"]["views"]
        if len(chosen) != 1 or chosen[0]["state"] != "chosen":
            return False
        action = chosen[0]["selected_action"]
        if action["adapter_id"] != "ordinary-native-channel" or action["scope"] != {
            "user_id": "u",
            "workspace_id": "w",
        }:
            return False
        positives = value["positive_events"]
        required_paths = [
            ("/relay/v1/recommendations", "POST"),
            ("/relay/v1/recommendations/" + chosen[0]["recommendation_id"] + "/choice", "POST"),
            ("/relay/v1/recommendations/" + chosen[0]["recommendation_id"] + "/feedback", "POST"),
            ("/relay/v1/feedback", "GET"),
            ("/relay/v1/feedback/reset", "POST"),
        ]
        for path, method in required_paths:
            attempts = [
                event
                for event in positives
                if event["kind"] == "http_attempt"
                and event["path"] == path
                and event["method"] == method
            ]
            if not any(
                any(
                    result["kind"] == "http"
                    and result.get("attempt_id") == attempt["attempt_id"]
                    and 200 <= result["status"] < 300
                    for result in positives
                )
                for attempt in attempts
            ):
                return False
        records = snapshots["correction"]["records"]
        if (
            len(records) != 1
            or records[0]["feedback"]["kind"] != "correction"
            or records[0]["recommendation_id"] != chosen[0]["recommendation_id"]
        ):
            return False
        if snapshots["delete"]["records"] or snapshots["reset"]["records"]:
            return False
        if not any(audit["operation"] == "delete" for audit in snapshots["delete"]["audits"]):
            return False
        if not any(audit["operation"] == "reset" for audit in snapshots["reset"]["audits"]):
            return False
        if any(
            event["kind"] == "http_attempt" and event["path"].startswith("/relay/v1/")
            for event in value["negative_events"]
        ):
            return False
        negatives = value["negative_receipts"]
        if len(negatives) != 2:
            return False
        for i, item in enumerate(negatives):
            observed = item["observed"]
            expected = capture["accounts"][i]
            if (
                observed["socket_id"] != capture["accounts"][1]["socket_id"]
                or observed["frame"].get("ok") is not False
                or (observed["frame"].get("error") or {}).get("code")
                not in ({"FORBIDDEN", "NOT_FOUND"} if i == 0 else {"INVALID_REQUEST"})
            ):
                return False
            if item["target"] != dict(
                sessionKey=expected["session_key"],
                expectedSessionId=expected["native_session_id"],
                expectedLifecycleRevision=expected["lifecycle_revision"],
            ):
                return False
            original = next(
                event
                for event in capture["events"]
                if event.get("kind") == "native_rpc" and event.get("request_id") == observed["id"]
            )
            if (
                original["socket_id"] != observed["socket_id"]
                or original["method"] != "relaymind.recommendations"
                or original["ok"] is not False
            ):
                return False
        retirement = value["retirement"]
        attempts = [
            event
            for event in retirement["after_window"]
            if event["kind"] == "http_attempt" and event["path"] == "/relay/v1/feedback"
        ]
        failures = retirement["fetch_failures"]
        retired = retirement["ui_retired"]
        ui_person = value["ui_person"]
        person = ui_person["frame"]["payload"]
        account = capture["accounts"][0]
        if ui_person["frame"].get("ok") is not True or any(
            person[field] != account[target]
            for field, target in {
                "profileId": "profile_id",
                "authenticatedUserId": "native_person_email",
                "sessionKey": "session_key",
                "sessionId": "native_session_id",
                "lifecycleRevision": "lifecycle_revision",
            }.items()
        ):
            return False
        guard_events = retirement["ui_guard_events"]
        if (
            [event["phase"] for event in guard_events]
            != ["prepared", "connection_retired", "guard_denied"]
            or any(
                event["requestId"] != retirement["ui_guard_request"]
                or event["connectionId"] != person["connectionId"]
                for event in guard_events
            )
            or guard_events[-1]["reason"] != "post_await_authority_retired"
        ):
            return False
        return (
            retirement["held_backend_entered"] is True
            and len(attempts) == len(failures) == 1
            and retirement["attempt"] == attempts[0]
            and failures[0]["attempt_id"] == attempts[0]["attempt_id"]
            and failures[0]["aborted"] is True
            and failures[0]["path"] == "/relay/v1/feedback"
            and retirement["connection_id"] == retired["connectionId"] == person["connectionId"]
            and retirement["native_socket_id"] == ui_person["socket_id"]
            and int(attempts[0]["monotonic_ns"])
            <= int(retired["monotonic_ns"])
            <= int(failures[0]["monotonic_ns"])
        )
    except (KeyError, TypeError, ValueError, IndexError, StopIteration):
        return False


SELECTED_LABEL = (
    "Exact native revision selected for the next turn; instruction use is not yet observed."
)
_SKILL_ID = re.compile(r"[a-f0-9-]{36}")
_REVISION = re.compile(r"[a-f0-9]{64}")
_SKILL_NEGATIVES = {"unmapped_skill", "foreign_person"}


def ordinary_skill_pass(capture):
    """#197 (RM39/AC30; spec 7.6.1): exact native Skill Library selection.

    Requires the ordinary channel proof plus a saved library revision, the
    operator mapping binding that exact revision to the offered asset bytes,
    the closed bridge receipt that claims selection only (never instruction
    use), the official UI status, and refusals for an unmapped skill and for
    another person. Loaded instruction use is a separate, unclaimed gate.
    """
    try:
        if not ordinary_pass(capture):
            return False
        value = capture["ordinary_skill"]
        saved, mapping = value["library_save"], value["mapping"]
        skill_id, revision = saved["skill_id"], saved["revision"]
        if (
            saved["ok"] is not True
            or not isinstance(skill_id, str)
            or not _SKILL_ID.fullmatch(skill_id)
            or not isinstance(revision, str)
            or not _REVISION.fullmatch(revision)
        ):
            return False
        if (
            mapping["skillId"] != skill_id
            or mapping["revision"] != revision
            or mapping["contentHash"] != value["content_sha256"]
            or not _REVISION.fullmatch(value["content_sha256"])
        ):
            return False
        receipt = value["receipt"]
        if receipt["ok"] is not True or receipt["payload"] != dict(
            skillId=skill_id,
            revision=revision,
            sessionActivation="next-turn",
            instructions="not_observed",
        ):
            return False
        if value["ui_status"] != "selected":
            return False
        negatives = {item["case"]: item for item in value["negatives"]}
        return set(negatives) == _SKILL_NEGATIVES and all(
            item["ok"] is False for item in negatives.values()
        )
    except (KeyError, TypeError):
        return False


class OrdinaryComposition(BASE.ActionComposition):
    def __init__(self, *args, archive, runs, skill_text, skill_asset, **kwargs):
        super().__init__(*args, **kwargs)
        self.archive, self.runs = archive.resolve(strict=True), runs
        self.skill_text, self.skill_asset = skill_text, skill_asset
        self._failure = {}
        self._ui_action = None

    @contextmanager
    def step(self, name):
        if name not in ORDINARY_FAILURE_STEPS:
            raise ValueError("ordinary_step_failed")
        try:
            yield
        except Exception as error:
            if self._failure:
                raise
            error_class = ordinary_error_class(error)
            self._failure = dict(ordinary_failure_step=name, ordinary_error_class=error_class)
            if name == "ui_actions" and self._ui_action in ORDINARY_UI_ACTIONS:
                self._failure["ordinary_ui_action"] = self._ui_action
            if isinstance(error, TimeoutError) and str(error) == "overall_deadline_exceeded":
                raise
            if error_class.startswith("playwright_"):
                raise ValueError("ordinary_" + error_class) from None
            if isinstance(error, ValueError) and str(error) in (
                self.failure_reasons() | BASE.person_module().SAFE_REASONS
            ):
                raise
            raise ValueError("ordinary_step_failed") from None

    def set_ui_action(self, action):
        if action not in ORDINARY_UI_ACTIONS:
            raise ValueError("ordinary_step_failed")
        self._ui_action = action

    def failure_diagnostics(self):
        return dict(self._failure)

    @ordinary_step("configure")
    def configure(self, config, work, origin):
        super().configure(config, work, origin)
        # No companion factory is installed in this run.
        config["plugins"]["allow"].remove("relaymind-action-qualification")
        config["plugins"]["load"]["paths"].remove(
            str(ROOT / "integrations/openclaw/test/host-action-qualification")
        )
        del config["plugins"]["entries"]["relaymind-action-qualification"]
        observer = Path(__file__).parent / "observer"
        config["plugins"]["allow"].append("relaymind-ordinary-observer")
        config["plugins"]["load"]["paths"].append(str(observer))
        config["plugins"]["entries"]["relaymind-ordinary-observer"] = dict(
            enabled=True, config=dict(eventFile=str(self.event_file))
        )
        config["gateway"]["controlUi"]["experimental"] = dict(customPlugins=True)
        self.host_config_path = work / "openclaw.json"

    @ordinary_step("install")
    def prepare_host(self, host, environment, work, config_path):
        # Operator installation is the ordinary pinned CLI, no constructor override.
        log = work / "private-ordinary-install.log"
        with log.open("wb") as output:
            result = subprocess.run(
                host
                + [
                    "plugins",
                    "install",
                    "npm-pack:" + str(self.archive),
                    "--force",
                    "--accept-capabilities",
                ],
                env=environment,
                cwd=work,
                stdout=output,
                stderr=subprocess.STDOUT,
                timeout=120,
            )
        log.chmod(0o600)
        self.installation = dict(
            exit_code=result.returncode,
            archive_sha256=hashlib.sha256(self.archive.read_bytes()).hexdigest(),
        )
        if result.returncode != 0:
            raise ValueError("ordinary_install_failed")
        self.host_config = json.loads(config_path.read_bytes())

    @ordinary_step("sources")
    def source_paths(self):
        return super().source_paths() + [
            Path(__file__),
            *sorted((Path(__file__).parent / "observer").iterdir()),
        ]

    def failure_reasons(self):
        return super().failure_reasons() | {
            "ordinary_step_failed",
            "ordinary_playwright_timeout",
            "ordinary_playwright_target_closed",
            "ordinary_playwright_error",
            "ordinary_install_failed",
            "ordinary_entry_unavailable",
            "ordinary_guard_refused",
            "ordinary_ui_timeout",
            "ordinary_observe_timeout",
            "ordinary_zero_http_failed",
            "ordinary_abort_unobserved",
            "ordinary_observer_blind",
            "ordinary_skill_save_failed",
            "ordinary_skill_selection_failed",
        }

    def observe_backend(self):
        # sql_read includes an unbounded lock acquisition. Keep it off the browser
        # thread; a daemon cannot prevent process exit if the gateway stays stuck.
        # Do not use an executor context manager, whose exit would join forever.
        outcome = Queue(maxsize=1)

        def read():
            try:
                outcome.put((self._read_backend(), None))
            except Exception as error:
                outcome.put((None, error))

        worker = threading.Thread(target=read, daemon=True, name="ordinary-observe")
        worker.start()
        worker.join(ORDINARY_OBSERVE_SECONDS)
        if worker.is_alive():
            raise ValueError("ordinary_observe_timeout")
        value, error = outcome.get_nowait()
        if error is not None:
            raise error
        return value

    def _read_backend(self):
        with self.runs.sql_read() as db:
            return dict(
                views=[
                    json.loads(row[0])
                    for row in db.execute(
                        "SELECT view_json FROM recommendations ORDER BY created_at"
                    )
                ],
                records=[
                    json.loads(row[0])
                    for row in db.execute(
                        "SELECT record_json FROM feedback_records ORDER BY insertion_sequence"
                    )
                ],
                audits=[
                    json.loads(row[0])
                    for row in db.execute(
                        "SELECT audit_json FROM feedback_audits ORDER BY timestamp"
                    )
                ],
                events=[
                    json.loads(row[0])
                    for row in db.execute(
                        "SELECT event_json FROM recommendation_events "
                        "ORDER BY recommendation_id,seq"
                    )
                ],
            )

    @ordinary_step("persons_ready")
    def persons_ready(self, rpc, pages, sessions, capture):
        account = capture["accounts"][0]
        person = next(
            event["payload"]
            for event in capture["events"]
            if event["id"] == account["rpc_evidence_id"]
        )
        self.config["recommendations"]["bindings"] = [
            dict(
                profileId=account["profile_id"],
                authenticatedUserId=account["native_person_email"],
                agentId="main",
                workspaceDir=person["workspaceDir"],
                userId="u",
                workspaceId="w",
                actionTokenEnv="RM_QUALIFICATION_ACTION_TOKEN",
            )
        ]
        self.host_config["plugins"]["allow"] = list(
            dict.fromkeys([*self.host_config["plugins"]["allow"], "relaymind"])
        )
        skill = self.save_library_skill(rpc, pages[0], sessions[0])
        with self.step("entry_read"):
            capture["ordinary_skill"] = skill
            self.config["recommendations"]["skills"] = [skill["mapping"]]
            self.host_config["plugins"]["entries"]["relaymind"] = dict(
                enabled=True, config=self.config
            )
            private_json(self.host_config_path, self.host_config)
            value = dict(
                installation=self.installation,
                default_registration=True,
                constructor_override=False,
            )
            capture["ordinary_actions"] = value
            deadline = time.monotonic() + 25
            while True:
                probe = rpc(
                    pages[0],
                    "relaymind.recommendations",
                    dict(
                        sessions[0],
                        input=dict(kind="listFeedback", page=dict(limit=1)),
                        idempotencyKey="ordinary-entry-read",
                    ),
                )
                if probe["frame"].get("ok") is True:
                    value["entry_read"] = probe
                    events = [
                        json.loads(line) for line in self.event_file.read_bytes().splitlines()
                    ]
                    attempts = [
                        event
                        for event in events
                        if event["kind"] == "http_attempt" and event["path"] == "/relay/v1/feedback"
                    ]
                    if not any(
                        any(
                            result["kind"] == "http"
                            and result.get("attempt_id") == attempt["attempt_id"]
                            and result["status"] == 200
                            for result in events
                        )
                        for attempt in attempts
                    ):
                        raise ValueError("ordinary_observer_blind")
                    value["entry_events"] = events
                    break
                error = probe["frame"].get("error") or {}
                details = error.get("details") or {}
                pending_scope = error.get("code") == "FORBIDDEN" and details == {
                    "code": "MISSING_SCOPE",
                    "missingScope": "operator.admin",
                    "requiredScopes": ["operator.admin"],
                }
                message = error.get("message", "")
                safe_messages = {
                    "missing scope: operator.admin",
                    "missing scope: operator.write",
                    "Gateway requester authority changed",
                    "native_action_unavailable",
                    "unknown method: relaymind.recommendations",
                }
                value.setdefault("entry_failures", []).append(
                    {
                        "request_id": probe["id"],
                        "socket_id": probe["socket_id"],
                        "error_code": error.get("code"),
                        "message": message
                        if message in safe_messages
                        else "unclassified_native_refusal",
                        "pending_publication": pending_scope,
                    }
                )
                if not pending_scope and message != "unknown method: relaymind.recommendations":
                    raise ValueError("ordinary_guard_refused")
                if time.monotonic() >= deadline:
                    raise ValueError("ordinary_entry_unavailable")
                pages[0].wait_for_timeout(500)
        with self.step("ui_actions"):
            value["production_registry_enabled"] = True
            self.set_ui_action("open")
            ui_page = pages[0].context.new_page()
            try:
                suffix = sessions[0]["sessionKey"].removeprefix("agent:main:")
                if "/" in suffix or not suffix.startswith("person-"):
                    raise ValueError("ordinary_guard_refused")
                ui_page.goto(
                    self.origin + "/chat/main/" + suffix,
                    wait_until="domcontentloaded",
                    timeout=10000,
                )
                BASE.person_module().wait_for_wire_ready(
                    ui_page, 20, ValueError("ordinary_entry_unavailable")
                )
                ui_person = rpc(ui_page, "rmqualification.person", sessions[0])
                payload = ui_person["frame"].get("payload") or {}
                if ui_person["frame"].get("ok") is not True or any(
                    payload.get(field) != person.get(field)
                    for field in (
                        "profileId",
                        "authenticatedUserId",
                        "sessionKey",
                        "sessionId",
                        "lifecycleRevision",
                        "workspaceDir",
                    )
                ):
                    raise ValueError("ordinary_guard_refused")
                value["ui_person"] = ui_person
                value["ui"] = run_ui(ui_page, sessions[0], self.observe_backend, self.set_ui_action)
                self.select_library_skill(rpc, pages, sessions, ui_page, capture["ordinary_skill"])
            except Exception:
                try:
                    ui_page.close()
                except TimeoutError:
                    raise  # Never swallow the overall SIGALRM during cleanup.
                except Exception:
                    pass  # A closed page cannot replace the original failure.
                raise
        with self.step("negative_actions"):
            self.ui_page = ui_page
            self.ui_person = ui_person
            value["positive_events"] = [
                json.loads(line) for line in self.event_file.read_bytes().splitlines()
            ]
            start = len(self.event_file.read_bytes())
            negative = []
            for target in (sessions[0], sessions[1]):
                observed = rpc(
                    pages[1],
                    "relaymind.recommendations",
                    dict(
                        target,
                        input=dict(kind="listFeedback", page=dict(limit=1)),
                        idempotencyKey=secrets.token_hex(16),
                    ),
                )
                negative.append(dict(observed=observed, target=target))
            value["negative_receipts"] = negative
            value["negative_events"] = [
                json.loads(line) for line in self.event_file.read_bytes()[start:].splitlines()
            ]
            if any(item["observed"]["frame"].get("ok") is not False for item in negative) or any(
                event["kind"] == "http_attempt" for event in value["negative_events"]
            ):
                raise ValueError("ordinary_zero_http_failed")

    @ordinary_step("save_library_skill")
    def save_library_skill(self, rpc, page, session):
        """Save the gateway's exact fixture SKILL.md into this person's library."""
        saved = rpc(
            page,
            "skills.library.save",
            dict(
                expectedRevision=None,
                slug="relaymind-qualification-" + secrets.token_hex(4),
                content=self.skill_text,
            ),
        )
        payload = saved["frame"].get("payload") if saved["frame"].get("ok") is True else None
        entry = None
        if isinstance(payload, dict):
            entry = payload.get("entry") if isinstance(payload.get("entry"), dict) else payload
        if not isinstance(entry, dict) or not isinstance(entry.get("skillId"), str):
            raise ValueError("ordinary_skill_save_failed")
        skill_id, revision = entry["skillId"], entry.get("revision")
        if not isinstance(revision, str):
            raise ValueError("ordinary_skill_save_failed")
        asset = self.skill_asset
        return dict(
            library_save=dict(ok=True, skill_id=skill_id, revision=revision),
            content_sha256=hashlib.sha256(self.skill_text.encode("utf-8")).hexdigest(),
            mapping=dict(
                assetId=asset["asset_id"],
                contentHash=asset["content_hash"],
                sourceVersion=asset["source_version"],
                skillId=skill_id,
                revision=revision,
            ),
        )

    @ordinary_step("select_library_skill")
    def select_library_skill(self, rpc, pages, sessions, ui_page, skill):
        """Exact selection through the bridge, the official UI and two refusals."""
        views = self.observe_backend()["views"]
        chosen = [v for v in views if v["state"] == "chosen"]
        if len(chosen) != 1:
            raise ValueError("ordinary_skill_selection_failed")
        mapping = skill["mapping"]
        params = dict(
            sessions[0],
            recommendationId=chosen[0]["recommendation_id"],
            assetId=mapping["assetId"],
            contentHash=mapping["contentHash"],
            skillId=mapping["skillId"],
            revision=mapping["revision"],
        )
        receipt = rpc(pages[0], "relaymind.skill", params)
        skill["receipt"] = dict(
            ok=receipt["frame"].get("ok") is True, payload=receipt["frame"].get("payload")
        )
        negatives = []
        for case, page, change in (
            ("unmapped_skill", pages[0], dict(skillId=str(uuid.uuid4()))),
            ("foreign_person", pages[1], {}),
        ):
            observed = rpc(page, "relaymind.skill", dict(params, **change))
            negatives.append(
                dict(
                    case=case,
                    ok=observed["frame"].get("ok") is True,
                    error_code=BASE.person_module().native_error_code(observed["frame"]),
                )
            )
        skill["negatives"] = negatives
        ui_page.get_by_role("button", name="Select native skill", exact=True).click()
        ui_page.get_by_role("combobox", name="Select native skill").select_option(
            value=json.dumps(
                dict(skillId=mapping["skillId"], revision=mapping["revision"]),
                separators=(",", ":"),
            )
        )
        ui_page.get_by_role("button", name="Select for next turn", exact=True).click()
        try:
            ui_page.get_by_text(SELECTED_LABEL, exact=True).wait_for(timeout=15000)
            skill["ui_status"] = "selected"
        except Exception:
            skill["ui_status"] = "unavailable"
            raise
        listed = rpc(pages[0], "skills.library.list", dict(sessionKey=sessions[0]["sessionKey"]))
        skill["library_list"] = dict(
            ok=listed["frame"].get("ok") is True, payload=listed["frame"].get("payload")
        )

    def native_events(self):
        path = self.work / "native-events.jsonl"
        return [json.loads(line) for line in path.read_bytes().splitlines()]

    @ordinary_step("retirement_prepared")
    def before_retirement(self, page, session):
        self.retirement_start = len(self.event_file.read_bytes())
        self.ui_guard_request = self.ui_page.evaluate(
            "params => window.__rmPersonWire.startRpc('rmqualification.person',params)",
            dict(session, hold=True),
        )
        deadline = time.monotonic() + HOST_EVENT_WAIT_SECONDS
        while not any(
            event.get("requestId") == self.ui_guard_request and event.get("phase") == "prepared"
            for event in self.native_events()
        ):
            if time.monotonic() >= deadline:
                raise ValueError("ordinary_abort_unobserved")
            time.sleep(0.01)
        with self.step("retirement_attempt"):
            super().before_retirement(self.ui_page, session)
            events = [
                json.loads(line)
                for line in self.event_file.read_bytes()[self.retirement_start :].splitlines()
            ]
            attempts = [
                event
                for event in events
                if event["kind"] == "http_attempt" and event["path"] == "/relay/v1/feedback"
            ]
            if len(attempts) != 1:
                raise ValueError("ordinary_abort_unobserved")
            self.held_attempt = attempts[0]

    @ordinary_step("retirement_abort")
    def after_retirement(self, capture):
        self.release.set()
        deadline = time.monotonic() + HOST_EVENT_WAIT_SECONDS
        while time.monotonic() < deadline:
            events = [
                json.loads(line)
                for line in self.event_file.read_bytes()[self.retirement_start :].splitlines()
            ]
            failures = [
                event
                for event in events
                if event["kind"] == "http_error"
                and event["path"] == "/relay/v1/feedback"
                and event["aborted"] is True
                and event["attempt_id"] == self.held_attempt["attempt_id"]
            ]
            ui_retired = next(
                (
                    event
                    for event in self.native_events()
                    if event.get("requestId") == self.ui_guard_request
                    and event.get("phase") == "connection_retired"
                    and event.get("connection_aborted") is True
                ),
                None,
            )
            if failures and ui_retired is not None:
                capture["ordinary_actions"]["retirement"] = dict(
                    held_backend_entered=self.entered.is_set(),
                    fetch_failures=failures,
                    attempt=self.held_attempt,
                    request_id=self.held_request,
                    connection_id=self.ui_person["frame"]["payload"]["connectionId"],
                    native_socket_id=self.ui_person["socket_id"],
                    ui_guard_request=self.ui_guard_request,
                    ui_retired=ui_retired,
                    after_window=events,
                )
                return
            time.sleep(0.01)
        raise ValueError("ordinary_abort_unobserved")

    @ordinary_step("finalize")
    def finalize(self, capture):
        relevant = [
            event
            for event in self.native_events()
            if event.get("requestId") == self.ui_guard_request
        ]
        capture["ordinary_actions"]["retirement"]["ui_guard_events"] = relevant


def run_ui(page, session, observe, record_action=lambda action: None):
    snapshots = {}
    record_action("open")
    # Real native navigation preserves the authenticated socket; no fabricated client.
    page.get_by_role("link", name="RelayMind recommendations", exact=True).click(timeout=10000)
    page.get_by_label("Task", exact=True).fill(
        "hello greeting ordinary native person " + secrets.token_hex(4), timeout=10000
    )

    def settle(name, predicate):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            value = observe()
            if predicate(value):
                snapshots[name] = value
                return
            page.wait_for_timeout(100)
        raise ValueError("ordinary_ui_timeout")

    record_action("recommend")
    page.get_by_role("button", name="Recommend", exact=True).click(timeout=10000)
    settle("recommend", lambda s: len(s["views"]) == 1 and s["views"][0]["state"] == "ready")
    record_action("choose")
    page.get_by_role("button", name="Choose 1", exact=True).click(timeout=10000)
    settle("choice", lambda s: s["views"][0]["state"] == "chosen")
    record_action("correct")
    page.get_by_role("button", name="Correct to 1", exact=True).click(timeout=10000)
    settle("correction", lambda s: len(s["records"]) == 1)
    record_action("inspect")
    page.get_by_role("button", name="Inspect feedback", exact=True).click(timeout=10000)
    page.get_by_role("button", name="Delete feedback", exact=True).wait_for(timeout=10000)
    snapshots["inspect"] = observe()
    record_action("delete")
    page.get_by_role("button", name="Delete feedback", exact=True).click(timeout=10000)
    settle(
        "delete",
        lambda s: not s["records"] and any(a["operation"] == "delete" for a in s["audits"]),
    )
    record_action("reset")
    page.once("dialog", lambda dialog: dialog.accept())
    page.get_by_role("button", name="Reset feedback", exact=True).click(timeout=10000)
    settle("reset", lambda s: any(a["operation"] == "reset" for a in s["audits"]))
    return dict(
        snapshots=snapshots,
        mechanism="official native ControlUiHost.request via real DOM buttons",
        locale="en-US",
    )


def main(argv=None):
    import argparse

    from api_helpers import RunningServer
    from recommendation_helpers import setup
    from relaymind.recommendations.registrations import (
        ActionRegistrations,
        Registration,
        RegistrationSnapshot,
    )
    from relaymind.store.asset_feedback import AssetFeedbackStore

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tarball", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    backend = args.work_dir.parent / (args.work_dir.name + "-gateway")
    backend.mkdir(mode=0o700)
    app, service, runs, _ = setup(backend)
    app.state.application_token = secrets.token_urlsafe(48)
    action_token = secrets.token_urlsafe(48)
    registration = ActionRegistrations(runs)  # Actual DEFAULT production resolver.
    registration.replace(
        RegistrationSnapshot(
            (
                Registration(
                    "ordinary-native-channel",
                    "u",
                    "w",
                    hashlib.sha256(action_token.encode()).hexdigest(),
                    True,
                    "ordinary-disposable-host",
                    "ordinary-person-mapping",
                    "openclaw-native-2026.10.7.v1",
                ),
            )
        )
    )
    app.state.action_registrations = registration
    app.state.feedback_store = AssetFeedbackStore(
        service.store, service.provider.store, registration, policy=service.provider.policy
    )
    hold, entered, release = threading.Event(), threading.Event(), threading.Event()

    @app.middleware("http")
    async def hold_private_read(request, call_next):
        if hold.is_set() and request.method == "GET" and request.url.path == "/relay/v1/feedback":
            entered.set()
            await asyncio.to_thread(release.wait, HOST_EVENT_WAIT_SECONDS)
        return await call_next(request)

    asyncio.run(service.refresh_catalog())
    # The exact offered fixture asset and its original SKILL.md bytes (#197).
    record = service.provider.store.snapshot().assets[0]
    skill_text = (backend / "source" / "SKILL.md").read_text(encoding="utf-8")
    try:
        with RunningServer(app) as server:
            extension = OrdinaryComposition(
                server.base_url,
                app.state.application_token,
                action_token,
                hold,
                entered,
                release,
                archive=args.tarball,
                runs=runs,
                skill_text=skill_text,
                skill_asset=dict(
                    asset_id=record.asset_id,
                    content_hash=record.content_hash,
                    source_version=record.source_version,
                ),
            )
            code = BASE.person_module().run_qualification(
                args.work_dir, args.out, extension=extension
            )
        capture = json.loads(args.out.read_bytes())
        capture["checks"]["ordinary_actions"] = ordinary_pass(capture)
        capture["checks"]["ordinary_skill"] = ordinary_skill_pass(capture)
        BASE.person_module().write_capture(args.out, capture)
        passed = capture["checks"]["ordinary_actions"] and capture["checks"]["ordinary_skill"]
        return 0 if code == 0 and passed else 2
    finally:
        release.set()
        asyncio.run(service.stop())
        runs.close()


if __name__ == "__main__":
    raise SystemExit(main())
