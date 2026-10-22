"""The routing cache in the gateway (RM01, RM07, RM08, RM22, RM24, AC13; spec 0.26 section 10).

All with model doubles: a scripted routing turn that is missing proves
that no routing generation ran.
"""

import dataclasses
import shutil

from gateway_helpers import (
    LOCAL_MODEL_ID,
    STAGING_OK,
    build_gateway,
    executed_tools,
    health_tail,
    purposes,
    route_turn,
    run,
    tool_turn,
)
from helpers import DEMO_SKILLS_ROOT, FakeClock, ScriptedAdapter, of_type
from relaymind.orchestrator.local_loop import ConfiguredModel, LocalOrchestrator
from relaymind.orchestrator.routing_cache import RoutingCache
from relaymind.orchestrator.run_state import RunState
from relaymind.tools import CallerContext, OperatorPolicy
from relaymind.tools.sandbox import DEMO_TOOL_NAMES, MUTATE_PERMISSION, READ_PERMISSION

HEALTH = "check-service-health"
PROMPT = "Is staging-api healthy right now?"


def cached_gateway(tmp_path, turns, **options):
    clock = FakeClock()
    cache = RoutingCache(clock=clock.monotonic)
    gateway = build_gateway(tmp_path, turns, clock=clock, routing_cache=cache, **options)
    return gateway, cache, clock


def second_orchestrator(gateway, cache, turns, *, skills=None, caller=None):
    """Another orchestrator over the same store, tools, and cache, with its own model double."""
    adapter = ScriptedAdapter(turns=turns, input_tokens=800)
    orchestrator = LocalOrchestrator(
        ConfiguredModel(adapter=adapter, model_id=LOCAL_MODEL_ID),
        skills=skills or gateway.skills,
        tools=gateway.tools,
        store=gateway.store,
        policy=OperatorPolicy(enabled_tools=DEMO_TOOL_NAMES),
        caller=caller or CallerContext(permissions=frozenset({READ_PERMISSION})),
        routing_cache=cache,
    )
    return dataclasses.replace(
        gateway, orchestrator=orchestrator, adapter=adapter, skills=orchestrator.skills
    )


def routing_calls(adapter):
    """The routing generations a model double was asked for."""
    return [r for r in adapter.requests if r.messages[0].content.startswith("You are a request")]


def test_the_cache_is_off_by_default(tmp_path):
    """RM24: no cache unless configured; nothing is read, written, or labeled."""
    gateway = build_gateway(
        tmp_path, [route_turn(HEALTH), *health_tail(), route_turn(HEALTH), *health_tail()]
    )
    assert gateway.orchestrator.routing_cache is None
    first, first_events = run(gateway)
    second, second_events = run(gateway)
    for events in (first_events, second_events):
        assert not of_type(events, "cache.hit")
        assert "routing_cache" not in events[0].payload
        assert "routing_cache" not in of_type(events, "route.selected")[0].payload
    assert len(routing_calls(gateway.adapter)) == 2


def test_a_repeated_request_skips_the_routing_generation(tmp_path):
    """RM24, RM22, RM01: the second run is routed from the cache, with cache.hit."""
    gateway, cache, _ = cached_gateway(
        tmp_path, [route_turn(HEALTH), *health_tail(), *health_tail()]
    )
    first, first_events = run(gateway)
    assert first.state is RunState.COMPLETED
    assert of_type(first_events, "route.selected")[0].payload["routing_cache"] == "miss"
    assert first_events[0].payload["routing_cache"] is True

    second, events = run(gateway)
    assert second.state is RunState.COMPLETED and second.answer == STAGING_OK
    assert len(routing_calls(gateway.adapter)) == 1
    (hit,) = of_type(events, "cache.hit")
    skill = gateway.skills.get(HEALTH)
    assert hit.actor == "gateway"
    assert hit.payload["originating_version"] == skill.version
    assert hit.payload["originating_run_id"] == first.run_id
    assert hit.payload["skill"] == {
        "name": HEALTH,
        "version": skill.version,
        "content_hash": skill.content_hash,
    }
    assert isinstance(hit.payload["age_ms"], int) and len(hit.payload["key"]) == 64
    (selected,) = of_type(events, "route.selected")
    assert selected.actor == "gateway"
    assert selected.payload["mechanism"] == "routing_cache"
    assert selected.payload["routing_cache"] == "hit"
    assert selected.payload["decision"]["skill_id"] == HEALTH
    assert selected.payload["decision"]["route"] == "local"
    assert "model" not in selected.payload and "call_id" not in selected.payload
    assert events.index(hit) < events.index(selected)
    # RM01: still no external call, and no routing model call is recorded.
    assert "routing" not in purposes(events)
    assert of_type(events, "run.completed")[0].payload["external_calls"] == 0
    assert cache.counters()["hits"] == 1


