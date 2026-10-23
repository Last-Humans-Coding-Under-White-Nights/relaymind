"""TraceEvent schema and write-time validation (spec section 8; RM22, RM25).

Section 8: TraceEvent carries schema_version=1, run_id, seq, timestamp,
elapsed_ms, event_type, actor, and payload, published as a versioned
JSON Schema and validated at the boundary. RM22 lists the required
event types; cached routes emit cache.hit with the originating version,
and every tool event includes its operation or proposal ID and its
originating model. RM16: model events and run.completed name the
models, so a completed run identifies every actual model call and the
final-answer model. RM25: timestamps are UTC. Part of issue #27.
Spec 0.6 (RM22, section 8): clarification.answered follows every resume
with input text, and both clarification events name the missing input.
"""

import copy

import jsonschema
import pytest

from helpers import ACCEPTED_PAYLOAD, TERMINAL_EVIDENCE
from relaymind.store.trace_event import (
    TRACE_EVENT_SCHEMA,
    EventType,
    TraceEvent,
    TraceEventError,
    validate_trace_event,
)

VALID = {
    "schema_version": 1,
    "run_id": "run_demo_001",
    "seq": 7,
    "timestamp": "2026-10-20T03:00:00Z",
    "elapsed_ms": 420,
    "event_type": "tool.proposed",
    "actor": "local",
    "payload": {
        "proposal_id": "proposal_01",
        "model": "qwen3-0.6b-q8_0",
        "tool_name": "get_service_health",
        "arguments": {"service": "staging-api"},
        "skill_id": "check-service-health",
    },
}

REQUIRED_EVENT_TYPES = {
    "run.accepted",
    "route.selected",
    "skill.loaded",
    "model.started",
    "model.finished",
    "tool.proposed",
    "tool.started",
    "tool.finished",
    "clarification.required",
    "clarification.answered",
    "confirmation.required",
    "handoff.started",
    "answer.delta",
    "run.completed",
    "run.failed",
    "run.cancelled",
    "cache.hit",
}


TOOL_EVENT_TYPES = ("tool.proposed", "tool.started", "tool.finished")

# Non-tool events with required payload keys, each with a valid payload
# (RM16, RM22, section 5).
REQUIRED_PAYLOAD = {
    "model.started": {"call_id": "call_1", "model": "qwen3"},
    "model.finished": {"call_id": "call_1", "model": "qwen3"},
    "run.completed": {"final_answer_model": "qwen3", **TERMINAL_EVIDENCE},
    "run.failed": {"error_code": "x", **TERMINAL_EVIDENCE},
    # Spec 0.17 section 8: every run.cancelled names its reason.
    "run.cancelled": {"reason": "cancel_requested", "active_ms": 0, "waiting_ms": 0},
    "cache.hit": {"originating_version": "v1"},
    # RM06: every load emits skill.loaded with the name, version, and hash.
    "skill.loaded": {
        "name": "check-service-health",
        "version": "1.0.0",
        "content_hash": "sha256:ab12",
    },
    # Spec 0.6 section 8: the clarification payloads name the input the
    # run waits for, and the question in the run's language.
    "clarification.required": {
        "question": "Which service?",
        "language": "en",
        "reason_code": "missing_input",
        "skill_id": "check-service-health",
        "missing": "service",
        "waiting_seconds": 300.0,
    },
    # Spec 0.6 section 8: the redacted resume input, the input it
    # answers, and the monotonic waiting time (RM25).
    "clarification.answered": {"input": "staging-api", "missing": "service", "waited_ms": 1200},
    # Spec 0.10 section 8 (RM11, AC05): the approval, the tool, the
    # proposal, the canonical redacted arguments, the target, the bound
    # run version, the UTC expiry, and the proposing model.
    "confirmation.required": {
        "approval_id": "approval_1",
        "tool_name": "delete_demo_database",
        "proposal_id": "proposal_1",
        "arguments": {"database_id": "demo-orders-db", "expected_revision": 3},
        "target": "demo-orders-db",
        "state_version": 4,
        "expires_at": "2026-10-20T03:05:00.000000Z",
        "model": "qwen3-0.6b-q8_0",
    },
    # Spec 0.9 section 8 (RM13): the RM04 reason, the external model, the
    # selected skill (or null), the completed operations the handoff
    # envelope carries, the allowed tools, and the measured request.
    "handoff.started": {
        "reason_code": "complex_skill",
        "model": "external-model",
        "skill": {
            "name": "investigate-performance",
            "version": "0.1.1",
            "content_hash": "sha256:ab12",
        },
        "operation_ids": ["op_1"],
        "allowed_tools": ["get_metrics", "get_recent_logs"],
        "request": {"size": 1200, "limit": 32768, "method": "utf8_bytes"},
    },
}

# A non-blank string in the published schema: at least one non-space character.
TEXT = {"type": "string", "pattern": "\\S"}

# Payload keys whose published schema is not plain non-blank text.
# Spec 0.6 section 8: clarification.required carries the clarify
# decision's skill_id (or null) and the section 11 waiting limit.
TERMINAL_FIELDS = {
    "model_calls": {"type": "array", "items": {"$ref": "#/$defs/model_call"}},
    **{
        key: {"type": "integer", "minimum": 0}
        for key in ("active_ms", "waiting_ms", "external_calls")
    },
}
# Spec 0.31 (RM22): a gateway-made tool call has no originating model; the schema
# makes the model nullable and a second rule requires text for every other actor.
NULLABLE_MODEL = {"model": {"type": ["string", "null"], "pattern": "\\S"}}
NON_TEXT_PAYLOAD = {
    **dict.fromkeys(("tool.proposed", "tool.started", "tool.finished"), NULLABLE_MODEL),
    "run.completed": TERMINAL_FIELDS,
    "run.failed": TERMINAL_FIELDS,
    # Spec 0.17 section 8: run.cancelled names one of two reasons.
    "run.cancelled": {**TERMINAL_FIELDS, "reason": {"enum": ["user_denied", "cancel_requested"]}},
    "clarification.required": {
        "skill_id": {"type": ["string", "null"], "pattern": "\\S"},
        "waiting_seconds": {"type": "number", "exclusiveMinimum": 0},
    },
    "clarification.answered": {"waited_ms": {"type": "integer", "minimum": 0}},
    # Spec 0.10 section 8 (RM11): the canonical arguments, the bound run
    # version, and the UTC expiry.
    "confirmation.required": {
        "arguments": {"type": "object"},
        "state_version": {"type": "integer", "minimum": 1},
        "expires_at": TRACE_EVENT_SCHEMA["properties"]["timestamp"],
    },
    "handoff.started": {
        "skill": {
            "type": ["object", "null"],
            "properties": {key: TEXT for key in ("name", "version", "content_hash")},
            "required": ["name", "version", "content_hash"],
            "additionalProperties": False,
        },
        "operation_ids": {"type": "array", "items": TEXT},
        "allowed_tools": {"type": "array", "items": TEXT},
        "request": {
            "type": "object",
            "properties": {
                "size": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1},
                "method": {"enum": ["tokenizer", "utf8_bytes"]},
            },
            "required": ["size", "limit", "method"],
            "additionalProperties": False,
        },
    },
}


def with_changes(**changes):
    data = copy.deepcopy(VALID)
    data.update(changes)
    return data


def test_the_section_8_example_validates_into_a_typed_event():
    event = validate_trace_event(VALID)
    assert event == TraceEvent(
        run_id="run_demo_001",
        seq=7,
        timestamp="2026-10-20T03:00:00Z",
        elapsed_ms=420,
        event_type=EventType.TOOL_PROPOSED,
        actor="local",
        payload=VALID["payload"],
    )
    assert event.schema_version == 1
    assert event.to_json_object() == VALID


def test_event_types_are_exactly_the_rm22_list():
    assert {member.value for member in EventType} == REQUIRED_EVENT_TYPES


