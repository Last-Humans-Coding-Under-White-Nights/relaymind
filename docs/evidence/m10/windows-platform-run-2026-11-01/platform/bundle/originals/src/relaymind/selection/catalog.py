"""The catalog a SelectionRequest carries: one entry per offered skill (RM28).

Spec 0.31 RM28: id, version, content hash, description, languages, local
eligibility, allowed tools, and example requests. The example requests
are read through the registry (``SkillRegistry.read_resource``) from the
skill's declared ``examples.json``, in both languages; a skill that
declares none has an empty list.
"""

from collections.abc import Iterable
from typing import Any

from relaymind.selection.example_match import EXAMPLES_RESOURCE, example_requests
from relaymind.skills import SkillRegistry

__all__ = ["catalog_entries"]


def catalog_entries(registry: SkillRegistry, names: Iterable[str]) -> list[dict[str, Any]]:
    """The SelectionRequest catalog of the named skills, in the order given."""
    entries = []
    for name in names:
        skill = registry.get(name)
        examples = (
            example_requests(registry.read_resource(name, EXAMPLES_RESOURCE))
            if EXAMPLES_RESOURCE in skill.resource_texts
            else []
        )
        entries.append(
            {
                "id": skill.name,
                "version": skill.version,
                "content_hash": skill.content_hash,
                "description": skill.description,
                "languages": list(skill.languages),
                "local_eligible": skill.local_eligible,
                "allowed_tools": list(skill.allowed_tools),
                "examples": examples,
            }
        )
    return entries