def test_a_hit_executes_fresh_tools_every_time(tmp_path):
    """RM24, RM10: a cached route is not a cached answer; the tool runs again."""
    gateway, _, _ = cached_gateway(tmp_path, [route_turn(HEALTH), *health_tail(), *health_tail()])
    run(gateway)
    _, events = run(gateway)
    assert executed_tools(events) == ["get_service_health"]
    (finished,) = of_type(events, "tool.finished")
    assert finished.payload["status"] == "succeeded"
    first_ops = {o["operation_id"] for o in gateway.store.tool_operations(events[0].run_id)}
    assert first_ops  # a fresh operation under the new run


def test_a_hit_still_checks_current_authorization(tmp_path, monkeypatch):
    """RM24, RM08, RM09: the tools are intersected again, and an unpermitted call is refused."""
    unauthorized = tool_turn(
        "delete_demo_database", database_id="demo-orders-db", expected_revision=1
    )
    gateway, _, _ = cached_gateway(
        tmp_path,
        [route_turn(HEALTH), *health_tail(), unauthorized, *health_tail()],
    )
    run(gateway)
    calls = []
    original = gateway.tools.available_tools

    def spy(**kwargs):
        calls.append(kwargs["caller"])
        return original(**kwargs)

    monkeypatch.setattr(gateway.tools, "available_tools", spy)
    _, events = run(gateway)
    assert of_type(events, "cache.hit")
    assert len(calls) >= 1
    assert "delete_demo_database" not in executed_tools(events)
    refused = [e for e in of_type(events, "tool.proposed") if e.payload.get("admissible") is False]
    assert refused and refused[0].payload["tool_name"] == "delete_demo_database"


def test_a_different_request_or_language_is_a_miss(tmp_path):
    """RM24: exact request and context matching."""
    gateway, cache, _ = cached_gateway(
        tmp_path,
        [route_turn(HEALTH), *health_tail(), route_turn(HEALTH), *health_tail()],
    )
    run(gateway)
    _, events = run(gateway, "Is staging-api healthy right now? Please check.")
    assert not of_type(events, "cache.hit")
    assert of_type(events, "route.selected")[0].payload["routing_cache"] == "miss"
    assert cache.hits == 0


def test_the_entry_expires_after_five_minutes(tmp_path):
    """RM24: five-minute TTL."""
    gateway, cache, clock = cached_gateway(
        tmp_path,
        [route_turn(HEALTH), *health_tail(), route_turn(HEALTH), *health_tail()],
    )
    run(gateway)
    clock.advance(300)
    _, events = run(gateway)
    assert not of_type(events, "cache.hit")
    assert cache.expired == 1 and len(routing_calls(gateway.adapter)) == 2


def test_mutating_skills_and_non_local_routes_are_never_stored(tmp_path):
    """RM24, RM11: only local read-only routes are eligible."""
    caller = CallerContext(permissions=frozenset({READ_PERMISSION, MUTATE_PERMISSION}))
    delete = tool_turn("delete_demo_database", database_id="demo-orders-db", expected_revision=1)
    gateway, cache, _ = cached_gateway(
        tmp_path,
        [route_turn("delete-demo-database"), delete, route_turn("investigate-performance")],
        caller=caller,
    )
    run(gateway, "Delete demo-orders-db please")
    run(gateway, "Why is staging-api slow?")
    assert cache.stores == 0 and len(cache) == 0