def test_published_schema_matches_the_validator():
    props = TRACE_EVENT_SCHEMA["properties"]
    assert set(TRACE_EVENT_SCHEMA["required"]) == set(VALID)
    assert TRACE_EVENT_SCHEMA["additionalProperties"] is False
    assert props["schema_version"] == {"const": 1}
    assert set(props["event_type"]["enum"]) == REQUIRED_EVENT_TYPES


@pytest.mark.parametrize("field", sorted(VALID))
def test_a_missing_field_is_rejected(field):
    data = copy.deepcopy(VALID)
    del data[field]
    with pytest.raises(TraceEventError, match=field):
        validate_trace_event(data)


def test_unknown_fields_are_rejected():
    with pytest.raises(TraceEventError, match="unknown fields: reasoning"):
        validate_trace_event(with_changes(reasoning="hidden"))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", 2),
        ("schema_version", True),
        ("run_id", ""),
        ("run_id", 5),
        ("seq", 0),
        ("seq", 1.5),
        ("seq", True),
        ("elapsed_ms", -1),
        ("elapsed_ms", "420"),
        ("event_type", "tool.executed"),
        ("actor", " "),
        ("payload", []),
    ],
)
def test_bad_values_are_rejected(field, value):
    with pytest.raises(TraceEventError, match=field):
        validate_trace_event(with_changes(**{field: value}))


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-10-20T03:00:00",  # no zone
        "2026-10-20T03:00:00+08:00",  # not UTC
        "2026-10-20 03:00:00Z",
        "2026-13-20T03:00:00Z",  # not a date
        "2026-10-20T24:00:00Z",  # hour 24 is not an RFC 3339 time
        "yesterday",
    ],
)
def test_timestamps_must_be_utc(timestamp):
    """RM25: event timestamps are UTC."""
    with pytest.raises(TraceEventError, match="timestamp"):
        validate_trace_event(with_changes(timestamp=timestamp))


def test_fractional_utc_timestamps_are_accepted():
    event = validate_trace_event(with_changes(timestamp="2026-10-20T03:00:00.123456Z"))
    assert event.timestamp == "2026-10-20T03:00:00.123456Z"


@pytest.mark.parametrize("event_type", TOOL_EVENT_TYPES)
def test_tool_events_need_an_operation_or_proposal_id(event_type):
    """RM22: every tool event includes its operation or proposal ID."""
    payload = {"model": "qwen3-0.6b-q8_0", "tool_name": "get_service_health"}
    with pytest.raises(TraceEventError, match="operation_id or proposal_id"):
        validate_trace_event(with_changes(event_type=event_type, payload=payload))
    for key in ("operation_id", "proposal_id"):
        validate_trace_event(with_changes(event_type=event_type, payload={**payload, key: "op_1"}))


@pytest.mark.parametrize("event_type", TOOL_EVENT_TYPES)
def test_tool_events_need_the_originating_model(event_type):
    """RM22: every tool event includes its originating model."""
    payload = {"operation_id": "op_1", "tool_name": "get_service_health"}
    with pytest.raises(TraceEventError, match="model"):
        validate_trace_event(with_changes(event_type=event_type, payload=payload))


@pytest.mark.parametrize(
    ("event_type", "payload"),
    list(REQUIRED_PAYLOAD.items()),
    ids=list(REQUIRED_PAYLOAD),
)
def test_events_carry_their_required_payload_keys(event_type, payload):
    """Each required key, when missing, is rejected by name.

    RM16: model events name the call and the model, and run.completed
    the final-answer model. Section 5: a failed run retains an error
    code. RM22: cached routes emit cache.hit with the originating version.
    """
    validate_trace_event(with_changes(event_type=event_type, payload=payload))
    for missing in payload:
        incomplete = {key: value for key, value in payload.items() if key != missing}
        with pytest.raises(TraceEventError, match=missing):
            validate_trace_event(with_changes(event_type=event_type, payload=incomplete))


def test_the_published_schema_states_the_payload_rules():
    """The JSON Schema carries the same per-event payload requirements."""
    required = {}
    one_of_ids = {}
    for rule in TRACE_EVENT_SCHEMA["allOf"]:
        # run.accepted and the seq 1 rule are checked in their own test.
        if rule["if"]["properties"].get("event_type", {}).get("enum") in (None, ["run.accepted"]):
            continue
        payload = rule["then"]["properties"]["payload"]
        for event_type in rule["if"]["properties"]["event_type"]["enum"]:
            special = NON_TEXT_PAYLOAD.get(event_type, {})
            # An event type may have more than one rule (tool.finished
            # also states the recovered rule); their requirements combine.
            for key in payload.get("required", []):
                assert payload["properties"][key] == special.get(key, TEXT)
            required.setdefault(event_type, []).extend(payload.get("required", []))
            one_of_ids.setdefault(event_type, []).extend(
                alt["required"] for alt in payload.get("anyOf", [])
            )
    assert required == {
        **{event_type: ["model"] for event_type in TOOL_EVENT_TYPES},
        **{event_type: list(payload) for event_type, payload in REQUIRED_PAYLOAD.items()},
    }
    assert one_of_ids == {
        **{event_type: [["operation_id"], ["proposal_id"]] for event_type in TOOL_EVENT_TYPES},
        **{event_type: [] for event_type in REQUIRED_PAYLOAD},
    }


CLARIFICATION = with_changes(
    event_type="clarification.required", payload=REQUIRED_PAYLOAD["clarification.required"]
)


def clarification_with(**changes):
    data = copy.deepcopy(CLARIFICATION)
    data["payload"].update(changes)
    return data


def test_a_clarification_without_a_chosen_skill_has_a_null_skill_id():
    """Spec 0.6 section 8: skill_id is the clarify decision's skill_id, or null."""
    event = validate_trace_event(clarification_with(skill_id=None, missing="skill_selection"))
    assert event.payload["skill_id"] is None


@pytest.mark.parametrize("seconds", [300, 0.5])
def test_the_clarification_waiting_limit_is_any_positive_number(seconds):
    """Spec 0.6 section 8: waiting_seconds is the section 11 waiting limit in force."""
    validate_trace_event(clarification_with(waiting_seconds=seconds))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("skill_id", ""),
        ("skill_id", " "),
        ("skill_id", 5),
        ("waiting_seconds", None),
        ("waiting_seconds", 0),
        ("waiting_seconds", -1.0),
        ("waiting_seconds", True),
        ("waiting_seconds", "300"),
        # RM03: a limit is finite; infinity would be no limit (review 61 #4335042335).
        ("waiting_seconds", float("inf")),
        ("waiting_seconds", float("nan")),
    ],
)
def test_clarification_skill_and_waiting_values_are_checked(field, value):
    """Spec 0.6 section 8: skill_id is non-blank text or null, and
    waiting_seconds is a positive number."""
    with pytest.raises(TraceEventError, match=field):
        validate_trace_event(clarification_with(**{field: value}))


ANSWERED = with_changes(
    event_type="clarification.answered", payload=REQUIRED_PAYLOAD["clarification.answered"]
)


def answered_with(**changes):
    data = copy.deepcopy(ANSWERED)
    data["payload"].update(changes)
    return data


def test_a_resume_answered_at_once_waited_zero_ms():
    """Spec 0.6 section 8, RM25: waited_ms is a non-negative integer, so 0 is valid."""
    event = validate_trace_event(answered_with(waited_ms=0))
    assert event.payload["waited_ms"] == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("input", ""),
        ("input", " "),
        ("input", None),
        ("input", 5),
        ("waited_ms", None),
        ("waited_ms", -1),
        ("waited_ms", 1.5),
        ("waited_ms", True),
        ("waited_ms", "1200"),
    ],
)
def test_clarification_answered_input_and_waiting_values_are_checked(field, value):
    """Spec 0.6 section 8: input is non-blank (redacted) text, and waited_ms
    is a non-negative integer (RM25)."""
    with pytest.raises(TraceEventError, match=field):
        validate_trace_event(answered_with(**{field: value}))


