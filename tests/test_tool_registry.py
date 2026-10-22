"""Tool registry: declarations, schemas, and permission intersection.

RM08: registered tools declare a name, input schema, output schema,
execution timeout, side-effect classification, handler, and
authorization rule. Availability is the intersection of the skill
allowlist, operator policy, and caller permissions. Names in a
client-supplied tools array do not register executable handlers.

RM09: the full tool name and arguments are validated before execution,
undeclared fields are rejected where the schema disallows them, and
returned data is validated. Partial streamed argument JSON never
executes. Section 8: the ToolResult envelope. Section 11: the tool
timeout is two seconds unless a tool declares a shorter one.
Issue #10.
"""

import copy
import dataclasses

import pytest

from helpers import SERVICE_INPUT_SCHEMA, FakeClock, make_tool_declaration
from relaymind.tools import (
    TOOL_RESULT_SCHEMA,
    CallerContext,
    OperatorPolicy,
    SideEffect,
    ToolConflictError,
    ToolDeclaration,
    ToolRegistrationError,
    ToolRegistry,
    ToolRejectedError,
    ToolResult,
    ToolStatus,
    ToolUnavailableError,
    validate_tool_result,
)


def registry_with(*declarations: ToolDeclaration) -> ToolRegistry:
    registry = ToolRegistry(clock=FakeClock())
    for declaration in declarations:
        registry.register(declaration)
    return registry


READER = CallerContext(permissions=frozenset({"demo.read"}))
ALL_ON = OperatorPolicy(enabled_tools=frozenset({"echo_service"}))


# --- RM08: registration requires every declared field ----------------------


@pytest.mark.parametrize(
    "field",
    [
        "name",
        "description",
        "input_schema",
        "output_schema",
        "timeout_seconds",
        "side_effect",
        "handler",
        "required_permission",
    ],
)
def test_registration_rejects_a_missing_declared_field(field):
    """RM08: each declared field is required at registration."""
    declaration = make_tool_declaration(**{field: None})
    with pytest.raises(ToolRegistrationError, match=field):
        ToolRegistry().register(declaration)


def test_registration_rejects_a_handler_that_is_not_callable():
    with pytest.raises(ToolRegistrationError, match="handler"):
        ToolRegistry().register(make_tool_declaration(handler="get_service_health"))


def test_registration_rejects_an_unknown_side_effect_class():
    with pytest.raises(ToolRegistrationError, match="side_effect"):
        ToolRegistry().register(make_tool_declaration(side_effect="maybe"))


@pytest.mark.parametrize("timeout", [0, -1.0, 2.5, float("nan")])
def test_registration_rejects_a_timeout_outside_the_section_11_bound(timeout):
    """Section 11: two seconds unless a tool declares a shorter timeout."""
    with pytest.raises(ToolRegistrationError, match="timeout"):
        ToolRegistry().register(make_tool_declaration(timeout_seconds=timeout))


def test_registration_accepts_the_two_second_default_bound():
    registry = registry_with(make_tool_declaration(timeout_seconds=2.0))
    assert registry.get("echo_service").timeout_seconds == 2.0


def test_registration_rejects_an_input_schema_that_allows_undeclared_fields():
    """Section 9 and RM09: input objects use additionalProperties=false."""
    loose = {**SERVICE_INPUT_SCHEMA, "additionalProperties": True}
    with pytest.raises(ToolRegistrationError, match="additionalProperties"):
        ToolRegistry().register(make_tool_declaration(input_schema=loose))
    missing = {
        key: value for key, value in SERVICE_INPUT_SCHEMA.items() if key != "additionalProperties"
    }
    with pytest.raises(ToolRegistrationError, match="additionalProperties"):
        ToolRegistry().register(make_tool_declaration(input_schema=missing))


def test_registration_rejects_an_invalid_json_schema():
    with pytest.raises(ToolRegistrationError, match="output_schema"):
        ToolRegistry().register(make_tool_declaration(output_schema={"type": "not-a-type"}))


@pytest.mark.parametrize("name", ["", "Get-Health", "get health", "1tool"])
def test_registration_rejects_a_malformed_tool_name(name):
    with pytest.raises(ToolRegistrationError, match="name"):
        ToolRegistry().register(make_tool_declaration(name=name))


