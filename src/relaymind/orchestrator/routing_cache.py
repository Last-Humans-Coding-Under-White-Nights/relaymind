"""Routing-result cache: exact match, five minutes, 256 entries (RM07, RM24, AC13).

Spec 0.26 section 10. The cache is off unless the operator sets
``cache.routing`` (spec section 14.1); this module only stores. It holds
the skill choice of a routing generation for a local read-only route,
never a decision, an answer, or a tool result. The orchestrator checks
authorization and runs every tool fresh on a hit.

Entries depend on the skills offered and the caller's permissions and
enabled tools. All entries share one context: a lookup or store under a
different context removes every entry at once (invalidation), so a
changed skill or permission does not wait for the TTL. The key also
holds those parts, so an entry of another context could never be a hit
in any case.
"""

import time
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any

from relaymind.adapters import Message
from relaymind.adapters.base import ToolSchema
from relaymind.canonical import canonical_digest
from relaymind.orchestrator.router import SYSTEM_PROMPT
from relaymind.skills import Skill
from relaymind.tools import CallerContext, OperatorPolicy, ToolRegistry
from relaymind.tools.registry import SideEffect

# Spec 0.26 section 10: the limits are fixed by the specification.
TTL_SECONDS = 300
MAX_ENTRIES = 256

# Bumped whenever spec section 10 changes what the key holds or what is eligible.
POLICY_VERSION = "1"


@dataclass(frozen=True)
class CacheContext:
    """What entries depend on: skill hashes, caller permissions, enabled tools."""

    skill_hashes: Mapping[str, str]
    permissions: frozenset[str]
    enabled_tools: frozenset[str]

    def __post_init__(self) -> None:
        # Keep a snapshot even when the caller later changes its source mapping.
        object.__setattr__(self, "skill_hashes", MappingProxyType(dict(self.skill_hashes)))

    def __hash__(self) -> int:
        return hash((frozenset(self.skill_hashes.items()), self.permissions, self.enabled_tools))


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
    stored_at: float
    looked_up: bool = False


def _plain(value: Any) -> Any:
    """A JSON value with read-only mappings and tuples made plain, for canonical_digest."""
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(item) for item in value]
    return value


def routing_prompt_version(
    functions: Sequence[ToolSchema], *, system_prompt: str = SYSTEM_PROMPT
) -> str:
    """The digest of the routing prompt and functions: it changes whenever they change.

    Spec 0.26 section 10 asks for a routing prompt version that changes
    with the prompt or the functions; deriving it makes that hold without
    a manual bump.
    """
    return canonical_digest(
        {
            "system_prompt": system_prompt,
            "functions": [
                [schema.name, schema.description, _plain(schema.input_schema)]
                for schema in functions
            ],
        }
    )


def _message_part(message: Message) -> list[Any]:
    """Role, text, and tool-call references of one message, as the router receives it."""
    return [
        message.role.value,
        message.content,
        message.tool_call_id,
        [[call.call_id, call.tool_name, _plain(call.arguments)] for call in message.tool_calls],
    ]


def routing_cache_key(parts: Mapping[str, Any]) -> str:
    """The SHA-256 digest of the canonical form of the key parts and the policy version."""
    return canonical_digest({**parts, "policy_version": POLICY_VERSION})


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
    routing_prompt_version: str,
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
            "messages": [_message_part(message) for message in messages],
            "language": language,
            "mode": mode,
            "permissions": sorted(context.permissions),
            "enabled_tools": sorted(context.enabled_tools),
            "skills": {skill.name: [skill.version, skill.content_hash] for skill in skills},
            "tools": {name: tools.get(name).schema_hash for name in allowed},
            "model": model_id,
            "enabled_workflows": sorted(enabled_workflows),
            "routing_prompt_version": routing_prompt_version,
        }
    )
    return key, context


class RoutingCache:
    """A bounded, expiring map of key digest to skill choice.

    Entries are kept in store order. The clock is monotonic, so the
    oldest entries are first, and expired entries are removed from the
    front before every lookup and store.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self._context: CacheContext | None = None
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
        self._expire(now)
        entry = self._entries.get(key)
        if entry is None:
            self.misses += 1
            return None
        self.hits += 1
        self._entries[key] = replace(entry, looked_up=True)
        return CacheHit(entry.route, max(0, int((now - entry.stored_at) * 1000)))

    def store(self, key: str, context: CacheContext, route: CachedRoute) -> None:
        self._invalidate(context)
        now = self._clock()
        self._expire(now)
        self._entries.pop(key, None)
        self._entries[key] = _Entry(route, now)
        self.stores += 1
        while len(self._entries) > MAX_ENTRIES:
            self._entries.popitem(last=False)
            self.evicted += 1

    def drop(self, key: str) -> None:
        """Remove an entry, reclassifying its latest lookup, if any, as a miss."""
        entry = self._entries.pop(key, None)
        if entry is not None and entry.looked_up:
            self.hits -= 1
            self.misses += 1

    def _expire(self, now: float) -> None:
        while self._entries:
            oldest = next(iter(self._entries.values()))
            if now - oldest.stored_at < TTL_SECONDS:
                return
            self._entries.popitem(last=False)
            self.expired += 1

    def _invalidate(self, context: CacheContext) -> None:
        if context == self._context:
            return
        self.invalidated += len(self._entries)
        self._entries.clear()
        self._context = context