# -- run.accepted (spec section 8 and RM22) ----------------------------------

EXECUTION_MODES = ["auto", "local_only", "external_only", "parallel_demo"]

ACCEPTED = with_changes(seq=1, event_type="run.accepted", actor="gateway", payload=ACCEPTED_PAYLOAD)


def accepted_with(**changes):
    data = copy.deepcopy(ACCEPTED)
    data["payload"].update(changes)
    return data


def test_a_run_accepted_event_with_the_section_8_payload_validates():
    """Section 8: the run.accepted payload as the spec defines it."""
    event = validate_trace_event(ACCEPTED)
    assert event.event_type is EventType.RUN_ACCEPTED
    assert event.payload == ACCEPTED_PAYLOAD


@pytest.mark.parametrize("mode", EXECUTION_MODES)
def test_run_accepted_records_any_native_execution_mode(mode):
    """Section 7.2: chat and native runs share one execution mode enumeration."""
    validate_trace_event(
        accepted_with(requested_execution_mode=mode, effective_execution_mode=mode)
    )


MODEL_ALIASES = ["relaymind", "relaymind-client-tools", None]


@pytest.mark.parametrize("alias", MODEL_ALIASES)
def test_run_accepted_records_the_model_alias(alias):
    """Section 8 (spec 0.6): model_alias is the chat alias, or null for a native run."""
    event = validate_trace_event(accepted_with(model_alias=alias))
    assert event.payload["model_alias"] == alias


def test_run_accepted_may_offer_no_skills():
    """Section 8: skills_offered is empty for the client-owned alias."""
    validate_trace_event(accepted_with(skills_offered=[], skill_roots=[]))


@pytest.mark.parametrize("missing", sorted(ACCEPTED_PAYLOAD))
def test_run_accepted_needs_every_section_8_payload_field(missing):
    """Section 8, RM03: the requested and effective mode, budgets, and catalog are recorded."""
    data = copy.deepcopy(ACCEPTED)
    del data["payload"][missing]
    with pytest.raises(TraceEventError, match=missing):
        validate_trace_event(data)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("requested_execution_mode", "fast"),
        ("requested_execution_mode", ""),
        ("effective_execution_mode", None),
        ("effective_execution_mode", "fast"),
        ("model_alias", "gpt-4o"),
        ("model_alias", ""),
        ("model_alias", ["relaymind"]),
        ("budgets", None),
        ("budgets", [8, 6]),
        ("skill_roots", "/srv/relaymind/skills"),
        ("skill_roots", [""]),
        ("skill_roots", [3]),
        ("skills_offered", None),
        ("original_messages", None),
        ("original_messages", "Is staging-api healthy?"),
        ("original_messages", ["Is staging-api healthy?"]),
        ("original_messages", [{"role": "user"}]),
        ("original_messages", [{"role": "tool", "content": "x"}]),
        ("original_messages", [{"role": "user", "content": None}]),
        ("original_messages", [{"role": "user", "content": "x", "extra": 1}]),
        ("skills_offered", ["check-service-health"]),
        ("skills_offered", [{"name": "check-service-health", "version": "1.0.0"}]),
        (
            "skills_offered",
            [{"name": "check-service-health", "version": "", "content_hash": "sha256:ab12"}],
        ),
    ],
)
def test_run_accepted_payload_values_are_checked(field, value):
    """Section 8: each run.accepted payload field has the defined shape."""
    with pytest.raises(TraceEventError, match=field):
        validate_trace_event(accepted_with(**{field: value}))


@pytest.mark.parametrize(
    "options",
    [
        {},
        {"max_completion_tokens": 64},
        {"max_completion_tokens": 64, "temperature": 0.2, "top_p": 0.9},
        {"temperature": 0, "top_p": 1},
    ],
)
def test_run_accepted_answer_options_validate(options):
    """RM22, spec 0.22 section 8: answer_options holds only the applied options."""
    validate_trace_event(accepted_with(answer_options=options))


def test_run_accepted_without_answer_options_is_a_run_stored_before_0_22():
    """Spec 0.22 section 8: the field is absent in older runs, which stay valid."""
    assert "answer_options" not in ACCEPTED_PAYLOAD
    validate_trace_event(ACCEPTED)


BAD_ANSWER_OPTIONS = [
    None,
    [],
    "64",
    {"max_tokens": 64},
    {"max_completion_tokens": 0},
    {"max_completion_tokens": True},
    {"max_completion_tokens": 1.5},
    {"temperature": "hot"},
    {"temperature": True},
    {"temperature": -1},
    {"top_p": 0},
    {"top_p": 1.5},
    {"top_p": None},
]


@pytest.mark.parametrize("options", BAD_ANSWER_OPTIONS)
def test_run_accepted_answer_options_are_checked(options):
    """RM22, spec 0.22 section 8: other keys and values are rejected by name."""
    with pytest.raises(TraceEventError, match="answer_options"):
        validate_trace_event(accepted_with(answer_options=options))


def test_the_published_schema_states_the_answer_options():
    """The JSON Schema carries answer_options as an optional, closed object."""
    payload = next(
        rule["then"]["properties"]["payload"]
        for rule in TRACE_EVENT_SCHEMA["allOf"]
        if rule["if"]["properties"].get("event_type", {}).get("enum") == ["run.accepted"]
    )
    assert "answer_options" not in payload["required"]
    options = payload["properties"]["answer_options"]
    assert options["type"] == "object"
    assert options["additionalProperties"] is False
    assert sorted(options["properties"]) == ["max_completion_tokens", "temperature", "top_p"]


def test_run_accepted_is_the_first_event_of_a_run():
    """RM22: run.accepted is the first event (seq 1), and seq 1 is only run.accepted."""
    with pytest.raises(TraceEventError, match="seq"):
        validate_trace_event({**copy.deepcopy(ACCEPTED), "seq": 2})
    with pytest.raises(TraceEventError, match="run.accepted"):
        validate_trace_event(with_changes(seq=1))


def test_the_published_schema_states_the_run_accepted_rules():
    """The JSON Schema carries the run.accepted payload and its seq 1 position."""
    rules = TRACE_EVENT_SCHEMA["allOf"]
    accepted = [
        rule
        for rule in rules
        if rule["if"]["properties"].get("event_type", {}).get("enum") == ["run.accepted"]
    ]
    assert len(accepted) == 1
    then = accepted[0]["then"]["properties"]
    assert then["seq"] == {"const": 1}
    payload = then["payload"]
    assert sorted(payload["required"]) == sorted(ACCEPTED_PAYLOAD)
    props = payload["properties"]
    assert props["requested_execution_mode"] == {"enum": [*EXECUTION_MODES, None]}
    assert props["effective_execution_mode"] == {"enum": EXECUTION_MODES}
    assert props["model_alias"] == {"enum": MODEL_ALIASES}
    assert props["budgets"] == {"type": "object"}
    assert props["skill_roots"] == {"type": "array", "items": TEXT}
    messages = props["original_messages"]
    assert messages["type"] == "array"
    assert sorted(messages["items"]["required"]) == ["content", "role"]
    assert messages["items"]["additionalProperties"] is False
    offered = props["skills_offered"]
    assert offered["type"] == "array"
    assert sorted(offered["items"]["required"]) == ["content_hash", "name", "version"]
    for key in ("name", "version", "content_hash"):
        assert offered["items"]["properties"][key] == TEXT
    first = [rule for rule in rules if "seq" in rule["if"]["properties"]]
    assert len(first) == 1
    assert first[0]["if"] == {"properties": {"seq": {"const": 1}}, "required": ["seq"]}
    assert first[0]["then"] == {"properties": {"event_type": {"const": "run.accepted"}}}


# -- the validator and the published schema agree (section 8) ----------------

SCHEMA_VALIDATOR = jsonschema.Draft202012Validator(
    TRACE_EVENT_SCHEMA, format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER
)


