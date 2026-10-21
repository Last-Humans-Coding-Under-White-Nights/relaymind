"""Router: local-first routing by native function calling (RM01, RM02, RM05).

Mechanism (docs/ROADMAP.md, decided 2026-10-21 from the #48 routing
probe; spec section 6 allows it): the routing generation offers one
function per advertised skill, plus request_clarification. The model is
trusted only for the skill choice:

- The gateway builds the RouteDecision. Route and reason code come from
  the selected skill's registry entry and the run's routing context,
  never from the model (RM04, section 4.4), and every decision goes
  through the published boundary validator with a RoutingContext
  (section 8) before anything else happens.
- A skill that declares a parallel-workflow ID routes parallel only when
  operator configuration enables that workflow and the effective
  execution mode admits it (parallel_demo, or auto where operator policy
  allows it). Otherwise it routes external with reason parallel_workflow
  (section 4.4). local_only and external_only never route parallel.
- The decision's language comes from the conversation: the latest user
  turn with words in it, so an identifier-only reply such as
  ``staging-api`` does not change it. The model's language argument is
  required by the function schema but not used alone: in the probe it
  was wrong for 5 of 6 Chinese prompts. The run keeps the language of
  its first RouteDecision (section 5).
- Missing identifiers are not left to the model (RM05). A selected skill
  whose tools all need an enumerated identifier (such as ``service``)
  gets a clarify decision with missing_input, naming that skill and the
  missing argument, when no allowed value appears in a user turn
  (``relaymind.orchestrator.identifiers``).

Anything else is invalid routing output and raises RouteDecisionError:
no call, more than one call, an unknown function, undeclared or invalid
arguments. Invalid output never reaches tool execution (RM02).

Router input is measured with the local tokenizer and capped at
``LocalBudgets.routing_input_tokens`` (2,048 by default, section 4.1).
An input over the cap is never truncated: it takes the bypass decision
(route external, reason context_limit) without a routing generation. In
external_only no routing generation runs either: the gateway records
its own decision (route external, reason execution_mode, section 7.4).

The routing functions exist only in the routing request. They are
internal control actions and are never registered as application tools
(RM09).
"""

from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from relaymind.adapters import GenerationOptions, GenerationRequest, Message, Role, ToolCall
from relaymind.adapters.base import ToolSchema
from relaymind.orchestrator import identifiers
from relaymind.orchestrator.budgets import LocalBudgets
from relaymind.orchestrator.route_decision import (
    SCHEMA_VERSION,
    ExecutionMode,
    Language,
    Route,
    RouteDecision,
    RouteDecisionError,
    RoutingContext,
    parse_route_decision,
)
from relaymind.skills import Skill, SkillNotFoundError, SkillRegistry
from relaymind.tools import ToolRegistry

CLARIFY_FUNCTION = "request_clarification"

# Section 8 clarification.required: what a model-issued clarification
# waits for, since no skill was chosen.
SKILL_SELECTION = "skill_selection"

# Section 4.2: at most eight short skill descriptions per router prompt.
MAX_ADVERTISED_SKILLS = 8

_LANGUAGE_PROPERTY = {"enum": ["en", "zh"], "description": "en for English, zh for Chinese."}

SYSTEM_PROMPT = (
    "You are a request router. Call exactly one function for the user's request. "
    "Each function is a skill. If the request does not name the service or "
    f"resource it needs, call {CLARIFY_FUNCTION} instead. Never guess a service "
    "or resource name. Set language to en for an English request and zh for a "
    "Chinese request."
)


class RouterConfigError(ValueError):
    """The advertised catalog is not usable (section 4.2)."""


@dataclass(frozen=True)
class Routing:
    """A validated decision, and for clarify the input the run waits for."""

    decision: RouteDecision
    missing: str | None = None


def request_language(messages: Sequence[Message]) -> Language:
    """The conversation language: zh when the latest user turn with words is Chinese.

    User turns that hold only identifiers, numbers, and punctuation are
    skipped, so an ID-only follow-up keeps the language (section 5).
    """
    for message in reversed(messages):
        if message.role is not Role.USER or not message.content:
            continue
        if identifiers.is_identifier_only(message.content):
            continue
        return Language.ZH if identifiers.has_cjk(message.content) else Language.EN
    return Language.EN