def test_a_clarification_is_not_stored(tmp_path):
    """RM24: a clarify route is not a local route."""
    from gateway_helpers import clarify_turn

    gateway, cache, _ = cached_gateway(tmp_path, [clarify_turn()])
    run(gateway, "Is it healthy?")
    assert cache.stores == 0


def edit_skill(root):
    path = root / HEALTH / "SKILL.md"
    path.write_text(path.read_text("utf-8") + "\nAlso mention the time.\n", "utf-8")


def test_changing_a_skill_invalidates_the_entries(tmp_path):
    """AC13, RM07: after a skill change the cache is empty and the run routes by model."""
    root = tmp_path / "skills"
    shutil.copytree(DEMO_SKILLS_ROOT, root)
    gateway, cache, _ = cached_gateway(
        tmp_path, [route_turn(HEALTH), *health_tail()], skill_roots=[root]
    )
    run(gateway)
    assert len(cache) == 1
    edit_skill(root)
    changed = second_orchestrator(
        gateway, cache, [route_turn(HEALTH), *health_tail()], skills=gateway.skills.reloaded()
    )
    outcome, events = run(changed)
    assert outcome.state is RunState.COMPLETED
    assert not of_type(events, "cache.hit")
    assert cache.invalidated == 1
    assert len(routing_calls(changed.adapter)) == 1
    new_hash = changed.skills.get(HEALTH).content_hash
    assert new_hash != gateway.skills.get(HEALTH).content_hash
    # The new entry is under the new hash, and it serves a repeat.
    again = second_orchestrator(changed, cache, health_tail(), skills=changed.skills)
    _, repeat = run(again)
    assert of_type(repeat, "cache.hit")[0].payload["skill"]["content_hash"] == new_hash


def test_changing_the_caller_permissions_invalidates_and_applies(tmp_path):
    """AC13, RM08: a permission change drops entries; current permissions apply."""
    gateway, cache, _ = cached_gateway(tmp_path, [route_turn(HEALTH), *health_tail()])
    run(gateway)
    assert len(cache) == 1
    nobody = second_orchestrator(
        gateway, cache, [route_turn(HEALTH)], caller=CallerContext(permissions=frozenset())
    )
    outcome, events = run(nobody)
    assert not of_type(events, "cache.hit")
    assert cache.invalidated == 1
    assert outcome.state is RunState.FAILED  # capability_missing, as without a cache
    assert executed_tools(events) == []


def test_the_baseline_stays_cache_off(tmp_path):
    """AC13: a gateway built without the setting never touches a cache."""
    gateway = build_gateway(tmp_path, [route_turn(HEALTH), *health_tail()])
    run(gateway)
    assert gateway.orchestrator.routing_cache is None


def test_the_hit_run_costs_no_routing_tokens(tmp_path):
    """RM24: the second run's model calls have no routing purpose."""
    gateway, _, _ = cached_gateway(tmp_path, [route_turn(HEALTH), *health_tail(), *health_tail()])
    _, first = run(gateway)
    _, second = run(gateway)
    assert purposes(first)[0] == "routing"
    assert "routing" not in purposes(second)


def test_the_trace_view_shows_a_cached_route_as_gateway_made(tmp_path):
    """RM23, RM25, spec 0.26: the mechanism is visible, and the hit is not a model action."""
    from relaymind.ui.trace_view import build_trace_view

    gateway, _, _ = cached_gateway(tmp_path, [route_turn(HEALTH), *health_tail(), *health_tail()])
    run(gateway)
    outcome, events = run(gateway)
    view = build_trace_view(gateway.store, outcome.run_id)
    assert view["route"]["mechanism"] == "routing_cache"
    proposal = of_type(events, "tool.proposed")[0]
    assert view["timing"]["first_model_action_ms"] == proposal.elapsed_ms


# -- review findings for #31 ------------------------------------------------------


def labeled_route(events):
    (selected,) = of_type(events, "route.selected")
    return selected.payload


def test_a_bypass_route_of_a_cache_run_is_labeled_a_miss(tmp_path):
    """RM22, spec 0.26 Labels: every route.selected of a cache run carries hit or miss."""
    gateway, _, _ = cached_gateway(tmp_path, [], input_tokens=2049)
    _, events = run(gateway)
    payload = labeled_route(events)
    assert payload["mechanism"] == "bypass"
    assert payload["routing_cache"] == "miss"


