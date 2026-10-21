"""Tool registry: declarations, permissions, and validated calls.

RM08: a registered tool declares a name, input schema, output schema,
execution timeout, side-effect classification, handler, and
authorization rule. Availability is the intersection of the skill
allowlist, operator policy, and caller permissions; a name that is not
registered here never gains a handler, whatever a client sends.

RM09: ``validate`` checks the full tool name and the decoded argument
object before any handler runs, ``run`` runs a validated call, and
``invoke`` does both. Returned data is checked against the output
schema. Input objects must use
additionalProperties=false (section 9), so undeclared fields are
rejected. Numbers are typed strictly: where a schema says integer, or
lists integers in an enum or const, a float such as 60.0 or a boolean
is rejected, never converted. A rejection message names the JSON path
and the failed schema keyword, never the rejected value (RM12).

RM10: a mutating handler that raises an unexpected error, or returns
data outside its output schema, may have changed something, so its
result is unknown_outcome, not a definite failure. ToolUnavailableError
and ToolConflictError mean the handler refused before any change, so
those stay failed.

Operation persistence, replay, and timeouts with unknown outcomes
belong to the executor (RM10, ``relaymind.tools.executor``). This
module records the declared timeout; the executor enforces it.
"""

import copy
import dataclasses
import hashlib
import math
import re
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError
from jsonschema.validators import extend

from relaymind.adapters.base import ToolSchema
from relaymind.canonical import canonical_json
from relaymind.schema_util import first_schema_error, freeze_json, load_packaged_schema
from relaymind.store import TraceClock

# Section 11: two seconds unless a tool declares a shorter timeout.
MAX_TOOL_TIMEOUT_SECONDS = 2.0

TOOL_RESULT_SCHEMA: dict[str, Any] = load_packaged_schema(__package__, "tool_result.v1.json")
_RESULT_VALIDATOR = Draft202012Validator(TOOL_RESULT_SCHEMA)


def _same_json(left: object, right: object) -> bool:
    """JSON equality that keeps integers, floats, and booleans apart."""
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, int | float) and isinstance(right, int | float):
        return type(left) is type(right) and left == right
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        return left.keys() == right.keys() and all(_same_json(left[k], right[k]) for k in left)
    if isinstance(left, list | tuple) and isinstance(right, list | tuple):
        return len(left) == len(right) and all(map(_same_json, left, right))
    return type(left) is type(right) and left == right


def _strict_enum(validator: Any, enums: Any, instance: Any, schema: Any) -> Any:
    if not any(_same_json(instance, item) for item in enums):
        yield ValidationError("value is not one of the allowed values")


def _strict_const(validator: Any, const: Any, instance: Any, schema: Any) -> Any:
    if not _same_json(instance, const):
        yield ValidationError("value is not the allowed value")


def _is_strict_integer(checker: Any, instance: object) -> bool:
    return isinstance(instance, int) and not isinstance(instance, bool)


# RM09, section 9: draft 2020-12 counts 60.0 as an integer and as equal
# to 60. The gateway does not: tool handlers get exactly the declared types.
StrictValidator = extend(
    Draft202012Validator,
    validators={"enum": _strict_enum, "const": _strict_const},
    type_checker=Draft202012Validator.TYPE_CHECKER.redefine("integer", _is_strict_integer),
)

_TOOL_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")

Handler = Callable[[dict[str, Any]], Any]


class SideEffect(StrEnum):
    """Side-effect classification of a tool (RM08)."""

    READ_ONLY = "read_only"
    MUTATING = "mutating"


class ToolStatus(StrEnum):
    """ToolResult status values (section 8)."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN_OUTCOME = "unknown_outcome"


class ToolRegistrationError(ValueError):
    """A tool declaration is incomplete or invalid (RM08)."""


class ToolRejectedError(ValueError):
    """A tool call was rejected before execution (RM09).

    code is one of unknown_tool, tool_not_permitted, invalid_arguments.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ToolUnavailableError(RuntimeError):
    """Raised by a handler that cannot reach the service behind it.

    The handler must not have changed anything when it raises this, so
    a mutating tool's result stays a definite failure (RM10).
    """