def test_registration_rejects_a_duplicate_name():
    registry = registry_with(make_tool_declaration())
    with pytest.raises(ToolRegistrationError, match="already registered"):
        registry.register(make_tool_declaration())


def test_the_schema_hash_changes_when_a_tool_schema_changes():
    """RM07: a changed tool schema must change a hash that skills fold in."""
    before = registry_with(make_tool_declaration()).get("echo_service").schema_hash
    narrowed = {**SERVICE_INPUT_SCHEMA, "properties": {"service": {"enum": ["staging-api"]}}}
    after = (
        registry_with(make_tool_declaration(input_schema=narrowed)).get("echo_service").schema_hash
    )
    assert before.startswith("sha256:")
    assert before != after


# --- RM08: availability is the three-way intersection ----------------------


def test_a_tool_is_available_when_all_three_legs_allow_it():
    registry = registry_with(make_tool_declaration())
    available = registry.available_tools(
        skill_allowlist=["echo_service"], policy=ALL_ON, caller=READER
    )
    assert available == frozenset({"echo_service"})


@pytest.mark.parametrize(
    ("allowlist", "policy", "caller"),
    [
        pytest.param([], ALL_ON, READER, id="skill-allowlist"),
        pytest.param(
            ["echo_service"],
            OperatorPolicy(enabled_tools=frozenset()),
            READER,
            id="operator-policy",
        ),
        pytest.param(
            ["echo_service"],
            ALL_ON,
            CallerContext(permissions=frozenset({"demo.mutate"})),
            id="caller-permission",
        ),
    ],
)
def test_a_tool_is_unavailable_when_one_leg_denies_it(allowlist, policy, caller):
    """RM08: availability is the intersection of all three legs."""
    registry = registry_with(make_tool_declaration())
    assert (
        registry.available_tools(skill_allowlist=allowlist, policy=policy, caller=caller) == set()
    )


def test_a_name_that_is_allowed_everywhere_but_not_registered_has_no_handler():
    """RM08: a client-supplied name never becomes an executable tool."""
    registry = registry_with(make_tool_declaration())
    available = registry.available_tools(
        skill_allowlist=["echo_service", "run_shell"],
        policy=OperatorPolicy(enabled_tools=frozenset({"echo_service", "run_shell"})),
        caller=READER,
    )
    assert available == frozenset({"echo_service"})
    with pytest.raises(ToolRejectedError) as excinfo:
        registry.invoke("run_shell", {"cmd": "ls"}, available={"run_shell"}, operation_id="op_1")
    assert excinfo.value.code == "unknown_tool"


# --- RM09: validation before execution -------------------------------------


class CountingHandler:
    def __init__(self, result=None):
        self.calls = []
        self.result = result

    def __call__(self, args):
        self.calls.append(args)
        return self.result if self.result is not None else {"service": args["service"]}


def test_a_valid_call_executes_and_returns_a_succeeded_result():
    handler = CountingHandler()
    registry = registry_with(make_tool_declaration(handler=handler))
    result = registry.invoke(
        "echo_service", {"service": "staging-api"}, available={"echo_service"}, operation_id="op_1"
    )
    assert handler.calls == [{"service": "staging-api"}]
    assert result.status is ToolStatus.SUCCEEDED
    assert result.data == {"service": "staging-api"}
    assert result.error is None
    assert result.operation_id == "op_1"
    assert result.tool_name == "echo_service"
    assert result.observed_at == "2026-10-20T03:00:00Z"
    assert result.truncated is False
    assert result.duration_ms >= 0
    validate_tool_result(result.to_json_object())


def test_a_registered_tool_outside_the_available_set_is_rejected_without_execution():
    handler = CountingHandler()
    registry = registry_with(make_tool_declaration(handler=handler))
    with pytest.raises(ToolRejectedError) as excinfo:
        registry.invoke(
            "echo_service", {"service": "staging-api"}, available=set(), operation_id="o"
        )
    assert excinfo.value.code == "tool_not_permitted"
    assert handler.calls == []


