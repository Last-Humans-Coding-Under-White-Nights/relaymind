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
from collections.abc import Callable
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


def wait_for_backend_release(release: threading.Event) -> None:
    # Cover the base connection and UI/fetch observation windows, plus setup.
    # Expiry is failure, never permission to complete the held HTTP response.
    # The enclosing harness retains its 570-second deadline.
    if not release.wait(3 * HOST_EVENT_WAIT_SECONDS):
        raise TimeoutError("ordinary_abort_unobserved")


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
_SKILL_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_REVISION = re.compile(r"[a-f0-9]{64}")
_SKILL_NEGATIVES = {
    "unmapped_skill": "INVALID_REQUEST",
    "foreign_person": "FORBIDDEN",
    "foreign_person_own_session": "INVALID_REQUEST",
}


def library_revision_selected(listed, skill_id, revision, session_key):
    """Require a successful native listing that proves the selection took effect.

    The library must hold the exact saved, not removed, revision, and the
    listed session must be the authenticated target session whose selections
    are exactly that skill at that revision. A listing that only proves the
    revision exists cannot show a no-op or wrong-session activation.
    """
    try:
        payload = listed["payload"]
        session = payload["session"]
        selections = session["selections"]
        return (
            listed["ok"] is True
            and isinstance(session_key, str)
            and session["sessionKey"] == session_key
            and isinstance(selections, list)
            and len(selections) == 1
            and selections[0]["skillId"] == skill_id
            and selections[0]["revision"] == revision
            and any(
                entry["skillId"] == skill_id
                and entry["revision"] == revision
                and entry.get("removed") is not True
                for entry in payload["entries"]
            )
        )
    except (KeyError, TypeError, IndexError):
        return False