class ToolConflictError(RuntimeError):
    """Raised by a handler whose precondition no longer holds.

    The handler must not have changed anything when it raises this.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ToolDeclaration:
    """Everything RM08 requires a registered tool to declare.

    required_permission is the authorization rule: the caller permission
    a call needs. The registered declaration holds deep, read-only copies
    of both schemas (RM07), so they cannot drift from its schema hash.
    """

    name: str
    description: str
    input_schema: Mapping[str, Any]
    output_schema: Mapping[str, Any]
    timeout_seconds: float
    side_effect: SideEffect
    handler: Handler
    required_permission: str


@dataclass(frozen=True)
class RegisteredTool:
    """A declaration accepted by the registry, with its schema hash."""

    declaration: ToolDeclaration
    schema_hash: str
    _input: Any = field(repr=False, compare=False)
    _output: Any = field(repr=False, compare=False)

    @property
    def name(self) -> str:
        return self.declaration.name

    @property
    def timeout_seconds(self) -> float:
        return self.declaration.timeout_seconds

    @property
    def side_effect(self) -> SideEffect:
        return self.declaration.side_effect


@dataclass(frozen=True)
class OperatorPolicy:
    """Operator configuration: the tools enabled on this gateway (RM08)."""

    enabled_tools: frozenset[str]


@dataclass(frozen=True)
class CallerContext:
    """What the authenticated caller is permitted to do (RM08)."""

    permissions: frozenset[str]


@dataclass(frozen=True)
class ToolResult:
    """The common ToolResult envelope (section 8)."""

    operation_id: str
    tool_name: str
    status: ToolStatus
    data: dict[str, Any] | None
    error: dict[str, str] | None
    observed_at: str
    duration_ms: int
    source_ref: str
    truncated: bool

    @classmethod
    def from_json_object(cls, data: Mapping[str, Any]) -> "ToolResult":
        """A ToolResult from its published schema shape (validated first)."""
        validate_tool_result(data)
        return cls(
            operation_id=data["operation_id"],
            tool_name=data["tool_name"],
            status=ToolStatus(data["status"]),
            data=copy.deepcopy(data["data"]),
            error=dict(data["error"]) if data["error"] is not None else None,
            observed_at=data["observed_at"],
            duration_ms=data["duration_ms"],
            source_ref=data["source_ref"],
            truncated=data["truncated"],
        )

    def to_json_object(self) -> dict[str, Any]:
        """The result in its published schema shape."""
        return {
            "operation_id": self.operation_id,
            "tool_name": self.tool_name,
            "status": self.status.value,
            "data": copy.deepcopy(self.data),
            "error": dict(self.error) if self.error is not None else None,
            "observed_at": self.observed_at,
            "duration_ms": self.duration_ms,
            "source_ref": self.source_ref,
            "truncated": self.truncated,
        }


def validate_tool_result(data: object) -> None:
    """Raise ValueError unless data matches the published ToolResult schema."""
    error = first_schema_error(_RESULT_VALIDATOR, data)
    if error is not None:
        raise ValueError(f"invalid ToolResult: {error.message}")


def utc_timestamp(moment: datetime) -> str:
    """Format a time as a section 9 UTC timestamp such as 2026-10-20T03:00:00Z."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _schema_validator(declaration: ToolDeclaration, name: str) -> Any:
    schema = getattr(declaration, name)
    if not isinstance(schema, Mapping):
        raise ToolRegistrationError(f"'{name}' must be a JSON Schema object")
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise ToolRegistrationError(f"'{name}' is not a valid JSON Schema: {exc.message}") from exc
    return StrictValidator(schema)


def _rejection_message(error: ValidationError) -> str:
    """Path and keyword only; the rejected value is never repeated (RM12)."""
    return f"argument at {error.json_path} failed the {error.validator!r} schema rule"


def _unknown_outcome(name: str, what: str) -> dict[str, str]:
    """A mutating handler ended without a definite outcome (RM10).

    Its side effect may or may not have happened, so the result is
    unknown_outcome, which is never retried automatically.
    """
    return {
        "code": "unknown_outcome",
        "message": f"mutating tool {name!r} {what}; its side effect may or may not have happened",
    }


