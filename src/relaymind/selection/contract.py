"""The published selection interface: SelectionRequest, SelectionResult, Selector (RM28).

Spec 0.31 RM28 and section 8. Skill selection goes through one
interface so that a third-party system can replace it by configuration.
The two shapes are published as JSON Schemas next to this module
(``schemas/selection_request.v1.json`` and
``schemas/selection_result.v1.json``) and validated at the boundary in
both directions: the gateway checks every request it sends and every
result it gets back. A selector proposes; the gateway admits
(``relaymind.selection.admission``).

A selector returns the SelectionResult as a JSON object, the shape an
out-of-process selector sends over HTTP (#133), so a built-in selector
passes the same check as any other. The context carries what only an
in-process, model-based selector needs: the run's own messages and the
callback that runs its routing generation through the gateway, so the
model call stays in the trace, under the budgets, and cancellable.
"""

import copy
import math
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Protocol

import jsonschema

from relaymind.adapters import GenerationOptions, Message, ToolCall
from relaymind.adapters.base import ToolSchema
from relaymind.schema_util import (
    FrozenJSONObject,
    first_schema_error,
    freeze_json,
    load_packaged_schema,
)

__all__ = [
    "SCHEMA_VERSION",
    "SELECTION_REQUEST_SCHEMA",
    "SELECTION_RESULT_SCHEMA",
    "Candidate",
    "CandidateKind",
    "GenerateRouting",
    "RoutingGeneration",
    "SelectionContext",
    "SelectionError",
    "SelectionRequest",
    "SelectionResult",
    "Selector",
    "SelectorError",
    "parse_selection_request",
    "parse_selection_result",
]

SCHEMA_VERSION = 1

SELECTION_REQUEST_SCHEMA: dict[str, Any] = load_packaged_schema(
    "relaymind.selection", "selection_request.v1.json"
)
SELECTION_RESULT_SCHEMA: dict[str, Any] = load_packaged_schema(
    "relaymind.selection", "selection_result.v1.json"
)
_REQUEST_VALIDATOR = jsonschema.Draft202012Validator(SELECTION_REQUEST_SCHEMA)
_RESULT_VALIDATOR = jsonschema.Draft202012Validator(SELECTION_RESULT_SCHEMA)


class SelectionError(ValueError):
    """A SelectionRequest or SelectionResult failed boundary validation (RM28)."""


class SelectorError(Exception):
    """A selector could not produce a result; the chain records an error and falls through."""


class CandidateKind(StrEnum):
    SKILL = "skill"
    CLARIFY = "clarify"
    NONE = "none"


def _check(validator: Any, data: object, kind: str) -> None:
    error = first_schema_error(validator, data)
    if error is not None:
        where = "/".join(str(part) for part in error.path)
        raise SelectionError(f"{kind}: {error.message}" + (f" (at {where})" if where else ""))


