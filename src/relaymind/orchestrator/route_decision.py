"""RouteDecision: the router's normalized output (spec section 8; RM02).

The JSON Schema is published next to this module as
``schemas/route_decision.v1.json``; the validator below enforces the
same contract at the boundary. A decision that fails validation never
reaches tool execution (RM02): the caller gets a RouteDecisionError
naming the field, and the router treats it as invalid output.

Spec 0.6 adds cross-field rules to the field checks. The rules that
need nothing but the decision (a clarify route carries a clarification,
every other route carries none) always apply. The rules that depend on
the run (managed or client-owned catalog, the effective execution mode,
the operator-enabled parallel workflow) apply when the caller passes a
RoutingContext; without one they are left to the router, which is the
only place that knows them.

Syntactically valid JSON is not evidence that the selected route is
correct (spec section 6). This module checks shape, catalog membership,
and the section 8 rules only; whether a route is admissible for the run
is otherwise the router's decision.
"""

import json
import math
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType

from relaymind.schema_util import check_closed_fields, check_schema_version, load_packaged_schema

SCHEMA_VERSION = 1

# Named explicitly: __package__ is empty when this module is loaded
# outside its package, and the schema must still be found.
ROUTE_DECISION_SCHEMA: dict[str, object] = load_packaged_schema(
    "relaymind.orchestrator", "route_decision.v1.json"
)


class Route(StrEnum):
    """Supported routing decisions (RM02)."""

    LOCAL = "local"
    EXTERNAL = "external"
    CLARIFY = "clarify"
    PARALLEL = "parallel"


class Language(StrEnum):
    """Supported interaction languages (spec section 1)."""

    EN = "en"
    ZH = "zh"


class ExecutionMode(StrEnum):
    """Effective execution modes (spec section 7.4)."""

    AUTO = "auto"
    LOCAL_ONLY = "local_only"
    EXTERNAL_ONLY = "external_only"
    PARALLEL_DEMO = "parallel_demo"


# Operational labels from spec section 8, not generated reasoning.
REASON_CODES: frozenset[str] = frozenset(
    ROUTE_DECISION_SCHEMA["properties"]["reason_code"]["enum"]  # type: ignore[index]
)

# Spec 0.31 section 8: the reason code follows the route. An external route carries the
# reason for leaving the local path; local_budget, invalid_output, and tool_failure are
# escalation reasons recorded in the trace (RM04), never a routing decision's.
EXTERNAL_REASONS: tuple[str, ...] = (
    "complex_skill",
    "parallel_workflow",
    "context_limit",
    "execution_mode",
    "capability_missing",
    "no_local_model",
    "local_slow",
    "selection_failed",
)
_ROUTE_REASONS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "local": ("simple_skill",),
        "clarify": ("missing_input",),
        "parallel": ("parallel_workflow",),
        "external": EXTERNAL_REASONS,
    }
)

_FIELDS = ("schema_version", "route", "skill_id", "language", "reason_code", "clarification")


class RouteDecisionError(ValueError):
    """Routing output failed boundary validation (RM02)."""


@dataclass(frozen=True)
class RoutingContext:
    """What the run-dependent rules of spec section 8 need to know.

    managed is False for the client-owned alias, whose offered catalog
    is empty (section 7.2). execution_mode is the effective mode
    (section 7.4). parallel_workflows maps a skill id to the
    parallel-workflow ID its registry manifest declares (section 4.2);
    enabled_workflows lists the IDs operator configuration enables, and
    parallel_in_auto is the operator policy that admits the workflow in
    auto, off by default (section 4.4).
    """

    managed: bool = True
    execution_mode: ExecutionMode = ExecutionMode.AUTO
    parallel_workflows: Mapping[str, str] = field(default_factory=dict)
    enabled_workflows: Collection[str] = ()
    parallel_in_auto: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "execution_mode", ExecutionMode(self.execution_mode))
        object.__setattr__(
            self, "parallel_workflows", MappingProxyType(dict(self.parallel_workflows))
        )
        if isinstance(self.enabled_workflows, (str, bytes)):
            raise TypeError("enabled_workflows must be a collection of workflow IDs")
        object.__setattr__(self, "enabled_workflows", frozenset(self.enabled_workflows))

    def admits_parallel(self) -> bool:
        """Whether the effective mode may run the parallel workflow (4.4)."""
        if not self.managed:
            return False
        if self.execution_mode is ExecutionMode.PARALLEL_DEMO:
            return True
        return self.execution_mode is ExecutionMode.AUTO and self.parallel_in_auto


@dataclass(frozen=True)
class RouteDecision:
    """A validated routing decision."""

    route: Route
    skill_id: str | None
    language: Language
    reason_code: str
    clarification: str | None
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        # Accept the enum values as plain strings; an unknown value fails
        # here as a ValueError rather than later in to_json_object.
        object.__setattr__(self, "route", Route(self.route))
        object.__setattr__(self, "language", Language(self.language))

    def to_json_object(self) -> dict[str, object]:
        """The decision in its published schema shape."""
        return {
            "schema_version": self.schema_version,
            "route": self.route.value,
            "skill_id": self.skill_id,
            "language": self.language.value,
            "reason_code": self.reason_code,
            "clarification": self.clarification,
        }