def test_an_execution_mode_route_of_a_cache_run_is_labeled_a_miss(tmp_path):
    """RM22, spec 0.26 Labels: the external_only decision is labeled too."""
    gateway, _, _ = cached_gateway(tmp_path, [])
    _, events = run(gateway, execution_mode="external_only")
    payload = labeled_route(events)
    assert payload["mechanism"] == "execution_mode"
    assert payload["routing_cache"] == "miss"


def test_a_degraded_route_of_a_cache_run_is_labeled_a_miss(tmp_path):
    """RM22, spec 0.26 Labels: the degraded_mode decision is labeled too."""
    from gateway_helpers import text_turn
    from relaymind.adapters import GenerationFailed

    gateway, _, _ = cached_gateway(
        tmp_path,
        [[GenerationFailed(code="unreachable", message="no route to host")]],
        external_turns=[text_turn(STAGING_OK)],
        degraded_mode=True,
    )
    _, events = run(gateway)
    payload = labeled_route(events)
    assert payload["mechanism"] == "degraded_mode"
    assert payload["routing_cache"] == "miss"


def test_a_clarification_resume_route_of_a_cache_run_is_labeled_a_miss(tmp_path):
    """RM22, spec 0.26 Labels: the resumed decision is labeled too."""
    import asyncio

    gateway, _, _ = cached_gateway(tmp_path, [route_turn(HEALTH), *health_tail()])
    waiting, _ = run(gateway, "Is the service healthy?")
    assert waiting.state is RunState.WAITING_INPUT
    record = gateway.store.get_run(waiting.run_id)
    asyncio.run(gateway.orchestrator.resume(waiting.run_id, record.version, "staging-api"))
    selected = of_type(gateway.store.events(waiting.run_id), "route.selected")
    assert [e.payload["mechanism"] for e in selected] == [
        "native_function_calling",
        "clarification_resume",
    ]
    assert [e.payload["routing_cache"] for e in selected] == ["miss", "miss"]


def test_a_client_owned_route_of_a_cache_run_is_labeled_a_miss(tmp_path):
    """RM22, spec 0.26 Labels: the client alias decision is labeled too."""
    from api_helpers import RUN_ID_HEADER, chat, make_app
    from relaymind.api.chat import ClientToolsChat
    from relaymind.api.chat_request import CLIENT_TOOLS

    weather = {
        "type": "function",
        "function": {
            "name": "get_weather",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
                "additionalProperties": False,
            },
        },
    }
    gateway, _, _ = cached_gateway(
        tmp_path, [tool_turn("get_weather", call_id="call_w1", city="Oslo")], external_turns=[]
    )
    app = make_app(gateway, client_tools=ClientToolsChat(gateway.orchestrator))
    response = chat(app, prompt="What is the weather in Oslo?", model=CLIENT_TOOLS, tools=[weather])
    events = gateway.store.events(response.headers[RUN_ID_HEADER])
    assert events[0].payload["routing_cache"] is True
    payload = labeled_route(events)
    assert payload["mechanism"] == "client_owned"
    assert payload["routing_cache"] == "miss"


def test_a_hit_the_gateway_cannot_use_counts_as_a_miss(tmp_path, monkeypatch):
    """RM24, spec 0.26 Hit: a cached choice that is not a local route is a miss, in the counters."""
    from relaymind.orchestrator.route_decision import RouteDecisionError

    gateway, cache, _ = cached_gateway(
        tmp_path, [route_turn(HEALTH), *health_tail(), route_turn(HEALTH), *health_tail()]
    )
    run(gateway)

    def refuse(*args, **kwargs):
        raise RouteDecisionError("the cached skill no longer fits")

    monkeypatch.setattr(gateway.orchestrator.router, "resume_decision", refuse)
    _, events = run(gateway)
    assert not of_type(events, "cache.hit")
    assert labeled_route(events)["routing_cache"] == "miss"
    counters = cache.counters()
    assert (counters["hits"], counters["misses"]) == (0, 2)
