"""Routing-result cache: exact match, five minutes, 256 entries (RM07, RM24, AC13).

Spec 0.26 section 10. The cache is off unless the operator sets
``cache.routing`` (spec section 14.1); this module only stores. It holds
the skill choice of a routing generation for a local read-only route,
never a decision, an answer, or a tool result. The orchestrator checks
authorization and runs every tool fresh on a hit.

Entries depend on the skills offered and the caller's permissions and
enabled tools. A lookup under a context that differs from an entry's
removes that entry at once (invalidation), so a changed skill or
permission does not wait for the TTL. The key also holds those parts, so
an entry of another context could never be a hit in any case.
"""

import time
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from relaymind.adapters import Message
from relaymind.canonical import canonical_digest
from relaymind.skills import Skill
from relaymind.tools import CallerContext, OperatorPolicy, ToolRegistry
from relaymind.tools.registry import SideEffect

# Spec 0.26 section 10: the limits are fixed by the specification.
TTL_SECONDS = 300
MAX_ENTRIES = 256

# Bumped whenever the routing prompt (router.SYSTEM_PROMPT) changes.
ROUTING_PROMPT_VERSION = "1"
# Bumped whenever spec section 10 changes what the key holds or what is eligible.
POLICY_VERSION = "1"


@dataclass(frozen=True)
class CacheContext:
    """What entries depend on: skill hashes, caller permissions, enabled tools."""

    skill_hashes: Mapping[str, str]
    permissions: frozenset[str]
    enabled_tools: frozenset[str]


@dataclass(frozen=True)
class CachedRoute:
    """A stored skill choice and where it came from."""

    skill_id: str
    skill_version: str
    skill_hash: str
    run_id: str


@dataclass(frozen=True)
class CacheHit:
    route: CachedRoute
    age_ms: int


@dataclass(frozen=True)
class _Entry:
    route: CachedRoute
    context: CacheContext
    stored_at: float


def routing_cache_key(parts: Mapping[str, Any]) -> str:
    """The SHA-256 digest of the canonical form of the key parts and the two versions."""
    return canonical_digest(
        {
            **parts,
            "routing_prompt_version": ROUTING_PROMPT_VERSION,
            "policy_version": POLICY_VERSION,
        }
    )


def is_eligible(skill: Skill, tools: ToolRegistry) -> bool:
    """A skill whose every allowed tool is read-only (spec 0.26: no mutation route cache)."""
    return all(
        tools.get(name).side_effect is not SideEffect.MUTATING for name in skill.allowed_tools
    )


def key_and_context(
    *,
    messages: Sequence[Message],
    language: str,
    mode: str,
    offered: Iterable[Skill],
    tools: ToolRegistry,
    policy: OperatorPolicy,
    caller: CallerContext,
    model_id: str,
    enabled_workflows: Iterable[str],
) -> tuple[str, CacheContext]:
    """The key digest and the dependency context of one routing request (spec 0.26)."""
    skills = sorted(offered, key=lambda skill: skill.name)
    allowed = sorted({name for skill in skills for name in skill.allowed_tools})
    context = CacheContext(
        skill_hashes={skill.name: skill.content_hash for skill in skills},
        permissions=frozenset(caller.permissions),
        enabled_tools=frozenset(policy.enabled_tools),
    )
    key = routing_cache_key(
        {
            "messages": [[m.role.value, m.content, m.tool_call_id] for m in messages],
            "language": language,
            "mode": mode,
            "permissions": sorted(context.permissions),
            "enabled_tools": sorted(context.enabled_tools),
            "skills": {skill.name: [skill.version, skill.content_hash] for skill in skills},
            "tools": {name: tools.get(name).schema_hash for name in allowed},
            "model": model_id,
            "enabled_workflows": sorted(enabled_workflows),
        }
    )
    return key, context


class RoutingCache:
    """A bounded, expiring map of key digest to skill choice."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self.hits = 0
        self.misses = 0
        self.stores = 0
        self.expired = 0
        self.invalidated = 0
        self.evicted = 0

    def __len__(self) -> int:
        return len(self._entries)

    def counters(self) -> dict[str, int]:
        """The counts an experiment reports next to the cache-off baseline."""
        return {
            "hits": self.hits,
            "misses": self.misses,
            "stores": self.stores,
            "expired": self.expired,
            "invalidated": self.invalidated,
            "evicted": self.evicted,
        }

    def lookup(self, key: str, context: CacheContext) -> CacheHit | None:
        """The unexpired entry under key, after dropping entries stale for context."""
        self._invalidate(context)
        now = self._clock()
        entry = self._entries.get(key)
        if entry is not None and now - entry.stored_at >= TTL_SECONDS:
            del self._entries[key]
            self.expired += 1
            entry = None
        if entry is None:
            self.misses += 1
            return None
        self.hits += 1
        return CacheHit(entry.route, int((now - entry.stored_at) * 1000))

    def store(self, key: str, context: CacheContext, route: CachedRoute) -> None:
        self._entries.pop(key, None)
        self._entries[key] = _Entry(route, context, self._clock())
        self.stores += 1
        while len(self._entries) > MAX_ENTRIES:
            self._entries.popitem(last=False)
            self.evicted += 1

    def drop(self, key: str) -> None:
        """Remove one entry, for a hit the gateway could not use."""
        self._entries.pop(key, None)

    def _invalidate(self, context: CacheContext) -> None:
        stale = [
            key
            for key, entry in self._entries.items()
            if dict(entry.context.skill_hashes) != dict(context.skill_hashes)
            or entry.context.permissions != context.permissions
            or entry.context.enabled_tools != context.enabled_tools
        ]
        for key in stale:
            del self._entries[key]
        self.invalidated += len(stale)