@pytest.mark.parametrize(
    "arguments",
    [
        {"service": "staging-api", "verbose": True},
        {},
        {"service": "production-db"},
        {"service": 7},
    ],
)
def test_invalid_arguments_are_rejected_without_execution(arguments):
    """RM09: undeclared fields, missing fields, and out-of-enum values."""
    handler = CountingHandler()
    registry = registry_with(make_tool_declaration(handler=handler))
    with pytest.raises(ToolRejectedError) as excinfo:
        registry.invoke("echo_service", arguments, available={"echo_service"}, operation_id="o")
    assert excinfo.value.code == "invalid_arguments"
    assert handler.calls == []


@pytest.mark.parametrize("arguments", ['{"service": "staging-', '{"service": "staging-api"}', None])
def test_arguments_that_are_not_a_decoded_object_never_execute(arguments):
    """RM09: partial streamed argument JSON never executes; the caller
    must hand over one fully parsed object."""
    handler = CountingHandler()
    registry = registry_with(make_tool_declaration(handler=handler))
    with pytest.raises(ToolRejectedError) as excinfo:
        registry.invoke("echo_service", arguments, available={"echo_service"}, operation_id="o")
    assert excinfo.value.code == "invalid_arguments"
    assert handler.calls == []


def test_the_handler_gets_a_copy_of_the_arguments():
    seen = []

    def handler(args):
        args["service"] = "payments-api"
        seen.append(args)
        return {"service": "staging-api"}

    registry = registry_with(make_tool_declaration(handler=handler))
    original = {"service": "staging-api"}
    registry.invoke("echo_service", original, available={"echo_service"}, operation_id="o")
    assert original == {"service": "staging-api"}


# --- RM09: returned data is validated ---------------------------------------


@pytest.mark.parametrize(
    "returned",
    [{"service": "staging-api", "secret": "x"}, {}, ["staging-api"], "healthy"],
)
def test_returned_data_failing_the_output_schema_is_a_failed_result(returned):
    registry = registry_with(make_tool_declaration(handler=lambda args: returned))
    result = registry.invoke(
        "echo_service", {"service": "staging-api"}, available={"echo_service"}, operation_id="o"
    )
    assert result.status is ToolStatus.FAILED
    assert result.data is None
    assert result.error["code"] == "invalid_output"
    validate_tool_result(result.to_json_object())


def test_a_handler_that_cannot_reach_its_service_is_a_failed_result():
    """Section 9: failure to contact the tool itself is a failed ToolResult."""

    def handler(args):
        raise ToolUnavailableError("sandbox service is not reachable")

    registry = registry_with(make_tool_declaration(handler=handler))
    result = registry.invoke(
        "echo_service", {"service": "staging-api"}, available={"echo_service"}, operation_id="o"
    )
    assert result.status is ToolStatus.FAILED
    assert result.data is None
    assert result.error == {
        "code": "tool_unavailable",
        "message": "sandbox service is not reachable",
    }


def test_an_unexpected_handler_error_is_a_failed_result_without_internal_details():
    def handler(args):
        raise RuntimeError("password=hunter2")

    registry = registry_with(make_tool_declaration(handler=handler))
    result = registry.invoke(
        "echo_service", {"service": "staging-api"}, available={"echo_service"}, operation_id="o"
    )
    assert result.status is ToolStatus.FAILED
    assert result.error["code"] == "tool_failure"
    assert "hunter2" not in str(result.to_json_object())


# --- Section 8: the ToolResult envelope --------------------------------------


def test_the_tool_result_schema_is_published_and_versioned():
    assert TOOL_RESULT_SCHEMA["$id"].endswith("tool_result.v1.json")
    assert TOOL_RESULT_SCHEMA["additionalProperties"] is False
    assert set(TOOL_RESULT_SCHEMA["required"]) == {
        "operation_id",
        "tool_name",
        "status",
        "data",
        "error",
        "observed_at",
        "duration_ms",
        "source_ref",
        "truncated",
    }


def test_the_tool_result_validator_rejects_a_bad_envelope():
    good = ToolResult(
        operation_id="op_1",
        tool_name="echo_service",
        status=ToolStatus.SUCCEEDED,
        data={"service": "staging-api"},
        error=None,
        observed_at="2026-10-20T03:00:00Z",
        duration_ms=3,
        source_ref="tool:echo_service/op_1",
        truncated=False,
    ).to_json_object()
    validate_tool_result(good)
    for broken in (
        {**good, "status": "maybe"},
        {**good, "extra": 1},
        {**good, "observed_at": "yesterday"},
        {key: value for key, value in good.items() if key != "truncated"},
        {**good, "status": "succeeded", "error": {"code": "x", "message": "y"}},
        {**good, "status": "failed", "data": None, "error": None},
    ):
        with pytest.raises(ValueError):
            validate_tool_result(broken)


