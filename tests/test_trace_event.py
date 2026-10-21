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

from helpers import ACCEPTED_PAYLOAD
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
    "run.completed": {"final_answer_model": "qwen3"},
    "run.failed": {"error_code": "x"},
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
}

# Payload keys whose published schema is not plain non-blank text.
# Spec 0.6 section 8: clarification.required carries the clarify
# decision's skill_id (or null) and the section 11 waiting limit.
NON_TEXT_PAYLOAD = {
    "clarification.required": {
        "skill_id": {"type": ["string", "null"], "pattern": "\\S"},
        "waiting_seconds": {"type": "number", "exclusiveMinimum": 0},
    },
    "clarification.answered": {"waited_ms": {"type": "integer", "minimum": 0}},
}


# A non-blank string in the published schema: at least one non-space character.
TEXT = {"type": "string", "pattern": "\\S"}


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
            for key in payload["required"]:
                assert payload["properties"][key] == special.get(key, TEXT)
            required[event_type] = payload["required"]
            one_of_ids[event_type] = [alt["required"] for alt in payload.get("anyOf", [])]
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
    }
)


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
