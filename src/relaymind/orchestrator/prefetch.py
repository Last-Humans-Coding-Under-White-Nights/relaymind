"""Prefetch: resolving a skill's declared calls from the request (RM29, spec 0.32).

A skill manifest declares read-only calls (``skills/schemas/skill_manifest.v1.json``);
the registry checks them at load, without a request. This module holds
the two checks that need more than the manifest:

- ``check_prefetch_budgets`` compares the declared calls of every skill
  with the configured tool limits, at orchestrator construction, so a
  skill whose prefetch alone could not fit is a startup error and not
  a failure in the middle of a run.
- ``resolve_arguments`` finds a call's arguments in the request. An
  identifier is read by the RM05 rule: exactly one of the parameter's
  allowed values appears in the user turns, otherwise the call is not
  prefetched and the model proposes it as before. A constant is the
  value the manifest documents. A missing or ambiguous identifier skips
  a call, never the skill, and is not an error.

Running the calls belongs to the orchestrator (``local_loop``): they go
through the executor with every check of a model proposal.
"""

from collections.abc import Mapping
from typing import Any

from relaymind.orchestrator import identifiers
from relaymind.orchestrator.budgets import LocalBudgets
from relaymind.skills import PrefetchCall, SkillRegistry
from relaymind.skills.registry import REQUEST

__all__ = ["PrefetchBudgetError", "check_prefetch_budgets", "resolve_arguments"]


class PrefetchBudgetError(ValueError):
    """A skill's prefetch calls alone exceed a configured tool limit (RM29, section 11)."""


def check_prefetch_budgets(skills: SkillRegistry, budgets: LocalBudgets) -> None:
    """Refuse budgets that a skill's declared prefetch calls cannot fit in (RM29).

    Every declared call counts toward the entire-run tool limit
    (run_tool_executions), and, for a skill that can run locally, toward
    the per-stage limit of its first stage (tool_calls). A skill that is
    external-required has no local stage, so only the run limit applies.
    """
    for name in skills.names():
        skill = skills.get(name)
        declared = len(skill.prefetch)
        if declared > budgets.run_tool_executions:
            raise PrefetchBudgetError(
                f"skill {name!r} declares {declared} prefetch calls, but run_tool_executions"
                f" is {budgets.run_tool_executions}"
            )
        if skill.local_eligible and declared > budgets.tool_calls:
            raise PrefetchBudgetError(
                f"skill {name!r} declares {declared} prefetch calls, but tool_calls"
                f" is {budgets.tool_calls}"
            )


def resolve_arguments(
    call: PrefetchCall, input_schema: Mapping[str, Any], user_text: str
) -> dict[str, Any] | None:
    """The arguments of a declared call, or None when the request does not settle them (RM05).

    user_text is the text of the user turns (identifiers.provenance_text
    without tool results): the gateway's own questions and the model's
    turns are never provenance. The result follows the order of the
    declaration.
    """
    properties = input_schema.get("properties", {})
    arguments: dict[str, Any] = {}
    for binding in call.bindings:
        if binding.source != REQUEST:
            arguments[binding.name] = binding.value
            continue
        found = [v for v in properties[binding.name]["enum"] if identifiers.mentions(user_text, v)]
        if len(found) != 1:
            return None
        arguments[binding.name] = found[0]
    return arguments