def tool_event(**payload):
    return with_changes(payload={"model": "qwen3", **payload})


SAMPLE_EVENTS = {
    "section-8-example": VALID,
    "run-accepted": ACCEPTED,
    "operation-id-only": tool_event(operation_id="op_1"),
    "both-ids": tool_event(operation_id="op_1", proposal_id="p_1"),
    "fractional-timestamp": with_changes(timestamp="2026-10-20T03:00:00.123456Z"),
    **{
        f"{event_type}-payload": with_changes(event_type=event_type, payload=payload)
        for event_type, payload in REQUIRED_PAYLOAD.items()
    },
    # Invalid samples.
    "blank-run-id": with_changes(run_id=" "),
    "empty-run-id": with_changes(run_id=""),
    "blank-actor": with_changes(actor="\t"),
    "blank-model": tool_event(proposal_id="p_1", model=" "),
    "blank-proposal-id": tool_event(proposal_id=" "),
    "empty-operation-id-beside-a-proposal-id": tool_event(operation_id="", proposal_id="p_1"),
    "null-operation-id-beside-a-proposal-id": tool_event(operation_id=None, proposal_id="p_1"),
    "integer-operation-id": tool_event(operation_id=5, proposal_id="p_1"),
    "no-id": tool_event(),
    "blank-call-id": with_changes(
        event_type="model.started", payload={"call_id": " ", "model": "qwen3"}
    ),
    "blank-error-code": with_changes(event_type="run.failed", payload={"error_code": " "}),
    "blank-skill-root": accepted_with(skill_roots=[" "]),
    "blank-skill-version": accepted_with(
        skills_offered=[{"name": "n", "version": " ", "content_hash": "sha256:ab12"}]
    ),
    "hour-24": with_changes(timestamp="2026-10-20T24:00:00Z"),
    "second-60": with_changes(timestamp="2026-10-20T03:00:60Z"),
    "month-13": with_changes(timestamp="2026-13-20T03:00:00Z"),
    "tool-event-at-seq-1": with_changes(seq=1),
    "accepted-at-seq-2": {**copy.deepcopy(ACCEPTED), "seq": 2},
    "list-event-type": with_changes(event_type=["run.accepted"]),
    "list-execution-mode": accepted_with(requested_execution_mode=["auto"]),
    "native-run-alias": accepted_with(model_alias=None),
    "clarification-null-skill": clarification_with(skill_id=None),
    "clarification-blank-skill": clarification_with(skill_id=" "),
    "clarification-integer-wait": clarification_with(waiting_seconds=300),
    "clarification-zero-wait": clarification_with(waiting_seconds=0),
    "clarification-boolean-wait": clarification_with(waiting_seconds=True),
    "answered-zero-wait": answered_with(waited_ms=0),
    "answered-negative-wait": answered_with(waited_ms=-1),
    "answered-float-wait": answered_with(waited_ms=1.5),
    "answered-boolean-wait": answered_with(waited_ms=True),
    "answered-blank-input": answered_with(input=" "),
    "client-tools-alias": accepted_with(model_alias="relaymind-client-tools", skills_offered=[]),
    "unknown-alias": accepted_with(model_alias="gpt-4o"),
    "answer-options": accepted_with(
        answer_options={"max_completion_tokens": 64, "temperature": 0.2, "top_p": 0.9}
    ),
    "no-answer-options": accepted_with(answer_options={}),
    **{
        f"bad-answer-options-{i}": accepted_with(answer_options=o)
        for i, o in enumerate(BAD_ANSWER_OPTIONS)
    },
}


TIMING_FIELDS = ("external_calls", "active_ms", "waiting_ms")


def terminal_with(event_type, **changes):
    payload = {**REQUIRED_PAYLOAD[event_type], **changes}
    return with_changes(event_type=event_type, payload=payload)


@pytest.mark.parametrize("event_type", ["run.completed", "run.failed"])
def test_terminal_timing_fields_are_non_negative_integers(event_type):
    """Spec 0.6 section 8 / 12.1, RM16, RM25: run.completed and run.failed
    carry model_calls (an array of calls) and external_calls, active_ms,
    and waiting_ms as non-negative integers. A value of another shape is
    rejected by name; 0 is valid."""
    validate_trace_event(
        terminal_with(
            event_type,
            model_calls=[{"call_id": "c1", "model": "qwen3", "actor": "local", "purpose": "x"}],
            external_calls=0,
            active_ms=0,
            waiting_ms=0,
        )
    )
    for field in TIMING_FIELDS:
        for bad in (-1, 1.5, True, "3", None):
            with pytest.raises(TraceEventError, match=field):
                validate_trace_event(terminal_with(event_type, **{field: bad}))
    for bad in ("calls", {"call_id": "c1"}, [1]):
        with pytest.raises(TraceEventError, match="model_calls"):
            validate_trace_event(terminal_with(event_type, model_calls=bad))


SAMPLE_EVENTS.update(
    {
        "failed-with-timing": terminal_with(
            "run.failed", model_calls=[], external_calls=0, active_ms=12, waiting_ms=0
        ),
        "failed-negative-active": terminal_with("run.failed", active_ms=-1),
        "completed-float-waiting": terminal_with("run.completed", waiting_ms=1.5),
        "completed-boolean-external": terminal_with("run.completed", external_calls=True),
        "completed-text-model-calls": terminal_with("run.completed", model_calls="x"),
        "failed-scalar-model-call": terminal_with("run.failed", model_calls=[1]),
        "handoff-no-operations": terminal_with("handoff.started", operation_ids=[]),
        "handoff-blank-operation": terminal_with("handoff.started", operation_ids=[" "]),
        "handoff-text-operations": terminal_with("handoff.started", operation_ids="op_1"),
        "handoff-integer-operation": terminal_with("handoff.started", operation_ids=[5]),
        "handoff-blank-reason": terminal_with("handoff.started", reason_code=" "),
        "handoff-blank-model": terminal_with("handoff.started", model=" "),
        "handoff-no-skill": terminal_with("handoff.started", skill=None),
        "handoff-skill-text": terminal_with("handoff.started", skill="investigate-performance"),
        "handoff-skill-without-hash": terminal_with(
            "handoff.started", skill={"name": "a", "version": "1"}
        ),
        "handoff-skill-blank-version": terminal_with(
            "handoff.started", skill={"name": "a", "version": " ", "content_hash": "sha256:a"}
        ),
        "handoff-skill-extra-field": terminal_with(
            "handoff.started",
            skill={"name": "a", "version": "1", "content_hash": "sha256:a", "body": "x"},
        ),
        "handoff-no-tools": terminal_with("handoff.started", allowed_tools=[]),
        "handoff-text-tools": terminal_with("handoff.started", allowed_tools="get_metrics"),
        "handoff-blank-tool": terminal_with("handoff.started", allowed_tools=[" "]),
        "handoff-tokenizer-request": terminal_with(
            "handoff.started", request={"size": 0, "limit": 1, "method": "tokenizer"}
        ),
        "handoff-request-unknown-method": terminal_with(
            "handoff.started", request={"size": 1, "limit": 2, "method": "guess"}
        ),
        "handoff-request-negative-size": terminal_with(
            "handoff.started", request={"size": -1, "limit": 2, "method": "utf8_bytes"}
        ),
        "handoff-request-zero-limit": terminal_with(
            "handoff.started", request={"size": 0, "limit": 0, "method": "utf8_bytes"}
        ),
        "handoff-request-float-size": terminal_with(
            "handoff.started", request={"size": 1.5, "limit": 2, "method": "utf8_bytes"}
        ),
        "handoff-request-boolean-limit": terminal_with(
            "handoff.started", request={"size": 1, "limit": True, "method": "utf8_bytes"}
        ),
        "handoff-request-without-method": terminal_with(
            "handoff.started", request={"size": 1, "limit": 2}
        ),
        "handoff-request-extra-field": terminal_with(
            "handoff.started",
            request={"size": 1, "limit": 2, "method": "utf8_bytes", "tokens": 1},
        ),
        "handoff-request-text": terminal_with("handoff.started", request="1200"),
        "handoff-degraded": terminal_with(
            "handoff.started", degraded_mode=True, local_failure="local generation failed"
        ),
        "handoff-degraded-false": terminal_with(
            "handoff.started", degraded_mode=False, local_failure="x"
        ),
        "handoff-degraded-text": terminal_with(
            "handoff.started", degraded_mode="yes", local_failure="x"
        ),
        "handoff-degraded-no-cause": terminal_with("handoff.started", degraded_mode=True),
        "handoff-cause-without-flag": terminal_with("handoff.started", local_failure="x"),
        "handoff-degraded-blank-cause": terminal_with(
            "handoff.started", degraded_mode=True, local_failure=" "
        ),
    }
)


