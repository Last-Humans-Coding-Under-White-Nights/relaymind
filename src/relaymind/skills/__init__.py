"""Skill registry: package validation, instruction loading, and the
registered completion predicates (RM06, RM07; spec section 9).

See AGENTS.md (repository map) and docs/ARCHITECTURE.md.
"""

from relaymind.skills.registry import (
    MANIFEST_FILENAME,
    SKILL_FILENAME,
    SKILL_MANIFEST_SCHEMA,
    Skill,
    SkillConfigError,
    SkillDescription,
    SkillNotFoundError,
    SkillPackageError,
    SkillRegistry,
    SkillResourceError,
    SkillVersionMissingError,
    default_skill_roots,
)

__all__ = [
    "MANIFEST_FILENAME",
    "SKILL_FILENAME",
    "SKILL_MANIFEST_SCHEMA",
    "Skill",
    "SkillConfigError",
    "SkillDescription",
    "SkillNotFoundError",
    "SkillPackageError",
    "SkillRegistry",
    "SkillResourceError",
    "SkillVersionMissingError",
    "default_skill_roots",
]
