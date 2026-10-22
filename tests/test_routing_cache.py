"""The routing-result cache store (RM24, RM07, AC13; spec 0.26 section 10)."""

import pytest

from helpers import FakeClock
from relaymind.orchestrator.router import SYSTEM_PROMPT
from relaymind.orchestrator.routing_cache import (
    MAX_ENTRIES,
    POLICY_VERSION,
    TTL_SECONDS,
    CacheContext,
    CachedRoute,
    RoutingCache,
    routing_cache_key,
)

CONTEXT = CacheContext(
    skill_hashes={"check-service-health": "sha256:aaa"},
    permissions=frozenset({"demo.read"}),
    enabled_tools=frozenset({"get_service_health"}),
)


def route(run_id="run_1") -> CachedRoute:
    return CachedRoute(
        skill_id="check-service-health",
        skill_version="0.1.1",
        skill_hash="sha256:aaa",
        run_id=run_id,
    )


def parts(**changes):
    base = {
        "messages": [["user", "Is staging-api healthy right now?"]],
        "language": "en",
        "mode": "auto",
        "permissions": ["demo.read"],
        "enabled_tools": ["get_service_health"],
        "skills": {"check-service-health": ["0.1.1", "sha256:aaa"]},
        "tools": {"get_service_health": "sha256:t1"},
        "model": "local-1",
        "enabled_workflows": [],
        "routing_prompt_version": "v1",
    }
    base.update(changes)
    return base


def test_the_limits_are_the_policy_values():
    """RM24: five minutes, 256 entries."""
    assert TTL_SECONDS == 300
    assert MAX_ENTRIES == 256


def test_the_policy_version_is_pinned():
    """RM24: the policy version is a constant bumped with spec section 10.

    The routing prompt version is derived from the prompt and the
    functions (see test_the_routing_prompt_version_follows_the_prompt_and_the_functions).
    """
    assert POLICY_VERSION == "1"


def test_key_is_exact_over_every_part():
    """RM24: messages, language, permissions, skill and tool hashes, model, versions."""
    base = routing_cache_key(parts())
    assert routing_cache_key(parts()) == base
    changes = [
        {"messages": [["user", "Is staging-api healthy right now? "]]},
        {"messages": [["user", "Is staging-api healthy right now?"], ["assistant", "ok"]]},
        {"language": "zh"},
        {"mode": "local_only"},
        {"permissions": ["demo.read", "demo.mutate"]},
        {"enabled_tools": []},
        {"skills": {"check-service-health": ["0.1.2", "sha256:aaa"]}},
        {"skills": {"check-service-health": ["0.1.1", "sha256:bbb"]}},
        {"tools": {"get_service_health": "sha256:t2"}},
        {"model": "local-2"},
        {"enabled_workflows": ["health-and-diagnostic-checklist"]},
        {"routing_prompt_version": "v2"},
    ]
    keys = {routing_cache_key(parts(**change)) for change in changes}
    assert base not in keys and len(keys) == len(changes)


def test_key_holds_the_policy_version(monkeypatch):
    """RM24: a new policy version is a different key."""
    from relaymind.orchestrator import routing_cache

    base = routing_cache_key(parts())
    monkeypatch.setattr(routing_cache, "POLICY_VERSION", "2")
    assert routing_cache_key(parts()) != base


def test_a_stored_route_is_a_hit_until_the_ttl():
    """RM24: five-minute TTL on the monotonic clock."""
    clock = FakeClock()
    cache = RoutingCache(clock=clock.monotonic)
    key = routing_cache_key(parts())
    assert cache.lookup(key, CONTEXT) is None
    cache.store(key, CONTEXT, route())
    clock.advance(299)
    hit = cache.lookup(key, CONTEXT)
    assert hit is not None and hit.route.run_id == "run_1" and hit.age_ms == 299_000
    clock.advance(1)
    assert cache.lookup(key, CONTEXT) is None
    assert (cache.hits, cache.misses, cache.stores, cache.expired) == (1, 2, 1, 1)
    assert len(cache) == 0


def test_the_oldest_entry_goes_at_256():
    """RM24: at most 256 entries."""
    cache = RoutingCache(clock=FakeClock().monotonic)
    for number in range(MAX_ENTRIES + 1):
        cache.store(f"key_{number}", CONTEXT, route())
    assert len(cache) == MAX_ENTRIES
    assert cache.evicted == 1
    assert cache.lookup("key_0", CONTEXT) is None
    assert cache.lookup("key_1", CONTEXT) is not None


@pytest.mark.parametrize(
    "changed",
    [
        CacheContext(
            {"check-service-health": "sha256:bbb"}, CONTEXT.permissions, CONTEXT.enabled_tools
        ),
        CacheContext({}, CONTEXT.permissions, CONTEXT.enabled_tools),
        CacheContext(CONTEXT.skill_hashes, frozenset(), CONTEXT.enabled_tools),
        CacheContext(CONTEXT.skill_hashes, CONTEXT.permissions, frozenset()),
    ],
    ids=["skill_changed", "skill_removed", "permission_changed", "tools_changed"],
)
def test_a_changed_skill_or_permission_drops_the_dependent_entries(changed):
    """AC13, RM07: the lookup under a changed context removes the entries at once."""
    cache = RoutingCache(clock=FakeClock().monotonic)
    cache.store("k1", CONTEXT, route())
    cache.store("k2", CONTEXT, route())
    assert cache.lookup("k_other", changed) is None
    assert len(cache) == 0
    assert cache.invalidated == 2


