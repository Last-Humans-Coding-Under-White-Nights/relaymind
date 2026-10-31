"""RM37/RM39 AC28/AC30: ordinary installed entry, default Gateway registration, real person UI."""

import asyncio
import hashlib
import importlib.util
import json
import re
import secrets
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from functools import wraps
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests"), str(ROOT / "scripts")]


def base_module():
    path = ROOT / "integrations/openclaw/test/host-action-qualification/qualify.py"
    spec = importlib.util.spec_from_file_location("native_action_component", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BASE = base_module()


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


LOADED_USE_FINAL = "Native skill observation complete."
_SCOPE_FIELDS = ("sessionKey", "sessionId", "lifecycleRevision")
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


def read_observer_events(path, start=0):
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
            return events
        time.sleep(0.05)


def selected_pin(listed, mapping):
    try:
        if listed["frame"]["ok"] is not True:
            raise ValueError("ordinary_skill_control_failed")
        pins = listed["frame"]["payload"]["session"]["selections"]
        matching = [
            pin
            for pin in pins
            if pin["skillId"] == mapping["skillId"] and pin["revision"] == mapping["revision"]
        ]
        if len(matching) == 1 and isinstance(matching[0]["name"], str) and matching[0]["name"]:
            return matching[0]
    except (KeyError, TypeError):
        pass
    raise ValueError("ordinary_skill_control_failed")


def bounded_skill_content(content):
    if not isinstance(content, str) or not content or len(content.encode("utf-8")) > 256 * 1024:
        raise ValueError("ordinary_skill_read_failed")
    return content


def advertised_skill(requests, selected):
    """Return the one native advertisement for this skill, including its revision."""
    found = []
    if not requests:
        return None
    for message in requests[0]["body"]["messages"]:
        if message["role"] != "system":
            continue
        for block in re.findall(r"<skill>\s*(.*?)\s*</skill>", message["content"], re.S):
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


def _text_content(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list) and all(
        isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str)
        for item in content
    ):
        return "".join(item["text"] for item in content)
    raise ValueError("not_text_content")


def _loaded_round_trip(case, *, require_selected=True):
    """Actual native result and receiving HTTP tool message, joined by call ID."""
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
        or (require_selected and _text_content(event["result"]["content"]) != selected["content"])
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