def _optional_text(data: Mapping[str, object], name: str) -> str | None:
    value = data[name]
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise RouteDecisionError(f"'{name}' must be a non-blank string or null")
    return value


def _enum_value(allowed: Iterable[str], data: Mapping[str, object], name: str) -> str:
    """The field's value if it is one of allowed, listed in that order on error."""
    value = data[name]
    choices = list(allowed)
    if not isinstance(value, str) or value not in choices:
        raise RouteDecisionError(f"'{name}' must be one of {', '.join(choices)}")
    return value


def _check_cross_field_rules(
    route: Route,
    reason_code: str,
    skill_id: str | None,
    clarification: str | None,
    context: RoutingContext | None,
) -> None:
    """The spec section 8 rules that span fields.

    The clarification and reason code rules need only the decision. The
    skill_id and parallel rules need the run, so they apply only with a
    context.
    """
    if route is Route.CLARIFY:
        if clarification is None:
            raise RouteDecisionError("'clarification' is required when route is clarify")
    elif clarification is not None:
        raise RouteDecisionError(f"'clarification' must be null when route is {route.value}")
    allowed = _ROUTE_REASONS[route.value]
    if reason_code not in allowed:
        raise RouteDecisionError(
            f"'reason_code' {reason_code} does not follow route {route.value};"
            f" it must be one of {', '.join(allowed)}"
        )
    if context is None:
        return
    if not context.managed:
        # Section 7.2: the client-owned alias is offered no skills.
        if skill_id is not None:
            raise RouteDecisionError("'skill_id' must be null in client-owned mode")
        if route is Route.PARALLEL:
            raise RouteDecisionError("route parallel is invalid in client-owned mode")
        return
    if route is Route.LOCAL and skill_id is None:
        raise RouteDecisionError("'skill_id' is required when route is local")
    if route is Route.PARALLEL:
        if not context.admits_parallel():
            raise RouteDecisionError(
                f"route parallel is invalid in execution mode {context.execution_mode.value}"
            )
        if skill_id is None:
            raise RouteDecisionError("'skill_id' is required when route is parallel")
        workflow = context.parallel_workflows.get(skill_id)
        if workflow is None:
            raise RouteDecisionError(
                f"route parallel needs a skill with a parallel-workflow ID; "
                f"{skill_id!r} declares none"
            )
        if workflow not in context.enabled_workflows:
            raise RouteDecisionError(
                f"route parallel needs an enabled workflow; {workflow!r} is not enabled"
            )


def parse_route_decision(
    data: object,
    *,
    skill_ids: Collection[str],
    context: RoutingContext | None = None,
) -> RouteDecision:
    """Validate a decoded RouteDecision against the schema and catalog.

    skill_ids is the advertised catalog for this routing call; a skill
    outside it is rejected (RM02). context, when given, enables the
    run-dependent rules of spec section 8 (see RoutingContext).
    """
    data = check_closed_fields(data, _FIELDS, RouteDecisionError, kind="RouteDecision")
    check_schema_version(data["schema_version"], SCHEMA_VERSION, RouteDecisionError)
    route = Route(_enum_value(Route, data, "route"))
    language = Language(_enum_value(Language, data, "language"))
    reason_code = _enum_value(sorted(REASON_CODES), data, "reason_code")
    skill_id = _optional_text(data, "skill_id")
    if skill_id is not None and skill_id not in skill_ids:
        raise RouteDecisionError(f"'skill_id' {skill_id!r} is not in the advertised catalog")
    clarification = _optional_text(data, "clarification")
    _check_cross_field_rules(route, reason_code, skill_id, clarification, context)
    return RouteDecision(
        route=route,
        skill_id=skill_id,
        language=language,
        reason_code=reason_code,
        clarification=clarification,
    )


def _reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    obj: dict[str, object] = {}
    for key, value in pairs:
        if key in obj:
            raise RouteDecisionError(f"duplicate field: {key}")
        obj[key] = value
    return obj


def _reject_constant(name: str) -> float:
    raise RouteDecisionError(f"non-finite number in routing output: {name}")


def _finite_float(text: str) -> float:
    # 1e999 decodes to inf without going through parse_constant.
    value = float(text)
    if not math.isfinite(value):
        raise RouteDecisionError(f"non-finite number in routing output: {text}")
    return value


def parse_route_decision_json(
    text: str,
    *,
    skill_ids: Collection[str],
    context: RoutingContext | None = None,
) -> RouteDecision:
    """Parse model text that must be exactly one RouteDecision object.

    Prose around the object, partial JSON, duplicate keys, non-finite
    numbers, and anything else the decoder refuses (oversized integers,
    nesting too deep) are all invalid output; nothing is repaired here.
    """
    try:
        data = json.loads(
            text,
            object_pairs_hook=_reject_duplicates,
            parse_float=_finite_float,
            parse_constant=_reject_constant,
        )
    except RouteDecisionError:
        raise
    except (ValueError, RecursionError) as exc:
        # JSONDecodeError and the decoder's size limits are ValueErrors;
        # deep nesting is a RecursionError. One error type leaves here.
        message = exc.msg if isinstance(exc, json.JSONDecodeError) else str(exc)
        raise RouteDecisionError(f"routing output is not one JSON object: {message}") from exc
    return parse_route_decision(data, skill_ids=skill_ids, context=context)