HANDOFF_ACCEPTED = ("handoff-no-skill", "handoff-no-tools", "handoff-tokenizer-request")


def cancelled_with(**changes):
    return terminal_with("run.cancelled", **changes)


def confirmation_with(**changes):
    return terminal_with("confirmation.required", **changes)


SAMPLE_EVENTS.update(
    {
        "confirmation-redacted-arguments": confirmation_with(
            arguments={"database_id": "[REDACTED]"}, target="[REDACTED]"
        ),
        "confirmation-text-arguments": confirmation_with(arguments="database_id=demo-orders-db"),
        "confirmation-zero-version": confirmation_with(state_version=0),
        "confirmation-float-version": confirmation_with(state_version=4.5),
        "confirmation-boolean-version": confirmation_with(state_version=True),
        "confirmation-text-version": confirmation_with(state_version="4"),
        "confirmation-local-expiry": confirmation_with(expires_at="2026-10-20T11:05:00+08:00"),
        "confirmation-word-expiry": confirmation_with(expires_at="in five minutes"),
        "confirmation-blank-proposal": confirmation_with(proposal_id=" "),
        "confirmation-blank-model": confirmation_with(model=" "),
        "confirmation-with-token": confirmation_with(token="plaintext-token"),
        "confirmation-with-token-hash": confirmation_with(token_hash="sha256:ab12"),
        "confirmation-extra-field": confirmation_with(reasoning="hidden"),
        "cancelled-denied": cancelled_with(reason="user_denied", approval_id="approval_1"),
        "cancelled-with-reason": cancelled_with(reason="user_denied"),
        "cancelled-blank-reason": cancelled_with(reason=" "),
        "cancelled-integer-reason": cancelled_with(reason=1),
        "cancelled-blank-approval": cancelled_with(reason="user_denied", approval_id=" "),
        "cancelled-approval-without-reason": with_changes(
            event_type="run.cancelled",
            payload={"approval_id": "approval_1", "active_ms": 0, "waiting_ms": 0},
        ),
        "cancelled-requested-with-approval": cancelled_with(
            reason="cancel_requested", approval_id="approval_1"
        ),
        "cancelled-approval-other-reason": cancelled_with(
            reason="cancelled", approval_id="approval_1"
        ),
        "cancelled-without-reason": with_changes(
            event_type="run.cancelled", payload={"active_ms": 0, "waiting_ms": 0}
        ),
        "cancelled-unknown-reason": cancelled_with(reason="user_cancelled"),
    }
)


def test_the_confirmation_required_payload_is_the_section_8_contract():
    """RM11, RM22, AC05, spec 0.10 section 8: confirmation.required names
    the approval, tool, proposal, canonical redacted arguments (an
    object), target, the bound run version (a positive integer), the
    UTC expiry, and the proposing model. A payload of another shape is
    rejected at write time, by the key it breaks."""
    validate_trace_event(SAMPLE_EVENTS["confirmation-redacted-arguments"])
    for name, key in (
        ("confirmation-text-arguments", "arguments"),
        ("confirmation-zero-version", "state_version"),
        ("confirmation-float-version", "state_version"),
        ("confirmation-boolean-version", "state_version"),
        ("confirmation-text-version", "state_version"),
        ("confirmation-local-expiry", "expires_at"),
        ("confirmation-word-expiry", "expires_at"),
        ("confirmation-blank-proposal", "proposal_id"),
        ("confirmation-blank-model", "model"),
    ):
        with pytest.raises(TraceEventError, match=key):
            validate_trace_event(SAMPLE_EVENTS[name])


@pytest.mark.parametrize("key", ["token", "token_hash", "reasoning"])
def test_the_confirmation_required_payload_never_carries_the_token(key):
    """RM11, RM12, spec 0.10 section 8: the payload is a closed set of
    fields, so neither the token nor its hash, nor any other field, is
    ever written to the trace."""
    name = {"token": "confirmation-with-token", "token_hash": "confirmation-with-token-hash"}
    data = SAMPLE_EVENTS[name.get(key, "confirmation-extra-field")]
    with pytest.raises(TraceEventError, match=f"unknown fields: {key}"):
        validate_trace_event(data)


def test_a_native_cancel_of_a_waiting_run_names_its_voided_approval():
    """RM11, RM17, RM21, spec 0.17 sections 7.4 and 8: a native cancel of a
    run in waiting_confirmation voids the pending approval, and its
    run.cancelled carries reason cancel_requested with that approval_id."""
    validate_trace_event(SAMPLE_EVENTS["cancelled-requested-with-approval"])


@pytest.mark.parametrize("name", ["cancelled-without-reason", "cancelled-unknown-reason"])
def test_a_run_cancelled_needs_reason_user_denied_or_cancel_requested(name):
    """RM17, RM21, RM22, AC11, AC12, spec 0.17 sections 7.4 and 8: every
    run.cancelled names its reason, user_denied for a denied confirmation
    or cancel_requested for a cancel request. A payload without reason,
    or with another value, is rejected at write time."""
    with pytest.raises(TraceEventError, match="reason"):
        validate_trace_event(SAMPLE_EVENTS[name])


def test_a_denied_confirmation_cancels_with_its_reason_and_approval():
    """RM11, RM17, RM22, AC05, spec 0.10 section 8: a denied confirmation
    carries reason user_denied and the approval_id. An approval_id without
    reason user_denied or cancel_requested is rejected."""
    for name in ("cancelled-denied", "cancelled-with-reason"):
        validate_trace_event(SAMPLE_EVENTS[name])
    for name, key in (
        ("cancelled-blank-reason", "reason"),
        ("cancelled-integer-reason", "reason"),
        ("cancelled-blank-approval", "approval_id"),
        ("cancelled-approval-without-reason", "reason"),
        ("cancelled-approval-other-reason", "reason"),
    ):
        with pytest.raises(TraceEventError, match=key):
            validate_trace_event(SAMPLE_EVENTS[name])


def test_the_handoff_started_payload_names_skill_tools_and_request():
    """RM13, RM14, RM22, AC02, section 8: handoff.started records the skill
    (name, version, content_hash) or null, the allowed tools, and the
    measured external request (size, limit, method). A payload of another
    shape is rejected at write time, by the key it breaks."""
    for name in HANDOFF_ACCEPTED:
        validate_trace_event(SAMPLE_EVENTS[name])
    for name, key in (
        ("handoff-skill-text", "skill"),
        ("handoff-skill-without-hash", "skill"),
        ("handoff-skill-blank-version", "skill"),
        ("handoff-skill-extra-field", "skill"),
        ("handoff-text-tools", "allowed_tools"),
        ("handoff-blank-tool", "allowed_tools"),
        ("handoff-request-unknown-method", "request"),
        ("handoff-request-negative-size", "request"),
        ("handoff-request-zero-limit", "request"),
        ("handoff-request-float-size", "request"),
        ("handoff-request-boolean-limit", "request"),
        ("handoff-request-without-method", "request"),
        ("handoff-request-extra-field", "request"),
        ("handoff-request-text", "request"),
    ):
        with pytest.raises(TraceEventError, match=key):
            validate_trace_event(SAMPLE_EVENTS[name])