def test_declarations_are_immutable():
    declaration = make_tool_declaration()
    with pytest.raises(dataclasses.FrozenInstanceError):
        declaration.name = "other"  # type: ignore[misc]


def test_a_side_effect_given_as_its_plain_value_is_accepted():
    registry = registry_with(make_tool_declaration(side_effect="read_only"))
    assert registry.get("echo_service").side_effect is SideEffect.READ_ONLY


# --- Review fixes: strict numbers, value-free messages, full hash, bad names ----

COUNT_INPUT = {
    "type": "object",
    "properties": {
        "window_minutes": {"enum": [15, 60, 1440]},
        "limit": {"type": "integer", "minimum": 1},
        "mode": {"const": 1},
    },
    "additionalProperties": False,
}


@pytest.mark.parametrize(
    "arguments",
    [
        {"window_minutes": 60.0},
        {"limit": 2.0},
        {"mode": 1.0},
        {"mode": True},
        {"limit": True},
    ],
)
def test_whole_number_floats_and_booleans_are_not_integers(arguments):
    """RM09, section 9: a field the schema declares as integer, or as an
    enum or const of integers, only accepts an integer. 60.0 and True are
    rejected as invalid_arguments and the handler never runs."""
    handler = CountingHandler()
    registry = registry_with(
        make_tool_declaration(
            input_schema=COUNT_INPUT, output_schema={"type": "object"}, handler=handler
        )
    )
    with pytest.raises(ToolRejectedError) as excinfo:
        registry.invoke("echo_service", arguments, available={"echo_service"}, operation_id="o")
    assert excinfo.value.code == "invalid_arguments"
    assert handler.calls == []


def test_integer_arguments_still_pass_the_strict_check():
    """RM09: real integers are accepted."""
    registry = registry_with(
        make_tool_declaration(
            input_schema=COUNT_INPUT, output_schema={"type": "object"}, handler=lambda a: {}
        )
    )
    result = registry.invoke(
        "echo_service",
        {"window_minutes": 60, "limit": 2, "mode": 1},
        available={"echo_service"},
        operation_id="o",
    )
    assert result.status is ToolStatus.SUCCEEDED


def test_output_with_a_float_where_an_integer_is_declared_is_a_failed_result():
    """RM09: returned data is validated with the same strict number types."""
    registry = registry_with(
        make_tool_declaration(
            output_schema={"type": "object", "properties": {"revision": {"type": "integer"}}},
            handler=lambda args: {"revision": 4.0},
        )
    )
    result = registry.invoke(
        "echo_service", {"service": "staging-api"}, available={"echo_service"}, operation_id="o"
    )
    assert result.status is ToolStatus.FAILED
    assert result.error["code"] == "invalid_output"


@pytest.mark.parametrize(
    ("arguments", "path", "keyword"),
    [
        ({"service": "sk-live-SECRET-VALUE"}, "$.service", "enum"),
        ({"service": "staging-api", "token": "sk-live-SECRET-VALUE"}, "$", "additionalProperties"),
        ({"service": ["sk-live-SECRET-VALUE"]}, "$.service", "enum"),
    ],
)
def test_invalid_argument_messages_name_the_path_and_keyword_only(arguments, path, keyword):
    """RM12: a rejection message never repeats the rejected value; it is
    built from the JSON path and the validator keyword."""
    registry = registry_with(make_tool_declaration())
    with pytest.raises(ToolRejectedError) as excinfo:
        registry.invoke("echo_service", arguments, available={"echo_service"}, operation_id="o")
    message = str(excinfo.value)
    assert "SECRET" not in message
    assert "token" not in message
    assert path in message
    assert keyword in message


