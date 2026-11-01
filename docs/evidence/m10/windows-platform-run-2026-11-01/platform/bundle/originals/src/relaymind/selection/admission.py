"""Admission: the gateway turns a selector's proposal into a routing decision (RM28).

Spec 0.31 RM02, RM05, RM08, and RM28. A selector proposes; the gateway
admits. For a skill it checks catalog membership, then builds the route
from the registry entry and the run's mode and policy, and runs the RM05
identifier check, asking the clarification itself, exactly as the router
does for its own routing generation (``Router.skill_routing``). For a
clarification it builds the clarify decision that waits for
skill_selection. The narrowing is intersected with the skill's allowlist,
so it can only remove tools (RM08). The alternatives are never read.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from relaymind.adapters import Message
from relaymind.orchestrator.route_decision import ExecutionMode, Language, RouteDecisionError
from relaymind.orchestrator.router import Router, Routing, request_language
from relaymind.selection.chain import InadmissibleError
from relaymind.selection.contract import CandidateKind, SelectionResult

__all__ = ["Admission", "admit"]


@dataclass(frozen=True)
class Admission:
    """An admitted selection: the routing, and the narrowed allowlist when there is one.

    tools is None when the selector narrowed nothing; otherwise it is the
    skill's allowed tools that the narrowing names, in allowlist order,
    which may be empty.
    """

    routing: Routing
    tools: tuple[str, ...] | None = None


def admit(
    result: SelectionResult,
    *,
    router: Router,
    messages: Sequence[Message],
    mode: ExecutionMode,
    language: Language | None,
    no_local: bool = False,
) -> Admission:
    """The gateway's decision for a selector's skill or clarification, or InadmissibleError.

    language is the run's language once it has one (section 5); the first
    decision takes it from the conversation, as the router does. no_local is
    a run without a usable local model: a skill then routes external with
    reason no_local_model (RM26).
    """
    run_language = language or request_language(messages)
    candidate = result.candidate
    try:
        if candidate.kind == CandidateKind.CLARIFY:
            assert candidate.clarification is not None
            return Admission(
                router.clarify_routing(candidate.clarification, mode=mode, language=run_language)
            )
        if candidate.kind != CandidateKind.SKILL or candidate.skill_id is None:
            raise InadmissibleError(f"a {candidate.kind} candidate has nothing to admit")
        skill_id = candidate.skill_id
        if skill_id not in router.offered:
            raise InadmissibleError(f"skill {skill_id!r} is not in the advertised catalog")
        routing = router.skill_routing(
            skill_id, messages, mode=mode, language=run_language, no_local=no_local
        )
    except RouteDecisionError as exc:
        raise InadmissibleError(str(exc)) from exc
    if result.tools is None:
        return Admission(routing)
    named = set(result.tools)
    allowlist = router.registry.get(skill_id).allowed_tools
    return Admission(routing, tuple(tool for tool in allowlist if tool in named))