def recovered_failed_with(**changes):
    payload = {
        "error_code": "interrupted",
        "interrupted_state": "local_running",
        "settled_operations": [{"operation_id": "op_1", "status": "unknown_outcome"}],
        **changes,
    }
    return terminal_with("run.failed", **payload)


def recovered_payload_without(key):
    data = recovered_failed_with()
    del data["payload"][key]
    return data


def finished_with(**payload):
    return with_changes(
        event_type="tool.finished", payload={"model": "qwen3", "operation_id": "op_1", **payload}
    )


SAMPLE_EVENTS.update(
    {
        "recovered-failed": recovered_failed_with(),
        "recovered-failed-nothing-settled": recovered_failed_with(settled_operations=[]),
        "recovered-failed-read-settled": recovered_failed_with(
            interrupted_state="waiting_confirmation",
            settled_operations=[
                {"operation_id": "op_1", "status": "unknown_outcome"},
                {"operation_id": "op_2", "status": "failed"},
            ],
        ),
        "recovered-failed-without-state": recovered_payload_without("interrupted_state"),
        "recovered-failed-without-operations": recovered_payload_without("settled_operations"),
        "recovered-failed-terminal-state": recovered_failed_with(interrupted_state="completed"),
        "recovered-failed-unknown-state": recovered_failed_with(interrupted_state="sleeping"),
        "recovered-failed-text-operations": recovered_failed_with(settled_operations="op_1"),
        "recovered-failed-succeeded-operation": recovered_failed_with(
            settled_operations=[{"operation_id": "op_1", "status": "succeeded"}]
        ),
        "recovered-failed-blank-operation": recovered_failed_with(
            settled_operations=[{"operation_id": " ", "status": "failed"}]
        ),
        "recovered-failed-operation-without-status": recovered_failed_with(
            settled_operations=[{"operation_id": "op_1"}]
        ),
        "recovered-failed-operation-extra-field": recovered_failed_with(
            settled_operations=[{"operation_id": "op_1", "status": "failed", "result": {}}]
        ),
        "failed-other-code-with-state": recovered_failed_with(error_code="internal_error"),
        "failed-other-code-with-operations": terminal_with(
            "run.failed", error_code="internal_error", settled_operations=[]
        ),
        "finished-recovered": finished_with(
            status="unknown_outcome", replayed=False, recovered=True
        ),
        "finished-not-recovered": finished_with(replayed=True, recovered=False),
        "finished-recovered-replayed": finished_with(replayed=True, recovered=True),
        "finished-recovered-without-replayed": finished_with(recovered=True),
        "finished-text-recovered": finished_with(replayed=False, recovered="yes"),
    }
)

RECOVERED_FAILED_ACCEPTED = (
    "recovered-failed",
    "recovered-failed-nothing-settled",
    "recovered-failed-read-settled",
)
RECOVERED_FAILED_REJECTED = (
    ("recovered-failed-without-state", "interrupted_state"),
    ("recovered-failed-without-operations", "settled_operations"),
    ("recovered-failed-terminal-state", "interrupted_state"),
    ("recovered-failed-unknown-state", "interrupted_state"),
    ("recovered-failed-text-operations", "settled_operations"),
    ("recovered-failed-succeeded-operation", "settled_operations"),
    ("recovered-failed-blank-operation", "settled_operations"),
    ("recovered-failed-operation-without-status", "settled_operations"),
    ("recovered-failed-operation-extra-field", "settled_operations"),
    ("failed-other-code-with-state", "interrupted_state"),
    ("failed-other-code-with-operations", "settled_operations"),
)


def test_a_recovered_run_failed_names_its_state_and_settled_operations():
    """RM10, RM17, RM22, AC12, spec sections 5 and 8: a run.failed with
    error_code interrupted names interrupted_state (the non-terminal
    state the run was in) and settled_operations (closed objects of a
    non-blank operation_id and a status of unknown_outcome or failed).
    Both fields are required with interrupted and allowed only with it.
    The validator and the published schema both reject other shapes."""
    for name in RECOVERED_FAILED_ACCEPTED:
        validate_trace_event(SAMPLE_EVENTS[name])
        assert SCHEMA_VALIDATOR.is_valid(SAMPLE_EVENTS[name])
    for name, key in RECOVERED_FAILED_REJECTED:
        with pytest.raises(TraceEventError, match=key):
            validate_trace_event(SAMPLE_EVENTS[name])
        assert not SCHEMA_VALIDATOR.is_valid(SAMPLE_EVENTS[name]), name


def test_a_recovered_tool_finished_is_never_a_replay():
    """RM10, RM22, AC12, spec sections 5 and 8: tool.finished declares
    recovered as a boolean, and a recovered result has replayed false.
    The validator and the published schema both reject other shapes."""
    for name in ("finished-recovered", "finished-not-recovered"):
        validate_trace_event(SAMPLE_EVENTS[name])
        assert SCHEMA_VALIDATOR.is_valid(SAMPLE_EVENTS[name])
    for name, key in (
        ("finished-recovered-replayed", "replayed"),
        ("finished-recovered-without-replayed", "replayed"),
        ("finished-text-recovered", "recovered"),
    ):
        with pytest.raises(TraceEventError, match=key):
            validate_trace_event(SAMPLE_EVENTS[name])
        assert not SCHEMA_VALIDATOR.is_valid(SAMPLE_EVENTS[name]), name


def test_a_degraded_handoff_names_its_cause():
    """Spec 0.13 sections 8 and 11: degraded_mode (true) and local_failure go together."""
    validate_trace_event(SAMPLE_EVENTS["handoff-degraded"])
    for name in (
        "handoff-degraded-false",
        "handoff-degraded-text",
        "handoff-degraded-no-cause",
        "handoff-cause-without-flag",
        "handoff-degraded-blank-cause",
    ):
        with pytest.raises(TraceEventError):
            validate_trace_event(SAMPLE_EVENTS[name])


def validator_accepts(data):
    try:
        validate_trace_event(data)
    except TraceEventError:
        return False
    return True


@pytest.mark.parametrize("name", list(SAMPLE_EVENTS))
def test_the_validator_and_the_published_schema_agree(name):
    """Section 8: the published JSON Schema and the validator are equivalent.

    Each sample is checked with jsonschema against trace_event.v1.json
    and with validate_trace_event; both accept it or both reject it.
    Non-blank text (run_id, actor, IDs, models, codes, skill roots and
    fields) means at least one non-space character in both, and an
    operation_id or proposal_id that is present must be non-blank even
    when the other ID is valid. Calendar validity beyond the pattern
    (for example 30 February) is the schema's date-time format.
    """
    data = SAMPLE_EVENTS[name]
    assert SCHEMA_VALIDATOR.is_valid(data) is validator_accepts(data)


def test_valid_samples_pass_the_published_schema():
    """Section 8: the valid samples, including the spec example, pass jsonschema."""
    for name in ("section-8-example", "run-accepted", "operation-id-only"):
        SCHEMA_VALIDATOR.validate(SAMPLE_EVENTS[name])


# -- Review fixes: coercion, schema conditions, sequences, exports ------------


def test_a_trace_event_built_with_a_plain_event_type_string_is_coerced():
    """Section 8, RM22: TraceEvent holds an EventType even when a caller
    passes the plain string, so to_json_object works; an unknown type is
    refused."""
    event = TraceEvent(
        run_id="run_1",
        seq=2,
        timestamp="2026-10-20T03:00:00Z",
        elapsed_ms=0,
        event_type="answer.delta",
        actor="local",
        payload={"text": "hi"},
    )
    assert event.event_type is EventType.ANSWER_DELTA
    assert event.to_json_object()["event_type"] == "answer.delta"
    with pytest.raises(ValueError):
        TraceEvent(
            run_id="run_1",
            seq=2,
            timestamp="2026-10-20T03:00:00Z",
            elapsed_ms=0,
            event_type="invented.event",
            actor="local",
        )