def loaded_use_observation(case):
    """Closed observations, with no stale/deleted/reset acceptance rule invented."""
    if case["chat"]["frame"]["ok"] is False or case["terminal"].get("state") in {
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


def _control_observed(case):
    name, before, current = case["case"], case["before"], case["current"]
    if name == "positive":
        return case["intervention"] is None and before == current
    change = case["intervention"]
    if change["frame"]["ok"] is not True:
        return False
    params, selected = change["params"], case["selected"]
    if name == "reset":
        entry = change["frame"]["payload"]["entry"]
        return (
            change["method"] == "sessions.reset"
            and params
            == dict(key=before["sessionKey"], expectedSessionId=before["sessionId"], reason="reset")
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
                read["ok"] is not True
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
            if selection["ok"] is not True or not _control_observed(case):
                return False
            if case["case"] == "positive":
                if selection["payload"] != expected:
                    return False
            else:
                payload = selection["payload"]
                pins = payload["selections"]
                if (
                    payload["sessionKey"] != case["before"]["sessionKey"]
                    or payload["sessionActivation"] != "next-turn"
                    or len(pins) != 1
                    or pins[0]["skillId"] != selected["skillId"]
                    or pins[0]["revision"] != selected["revision"]
                    or pins[0]["name"] != selected["name"]
                ):
                    return False
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
                subscription["ok"] is not True
                or subscription["subscribed"] is not True
                or subscription["key"] != case["current"]["sessionKey"]
                or not isinstance(subscription["socket_id"], str)
                or not subscription["socket_id"]
                or subscription["socket_id"] != chat["socket_id"]
                or (terminal and terminal["socket_id"] != subscription["socket_id"])
            ):
                return False
            receiver_pairs = [(a["idempotency_key"], a["run_id"]) for a in case["requests"]]
            provider_pairs = [(a["idempotencyKey"], a["runId"]) for a in case["provider_attempts"]]
            if (
                len(provider_pairs) != len(receiver_pairs)
                or sorted(provider_pairs) != sorted(receiver_pairs)
                or any(
                    a["event"] != "relaymind.inference_attempt" for a in case["provider_attempts"]
                )
            ):
                return False
            if (
                chat["params"]["sessionKey"] != case["current"]["sessionKey"]
                or chat["params"]["message"] != case["task"]
            ):
                return False
            host_id = chat["params"]["idempotencyKey"]
            if chat["frame"]["ok"] is True:
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
                chat["frame"]["ok"] is not False
                or not chat["frame"].get("error")
                or case["requests"]
                or case["tools"]
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
                    message["role"] == "user" and case["task"] == _text_content(message["content"])
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

    def begin(self, selected, task):
        self.selected, self.task, self.generations = dict(selected), task, 0
        self.window_start = len(self.requests)

    def generate(self, request):
        from gateway_helpers import text_turn, tool_turn

        self.generations += 1
        if self.selected is None or self.generations > 2:
            raise ValueError("ordinary_skill_generation_bound")
        messages = request.messages
        if not any(
            message.role.value == "user" and self.task == (message.content or "")
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
                self.advertised_name = names[0]
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
        record = dict(
            request_id=str(uuid.uuid4()),
            idempotency_key=request.headers.get("Idempotency-Key"),
            body=json.loads(body),
        )
        overflow = len(observation.requests) - observation.window_start >= 4
        if overflow:
            # One retained overflow per window; repeated rejection cannot fill the global cap.
            if len(observation.requests) - observation.window_start == 4:
                observation.requests.append(dict(record, status=429, run_id=None))
            return JSONResponse({"error": "observation_request_bound"}, status_code=429)
        observation.requests.append(record)
        response = await call_next(request)
        record.update(
            status=response.status_code, run_id=response.headers.get("X-RelayMind-Run-Id")
        )
        return response

    return app, gateway, observation


class OrdinaryComposition(BASE.ActionComposition):
    def __init__(self, *args, archive, runs, skill_text, skill_asset, observation=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.archive, self.runs = archive.resolve(strict=True), runs
        self.skill_text, self.skill_asset = skill_text, skill_asset
        self.observation = observation
        self._failure = {}

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
            if isinstance(error, TimeoutError) and str(error) == "overall_deadline_exceeded":
                raise
            if error_class.startswith("playwright_"):
                raise ValueError("ordinary_" + error_class) from None
            if isinstance(error, ValueError) and str(error) in (
                self.failure_reasons() | BASE.person_module().SAFE_REASONS
            ):
                raise
            raise ValueError("ordinary_step_failed") from None

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
        config["agents"]["defaults"].update(
            model=dict(primary="relaymind/relaymind-client-tools"),
            heartbeat=dict(every="0m"),
        )
        config["tools"] = dict(allow=["skills_read"], toolSearch=False)
        self.host_config_path = work / "openclaw.json"
        self.provider_attempt_file = work / "provider-attempts.jsonl"
        self.provider_attempt_file.write_bytes(b"")
        self.provider_attempt_file.chmod(0o600)
        if self.observation is not None:
            self.config["diagnosticsPath"] = str(self.provider_attempt_file)

    def environment(self):
        return dict(super().environment(), RELAYMIND_APP_TOKEN=self.application_token)

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
            "ordinary_zero_http_failed",
            "ordinary_abort_unobserved",
            "ordinary_observer_blind",
            "ordinary_skill_save_failed",
            "ordinary_skill_selection_failed",
            "ordinary_skill_turn_timeout",
            "ordinary_skill_subscription_failed",
            "ordinary_skill_terminal_ambiguous",
            "ordinary_skill_scope_changed",
            "ordinary_skill_read_failed",
            "ordinary_skill_control_failed",
            "ordinary_skill_generation_bound",
            "ordinary_skill_task_mismatch",
            "ordinary_skill_native_event_invalid",
        }

    def observe_backend(self):
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
            ui_page = pages[0].context.new_page()
            try:
                suffix = sessions[0]["sessionKey"].removeprefix("agent:main:")
                if "/" in suffix or not suffix.startswith("person-"):
                    raise ValueError("ordinary_guard_refused")
                ui_page.goto(self.origin + "/chat/main/" + suffix, wait_until="domcontentloaded")
                deadline = time.monotonic() + 20
                while not ui_page.evaluate("() => window.__rmPersonWire?.state().ready === true"):
                    if time.monotonic() >= deadline:
                        raise ValueError("ordinary_entry_unavailable")
                    ui_page.wait_for_timeout(100)
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
                value["ui"] = run_ui(ui_page, sessions[0], self.observe_backend)
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
            value["positive_events"] = read_observer_events(self.event_file)
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
            value["negative_events"] = read_observer_events(self.event_file, start)
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

    def loaded_use_turn(self, rpc, page, session, case, *, timeout=60):
        """Collect a bounded native browser turn; hooks only observe, never dispatch."""
        subscription = rpc(
            page,
            "sessions.messages.subscribe",
            dict(key=case["current"]["sessionKey"], subscriptionId="relaymind-loaded-observation"),
        )
        if (
            subscription["frame"].get("ok") is not True
            or subscription["frame"]["payload"].get("subscribed") is not True
            or subscription["frame"]["payload"].get("key") != case["current"]["sessionKey"]
        ):
            raise ValueError("ordinary_skill_subscription_failed")
        case["subscription"] = dict(
            ok=True,
            subscribed=True,
            key=subscription["frame"]["payload"]["key"],
            socket_id=subscription["socket_id"],
        )
        self.observation.begin(case["selected"], case["task"])
        start = len(self.observation.requests)
        params = dict(
            sessionKey=case["current"]["sessionKey"],
            message=case["task"],
            idempotencyKey=str(uuid.uuid4()),
        )
        sent = rpc(page, "chat.send", params)
        case["chat"] = dict(params=params, frame=sent["frame"], socket_id=sent["socket_id"])
        case.update(
            tools=[],
            requests=[],
            provider_attempts=[],
            advertised_revision=None,
            terminal={},
            observed="incomplete",
        )
        if sent["frame"].get("ok") is False:
            case["observed"] = "refused"
            return
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            terminal = page.evaluate(
                "id => window.__rmPersonWire.turnEvents(id)", sent["frame"]["payload"]["runId"]
            )
            if terminal:
                if len(terminal) != 1:
                    raise ValueError("ordinary_skill_terminal_ambiguous")
                case["terminal"] = terminal[0]
                break
            page.wait_for_timeout(100)
        else:
            events = read_observer_events(self.event_file)
            case["tools"] = [
                dict(event=event["event"], context=event["context"])
                for event in events
                if event.get("kind") == "native_skill_tool"
                and event.get("context", {}).get("runId") == params["idempotencyKey"]
            ]
            case["requests"] = self.observation.requests[start:]
            raise ValueError("ordinary_skill_turn_timeout")
        # Tool hooks are asynchronous; allow their bounded delivery to catch up
        # after the terminal event before reading the passive capture file.
        deadline = time.monotonic() + 3
        while True:
            events = read_observer_events(self.event_file)
            case["tools"] = [
                dict(event=event["event"], context=event["context"])
                for event in events
                if event.get("kind") == "native_skill_tool"
                and event.get("context", {}).get("runId") == params["idempotencyKey"]
            ]
            case["requests"] = self.observation.requests[start:]
            has_result = any(
                message["role"] == "tool"
                for attempt in case["requests"]
                for message in attempt["body"]["messages"]
            )
            case["provider_attempts"] = [
                record
                for record in read_observer_events(self.provider_attempt_file)
                if record.get("idempotencyKey") in {a["idempotency_key"] for a in case["requests"]}
            ]
            advertisement = advertised_skill(case["requests"], case["selected"])
            case["advertised_revision"] = advertisement[1] if advertisement else None
            if (case["tools"] or not has_result) and len(case["provider_attempts"]) == len(
                case["requests"]
            ):
                break
            if time.monotonic() >= deadline:
                raise ValueError("ordinary_skill_turn_timeout")
            page.wait_for_timeout(50)
        after = rpc(page, "rmqualification.person", session)
        if (
            after["frame"].get("ok") is not True
            or {field: after["frame"]["payload"][field] for field in _SCOPE_FIELDS}
            != case["current"]
        ):
            raise ValueError("ordinary_skill_scope_changed")
        case["observed"] = loaded_use_observation(case)

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
                skill = self.save_library_skill(rpc, page, selected_session)
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
                selection = dict(
                    ok=activated["frame"].get("ok"), payload=activated["frame"].get("payload")
                )
            mapping = skill["mapping"]
            person = rpc(page, "rmqualification.person", selected_session)
            if person["frame"].get("ok") is not True:
                raise ValueError("ordinary_skill_scope_changed")
            scope = {field: person["frame"]["payload"][field] for field in _SCOPE_FIELDS}
            read = rpc(
                page,
                "skills.library.read",
                dict(
                    sessionKey=scope["sessionKey"],
                    skillId=mapping["skillId"],
                    revision=mapping["revision"],
                ),
            )
            if read["frame"].get("ok") is not True:
                raise ValueError("ordinary_skill_read_failed")
            payload = read["frame"]["payload"]
            listed = rpc(page, "skills.library.list", dict(sessionKey=scope["sessionKey"]))
            pin = selected_pin(listed, mapping)
            selected = dict(
                skillId=mapping["skillId"],
                revision=mapping["revision"],
                content=bounded_skill_content(payload["content"]),
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
                    case["current"] = {
                        field: current["frame"]["payload"][field] for field in _SCOPE_FIELDS
                    }
            if not _control_observed(case):
                raise ValueError("ordinary_skill_control_failed")
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
        self.release.set()
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


def run_ui(page, session, observe):
    snapshots = {}
    # Real native navigation preserves the authenticated socket; no fabricated client.
    page.get_by_role("link", name="RelayMind recommendations", exact=True).click(timeout=10000)
    page.get_by_label("Task", exact=True).fill(
        "hello greeting ordinary native person " + secrets.token_hex(4)
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

    page.get_by_role("button", name="Recommend", exact=True).click()
    settle("recommend", lambda s: len(s["views"]) == 1 and s["views"][0]["state"] == "ready")
    page.get_by_role("button", name="Choose 1", exact=True).click()
    settle("choice", lambda s: s["views"][0]["state"] == "chosen")
    page.get_by_role("button", name="Correct to 1", exact=True).click()
    settle("correction", lambda s: len(s["records"]) == 1)
    page.get_by_role("button", name="Inspect feedback", exact=True).click()
    page.get_by_role("button", name="Delete feedback", exact=True).wait_for(timeout=10000)
    snapshots["inspect"] = observe()
    page.get_by_role("button", name="Delete feedback", exact=True).click()
    settle(
        "delete",
        lambda s: not s["records"] and any(a["operation"] == "delete" for a in s["audits"]),
    )
    page.once("dialog", lambda dialog: dialog.accept())
    page.get_by_role("button", name="Reset feedback", exact=True).click()
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
    parser.add_argument("--tarball", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--observe-loaded-use", action="store_true")
    args = parser.parse_args(argv)
    backend = args.work_dir.parent / (args.work_dir.name + "-gateway")
    backend.mkdir(mode=0o700)
    _, service, runs, _ = setup(backend)
    app, gateway, observation = loaded_use_gateway(backend / "inference", service)
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
                observation=observation if args.observe_loaded_use else None,
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
        gateway.store.close()


if __name__ == "__main__":
    raise SystemExit(main())