@pytest.mark.parametrize(
    "change",
    [
        {"description": "Return the service ID, differently."},
        {"timeout_seconds": 0.5},
        {"required_permission": "demo.admin"},
        {"side_effect": SideEffect.MUTATING},
    ],
)
def test_the_schema_hash_covers_description_timeout_and_permission(change):
    """RM07, RM08: every declared contract field the model or the gateway
    relies on changes the hash that skills fold into their content hash."""
    before = registry_with(make_tool_declaration()).get("echo_service").schema_hash
    after = registry_with(make_tool_declaration(**change)).get("echo_service").schema_hash
    assert before != after


@pytest.mark.parametrize("name", [["echo_service"], {"name": "echo_service"}, 7, None, b"echo"])
def test_a_tool_name_that_is_not_a_string_is_an_unknown_tool(name):
    """RM09: the full tool name is validated; a non-string or unhashable
    name is rejected as unknown_tool, never a TypeError."""
    handler = CountingHandler()
    registry = registry_with(make_tool_declaration(handler=handler))
    with pytest.raises(ToolRejectedError) as excinfo:
        registry.invoke(
            name, {"service": "staging-api"}, available={"echo_service"}, operation_id="o"
        )
    assert excinfo.value.code == "unknown_tool"
    assert handler.calls == []


# --- RM10: a mutating handler that fails without a definite outcome ------------


def _mutating(handler):
    registry = ToolRegistry(clock=FakeClock())
    registry.register(
        make_tool_declaration(
            name="delete_thing",
            side_effect=SideEffect.MUTATING,
            handler=handler,
            required_permission="demo.mutate",
        )
    )
    return registry


def _raise(exc):
    def handler(args):
        raise exc

    return handler


def _invoke_mutating(registry):
    return registry.invoke(
        "delete_thing", {"service": "staging-api"}, available={"delete_thing"}, operation_id="op_1"
    )


def test_an_unexpected_error_from_a_mutating_handler_is_an_unknown_outcome():
    """RM10: the side effect may or may not have happened, so the result is
    unknown_outcome, never a definite failure that invites a retry."""
    result = _invoke_mutating(_mutating(_raise(RuntimeError("connection reset after write"))))
    assert result.status is ToolStatus.UNKNOWN_OUTCOME
    assert result.data is None
    assert result.error["code"] == "unknown_outcome"
    assert "connection reset" not in result.error["message"]
    validate_tool_result(result.to_json_object())


def test_invalid_output_from_a_mutating_handler_is_an_unknown_outcome():
    """RM10: the handler ran, but its result cannot confirm what it changed."""
    result = _invoke_mutating(_mutating(lambda args: {"unexpected": True}))
    assert result.status is ToolStatus.UNKNOWN_OUTCOME
    assert result.error["code"] == "unknown_outcome"


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (ToolUnavailableError("down"), "tool_unavailable"),
        (ToolConflictError("revision_conflict", "stale"), "revision_conflict"),
    ],
)
def test_a_mutating_handler_that_refuses_before_any_change_is_a_definite_failure(exc, code):
    """RM10: unavailable and conflict errors are raised before any change,
    so their outcome is known."""
    result = _invoke_mutating(_mutating(_raise(exc)))
    assert result.status is ToolStatus.FAILED
    assert result.error["code"] == code


# --- Review fixes: output schema shape, operation IDs, durations, frozen schemas


@pytest.mark.parametrize(
    "output_schema",
    [{"type": "string"}, {"type": "array"}, {"type": ["object", "null"]}, {}],
)
def test_registration_requires_an_object_output_schema(output_schema):
    """RM08, section 8: ToolResult data on success is a JSON object, so an
    output schema that is not an object schema is refused at registration
    instead of failing every call later."""
    with pytest.raises(ToolRegistrationError, match="output_schema"):
        ToolRegistry().register(make_tool_declaration(output_schema=output_schema))


@pytest.mark.parametrize("operation_id", ["", "   ", None, 7, b"op_1"])
def test_invoke_requires_a_non_blank_operation_id(operation_id):
    """Section 8: ToolResult.operation_id is a non-empty string. A bad
    operation ID is an executor bug, not a model proposal, so it raises
    ValueError before the handler runs."""
    handler = CountingHandler()
    registry = registry_with(make_tool_declaration(handler=handler))
    with pytest.raises(ValueError, match="operation_id"):
        registry.invoke(
            "echo_service",
            {"service": "staging-api"},
            available={"echo_service"},
            operation_id=operation_id,
        )
    assert handler.calls == []