def test_every_event_type_condition_requires_the_event_type():
    """Section 8: in draft 2020-12 an if with only properties matches an
    object that lacks the property, so every event_type condition also
    requires event_type."""
    rules = [
        rule for rule in TRACE_EVENT_SCHEMA["allOf"] if "event_type" in rule["if"]["properties"]
    ]
    assert rules
    for rule in rules:
        assert rule["if"]["required"] == ["event_type"]


def test_run_accepted_takes_any_sequence_of_roots_and_offered_skills():
    """Section 8: skill_roots and skills_offered are arrays; a tuple from a
    Python caller is accepted and stored as a list. A string is not an
    array of roots."""
    skill = {"name": "n", "version": "1.0.0", "content_hash": "sha256:ab12"}
    event = validate_trace_event(
        accepted_with(skill_roots=("skills/", "team/"), skills_offered=(skill,))
    )
    assert event.payload["skill_roots"] == ["skills/", "team/"]
    assert event.payload["skills_offered"] == [skill]
    for value in ("skills/", b"skills/"):
        with pytest.raises(TraceEventError, match="skill_roots"):
            validate_trace_event(accepted_with(skill_roots=value))
    with pytest.raises(TraceEventError, match="skills_offered"):
        validate_trace_event(accepted_with(skills_offered="n"))


def test_the_store_package_exports_the_trace_event_api():
    """RM22: the trace event types are part of the store package API."""
    import relaymind.store as store

    for name in ("TraceEvent", "EventType", "TraceEventError", "validate_trace_event"):
        assert name in store.__all__
    assert store.TraceEvent is TraceEvent
    assert store.validate_trace_event is validate_trace_event


# -- Parallel workflow fields (spec 0.25 section 8; RM15, RM22, #30) -------------

WORKFLOW_SUMMARY = {
    "workflow_id": "health-and-diagnostic-checklist",
    "branch_wall_ms": 2000,
    "local_health_ms": 10,
    "external_checklist_ms": 2000,
    "extra_call_ms": 1500,
    "abandoned_calls": [],
}


def branch_event(event_type, **extra):
    payload = {
        **REQUIRED_PAYLOAD.get(event_type, {"model": "qwen3", "operation_id": "op_1"}),
        **extra,
    }
    return with_changes(event_type=event_type, payload=payload)


WORKFLOW_ACCEPTED = {
    "model-started-branch": branch_event("model.started", branch="local_health"),
    "model-finished-branch": branch_event("model.finished", branch="synthesis"),
    "tool-started-branch": branch_event("tool.started", branch="local_health"),
    "run-completed-workflow": branch_event("run.completed", workflow=WORKFLOW_SUMMARY),
    "run-failed-workflow": branch_event("run.failed", workflow=WORKFLOW_SUMMARY),
    "run-cancelled-workflow": branch_event(
        "run.cancelled", workflow={**WORKFLOW_SUMMARY, "extra_call_ms": None}
    ),
    "handoff-workflow": branch_event("handoff.started", workflow="health-and-diagnostic-checklist"),
}
WORKFLOW_REJECTED = {
    "unknown-branch": (branch_event("model.started", branch="left"), "branch"),
    "blank-branch": (branch_event("tool.finished", branch=" "), "branch"),
    "text-workflow-summary": (branch_event("run.completed", workflow="yes"), "workflow"),
    "negative-ms": (
        branch_event("run.completed", workflow={**WORKFLOW_SUMMARY, "branch_wall_ms": -1}),
        "branch_wall_ms",
    ),
    "missing-id": (
        branch_event(
            "run.completed",
            workflow={k: v for k, v in WORKFLOW_SUMMARY.items() if k != "workflow_id"},
        ),
        "workflow_id",
    ),
    "extra-field": (
        branch_event("run.completed", workflow={**WORKFLOW_SUMMARY, "guess": 1}),
        "guess",
    ),
    "blank-abandoned": (
        branch_event("run.completed", workflow={**WORKFLOW_SUMMARY, "abandoned_calls": [" "]}),
        "abandoned_calls",
    ),
    "blank-handoff-workflow": (branch_event("handoff.started", workflow=" "), "workflow"),
}


def test_workflow_fields_are_validated_and_published():
    """RM15, RM22, spec 0.25 section 8: branch and the workflow summary are checked.

    The validator and the published schema both accept the valid shapes
    and reject the others.
    """
    for name, data in WORKFLOW_ACCEPTED.items():
        validate_trace_event(data)
        assert SCHEMA_VALIDATOR.is_valid(data), name
    for name, (data, key) in WORKFLOW_REJECTED.items():
        with pytest.raises(TraceEventError, match=key):
            validate_trace_event(data)
        assert not SCHEMA_VALIDATOR.is_valid(data), name


# -- Model call fields (spec 0.31 section 8; RM16, RM22, RM25, RM27, #120) -------------------

FULL_USAGE = {"input_tokens": 100, "output_tokens": 8, "cached_tokens": 64, "reasoning_tokens": 0}

MODEL_FIELDS_ACCEPTED = {
    "started-request-options": branch_event(
        "model.started", request_options={"thinking": {"type": "disabled"}}
    ),
    "started-empty-request-options": branch_event("model.started", request_options={}),
    "finished-first-token": branch_event("model.finished", first_token_ms=420),
    "finished-first-token-zero": branch_event("model.finished", first_token_ms=0),
    "finished-first-token-null": branch_event("model.finished", first_token_ms=None),
    "finished-usage-null": branch_event("model.finished", usage=None),
    "finished-usage-full": branch_event("model.finished", usage=FULL_USAGE),
    "finished-usage-null-counts": branch_event(
        "model.finished",
        usage={**FULL_USAGE, "cached_tokens": None, "reasoning_tokens": None},
    ),
    "finished-usage-pre-0.31": branch_event(
        "model.finished",
        usage={"input_tokens": 1, "output_tokens": 1, "cached_tokens": 0},
    ),
    "finished-usage-bare": branch_event(
        "model.finished", usage={"input_tokens": 1, "output_tokens": 1}
    ),
}
MODEL_FIELDS_REJECTED = {
    "request-options-text": (
        branch_event("model.started", request_options="off"),
        "request_options",
    ),
    "request-options-null": (
        branch_event("model.started", request_options=None),
        "request_options",
    ),
    "request-options-list": (branch_event("model.started", request_options=[1]), "request_options"),
    "first-token-negative": (branch_event("model.finished", first_token_ms=-1), "first_token_ms"),
    "first-token-float": (branch_event("model.finished", first_token_ms=1.5), "first_token_ms"),
    "first-token-text": (branch_event("model.finished", first_token_ms="3"), "first_token_ms"),
    "first-token-bool": (branch_event("model.finished", first_token_ms=True), "first_token_ms"),
    "usage-text": (branch_event("model.finished", usage="many"), "usage"),
    "usage-missing-input": (
        branch_event("model.finished", usage={"output_tokens": 1}),
        "input_tokens",
    ),
    "usage-negative-cached": (
        branch_event("model.finished", usage={**FULL_USAGE, "cached_tokens": -1}),
        "cached_tokens",
    ),
    "usage-float-reasoning": (
        branch_event("model.finished", usage={**FULL_USAGE, "reasoning_tokens": 0.5}),
        "reasoning_tokens",
    ),
    "usage-extra-field": (
        branch_event("model.finished", usage={**FULL_USAGE, "guess": 1}),
        "guess",
    ),
}


def test_model_call_fields_are_validated_and_published():
    """RM16, RM22, RM27, spec 0.31 section 8: the validator and the schema agree on each shape."""
    for name, data in MODEL_FIELDS_ACCEPTED.items():
        validate_trace_event(data)
        assert SCHEMA_VALIDATOR.is_valid(data), name
    for name, (data, key) in MODEL_FIELDS_REJECTED.items():
        with pytest.raises(TraceEventError, match=key):
            validate_trace_event(data)
        assert not SCHEMA_VALIDATOR.is_valid(data), name