def _check_declaration(declaration: ToolDeclaration) -> None:
    if not isinstance(declaration.name, str) or not _TOOL_NAME.fullmatch(declaration.name):
        raise ToolRegistrationError(
            f"tool name {declaration.name!r} must be lowercase letters, digits, and underscores"
        )
    if not isinstance(declaration.description, str) or not declaration.description.strip():
        raise ToolRegistrationError("'description' must be a non-blank string")
    timeout = declaration.timeout_seconds
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, int | float)
        or math.isnan(timeout)
        or not 0 < timeout <= MAX_TOOL_TIMEOUT_SECONDS
    ):
        raise ToolRegistrationError(
            f"'timeout_seconds' must be above 0 and at most {MAX_TOOL_TIMEOUT_SECONDS}"
        )
    if declaration.side_effect not in tuple(SideEffect):
        raise ToolRegistrationError(
            f"'side_effect' must be one of {', '.join(member.value for member in SideEffect)}"
        )
    if not callable(declaration.handler):
        raise ToolRegistrationError("'handler' must be callable")
    permission = declaration.required_permission
    if not isinstance(permission, str) or not permission.strip():
        raise ToolRegistrationError("'required_permission' must be a non-blank string")


class ToolRegistry:
    """Registered gateway tools and their validated invocation (RM08, RM09)."""

    def __init__(self, *, clock: TraceClock | None = None) -> None:
        self._tools: dict[str, RegisteredTool] = {}
        self._clock = clock or TraceClock()

    def register(self, declaration: ToolDeclaration) -> RegisteredTool:
        """Validate a declaration and add it to the registry."""
        _check_declaration(declaration)
        for name in ("input_schema", "output_schema"):
            if not isinstance(getattr(declaration, name), Mapping):
                raise ToolRegistrationError(f"'{name}' must be a JSON Schema object")
        # RM07, RM08: the registry keeps its own deep, read-only copies, so
        # later changes to the caller's dicts cannot drift from the hash.
        declaration = dataclasses.replace(
            declaration,
            side_effect=SideEffect(declaration.side_effect),
            input_schema=freeze_json(declaration.input_schema),
            output_schema=freeze_json(declaration.output_schema),
        )
        input_validator = _schema_validator(declaration, "input_schema")
        output_validator = _schema_validator(declaration, "output_schema")
        schema = declaration.input_schema
        if schema.get("type") != "object" or schema.get("additionalProperties") is not False:
            raise ToolRegistrationError(
                "'input_schema' must be an object schema with additionalProperties=false"
            )
        # Section 8: ToolResult data on success is a JSON object.
        if declaration.output_schema.get("type") != "object":
            raise ToolRegistrationError("'output_schema' must be an object schema")
        if declaration.name in self._tools:
            raise ToolRegistrationError(f"tool {declaration.name!r} is already registered")
        digest = hashlib.sha256(
            canonical_json(
                {
                    "name": declaration.name,
                    "description": declaration.description,
                    "input_schema": declaration.input_schema,
                    "output_schema": declaration.output_schema,
                    "timeout_seconds": declaration.timeout_seconds,
                    "side_effect": declaration.side_effect.value,
                    "required_permission": declaration.required_permission,
                }
            ).encode("utf-8")
        ).hexdigest()
        registered = RegisteredTool(
            declaration=declaration,
            schema_hash=f"sha256:{digest}",
            _input=input_validator,
            _output=output_validator,
        )
        self._tools[declaration.name] = registered
        return registered

    def get(self, name: str) -> RegisteredTool:
        """The registered tool with this name; KeyError if there is none."""
        return self._tools[name]

    def names(self) -> frozenset[str]:
        """Names of every registered tool."""
        return frozenset(self._tools)

    def available_tools(
        self,
        *,
        skill_allowlist: Iterable[str],
        policy: OperatorPolicy,
        caller: CallerContext,
    ) -> frozenset[str]:
        """The RM08 intersection: skill allowlist, operator policy, caller permissions."""
        return frozenset(
            name
            for name in skill_allowlist
            if name in self._tools
            and name in policy.enabled_tools
            and self._tools[name].declaration.required_permission in caller.permissions
        )

    def tool_schemas(self, names: Iterable[str]) -> tuple[ToolSchema, ...]:
        """The model-facing descriptions of these registered tools, in order.

        A ToolSchema is what a model is offered; it carries no handler.
        """
        return tuple(
            ToolSchema(
                name=name,
                description=self._tools[name].declaration.description,
                input_schema=self._tools[name].declaration.input_schema,
            )
            for name in names
        )

    def validate(
        self,
        name: object,
        arguments: object,
        *,
        available: Collection[str],
    ) -> RegisteredTool:
        """Check the full tool name and decoded arguments (RM09); run nothing.

        Raises ToolRejectedError (unknown_tool, tool_not_permitted, or
        invalid_arguments). A call that passes may go to run() as is.
        """
        if not isinstance(name, str):
            raise ToolRejectedError("unknown_tool", "the tool name must be a string")
        tool = self._tools.get(name)
        if tool is None:
            raise ToolRejectedError("unknown_tool", f"tool {name!r} is not registered")
        if name not in available:
            raise ToolRejectedError("tool_not_permitted", f"tool {name!r} is not available")
        if not isinstance(arguments, Mapping):
            raise ToolRejectedError(
                "invalid_arguments", "tool arguments must be one fully decoded JSON object"
            )
        # A frozen mapping (an adapter ToolCall) is still one decoded object.
        error = first_schema_error(tool._input, dict(arguments))
        if error is not None:
            raise ToolRejectedError("invalid_arguments", _rejection_message(error))
        return tool

    def invoke(
        self,
        name: object,
        arguments: object,
        *,
        available: Collection[str],
        operation_id: str,
    ) -> ToolResult:
        """Validate and run one tool call.

        Rejections before execution raise ToolRejectedError and never
        call the handler. Once the handler runs, every outcome is a
        ToolResult. operation_id comes from the executor, not the model,
        so a blank or non-string one is a caller bug: ValueError.
        """
        tool = self.validate(name, arguments, available=available)
        assert isinstance(arguments, Mapping)  # validate() guarantees a decoded object
        return self.run(tool, arguments, operation_id=operation_id)

    def run(
        self, tool: RegisteredTool, arguments: Mapping[str, Any], *, operation_id: str
    ) -> ToolResult:
        """Run the handler of a call that validate() accepted; never raises for it.

        The caller has validated this exact tool and argument object
        (RM09); the handler gets a copy, and its output is checked
        against the output schema. A blank or non-string operation_id is
        a caller bug and raises ValueError before the handler runs.
        """
        # Section 8: ToolResult.operation_id is a non-empty string.
        if not isinstance(operation_id, str) or not operation_id.strip():
            raise ValueError("'operation_id' must be a non-blank string")
        name = tool.name
        started = self._clock.monotonic()
        mutating = tool.side_effect is SideEffect.MUTATING
        status = ToolStatus.FAILED
        data: dict[str, Any] | None = None
        error: dict[str, str] | None = None
        try:
            returned = tool.declaration.handler(copy.deepcopy(dict(arguments)))
        except ToolUnavailableError as exc:
            error = {"code": "tool_unavailable", "message": str(exc)}
        except ToolConflictError as exc:
            error = {"code": exc.code, "message": str(exc)}
        except Exception:
            if mutating:
                status, error = (
                    ToolStatus.UNKNOWN_OUTCOME,
                    _unknown_outcome(name, "raised an error"),
                )
            else:
                error = {"code": "tool_failure", "message": f"tool {name!r} raised an error"}
        else:
            if isinstance(returned, Mapping) and not any(tool._output.iter_errors(returned)):
                status, data = ToolStatus.SUCCEEDED, copy.deepcopy(dict(returned))
            elif mutating:
                status = ToolStatus.UNKNOWN_OUTCOME
                error = _unknown_outcome(name, "returned data outside its output schema")
            else:
                error = {
                    "code": "invalid_output",
                    "message": f"tool {name!r} returned data outside its output schema",
                }
        # Section 8: duration_ms has minimum 0, even if the clock steps back.
        duration_ms = max(0, int((self._clock.monotonic() - started) * 1000))
        return ToolResult(
            operation_id=operation_id,
            tool_name=name,
            status=status,
            data=data,
            error=error,
            observed_at=utc_timestamp(self._clock.utc_now()),
            duration_ms=duration_ms,
            source_ref=f"tool:{name}/{operation_id}",
            truncated=bool(data.get("truncated", False)) if data else False,
        )