@dataclass(frozen=True)
class SelectionRequest:
    """A validated, read-only SelectionRequest (RM28)."""

    data: FrozenJSONObject

    @property
    def run_id(self) -> str:
        return self.data["run_id"]

    @property
    def conversation(self) -> tuple[Mapping[str, str], ...]:
        return tuple(self.data["conversation"])

    @property
    def language(self) -> str:
        return self.data["language"]

    @property
    def execution_mode(self) -> str:
        return self.data["execution_mode"]

    @property
    def catalog(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(self.data["catalog"])

    @property
    def permissions(self) -> tuple[str, ...]:
        return tuple(self.data["permissions"])

    @property
    def deadline_ms(self) -> int | None:
        return self.data["deadline_ms"]

    def with_deadline(self, deadline_ms: int | None) -> "SelectionRequest":
        """The same request with another deadline, validated again."""
        return parse_selection_request({**self.to_json_object(), "deadline_ms": deadline_ms})

    def to_json_object(self) -> dict[str, Any]:
        """The request in its published shape, as an editable copy."""
        return copy.deepcopy(self.data)


def parse_selection_request(data: object) -> SelectionRequest:
    """Validate a SelectionRequest against the published schema."""
    _check(_REQUEST_VALIDATOR, data, "SelectionRequest")
    return SelectionRequest(freeze_json(data))


@dataclass(frozen=True)
class Candidate:
    """What a selector proposes: a skill, a clarification, or none."""

    kind: CandidateKind
    skill_id: str | None = None
    clarification: str | None = None

    def to_json_object(self) -> dict[str, str]:
        data = {"kind": self.kind.value}
        if self.kind == CandidateKind.SKILL:
            data["skill_id"] = self.skill_id
        elif self.kind == CandidateKind.CLARIFY:
            data["clarification"] = self.clarification
        return data


@dataclass(frozen=True)
class SelectionResult:
    """A validated SelectionResult (RM28). tools is None when the selector narrows nothing."""

    candidate: Candidate
    mechanism: str
    confidence: float | None
    selector_name: str
    selector_version: str
    model_calls: tuple[Mapping[str, str], ...] = ()
    tools: tuple[str, ...] | None = None
    alternatives: tuple[str, ...] = field(default=())

    def to_json_object(self) -> dict[str, Any]:
        """The result in its published shape."""
        data: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "candidate": self.candidate.to_json_object(),
            "mechanism": self.mechanism,
            "confidence": self.confidence,
            "selector": {"name": self.selector_name, "version": self.selector_version},
            "model_calls": [dict(call) for call in self.model_calls],
        }
        if self.tools is not None:
            data["tools"] = list(self.tools)
        if self.alternatives:
            data["alternatives"] = list(self.alternatives)
        return data


def parse_selection_result(data: object) -> SelectionResult:
    """Validate a SelectionResult against the published schema.

    JSON Schema cannot refuse infinity or NaN, so a confidence must also
    be finite; a flag is never a number.
    """
    _check(_RESULT_VALIDATOR, data, "SelectionResult")
    assert isinstance(data, Mapping)
    confidence = data["confidence"]
    if confidence is not None and not math.isfinite(confidence):
        raise SelectionError("SelectionResult: 'confidence' must be a finite number or null")
    candidate = data["candidate"]
    tools = data.get("tools")
    return SelectionResult(
        candidate=Candidate(
            kind=CandidateKind(candidate["kind"]),
            skill_id=candidate.get("skill_id"),
            clarification=candidate.get("clarification"),
        ),
        mechanism=data["mechanism"],
        confidence=None if confidence is None else float(confidence),
        selector_name=data["selector"]["name"],
        selector_version=data["selector"]["version"],
        model_calls=tuple(dict(call) for call in data["model_calls"]),
        tools=None if tools is None else tuple(tools),
        alternatives=tuple(data.get("alternatives", ())),
    )


@dataclass(frozen=True)
class RoutingGeneration:
    """What a routing generation produced: its calls, the model, and the call ID."""

    calls: tuple[ToolCall, ...]
    model: str
    call_id: str | None = None


# The callback that runs one routing generation: messages, the functions offered,
# and the options. In a gateway run it is the run's own generation path (RM03, RM16).
GenerateRouting = Callable[
    [Sequence[Message], Sequence[ToolSchema], GenerationOptions], Awaitable[RoutingGeneration]
]


@dataclass(frozen=True)
class SelectionContext:
    """What an in-process, model-based selector needs beyond the request.

    messages are the run's own messages, unredacted, exactly as the
    router has always received them, so the routing prompt and the
    routing cache key do not change. generate runs the selector's
    generation; it is None for a selector that is not model-based.
    """

    messages: tuple[Message, ...] = ()
    generate: GenerateRouting | None = None

    def without_generation(self) -> "SelectionContext":
        return replace(self, generate=None)


class Selector(Protocol):
    """A skill selector (RM28): it proposes, it never executes or grants anything.

    model_based is True for a selector whose work is a model generation:
    it is bounded by its generation's limits, not by
    selection.timeout_seconds (section 11), and it gets the routing
    generation callback in its context.
    """

    name: str
    version: str
    model_based: bool

    async def select(
        self, request: SelectionRequest, context: SelectionContext
    ) -> Mapping[str, Any]: ...