# -- Performance options and the applied answer pass (spec 0.32 section 8; RM31, RM32, #121) ---

PERFORMANCE_RECORD = {
    "answer_pass": "draft",
    "answer_stream": "buffered",
    "calls_per_generation": "many",
    "prefetch": True,
    "early_headers": True,
}

PERFORMANCE_ACCEPTED = {
    "accepted-performance-all": accepted_with(**PERFORMANCE_RECORD),
    "accepted-performance-defaults": accepted_with(
        answer_pass="final",
        answer_stream="buffered",
        calls_per_generation="one",
        prefetch=False,
        early_headers=False,
    ),
    "accepted-performance-live-label": accepted_with(answer_stream="live"),
    "accepted-before-0.31": ACCEPTED,
    "completed-draft": branch_event("run.completed", answer_pass_applied="draft"),
    "completed-final": branch_event("run.completed", answer_pass_applied="final"),
    "completed-before-0.32": branch_event("run.completed"),
}
PERFORMANCE_REJECTED = {
    "answer-pass-unknown": (accepted_with(answer_pass="later"), "answer_pass"),
    "answer-pass-null": (accepted_with(answer_pass=None), "answer_pass"),
    "answer-stream-unknown": (accepted_with(answer_stream="chunked"), "answer_stream"),
    "calls-unknown": (accepted_with(calls_per_generation="two"), "calls_per_generation"),
    "calls-number": (accepted_with(calls_per_generation=1), "calls_per_generation"),
    "prefetch-text": (accepted_with(prefetch="yes"), "prefetch"),
    "prefetch-number": (accepted_with(prefetch=1), "prefetch"),
    "early-headers-text": (accepted_with(early_headers="true"), "early_headers"),
    "early-headers-null": (accepted_with(early_headers=None), "early_headers"),
    "applied-unknown": (branch_event("run.completed", answer_pass_applied="both"), "answer_pass"),
    "applied-null": (branch_event("run.completed", answer_pass_applied=None), "answer_pass"),
    "applied-bool": (branch_event("run.completed", answer_pass_applied=True), "answer_pass"),
}


def test_performance_fields_are_validated_and_published():
    """RM31, RM32, spec 0.32 section 8: the validator and the schema agree on each shape.

    The fields are optional, because a run stored before spec 0.31 or 0.32 has none.
    """
    for name, data in PERFORMANCE_ACCEPTED.items():
        validate_trace_event(data)
        assert SCHEMA_VALIDATOR.is_valid(data), name
    for name, (data, key) in PERFORMANCE_REJECTED.items():
        with pytest.raises(TraceEventError, match=key):
            validate_trace_event(data)
        assert not SCHEMA_VALIDATOR.is_valid(data), name


# -- Gateway tool events and prefetch declarations (spec 0.32 sections 4.2, 8; RM22, RM29) ---

DECLARATIONS = {
    "check-service-health": [
        {"tool": "get_service_health", "arguments": {"service": {"from": "request"}}}
    ],
    "summarize-incident-counts": [],
    "investigate-performance": [
        {"tool": "get_latest_deployment", "arguments": {"service": {"from": "request"}}},
        {
            "tool": "get_recent_logs",
            "arguments": {"service": {"from": "request"}, "limit": {"constant": 50}},
        },
    ],
}


def gateway_tool_event(event_type, **extra):
    """A tool event of a gateway-made call: actor gateway, model null (RM22)."""
    data = branch_event(event_type, **{"model": None, **extra})
    data["actor"] = "gateway"
    return data


GATEWAY_ACCEPTED = {
    "proposed-gateway-null-model": gateway_tool_event(
        "tool.proposed", proposal_id="p_1", mechanism="prefetch"
    ),
    "started-gateway-null-model": gateway_tool_event("tool.started", mechanism="prefetch"),
    "finished-gateway-null-model": gateway_tool_event("tool.finished", mechanism="workflow"),
    "proposed-model-mechanism": branch_event("tool.proposed", proposal_id="p_1", mechanism="model"),
    "proposed-workflow-mechanism": branch_event(
        "tool.proposed", proposal_id="p_1", mechanism="workflow", step="read"
    ),
    "accepted-declarations": accepted_with(prefetch_declarations=DECLARATIONS),
    "accepted-no-skills-offered": accepted_with(prefetch_declarations={}),
}
GATEWAY_REJECTED = {
    "local-actor-null-model": (
        branch_event("tool.proposed", proposal_id="p_1", model=None),
        "model",
    ),
    # A null model needs the gateway-made marker, so a model-made call that lost its model
    # is still caught (RM22).
    "gateway-proposal-null-model-no-mechanism": (
        gateway_tool_event("tool.proposed", proposal_id="p_1"),
        "model",
    ),
    "gateway-proposal-null-model-mechanism-model": (
        gateway_tool_event("tool.proposed", proposal_id="p_1", mechanism="model"),
        "model",
    ),
    "gateway-started-null-model-no-mechanism": (gateway_tool_event("tool.started"), "model"),
    "gateway-finished-null-model-no-mechanism": (gateway_tool_event("tool.finished"), "model"),
    "started-mechanism-unknown": (
        gateway_tool_event("tool.started", mechanism="magic"),
        "mechanism",
    ),
    "gateway-blank-model": (
        gateway_tool_event("tool.started", model=" ", mechanism="prefetch"),
        "model",
    ),
    "gateway-missing-model": (
        {
            **gateway_tool_event("tool.started"),
            "payload": {
                "operation_id": "op_1",
                "tool_name": "get_service_health",
                "mechanism": "prefetch",
            },
        },
        "model",
    ),
    "mechanism-unknown": (
        branch_event("tool.proposed", proposal_id="p_1", mechanism="magic"),
        "mechanism",
    ),
    "mechanism-null": (
        branch_event("tool.proposed", proposal_id="p_1", mechanism=None),
        "mechanism",
    ),
    "workflow-without-step": (
        branch_event("tool.proposed", proposal_id="p_1", mechanism="workflow"),
        "step",
    ),
    "step-on-prefetch": (
        branch_event("tool.proposed", proposal_id="p_1", mechanism="prefetch", step="read"),
        "step",
    ),
    "declarations-list": (accepted_with(prefetch_declarations=[]), "prefetch_declarations"),
    "declarations-skill-not-list": (
        accepted_with(prefetch_declarations={"a-skill": {}}),
        "prefetch_declarations",
    ),
    "declarations-call-not-object": (
        accepted_with(prefetch_declarations={"a-skill": ["get_service_health"]}),
        "prefetch_declarations",
    ),
    "declarations-blank-tool": (
        accepted_with(prefetch_declarations={"a-skill": [{"tool": " ", "arguments": {}}]}),
        "prefetch_declarations",
    ),
    "declarations-arguments-list": (
        accepted_with(prefetch_declarations={"a-skill": [{"tool": "t", "arguments": []}]}),
        "prefetch_declarations",
    ),
    "declarations-unknown-field": (
        accepted_with(
            prefetch_declarations={"a-skill": [{"tool": "t", "arguments": {}, "guess": 1}]}
        ),
        "prefetch_declarations",
    ),
}


def test_gateway_tool_events_and_prefetch_declarations_are_validated_and_published():
    """RM22, RM29, spec 0.32 section 8: a gateway-made tool event has model null.

    The mechanism of a proposal and the run's prefetch declarations are
    optional, so events stored before 0.31 stay valid; the validator and
    the published schema agree on each shape.
    """
    for name, data in GATEWAY_ACCEPTED.items():
        validate_trace_event(data)
        assert SCHEMA_VALIDATOR.is_valid(data), name
    for name, (data, key) in GATEWAY_REJECTED.items():
        with pytest.raises(TraceEventError, match=key):
            validate_trace_event(data)
        assert not SCHEMA_VALIDATOR.is_valid(data), name