def test_drop_removes_one_entry():
    cache = RoutingCache(clock=FakeClock().monotonic)
    cache.store("k1", CONTEXT, route())
    cache.drop("k1")
    assert cache.lookup("k1", CONTEXT) is None


# -- review findings for #31 ------------------------------------------------------


def test_drop_of_an_unusable_hit_counts_it_as_a_miss():
    """RM24, spec 0.26 Hit: a hit the gateway could not use is a miss in the counters."""
    cache = RoutingCache(clock=FakeClock().monotonic)
    cache.store("k1", CONTEXT, route())
    assert cache.lookup("k1", CONTEXT) is not None
    cache.drop("k1")
    assert (cache.hits, cache.misses) == (0, 1)
    assert len(cache) == 0


def test_expired_entries_under_other_keys_are_removed_as_expired():
    """RM24: the TTL ends every entry, not only the one looked up; eviction is not expiry."""
    clock = FakeClock()
    cache = RoutingCache(clock=clock.monotonic)
    for number in range(MAX_ENTRIES):
        cache.store(f"old_{number}", CONTEXT, route())
    clock.advance(TTL_SECONDS)
    cache.store("new", CONTEXT, route())
    assert (cache.expired, cache.evicted, len(cache)) == (MAX_ENTRIES, 0, 1)
    clock.advance(1)
    cache.store("newer", CONTEXT, route())
    assert cache.lookup("unknown", CONTEXT) is None
    assert cache.expired == MAX_ENTRIES and len(cache) == 2


def test_a_store_under_a_new_context_drops_the_entries_of_the_old_one():
    """AC13, RM07: entries of another skill set or permission set never outlive the change."""
    changed = CacheContext(CONTEXT.skill_hashes, frozenset(), CONTEXT.enabled_tools)
    cache = RoutingCache(clock=FakeClock().monotonic)
    cache.store("k1", CONTEXT, route())
    cache.store("k2", changed, route())
    assert len(cache) == 1 and cache.invalidated == 1
    assert cache.lookup("k2", changed) is not None
    assert cache.lookup("k2", CONTEXT) is None
    assert len(cache) == 0 and cache.invalidated == 2


def key_of(messages, **changes):
    from relaymind.orchestrator.routing_cache import key_and_context

    class _Policy:
        enabled_tools = frozenset({"get_service_health"})

    class _Caller:
        permissions = frozenset({"demo.read"})

    options = {
        "messages": messages,
        "language": "en",
        "mode": "auto",
        "offered": [],
        "tools": None,
        "policy": _Policy(),
        "caller": _Caller(),
        "model_id": "local-1",
        "enabled_workflows": [],
        "routing_prompt_version": "v1",
    }
    options.update(changes)
    return key_and_context(**options)[0]


def assistant_call(**arguments):
    from relaymind.adapters import Message, Role, ToolCall

    return Message(
        role=Role.ASSISTANT,
        tool_calls=(ToolCall("call_1", "get_service_health", arguments),),
    )


def test_the_key_holds_the_tool_calls_of_assistant_turns():
    """RM24, spec 0.26 Key: the exact messages the router receives, tool calls included."""
    from relaymind.adapters import Message, Role

    user = Message(role=Role.USER, content="check it")
    answer = Message(role=Role.TOOL, content='{"ok": true}', tool_call_id="call_1")
    staging = key_of([user, assistant_call(service="staging-api"), answer])
    payments = key_of([user, assistant_call(service="payments-api"), answer])
    assert staging != payments
    assert staging == key_of([user, assistant_call(service="staging-api"), answer])


def test_the_key_holds_the_routing_prompt_version():
    """RM24: a different routing prompt version is a different key."""
    from relaymind.adapters import Message, Role

    messages = [Message(role=Role.USER, content="check it")]
    assert key_of(messages) != key_of(messages, routing_prompt_version="v2")


def test_the_routing_prompt_version_follows_the_prompt_and_the_functions():
    """RM24, spec 0.26 Key: derived from the routing prompt and functions, so no manual bump."""
    from relaymind.adapters.base import ToolSchema
    from relaymind.orchestrator.routing_cache import routing_prompt_version

    schema = {"type": "object", "properties": {}, "additionalProperties": False}
    functions = (ToolSchema("check-service-health", "Check a service", schema),)
    base = routing_prompt_version(functions)
    assert base == routing_prompt_version(functions)
    assert routing_prompt_version(functions, system_prompt=SYSTEM_PROMPT + " ") != base
    other = (ToolSchema("check-service-health", "Check one service", schema),)
    assert routing_prompt_version(other) != base
    assert routing_prompt_version(functions, system_prompt=SYSTEM_PROMPT) == base