def ordinary_skill_pass(capture):
    """#197 (RM39/AC30; spec 7.6.1): exact native Skill Library selection.

    Requires the ordinary channel proof plus a saved library revision, the
    operator mapping binding that exact revision to the offered asset bytes,
    the closed bridge receipt that claims selection only (never instruction
    use), the official UI status, a native listing whose authenticated target
    session holds exactly that revision as its selection, and refusals for an
    unmapped skill and for another person. Loaded instruction use is a
    separate, unclaimed gate.
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
        asset_id = mapping["assetId"]
        views = capture["ordinary_actions"]["ui"]["snapshots"]["choice"]["views"]
        chosen = [view for view in views if view["state"] == "chosen"]
        if (
            not isinstance(asset_id, str)
            or not asset_id.strip()
            or len(chosen) != 1
            or chosen[0]["selected_action"]["choice"]
            != dict(kind="asset", asset_id=asset_id, content_hash=mapping["contentHash"])
            or "sourceVersion" not in mapping
            or not any(
                item["asset"]["asset_id"] == asset_id
                and item["asset"]["content_hash"] == mapping["contentHash"]
                and item["asset"].get("source_version") == mapping["sourceVersion"]
                for item in chosen[0]["displayed_candidates"]
            )
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
        if not library_revision_selected(
            value["library_list"], skill_id, revision, capture["accounts"][0]["session_key"]
        ):
            return False
        if len(value["negatives"]) != len(_SKILL_NEGATIVES):
            return False
        negatives = {item["case"]: item for item in value["negatives"]}
        return negatives.keys() == _SKILL_NEGATIVES.keys() and all(
            item["ok"] is False and item["error_code"] == _SKILL_NEGATIVES[item["case"]]
            for item in value["negatives"]
        )
    except (KeyError, TypeError):
        return False


LOADED_USE_FINAL = "Native skill observation complete."
_SCOPE_FIELDS = ("sessionKey", "sessionId", "lifecycleRevision")
_TURN_FIELDS = ("chat", "tools", "requests", "terminal", "subscription", "provider_attempts")
_CASE_FIELDS = {
    "case",
    "task",
    "before",
    "current",
    "selected",
    "selection",
    "intervention",
    "chat",
    "tools",
    "requests",
    "terminal",
    "observed",
    "subscription",
    "provider_attempts",
    "advertised_revision",
    "failure_reason",
}


def closed_native_data(reason: str) -> Callable:
    """Translate malformed external structure at a named observation boundary."""

    def decorate(function):
        @wraps(function)
        def guarded(*args, **kwargs):
            try:
                return function(*args, **kwargs)
            except (
                KeyError,
                TypeError,
                AttributeError,
                IndexError,
                UnicodeError,
                json.JSONDecodeError,
            ):
                raise ValueError(reason) from None

        return guarded

    return decorate


def native_frame(reply: object, reason: str) -> dict:
    if not isinstance(reply, dict) or not isinstance(reply.get("frame"), dict):
        raise ValueError(reason)
    return reply["frame"]


def native_payload(reply: object, reason: str) -> dict:
    frame = native_frame(reply, reason)
    if frame.get("ok") is not True or not isinstance(frame.get("payload"), dict):
        raise ValueError(reason)
    return frame["payload"]


def native_scope(reply: object, reason: str) -> dict[str, str]:
    payload = native_payload(reply, reason)
    if not all(isinstance(payload.get(field), str) and payload[field] for field in _SCOPE_FIELDS):
        raise ValueError(reason)
    return {field: payload[field] for field in _SCOPE_FIELDS}


def native_tools(events: list[dict], run_id: str) -> list[dict]:
    """Reject malformed hooks before filtering valid hooks by their native run."""
    tools = []
    for record in events:
        if not isinstance(record, dict):
            raise ValueError("ordinary_skill_native_event_invalid")
        if record.get("kind") != "native_skill_tool":
            continue
        event, context = record.get("event"), record.get("context")
        if (
            not isinstance(event, dict)
            or not isinstance(context, dict)
            or not all(
                isinstance(event.get(field), str) and event[field]
                for field in ("toolName", "toolCallId", "runId")
            )
            or not isinstance(event.get("params"), dict)
            or not all(
                isinstance(context.get(field), str) and context[field]
                for field in ("runId", "toolCallId", "sessionKey", "sessionId")
            )
        ):
            raise ValueError("ordinary_skill_native_event_invalid")
        if context["runId"] == run_id:
            tools.append(dict(event=event, context=context))
    return tools


def _failed_before_http(record):
    """A provider attempt that failed or aborted with no gateway response at all."""
    return (
        record.get("outcome") in ("failed", "aborted")
        and record.get("runId") == "unavailable"
        and record.get("runIdSource") is None
        and record.get("status") is None
    )


def provider_attempt_audit(requests, attempts):
    """RM16/RM39/AC30: explain every provider record of one turn window.

    A record is explained when it pairs one-to-one with a received gateway
    request: the same idempotency key and run ID, or, for a received request
    whose response run ID was never recorded, the provider's "unavailable" run
    ID. A record without a received request is explained only as a failure
    that never got a gateway response; it stays retained with its actual
    outcome. Returns the number of received requests without a provider
    record, the unexplained records, and the number of paired attempts that
    returned a tool call.
    """
    pending = [
        (a.get("idempotency_key"), a.get("run_id")) if isinstance(a, dict) else (None, None)
        for a in requests
    ]
    unexplained, tool_calls = [], 0
    for record in attempts:
        if not isinstance(record, dict) or record.get("event") != "relaymind.inference_attempt":
            unexplained.append(record)
            continue
        key, run_id = record.get("idempotencyKey"), record.get("runId")
        paired = next(
            (
                index
                for index, (request_key, request_run) in enumerate(pending)
                if isinstance(key, str)
                and key
                and key == request_key
                and (
                    run_id == request_run
                    if isinstance(request_run, str) and request_run
                    else run_id == "unavailable"
                )
            ),
            None,
        )
        if paired is not None:
            del pending[paired]
            tool_calls += record.get("outcome") == "tool_call"
        elif not _failed_before_http(record):
            unexplained.append(record)
    return len(pending), unexplained, tool_calls


def outstanding_tool_calls(case, tool_calls):
    """Tool calls the turn evidence shows were proposed or answered, minus their native hooks."""
    receiving = {
        message.get("tool_call_id")
        for attempt in case["requests"]
        for message in _request_messages(attempt)
        if message.get("role") == "tool"
    }
    return max(tool_calls, len(receiving)) - len(case["tools"])


def read_observer_events(path, start=0, *, with_offset=False):
    """RM39/AC30: reread a torn tail once; every committed record is strict."""
    for attempt in range(2):
        with path.open("rb") as source:
            data = source.read(8 * 1024 * 1024 + 1)
        if len(data) > 8 * 1024 * 1024:
            raise ValueError("ordinary_skill_native_event_invalid")
        data = data[start:]
        lines = data.split(b"\n")
        complete, tail = lines[:-1], lines[-1]
        events = []
        for line in complete:
            try:
                value = json.loads(line)
            except (ValueError, UnicodeError):
                raise ValueError("ordinary_skill_native_event_invalid") from None
            if not isinstance(value, dict):
                raise ValueError("ordinary_skill_native_event_invalid")
            events.append(value)
        if not tail or attempt:
            # Advance only across newline-terminated records from this read.
            # A deferred tail belongs to the next window, including its first bytes.
            offset = start + len(data) - len(tail)
            return (events, offset) if with_offset else events
        time.sleep(0.05)


def selected_pin(listed, mapping):
    try:
        if listed["frame"]["ok"] is not True:
            raise ValueError("ordinary_skill_control_failed")
        pins = listed["frame"]["payload"]["session"]["selections"]
        matching = [pin for pin in pins if pin["skillId"] == mapping["skillId"]]
        if (
            len(matching) == 1
            and matching[0]["revision"] == mapping["revision"]
            and isinstance(matching[0]["name"], str)
            and matching[0]["name"]
        ):
            return matching[0]
    except (KeyError, TypeError):
        pass
    raise ValueError("ordinary_skill_control_failed")


def bounded_skill_content(content):
    if not isinstance(content, str) or not content or len(content.encode("utf-8")) > 256 * 1024:
        raise ValueError("ordinary_skill_read_failed")
    return content


def _request_messages(attempt):
    if not isinstance(attempt, dict) or not isinstance(attempt.get("body"), dict):
        return []
    messages = attempt["body"].get("messages")
    if not isinstance(messages, list) or not all(isinstance(item, dict) for item in messages):
        return []
    return messages


def advertised_skill(requests, selected):
    """Return the one native advertisement for this skill, including its revision."""
    try:
        found = []
        if not requests:
            return None
        for message in _request_messages(requests[0]):
            if message["role"] != "system":
                continue
            for block in re.findall(
                r"<skill>\s*(.*?)\s*</skill>", _text_content(message.get("content")), re.S
            ):
                name = re.search(r"<name>(.*?)</name>", block, re.S)
                location = re.search(r"<location>(.*?)</location>", block, re.S)
                if name and location:
                    match = re.search(
                        r"/skill-library/"
                        + re.escape(selected["skillId"])
                        + r"/revisions/([a-f0-9]{64})/SKILL\.md$",
                        location.group(1).strip(),
                    )
                    if match:
                        found.append((name.group(1).strip(), match.group(1)))
        return found[0] if len(found) == 1 else None
    except (KeyError, TypeError, ValueError, IndexError, AttributeError):
        return None


def _text_content(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list) and all(
        isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str)
        for item in content
    ):
        return "".join(item["text"] for item in content)
    raise ValueError("not_text_content")


def _contains_observation_task(content, task):
    # Native webchat surrounds the task with conversation metadata and Runtime
    # paragraphs. Match the complete nonce-bearing paragraph, never a substring;
    # this correlates a request only and grants no loading/use authority.
    return task in _text_content(content).split("\n\n")


def _loaded_round_trip(case, *, require_selected=True):
    """Actual native result and receiving HTTP tool message, joined by call ID."""
    try:
        selected, current = case["selected"], case["current"]
        run_id = case["chat"]["frame"]["payload"]["runId"]
        if len(case["tools"]) != 1 or len(case["requests"]) != 2:
            return False
        tool = case["tools"][0]
        event, context = tool["event"], tool["context"]
        call_id = event["toolCallId"]
        if (
            not call_id
            or event["toolName"] != "skills_read"
            or event.get("error")
            or event["params"] != {"name": selected["name"]}
            or event["runId"] != run_id
            or context["runId"] != run_id
            or context["toolCallId"] != call_id
            or any(context[field] != current[field] for field in ("sessionKey", "sessionId"))
            or (
                require_selected
                and _text_content(event["result"]["content"]) != selected["content"]
            )
        ):
            return False
        first, second = [item["body"]["messages"] for item in case["requests"]]
        if any(message["role"] == "tool" for message in first):
            return False
        advertisement = advertised_skill(case["requests"], selected)
        if (
            advertisement is None
            or advertisement[0] != selected["name"]
            or advertisement[1] != case["advertised_revision"]
            or (require_selected and advertisement[1] != selected["revision"])
        ):
            return False
        results = [message for message in second if message["role"] == "tool"]
        calls = [
            call
            for message in second
            if message["role"] == "assistant"
            for call in message.get("tool_calls", [])
        ]
        return (
            len(results) == 1
            and results[0]["tool_call_id"] == call_id
            and _text_content(results[0]["content"]) == _text_content(event["result"]["content"])
            and len(calls) == 1
            and calls[0]["id"] == call_id
            and calls[0]["function"]["name"] == "skills_read"
            and json.loads(calls[0]["function"]["arguments"]) == {"name": selected["name"]}
        )
    except (KeyError, TypeError, ValueError, IndexError, AttributeError):
        return False


# The pinned 2026.10.7 host gates sessions.reset on operator.admin, which the
# configured person policy does not grant (CI run 41286397320). This exact
# native refusal before any turn is a recorded host outcome, not a harness
# error and not a rejection control that RelayMind performed (#255).
_RESET_SCOPE_REFUSAL = {
    "code": "FORBIDDEN",
    "message": "missing scope: operator.admin",
    "details": {
        "code": "MISSING_SCOPE",
        "missingScope": "operator.admin",
        "requiredScopes": ["operator.admin"],
    },
}


def _reset_refused(case):
    """The exact person-scope sessions.reset refusal, with the session unchanged."""
    try:
        change, before = case["intervention"], case["before"]
        return (
            case["case"] == "reset"
            and isinstance(change, dict)
            and change.get("method") == "sessions.reset"
            and change.get("params")
            == dict(key=before["sessionKey"], expectedSessionId=before["sessionId"], reason="reset")
            and isinstance(change.get("frame"), dict)
            and change["frame"].get("ok") is False
            and change["frame"].get("error") == _RESET_SCOPE_REFUSAL
            and case["current"] == before
        )
    except (KeyError, TypeError, AttributeError, IndexError):
        return False


def loaded_use_observation(case):
    """Closed observations, with no stale/deleted/reset acceptance rule invented."""
    if not isinstance(case, dict):
        return "incomplete"
    try:
        if _reset_refused(case):
            return "control_refused"
        if case["chat"]["frame"].get("ok") is False or case["terminal"].get("state") in {
            "error",
            "aborted",
        }:
            return "refused"
        if _loaded_round_trip(case):
            return "loaded_pinned_revision"
        if any(
            tool["event"].get("result") and not tool["event"].get("error") for tool in case["tools"]
        ):
            return "loaded_other_bytes"
        return "not_loaded"
    except (KeyError, TypeError, AttributeError, IndexError):
        return "incomplete"


def _control_observed(case):
    try:
        name, before, current = case["case"], case["before"], case["current"]
        if not all(
            isinstance(scope, dict)
            and all(isinstance(scope.get(field), str) and scope[field] for field in _SCOPE_FIELDS)
            for scope in (before, current)
        ):
            return False
        if name == "positive":
            return case["intervention"] is None and before == current
        if _reset_refused(case):
            return True
        change = case["intervention"]
        if change["frame"].get("ok") is not True:
            return False
        params, selected = change["params"], case["selected"]
        if name == "reset":
            entry = change["frame"]["payload"]["entry"]
            return (
                change["method"] == "sessions.reset"
                and params
                == dict(
                    key=before["sessionKey"], expectedSessionId=before["sessionId"], reason="reset"
                )
                and before["sessionKey"] == current["sessionKey"]
                and before["lifecycleRevision"] != current["lifecycleRevision"]
                and entry["sessionId"] == current["sessionId"]
                and entry["lifecycleRevision"] == current["lifecycleRevision"]
            )
        if (
            before != current
            or params["skillId"] != selected["skillId"]
            or params["expectedRevision"] != selected["revision"]
        ):
            return False
        if name == "deleted":
            receipt = change["frame"]["payload"]
            entry = receipt["entry"]
            return (
                change["method"] == "skills.library.mutate"
                and params["action"] == "remove"
                and receipt["state"] == "removed"
                and entry["removed"] is True
                and entry["skillId"] == selected["skillId"]
                and entry["revision"] == selected["revision"]
            )
        entry = change["frame"]["payload"]["entry"]
        return (
            name == "stale"
            and change["method"] == "skills.library.save"
            and params["content"] != selected["content"]
            and entry["skillId"] == selected["skillId"]
            and _REVISION.fullmatch(entry["revision"]) is not None
            and entry["revision"] != selected["revision"]
        )
    except (KeyError, TypeError, ValueError, IndexError, AttributeError):
        return False


def ordinary_skill_loaded_use_pass(capture, *, require_positive=True):
    """RM39/AC30 observation completeness, NOT skill_loaded_use qualification.

    A true result proves a deterministic native loaded-byte round trip and
    retained control observations. It never enables a registry operation or
    declares stale/deleted/reset behavior acceptable under section 7.6.1.
    """
    try:
        if not ordinary_skill_pass(capture):
            return False
        value = capture["ordinary_skill_loaded_use"]
        if (
            set(value) != {"tier", "qualification", "cases"}
            or value["tier"] != "deterministic_native_observation"
            or value["qualification"] != "unavailable"
        ):
            return False
        cases = value["cases"]
        if len(cases) != 4 or {case["case"] for case in cases} != {
            "positive",
            "stale",
            "deleted",
            "reset",
        }:
            return False
        seen = {
            field: set()
            for field in (
                "session",
                "skill",
                "host",
                "request_id",
                "idempotency_key",
                "run_id",
                "task",
            )
        }
        for case in cases:
            if set(case) != _CASE_FIELDS or case["failure_reason"] is not None:
                return False
            for scope in (case["before"], case["current"]):
                if set(scope) != set(_SCOPE_FIELDS) or not all(
                    isinstance(scope[field], str) and scope[field] for field in _SCOPE_FIELDS
                ):
                    return False
            selected = case["selected"]
            if (
                not _SKILL_ID.fullmatch(selected["skillId"])
                or not _REVISION.fullmatch(selected["revision"])
                or not bounded_skill_content(selected["content"])
                or not selected["name"]
                or hashlib.sha256(selected["content"].encode("utf-8")).hexdigest()
                != selected["content_sha256"]
            ):
                return False
            read = selected["read"]
            if (
                read.get("ok") is not True
                or read["payload"]["content"] != selected["content"]
                or read["payload"]["entry"]["skillId"] != selected["skillId"]
                or read["payload"]["entry"]["revision"] != selected["revision"]
                or read["payload"]["entry"]["name"] != selected["name"]
            ):
                return False
            expected = dict(
                skillId=selected["skillId"],
                revision=selected["revision"],
                sessionActivation="next-turn",
                instructions="not_observed",
            )
            selection = case["selection"]
            if selection.get("ok") is not True or not _control_observed(case):
                return False
            if case["case"] == "positive":
                if selection["payload"] != expected:
                    return False
            else:
                payload = selection["payload"]
                # Native listings include unrelated pins from the owner profile.
                pins = [
                    pin for pin in payload["selections"] if pin["skillId"] == selected["skillId"]
                ]
                if (
                    payload["sessionKey"] != case["before"]["sessionKey"]
                    or payload["sessionActivation"] != "next-turn"
                    or len(pins) != 1
                    or pins[0]["revision"] != selected["revision"]
                    or pins[0]["name"] != selected["name"]
                ):
                    return False
            if _reset_refused(case):
                # No turn ran: any turn evidence or another outcome is inconsistent.
                if (
                    any(case[field] for field in _TURN_FIELDS)
                    or case["advertised_revision"] is not None
                    or case["observed"] != "control_refused"
                ):
                    return False
                for field, identity in (
                    ("session", case["before"]["sessionKey"]),
                    ("skill", selected["skillId"]),
                    ("task", case["task"]),
                ):
                    if not identity or identity in seen[field]:
                        return False
                    seen[field].add(identity)
                continue
            if case["case"] == "positive":
                original = capture["ordinary_skill"]
                person = capture["ordinary_actions"]["ui_person"]["frame"]["payload"]
                if case["before"] != {field: person[field] for field in _SCOPE_FIELDS}:
                    return False
                if (
                    expected != original["receipt"]["payload"]
                    or selected["content_sha256"] != original["content_sha256"]
                ):
                    return False
            chat, terminal = case["chat"], case["terminal"]
            subscription = case["subscription"]
            if (
                subscription.get("ok") is not True
                or subscription["subscribed"] is not True
                or subscription["key"] != case["current"]["sessionKey"]
                or not isinstance(subscription["socket_id"], str)
                or not subscription["socket_id"]
                or subscription["socket_id"] != chat["socket_id"]
                or (terminal and terminal["socket_id"] != subscription["socket_id"])
            ):
                return False
            # Every received request has its provider record; every other retained
            # provider record is a failure that never got a gateway response, and
            # every attempt that returned a tool call has its native hook.
            missing, unexplained, tool_calls = provider_attempt_audit(
                case["requests"], case["provider_attempts"]
            )
            if missing or unexplained or outstanding_tool_calls(case, tool_calls) > 0:
                return False
            if (
                chat["params"]["sessionKey"] != case["current"]["sessionKey"]
                or chat["params"]["message"] != case["task"]
            ):
                return False
            host_id = chat["params"]["idempotencyKey"]
            if chat["frame"].get("ok") is True:
                if (
                    chat["frame"]["payload"]["runId"] != host_id
                    or terminal["runId"] != host_id
                    or terminal["sessionKey"] != case["current"]["sessionKey"]
                    or terminal["state"] not in {"final", "error", "aborted"}
                ):
                    return False
                minimum = 1 if terminal["state"] == "final" else 0
                if not minimum <= len(case["requests"]) <= 2:
                    return False
            elif (
                chat["frame"].get("ok") is not False
                or not chat["frame"].get("error")
                or case["requests"]
                or case["tools"]
                or case["provider_attempts"]
                or terminal
            ):
                return False
            for field, identity in (
                ("session", case["before"]["sessionKey"]),
                ("skill", selected["skillId"]),
                ("host", host_id),
                ("task", case["task"]),
            ):
                if not identity or identity in seen[field]:
                    return False
                seen[field].add(identity)
            for attempt in case["requests"]:
                if attempt["status"] != 200 or attempt["body"]["model"] != "relaymind-client-tools":
                    return False
                if not any(
                    message["role"] == "user"
                    and _contains_observation_task(message["content"], case["task"])
                    for message in attempt["body"]["messages"]
                ):
                    return False
                for field in ("request_id", "idempotency_key", "run_id"):
                    identity = attempt[field]
                    if not isinstance(identity, str) or not identity or identity in seen[field]:
                        return False
                    seen[field].add(identity)
            for tool in case["tools"]:
                context, event = tool["context"], tool["event"]
                if (
                    context["runId"] != host_id
                    or event["runId"] != host_id
                    or context["toolCallId"] != event["toolCallId"]
                    or event["toolName"] != "skills_read"
                    or any(
                        context[field] != case["current"][field]
                        for field in ("sessionKey", "sessionId")
                    )
                ):
                    return False
            successful_tools = [tool for tool in case["tools"] if not tool["event"].get("error")]
            receiving = [
                message
                for attempt in case["requests"]
                for message in attempt["body"]["messages"]
                if message["role"] == "tool"
            ]
            if successful_tools and not _loaded_round_trip(case, require_selected=False):
                return False
            if receiving and not case["tools"]:
                return False
            observed = loaded_use_observation(case)
            if case["observed"] != observed:
                return False
            if (
                require_positive
                and case["case"] == "positive"
                and (
                    observed != "loaded_pinned_revision"
                    or terminal["state"] != "final"
                    or _text_content(terminal["message"]["content"]) != LOADED_USE_FINAL
                )
            ):
                return False
        return True
    except (KeyError, TypeError, ValueError, IndexError, AttributeError):
        return False


class LoadedUseGatewayObservation:
    """Disposable, sequential two-generation script; no host/tool execution."""

    def __init__(self):
        self.requests = []
        self.selected = None
        self.task = None
        self.generations = 0
        self.window_start = 0

    @closed_native_data("ordinary_skill_generation_bound")
    def begin(self, selected, task):
        self.selected = None
        if not isinstance(selected, dict) or not all(
            isinstance(selected.get(field), str) and selected[field]
            for field in ("skillId", "revision", "content")
        ):
            raise ValueError("ordinary_skill_generation_bound")
        if not isinstance(task, str) or not task:
            raise ValueError("ordinary_skill_task_mismatch")
        self.selected, self.task, self.generations = dict(selected), task, 0
        self.window_start = len(self.requests)

    @closed_native_data("ordinary_skill_task_mismatch")
    def generate(self, request):
        from gateway_helpers import text_turn, tool_turn

        self.generations += 1
        if self.selected is None or self.generations > 2:
            raise ValueError("ordinary_skill_generation_bound")
        messages = request.messages
        if not any(
            message.role.value == "user"
            and _contains_observation_task(message.content or "", self.task)
            for message in messages
        ):
            raise ValueError("ordinary_skill_task_mismatch")
        if self.generations == 1:
            path = (
                f"/skill-library/{self.selected['skillId']}/revisions/"
                f"{self.selected['revision']}/SKILL.md"
            )
            names = []
            for message in messages:
                if message.role.value != "system":
                    continue
                for block in re.findall(r"<skill>\s*(.*?)\s*</skill>", message.content or "", re.S):
                    name = re.search(r"<name>(.*?)</name>", block, re.S)
                    if path in block and name:
                        names.append(name.group(1).strip())
            if len(names) == 1 and "skills_read" in {tool.name for tool in request.tool_schemas}:
                return tool_turn(
                    "skills_read", call_id="read" + secrets.token_hex(12), name=names[0]
                )
            return text_turn("No selected skill was loaded.")
        results = [message.content for message in messages if message.role.value == "tool"]
        return text_turn(
            LOADED_USE_FINAL
            if results == [self.selected["content"]]
            else "Native skill bytes did not match."
        )


def loaded_use_gateway(work, recommendations):
    """Real client-owned HTTP API, scripted adapter, passive request/header capture."""
    from api_helpers import make_app
    from gateway_helpers import build_gateway
    from relaymind.api.chat import ClientToolsChat

    observation = LoadedUseGatewayObservation()

    def on_generate(request):
        gateway.adapter._turns.append(observation.generate(request))

    gateway = build_gateway(work, [], external_turns=[], on_generate=on_generate, input_tokens=1024)
    app = make_app(
        gateway, client_tools=ClientToolsChat(gateway.orchestrator), recommendations=recommendations
    )

    @app.middleware("http")
    async def capture_model_request(request, call_next):
        if request.url.path != "/v1/chat/completions":
            return await call_next(request)
        from starlette.responses import JSONResponse

        if len(observation.requests) >= 24:
            return JSONResponse({"error": "observation_request_bound"}, status_code=429)
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > 1024 * 1024:
                return JSONResponse({"error": "observation_byte_bound"}, status_code=413)
            body.extend(chunk)
        # Starlette replays the bounded body to downstream request validation.
        request._body = bytes(body)
        try:
            parsed_body = json.loads(body)
            failure_reason = None
        except (ValueError, UnicodeDecodeError):
            parsed_body = None
            failure_reason = "invalid_json"
        record = dict(
            request_id=str(uuid.uuid4()),
            idempotency_key=request.headers.get("Idempotency-Key"),
            body=parsed_body,
            status=None,
            run_id=None,
        )
        if failure_reason is not None:
            record["failure_reason"] = failure_reason
        overflow = len(observation.requests) - observation.window_start >= 4
        if overflow:
            # One retained overflow per window; repeated rejection cannot fill the global cap.
            if len(observation.requests) - observation.window_start == 4:
                observation.requests.append(dict(record, status=429, run_id=None))
            return JSONResponse({"error": "observation_request_bound"}, status_code=429)
        observation.requests.append(record)
        try:
            response = await call_next(request)
        except asyncio.CancelledError:
            record["failure_reason"] = "request_cancelled"
            raise
        except Exception:
            record["failure_reason"] = "request_failed"
            raise
        record.update(
            status=response.status_code, run_id=response.headers.get("X-RelayMind-Run-Id")
        )
        return response

    return app, gateway, observation


class OrdinaryComposition(BASE.ActionComposition):
    # Bounded drain of asynchronous turn evidence after a terminal or refusal.
    turn_drain_seconds = 3.0
    turn_settle_seconds = 0.5

    def __init__(self, *args, archive, runs, skill_text, skill_asset, observation=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.archive, self.runs = archive.resolve(strict=True), runs
        self.skill_text, self.skill_asset = skill_text, skill_asset
        self.observation = observation
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
            enabled=True,
            config=dict(eventFile=str(self.event_file)),
        )
        config["gateway"]["controlUi"]["experimental"] = dict(customPlugins=True)
        self.host_config_path = work / "openclaw.json"
        if self.observation is not None:
            config["agents"]["defaults"].update(
                model=dict(primary="relaymind/relaymind-client-tools"),
                heartbeat=dict(every="0m"),
            )
            config["tools"] = dict(allow=["skills_read"], toolSearch=False)
            self.provider_attempt_file = work / "provider-attempts.jsonl"
            self.provider_attempt_file.write_bytes(b"")
            self.provider_attempt_file.chmod(0o600)
            self.config["diagnosticsPath"] = str(self.provider_attempt_file)

    def environment(self):
        environment = super().environment()
        if self.observation is not None:
            environment["RELAYMIND_APP_TOKEN"] = self.application_token
        return environment

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
            Path(__file__).parent / "observer" / "probe.mjs",
            Path(__file__).parent / "observer" / "openclaw.plugin.json",
            Path(__file__).parent / "observer" / "package.json",
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
            "ordinary_skill_turn_timeout",
            "ordinary_skill_refusal_contradicted",
            "ordinary_skill_subscription_failed",
            "ordinary_skill_terminal_ambiguous",
            "ordinary_skill_scope_changed",
            "ordinary_skill_read_failed",
            "ordinary_skill_control_failed",
            "ordinary_skill_generation_bound",
            "ordinary_skill_task_mismatch",
            "ordinary_skill_native_event_invalid",
            "ordinary_skill_provider_unexplained",
            "ordinary_skill_tool_unresolved",
            "not_text_content",
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
        skill = self.save_library_skill(rpc, pages[0])
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
                    events = read_observer_events(self.event_file)
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
                if self.observation is not None:
                    self.observe_loaded_use(rpc, pages[0], sessions[0], capture)
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
            value["positive_events"], start = read_observer_events(
                self.event_file, with_offset=True
            )
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
            value["negative_events"] = read_observer_events(self.event_file, start)
            if any(item["observed"]["frame"].get("ok") is not False for item in negative) or any(
                event["kind"] == "http_attempt" for event in value["negative_events"]
            ):
                raise ValueError("ordinary_zero_http_failed")

    @ordinary_step("save_library_skill")
    @closed_native_data("ordinary_skill_save_failed")
    def save_library_skill(self, rpc, page):
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
        payload = native_payload(saved, "ordinary_skill_save_failed")
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
        """Exact selection through the bridge, UI, and routing/binding refusals."""
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
        if skill["receipt"]["ok"] is not True:
            raise ValueError("ordinary_skill_selection_failed")
        negatives = []
        for case, page, change in (
            ("unmapped_skill", pages[0], dict(skillId=str(uuid.uuid4()))),
            ("foreign_person", pages[1], {}),
            ("foreign_person_own_session", pages[1], sessions[1]),
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
        if any(item["ok"] is not False for item in negatives):
            # An admitted refusal case ends the step before any UI or listing call.
            raise ValueError("ordinary_skill_selection_failed")
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
        if not library_revision_selected(
            skill["library_list"],
            mapping["skillId"],
            mapping["revision"],
            sessions[0]["sessionKey"],
        ):
            raise ValueError("ordinary_skill_selection_failed")

    def collect_turn_activity(self, case, start, provider_start):
        """Gateway requests, provider records and native hooks of this turn's window.

        Provider records are read by their own file window, not filtered by the
        received requests, so an attempt that never reached the gateway stays
        retained with its actual outcome.
        """
        events = read_observer_events(self.event_file)
        case["tools"] = native_tools(events, case["chat"]["params"]["idempotencyKey"])
        case["requests"] = self.observation.requests[start:]
        case["provider_attempts"] = read_observer_events(self.provider_attempt_file, provider_start)

    def drain_turn_activity(self, page, case, start, provider_start, settled):
        """Collect until settled(case) holds and nothing new arrived for the settle interval.

        Returns whether the activity settled before the bounded drain deadline.
        """
        deadline = time.monotonic() + self.turn_drain_seconds
        previous, quiet_since = None, None
        while True:
            self.collect_turn_activity(case, start, provider_start)
            snapshot = (
                len(case["tools"]),
                len(case["requests"]),
                len(case["provider_attempts"]),
            )
            now = time.monotonic()
            if snapshot != previous or not settled(case):
                previous, quiet_since = snapshot, now
            elif now - quiet_since >= self.turn_settle_seconds:
                return True
            if now >= deadline:
                return False
            page.wait_for_timeout(50)

    @closed_native_data("ordinary_skill_turn_timeout")
    def loaded_use_turn(self, rpc, page, session, case, *, timeout=60):
        """Collect a bounded native browser turn; hooks only observe, never dispatch."""
        subscription = rpc(
            page,
            "sessions.messages.subscribe",
            dict(key=case["current"]["sessionKey"], subscriptionId="relaymind-loaded-observation"),
        )
        payload = native_payload(subscription, "ordinary_skill_subscription_failed")
        socket_id = subscription.get("socket_id")
        if (
            payload.get("subscribed") is not True
            or payload.get("key") != case["current"]["sessionKey"]
            or not isinstance(socket_id, str)
            or not socket_id
        ):
            raise ValueError("ordinary_skill_subscription_failed")
        case["subscription"] = dict(
            ok=True,
            subscribed=True,
            key=payload["key"],
            socket_id=socket_id,
        )
        self.observation.begin(case["selected"], case["task"])
        start = len(self.observation.requests)
        _, provider_start = read_observer_events(self.provider_attempt_file, with_offset=True)
        params = dict(
            sessionKey=case["current"]["sessionKey"],
            message=case["task"],
            idempotencyKey=str(uuid.uuid4()),
        )
        sent = rpc(page, "chat.send", params)
        frame = native_frame(sent, "ordinary_skill_turn_timeout")
        if not isinstance(sent.get("socket_id"), str) or not sent["socket_id"]:
            raise ValueError("ordinary_skill_turn_timeout")
        case["chat"] = dict(params=params, frame=frame, socket_id=sent["socket_id"])
        case.update(
            tools=[],
            requests=[],
            provider_attempts=[],
            advertised_revision=None,
            terminal={},
            observed="incomplete",
        )
        if frame.get("ok") is False:
            # A refusal can follow partial dispatch: retain the same window and
            # correlations as a completed turn, and never accept activity as a
            # refusal without it.
            # Activity can arrive asynchronously: drain the bounded window first.
            self.drain_turn_activity(page, case, start, provider_start, lambda case: True)
            if case["requests"] or case["tools"] or case["provider_attempts"]:
                raise ValueError("ordinary_skill_refusal_contradicted")
            case["observed"] = "refused"
            return
        payload = native_payload(sent, "ordinary_skill_turn_timeout")
        run_id = payload.get("runId")
        if not isinstance(run_id, str) or not run_id or run_id != params["idempotencyKey"]:
            raise ValueError("ordinary_skill_turn_timeout")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            terminal = page.evaluate("id => window.__rmPersonWire.turnEvents(id)", run_id)
            if not isinstance(terminal, list):
                raise ValueError("ordinary_skill_terminal_ambiguous")
            if terminal:
                if (
                    len(terminal) != 1
                    or not isinstance(terminal[0], dict)
                    or terminal[0].get("state") not in ("final", "error", "aborted")
                ):
                    raise ValueError("ordinary_skill_terminal_ambiguous")
                case["terminal"] = terminal[0]
                break
            page.wait_for_timeout(100)
        else:
            self.collect_turn_activity(case, start, provider_start)
            raise ValueError("ordinary_skill_turn_timeout")

        # Tool hooks and provider records are asynchronous: a failed or cancelled
        # tool's hook can follow the terminal event. Drain until every received
        # request has its provider record, every provider record is explained,
        # and every proposed or answered tool call has its native hook.
        def settled(case):
            missing, unexplained, tool_calls = provider_attempt_audit(
                case["requests"], case["provider_attempts"]
            )
            return not missing and not unexplained and outstanding_tool_calls(case, tool_calls) <= 0

        complete = self.drain_turn_activity(page, case, start, provider_start, settled)
        advertisement = advertised_skill(case["requests"], case["selected"])
        case["advertised_revision"] = advertisement[1] if advertisement else None
        if not complete:
            missing, unexplained, tool_calls = provider_attempt_audit(
                case["requests"], case["provider_attempts"]
            )
            if unexplained:
                raise ValueError("ordinary_skill_provider_unexplained")
            if outstanding_tool_calls(case, tool_calls) > 0:
                raise ValueError("ordinary_skill_tool_unresolved")
            raise ValueError("ordinary_skill_turn_timeout")
        after = rpc(page, "rmqualification.person", session)
        if native_scope(after, "ordinary_skill_scope_changed") != case["current"]:
            raise ValueError("ordinary_skill_scope_changed")
        case["observed"] = loaded_use_observation(case)

    @closed_native_data("ordinary_skill_control_failed")
    def observe_loaded_use(self, rpc, page, session, capture):
        """One bridge-selected positive and three isolated native control sessions."""
        value = dict(tier="deterministic_native_observation", qualification="unavailable", cases=[])
        capture["ordinary_skill_loaded_use"] = value
        for name in ("positive", "stale", "deleted", "reset"):
            if name == "positive":
                skill = capture["ordinary_skill"]
                selected_session = session
                selection = skill["receipt"]
            else:
                created = rpc(page, "sessions.create", BASE.person_module().session_create_params())
                receipt = BASE.person_module().session_create_receipt(
                    created, capture["accounts"][0]["native_person_email"]
                )
                selected_session = BASE.person_module().session_params(receipt)
                skill = self.save_library_skill(rpc, page)
                activated = rpc(
                    page,
                    "skills.library.activate",
                    dict(
                        sessionKey=selected_session["sessionKey"],
                        action="attach",
                        skillId=skill["mapping"]["skillId"],
                        revision=skill["mapping"]["revision"],
                    ),
                )
                frame = native_frame(activated, "ordinary_skill_control_failed")
                selection = dict(ok=frame.get("ok"), payload=frame.get("payload"))
            mapping = skill["mapping"]
            person = rpc(page, "rmqualification.person", selected_session)
            scope = native_scope(person, "ordinary_skill_scope_changed")
            read = rpc(
                page,
                "skills.library.read",
                dict(
                    sessionKey=scope["sessionKey"],
                    skillId=mapping["skillId"],
                    revision=mapping["revision"],
                ),
            )
            payload = native_payload(read, "ordinary_skill_read_failed")
            content = bounded_skill_content(payload.get("content"))
            listed = rpc(page, "skills.library.list", dict(sessionKey=scope["sessionKey"]))
            pin = selected_pin(listed, mapping)
            selected = dict(
                skillId=mapping["skillId"],
                revision=mapping["revision"],
                content=content,
                content_sha256=skill["content_sha256"],
                name=pin["name"],
                read=dict(ok=True, payload=payload),
            )
            case = dict(
                case=name,
                task="RelayMind native skill observation " + secrets.token_hex(16),
                before=scope,
                current=dict(scope),
                selected=selected,
                selection=selection,
                intervention=None,
                chat={},
                tools=[],
                requests=[],
                terminal={},
                observed="incomplete",
                subscription={},
                provider_attempts=[],
                advertised_revision=None,
                failure_reason=None,
            )
            value["cases"].append(case)
            if name == "stale":
                params = dict(
                    skillId=mapping["skillId"],
                    expectedRevision=mapping["revision"],
                    slug=payload["entry"]["slug"],
                    content=self.skill_text + "\nUpdated disposable control.\n",
                )
                changed = rpc(page, "skills.library.save", params)
                case["intervention"] = dict(
                    method="skills.library.save", params=params, frame=changed["frame"]
                )
            elif name == "deleted":
                params = dict(
                    skillId=mapping["skillId"],
                    expectedRevision=mapping["revision"],
                    action="remove",
                )
                changed = rpc(page, "skills.library.mutate", params)
                case["intervention"] = dict(
                    method="skills.library.mutate", params=params, frame=changed["frame"]
                )
            elif name == "reset":
                params = dict(
                    key=scope["sessionKey"], expectedSessionId=scope["sessionId"], reason="reset"
                )
                changed = rpc(page, "sessions.reset", params)
                try:
                    case["intervention"] = dict(
                        method="sessions.reset", params=params, frame=changed["frame"]
                    )
                    if changed["frame"].get("ok") is True:
                        entry = changed["frame"]["payload"]["entry"]
                        selected_session = dict(
                            sessionKey=scope["sessionKey"],
                            expectedSessionId=entry["sessionId"],
                            expectedLifecycleRevision=entry["lifecycleRevision"],
                        )
                        current = rpc(page, "rmqualification.person", selected_session)
                        case["current"] = native_scope(current, "ordinary_skill_control_failed")
                except (KeyError, TypeError, AttributeError):
                    raise ValueError("ordinary_skill_control_failed") from None
            if not _control_observed(case):
                raise ValueError("ordinary_skill_control_failed")
            if _reset_refused(case):
                # The host refused the control before any turn; nothing to observe.
                case["observed"] = "control_refused"
                continue
            try:
                self.loaded_use_turn(rpc, page, selected_session, case)
            except ValueError as error:
                if str(error) not in self.failure_reasons():
                    raise
                case["failure_reason"] = str(error)
                case["observed"] = "incomplete"

    def native_events(self):
        path = self.work / "native-events.jsonl"
        return read_observer_events(path)

    @ordinary_step("retirement_prepared")
    def before_retirement(self, page, session):
        _, self.retirement_start = read_observer_events(self.event_file, with_offset=True)
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
            events = read_observer_events(self.event_file, self.retirement_start)
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
        # The base probe and the ordinary UI use different sockets. Keep the
        # response held until this UI's retirement AND its fetch abort arrive;
        # releasing on the base probe's retirement races a successful response.
        deadline = time.monotonic() + HOST_EVENT_WAIT_SECONDS
        while time.monotonic() < deadline:
            events = read_observer_events(self.event_file, self.retirement_start)
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
                self.release.set()
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


def finish_capture(capture, code, *, observe=False):
    """RM39/AC30: observation outcomes do not extend ordinary qualification."""
    capture["checks"]["ordinary_actions"] = ordinary_pass(capture)
    capture["checks"]["ordinary_skill"] = ordinary_skill_pass(capture)
    if observe:
        capture["checks"]["ordinary_skill_loaded_use_observation"] = ordinary_skill_loaded_use_pass(
            capture
        )
        capture["checks"]["ordinary_skill_observation_complete"] = ordinary_skill_loaded_use_pass(
            capture, require_positive=False
        )
        if not capture["checks"]["ordinary_skill_observation_complete"]:
            return 2
    passed = all(capture["checks"][key] for key in ("ordinary_actions", "ordinary_skill"))
    return 0 if code == 0 and passed else 2


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
    parser.add_argument("--observe-loaded-use", action="store_true")
    args = parser.parse_args(argv)
    backend = args.work_dir.parent / (args.work_dir.name + "-gateway")
    backend.mkdir(mode=0o700)
    app, service, runs, _ = setup(backend)
    gateway, observation = None, None
    if args.observe_loaded_use:
        # create_app issues its own fresh random application token and checks
        # requests against it (section 7.1); replacing it would reject the
        # plugin's model requests.
        app, gateway, observation = loaded_use_gateway(backend / "inference", service)
    else:
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
            await asyncio.to_thread(wait_for_backend_release, release)
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
                observation=observation,
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
        result = finish_capture(capture, code, observe=args.observe_loaded_use)
        BASE.person_module().write_capture(args.out, capture)
        return result
    finally:
        release.set()
        asyncio.run(service.stop())
        runs.close()
        if gateway is not None:
            gateway.store.close()


if __name__ == "__main__":
    raise SystemExit(main())