class Router:
    """Builds the routing request and turns its output into a RouteDecision."""

    def __init__(
        self,
        skills: SkillRegistry,
        tools: ToolRegistry,
        *,
        budgets: LocalBudgets,
        offered: Iterable[str] | None = None,
        enabled_workflows: Collection[str] = (),
        parallel_in_auto: bool = False,
    ) -> None:
        names = list(skills.names() if offered is None else offered)
        if len(names) > MAX_ADVERTISED_SKILLS:
            raise RouterConfigError(
                f"{len(names)} skills offered; the router prompt holds at most "
                f"{MAX_ADVERTISED_SKILLS}"
            )
        if len(set(names)) != len(names):
            raise RouterConfigError("a skill is offered more than once")
        try:
            self._skills: dict[str, Skill] = {name: skills.get(name) for name in names}
        except SkillNotFoundError as exc:
            raise RouterConfigError(str(exc)) from exc
        self.registry = skills
        self.tools = tools
        self.budgets = budgets
        self.enabled_workflows = frozenset(enabled_workflows)
        self.parallel_in_auto = parallel_in_auto

    @property
    def offered(self) -> tuple[str, ...]:
        return tuple(self._skills)

    def context(self, mode: ExecutionMode) -> RoutingContext:
        """The section 8 routing context of a managed run in this mode."""
        return RoutingContext(
            managed=True,
            execution_mode=mode,
            parallel_workflows={
                name: skill.parallel_workflow
                for name, skill in self._skills.items()
                if skill.parallel_workflow is not None
            },
            enabled_workflows=self.enabled_workflows,
            parallel_in_auto=self.parallel_in_auto,
        )

    # -- request -----------------------------------------------------------

    def functions(self) -> tuple[ToolSchema, ...]:
        """One function per offered skill, then request_clarification."""
        schemas = [
            ToolSchema(
                name=skill.name,
                description=skill.description,
                input_schema={
                    "type": "object",
                    "properties": {"language": _LANGUAGE_PROPERTY},
                    "required": ["language"],
                    "additionalProperties": False,
                },
            )
            for skill in self._skills.values()
        ]
        schemas.append(
            ToolSchema(
                name=CLARIFY_FUNCTION,
                description=(
                    "Ask the user one short question when the request does not name the "
                    "service or resource it needs."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "language": _LANGUAGE_PROPERTY,
                        "question": {"type": "string"},
                    },
                    "required": ["language", "question"],
                    "additionalProperties": False,
                },
            )
        )
        return tuple(schemas)

    def routing_messages(self, messages: Sequence[Message]) -> tuple[Message, ...]:
        """The complete router input: the routing prompt, then the conversation."""
        return (Message(role=Role.SYSTEM, content=SYSTEM_PROMPT), *messages)

    def options(self) -> GenerationOptions:
        """Routing output is capped (section 11) and deterministic."""
        return GenerationOptions(
            max_output_tokens=self.budgets.routing_output_tokens, temperature=0
        )

    def build_request(self, messages: Sequence[Message], generation_id: str) -> GenerationRequest:
        """The routing generation request."""
        return GenerationRequest(
            generation_id=generation_id,
            messages=self.routing_messages(messages),
            tool_schemas=self.functions(),
            options=self.options(),
        )

    def within_input_cap(self, input_tokens: int) -> bool:
        return input_tokens <= self.budgets.routing_input_tokens

    # -- decisions -----------------------------------------------------------

    def _validated(self, mode: ExecutionMode, **fields: Any) -> RouteDecision:
        return parse_route_decision(
            {"schema_version": SCHEMA_VERSION, "clarification": None, "skill_id": None, **fields},
            skill_ids=self.offered,
            context=self.context(mode),
        )

    def bypass_decision(self, messages: Sequence[Message], mode: ExecutionMode) -> RouteDecision:
        """An oversized input skips the fast router (section 4.1)."""
        return self._validated(
            mode,
            route=Route.EXTERNAL.value,
            language=request_language(messages).value,
            reason_code="context_limit",
        )

    def execution_mode_decision(self, messages: Sequence[Message]) -> RouteDecision:
        """external_only: the gateway's own decision, no routing generation (7.4)."""
        return self._validated(
            ExecutionMode.EXTERNAL_ONLY,
            route=Route.EXTERNAL.value,
            language=request_language(messages).value,
            reason_code="execution_mode",
        )

    def decide(
        self,
        calls: Sequence[ToolCall],
        messages: Sequence[Message],
        *,
        mode: ExecutionMode = ExecutionMode.AUTO,
    ) -> Routing:
        """Map the routing generation's function calls to a validated decision."""
        if len(calls) != 1:
            raise RouteDecisionError(f"expected one routing function call, got {len(calls)}")
        call = calls[0]
        arguments = dict(call.arguments)
        language = request_language(messages).value
        if arguments.get("language") not in ("en", "zh"):
            raise RouteDecisionError("routing call 'language' must be en or zh")

        if call.tool_name == CLARIFY_FUNCTION:
            if set(arguments) != {"language", "question"}:
                raise RouteDecisionError("request_clarification takes language and question")
            question = arguments["question"]
            if not isinstance(question, str):
                raise RouteDecisionError("'question' must be a string")
            decision = self._validated(
                mode,
                route=Route.CLARIFY.value,
                language=language,
                reason_code="missing_input",
                clarification=question,
            )
            return Routing(decision, missing=SKILL_SELECTION)

        skill = self._skills.get(call.tool_name)
        if skill is None:
            raise RouteDecisionError(f"unknown routing function: {call.tool_name!r}")
        if set(arguments) != {"language"}:
            raise RouteDecisionError(f"skill function {skill.name!r} takes only language")

        missing = self._missing_identifier(skill, messages, Language(language))
        if missing is not None:
            name, label, values = missing
            decision = self._validated(
                mode,
                route=Route.CLARIFY.value,
                skill_id=skill.name,
                language=language,
                reason_code="missing_input",
                clarification=_clarification(Language(language), label, values),
            )
            return Routing(decision, missing=name)
        route, reason = self._route_for(skill, mode)
        decision = self._validated(
            mode, route=route.value, skill_id=skill.name, language=language, reason_code=reason
        )
        return Routing(decision)

    def _route_for(self, skill: Skill, mode: ExecutionMode) -> tuple[Route, str]:
        """Route and reason from the registry entry and the run (RM04, 4.4)."""
        if skill.parallel_workflow is not None:
            context = self.context(mode)
            if skill.parallel_workflow in context.enabled_workflows and context.admits_parallel():
                return Route.PARALLEL, "parallel_workflow"
            return Route.EXTERNAL, "parallel_workflow"
        if not skill.local_eligible:
            return Route.EXTERNAL, "complex_skill"
        return Route.LOCAL, "simple_skill"

    def _missing_identifier(
        self, skill: Skill, messages: Sequence[Message], language: Language
    ) -> tuple[str, str, tuple[str, ...]] | None:
        """An identifier every allowed tool needs that no user turn names.

        Returns the argument name, its schema label in the run's language
        (section 5), and its allowed values.
        """
        schemas = [self.tools.get(name).declaration.input_schema for name in skill.allowed_tools]
        per_tool = [identifiers.required_enum_identifiers(schema) for schema in schemas]
        if not per_tool:
            return None
        shared = set(per_tool[0]).intersection(*per_tool[1:])
        provenance = identifiers.provenance_text(messages)
        for name in sorted(shared):
            values = per_tool[0][name]
            if not any(identifiers.mentions(provenance, value) for value in values):
                return name, identifiers.label_in(schemas, name, language.value), values
        return None


def _clarification(language: Language, label: str, values: tuple[str, ...]) -> str:
    if language is Language.ZH:
        # A Chinese label joins the text directly; a Latin fallback keeps a space.
        gap = "" if identifiers.has_cjk(label) else " "
        return f"请问是哪个{gap}{label}？可选：{'、'.join(values)}。"
    return f"Which {label} do you mean? Options: {', '.join(values)}."
