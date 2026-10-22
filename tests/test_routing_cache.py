"""The routing-result cache store (RM24, RM07, AC13; spec 0.26 section 10)."""

import pytest

from helpers import FakeClock
from relaymind.canonical import canonical_digest
from relaymind.orchestrator.router import SYSTEM_PROMPT
from relaymind.orchestrator.routing_cache import (
    MAX_ENTRIES,
    POLICY_VERSION,
    ROUTING_PROMPT_VERSION,
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
    }
    base.update(changes)
    return base


def test_the_limits_are_the_policy_values():
    """RM24: five minutes, 256 entries."""
    assert TTL_SECONDS == 300
    assert MAX_ENTRIES == 256


def test_the_prompt_version_is_bumped_with_the_routing_prompt():
    """RM24: the key holds a routing prompt version; changing the prompt must change it.

    If this fails after an edit of the routing prompt, bump
    ROUTING_PROMPT_VERSION and update the digest here.
    """
    assert canonical_digest(SYSTEM_PROMPT) == (
        "d28e0009db29519eb31791be5e1dd8942a486401527333de2906bcb8bdb7c54f"
    )
    assert ROUTING_PROMPT_VERSION == "1"
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
    ]
    keys = {routing_cache_key(parts(**change)) for change in changes}
    assert base not in keys and len(keys) == len(changes)


def test_key_holds_the_prompt_and_policy_versions(monkeypatch):
    """RM24: a new routing prompt version or policy version is a different key."""
    from relaymind.orchestrator import routing_cache

    base = routing_cache_key(parts())
    monkeypatch.setattr(routing_cache, "ROUTING_PROMPT_VERSION", "2")
    assert routing_cache_key(parts()) != base
    monkeypatch.undo()
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