def test_a_monotonic_clock_that_steps_back_still_gives_a_valid_duration():
    """Section 8: duration_ms has minimum 0 in the ToolResult schema."""
    clock = FakeClock()
    registry = ToolRegistry(clock=clock)

    def handler(args):
        clock.mono -= 0.5
        return {"service": args["service"]}

    registry.register(make_tool_declaration(handler=handler))
    result = registry.invoke(
        "echo_service", {"service": "staging-api"}, available={"echo_service"}, operation_id="o"
    )
    assert result.duration_ms == 0
    validate_tool_result(result.to_json_object())


def test_registered_schemas_cannot_drift_from_the_schema_hash():
    """RM07, RM08: the registry keeps its own frozen copy of each schema.
    Changing the caller's dicts after registration, or trying to change
    the registered ones, leaves the schemas, validation, and hash as
    they were."""
    input_schema = copy.deepcopy(SERVICE_INPUT_SCHEMA)
    output_schema = copy.deepcopy(make_tool_declaration().output_schema)
    registry = ToolRegistry(clock=FakeClock())
    tool = registry.register(
        make_tool_declaration(input_schema=input_schema, output_schema=output_schema)
    )
    expected_hash = registry_with(make_tool_declaration()).get("echo_service").schema_hash
    assert tool.schema_hash == expected_hash

    input_schema["properties"]["service"]["enum"].append("invented-api")
    output_schema["required"].append("extra")
    declared = tool.declaration
    assert declared.input_schema == SERVICE_INPUT_SCHEMA
    assert declared.output_schema["required"] == ["service"]
    with pytest.raises(TypeError):
        declared.input_schema["properties"]["service"]["enum"].append("invented-api")
    with pytest.raises(TypeError):
        declared.output_schema["required"].append("extra")

    with pytest.raises(ToolRejectedError):
        registry.invoke(
            "echo_service",
            {"service": "invented-api"},
            available={"echo_service"},
            operation_id="o",
        )
    ok = registry.invoke(
        "echo_service", {"service": "staging-api"}, available={"echo_service"}, operation_id="o"
    )
    assert ok.status is ToolStatus.SUCCEEDED
    rehashed = ToolRegistry().register(dataclasses.replace(declared)).schema_hash
    assert rehashed == expected_hash


# --- RM11: the argument that names a mutating tool's target ----------------------


def test_a_tool_declares_no_confirmation_target_by_default():
    """RM11: only a tool that names its target can be confirmed."""
    tool = registry_with(make_tool_declaration()).get("echo_service")
    assert tool.declaration.confirmation_target is None
    assert tool.confirmation_target is None


def test_a_declared_confirmation_target_is_kept():
    declaration = make_tool_declaration(
        side_effect=SideEffect.MUTATING, confirmation_target="service"
    )
    assert registry_with(declaration).get("echo_service").confirmation_target == "service"


@pytest.mark.parametrize(
    "target", ["", "  ", 7, "region"], ids=["empty", "blank", "int", "unknown"]
)
def test_a_confirmation_target_must_be_a_declared_input_field(target):
    """RM11: the target is read from validated arguments, so it must be an input field."""
    with pytest.raises(ToolRegistrationError, match="confirmation_target"):
        registry_with(make_tool_declaration(confirmation_target=target))


def test_the_confirmation_target_is_part_of_the_schema_hash():
    """RM07, RM11: moving the target changes what a confirmation binds, so it changes the hash."""
    plain = registry_with(make_tool_declaration()).get("echo_service").schema_hash
    named = registry_with(make_tool_declaration(confirmation_target="service"))
    assert named.get("echo_service").schema_hash != plain


def test_a_confirmation_target_must_be_a_required_input_field():
    """RM11: the gateway reads the target from every admitted call, so it cannot be optional."""
    schema = {
        "type": "object",
        "properties": {"service": {"type": "string"}, "region": {"type": "string"}},
        "required": ["service"],
        "additionalProperties": False,
    }
    with pytest.raises(ToolRegistrationError, match="confirmation_target"):
        registry_with(make_tool_declaration(input_schema=schema, confirmation_target="region"))
