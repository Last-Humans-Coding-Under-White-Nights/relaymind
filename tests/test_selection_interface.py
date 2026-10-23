"""The selection interface: request and result schemas, the chain, admission (RM28, AC22).

Spec 0.31 RM02, RM05, RM08, RM22, RM28; sections 4.1, 8, 10, 11, and 14.1.
A selector proposes a skill, a clarification, or nothing; the gateway
validates the result, checks catalog membership and the mode, runs the
RM05 identifier check, intersects the narrowing with the allowlist, and
builds the RouteDecision. Everything here uses scripted selectors and
model doubles (spec section 13); the time limit is driven by an injected
sleep, never by the wall clock.
"""

import asyncio
import copy
import json
import math
from importlib import resources

import pytest

from gateway_helpers import (
    LOCAL_MODEL_ID,
    STAGING_OK,
    build_gateway,
    clarify_turn,
    executed_tools,
    health_tail,
    purposes,
    route_turn,
    run,
    text_turn,
    tool_turn,
)
from helpers import FakeClock, make_demo_skills, make_demo_tools, of_type
from relaymind.adapters import GenerationOptions, Message, Role, ToolCall
from relaymind.orchestrator.budgets import LocalBudgets
from relaymind.orchestrator.route_decision import ExecutionMode, Route
from relaymind.orchestrator.router import Router
from relaymind.orchestrator.run_state import RunState
from relaymind.selection import (
    EXHAUSTED_MECHANISM,
    MODEL_ROUTER,
    SELECTION_REQUEST_SCHEMA,
    SELECTION_RESULT_SCHEMA,
    InadmissibleError,
    ModelRouterSelector,
    Outcome,
    RoutingGeneration,
    SelectionContext,
    SelectionError,
    SelectionSettings,
    SelectorChain,
    SelectorError,
    parse_selection_request,
    parse_selection_result,
)
from relaymind.tools import CallerContext
from relaymind.tools.sandbox import MUTATE_PERMISSION, READ_PERMISSION

PROMPT = "Is staging-api healthy right now?"
HEALTH = "check-service-health"
DELETE_SKILL = "delete-demo-database"


def result(
    name="scripted",
    kind="skill",
    skill_id=HEALTH,
    *,
    clarification=None,
    tools=None,
    alternatives=None,
    mechanism="scripted_match",
    version="1",
    confidence=None,
):
    """A SelectionResult in its published JSON shape."""
    candidate = {"kind": kind}
    if kind == "skill":
        candidate["skill_id"] = skill_id
    if kind == "clarify":
        candidate["clarification"] = clarification
    data = {
        "schema_version": 1,
        "candidate": candidate,
        "mechanism": mechanism,
        "confidence": confidence,
        "selector": {"name": name, "version": version},
        "model_calls": [],
    }
    if tools is not None:
        data["tools"] = tools
    if alternatives is not None:
        data["alternatives"] = alternatives
    return data


class Scripted:
    """A selector double: returns its result, raises, or waits forever."""

    model_based = False

    def __init__(self, name, answer=None, *, raises=None, hang=False, version="1"):
        self.name = name
        self.version = version
        self.answer = answer
        self.raises = raises
        self.hang = hang
        self.requests = []
        self.contexts = []
        self.cancelled = False

    async def select(self, request, context):
        self.requests.append(request)
        self.contexts.append(context)
        if self.hang:
            try:
                await asyncio.Event().wait()
            finally:
                self.cancelled = True
        if self.raises is not None:
            raise self.raises
        return self.answer


def none_of(name):
    return Scripted(name, result(name, kind="none"))


def pick(name, skill_id=HEALTH, **fields):
    return Scripted(name, result(name, skill_id=skill_id, **fields))


def request_data(**changes):
    data = {
        "schema_version": 1,
        "run_id": "run_1",
        "conversation": [{"role": "user", "content": PROMPT}],
        "language": "en",
        "execution_mode": "auto",
        "catalog": [
            {
                "id": HEALTH,
                "version": "0.2.0",
                "content_hash": "sha256:abc",
                "description": "Report the current health of one demo service.",
                "languages": ["en", "zh"],
                "local_eligible": True,
                "allowed_tools": ["get_service_health"],
                "examples": ["Is staging-api healthy right now?"],
            }
        ],
        "permissions": ["demo.read"],
        "deadline_ms": None,
    }
    data.update(changes)
    return data


# --- the two published schemas (RM28, section 8) -----------------------------------


def test_the_two_schemas_are_published_closed_and_versioned():
    """RM28: selection_request.v1.json and selection_result.v1.json, schema_version 1, closed."""
    root = resources.files("relaymind.selection").joinpath("schemas")
    for name, loaded in (
        ("selection_request.v1.json", SELECTION_REQUEST_SCHEMA),
        ("selection_result.v1.json", SELECTION_RESULT_SCHEMA),
    ):
        schema = json.loads(root.joinpath(name).read_text("utf-8"))
        assert schema == loaded
        assert schema["additionalProperties"] is False
        assert schema["properties"]["schema_version"] == {"const": 1}
    request = SELECTION_REQUEST_SCHEMA
    assert set(request["required"]) == {
        "schema_version",
        "run_id",
        "conversation",
        "language",
        "execution_mode",
        "catalog",
        "permissions",
        "deadline_ms",
    }
    entry = request["$defs"]["skill"]
    assert entry["additionalProperties"] is False
    assert set(entry["required"]) == {
        "id",
        "version",
        "content_hash",
        "description",
        "languages",
        "local_eligible",
        "allowed_tools",
        "examples",
    }
    outcome = SELECTION_RESULT_SCHEMA
    assert set(outcome["required"]) == {
        "schema_version",
        "candidate",
        "mechanism",
        "confidence",
        "selector",
        "model_calls",
    }
    # Optional: the narrowing and the alternatives (RM28).
    assert {"tools", "alternatives"} <= set(outcome["properties"])


def test_a_valid_request_and_result_parse():
    request = parse_selection_request(request_data())
    assert request.run_id == "run_1"
    assert request.catalog[0]["id"] == HEALTH
    assert request.deadline_ms is None
    assert request.with_deadline(2000).deadline_ms == 2000
    parsed = parse_selection_result(
        result(tools=["get_service_health"], alternatives=[DELETE_SKILL], confidence=0.75)
    )
    assert parsed.candidate.kind == "skill"
    assert parsed.candidate.skill_id == HEALTH
    assert parsed.tools == ("get_service_health",)
    assert parsed.alternatives == (DELETE_SKILL,)
    assert parsed.confidence == 0.75
    assert parse_selection_result(parsed.to_json_object()) == parsed


@pytest.mark.parametrize(
    "change",
    [
        {"extra": 1},
        {"schema_version": 2},
        {"schema_version": True},
        {"candidate": {"kind": "skill"}},
        {"candidate": {"kind": "clarify", "clarification": "  "}},
        {"candidate": {"kind": "none", "skill_id": HEALTH}},
        {"candidate": {"kind": "maybe"}},
        {"confidence": 1.5},
        {"confidence": math.inf},
        {"confidence": math.nan},
        {"confidence": True},
        {"tools": ["get_service_health", "get_service_health"]},
        {"selector": {"name": "scripted"}},
        {"model_calls": [{"call_id": "c1"}]},
    ],
    ids=lambda change: next(iter(change)) + ":" + repr(next(iter(change.values())))[:20],
)
def test_an_invalid_result_is_refused_at_the_boundary(change):
    """RM28: the gateway validates every result against the published schema."""
    with pytest.raises(SelectionError):
        parse_selection_result({**result(), **change})


@pytest.mark.parametrize(
    "change",
    [
        {"language": "fr"},
        {"execution_mode": "turbo"},
        {"deadline_ms": 0},
        {"catalog": [{"id": HEALTH}]},
        {"conversation": [{"role": "tool", "content": "x"}]},
        {"secret": "x"},
    ],
    ids=lambda change: next(iter(change)),
)
def test_an_invalid_request_is_refused_at_the_boundary(change):
    """RM28: the request the gateway sends is validated too (both directions)."""
    with pytest.raises(SelectionError):
        parse_selection_request(request_data(**change))


def test_a_missing_request_field_is_refused():
    data = request_data()
    del data["permissions"]
    with pytest.raises(SelectionError, match="permissions"):
        parse_selection_request(data)


# --- the chain (RM28, section 11) ------------------------------------------------------


def admit_known(result):
    """A stand-in for gateway admission: only the health skill is in the catalog."""
    if result.candidate.kind == "skill" and result.candidate.skill_id != HEALTH:
        raise InadmissibleError(f"{result.candidate.skill_id} is not offered")
    return result.candidate


def chain_run(selectors, *, sleep=None, context=None, timeout_seconds=2.0):
    clock = FakeClock()
    options = {} if sleep is None else {"sleep": sleep}
    chain = SelectorChain(
        selectors, timeout_seconds=timeout_seconds, clock=clock.monotonic, **options
    )
    request = parse_selection_request(request_data())
    return asyncio.run(chain.run(request, context or SelectionContext(), admit_known))


async def instant(_seconds):
    """A sleep that ends at once: the time limit is reached before a waiting selector ends."""
    await asyncio.sleep(0)


async def never(_seconds):
    await asyncio.Event().wait()


def test_the_chain_records_every_attempt_in_order_and_stops_at_the_first_admitted():
    """RM28: none, an error, an invalid result, and an inadmissible result fall through."""
    selectors = [
        none_of("a"),
        Scripted("b", raises=RuntimeError("selector broke")),
        Scripted("c", {"not": "a result"}),
        pick("d", skill_id="no-such-skill"),
        pick("e"),
        pick("f"),
    ]
    outcome = chain_run(selectors, sleep=never)
    assert [(a.name, a.outcome) for a in outcome.attempts] == [
        ("a", Outcome.NONE),
        ("b", Outcome.ERROR),
        ("c", Outcome.ERROR),
        ("d", Outcome.INADMISSIBLE),
        ("e", Outcome.SELECTED),
    ]
    assert outcome.selector is selectors[4]
    assert outcome.admitted.skill_id == HEALTH
    assert selectors[5].requests == []
    record = outcome.selector_record()
    assert record["name"] == "e" and record["version"] == "1"
    assert record["mechanism"] == "scripted_match"
    assert record["attempts"][0] == {"name": "a", "outcome": "none"}


def test_a_selector_past_the_time_limit_times_out_and_is_cancelled():
    """Section 11: a non-model selector gets selection.timeout_seconds; then it falls through."""
    slow = Scripted("slow", result("slow"), hang=True)
    outcome = chain_run([slow, pick("next")], sleep=instant)
    assert [(a.name, a.outcome) for a in outcome.attempts] == [
        ("slow", Outcome.TIMEOUT),
        ("next", Outcome.SELECTED),
    ]
    assert slow.cancelled is True


@pytest.mark.parametrize("kind", ["skill", "none"])
def test_a_selector_cannot_return_a_result_after_its_timeout(kind):
    """RM28, AC22: a result returned during cancellation cleanup is still a timeout."""

    class LateResult(Scripted):
        async def select(self, request, context):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await asyncio.sleep(0)
                return result(self.name, kind=kind)

    outcome = chain_run([LateResult("late"), pick("next")], sleep=instant)
    assert [(a.name, a.outcome) for a in outcome.attempts] == [
        ("late", Outcome.TIMEOUT),
        ("next", Outcome.SELECTED),
    ]
    assert outcome.selector.name == "next"


def test_the_time_limit_and_the_deadline_come_from_the_configured_timeout():
    """Section 11: the configured limit bounds the selector and is its request deadline."""
    slept = []

    async def sleep(seconds):
        slept.append(seconds)
        await asyncio.Event().wait()

    selector = pick("a")
    chain_run([selector], sleep=sleep, timeout_seconds=0.75)
    assert slept == [0.75]
    assert selector.requests[0].deadline_ms == 750
    # A non-model selector is never handed the routing generation.
    assert selector.contexts[0].generate is None


class ModelBased(Scripted):
    model_based = True

    async def select(self, request, context):
        self.requests.append(request)
        self.contexts.append(context)
        generation = await context.generate((), (), GenerationOptions())
        return result(self.name, skill_id=generation.calls[0].tool_name)


def test_a_model_based_selector_is_bounded_by_its_generation_not_the_time_limit():
    """Section 11: a model-based selector has no selection time limit and no deadline."""
    slept = []

    async def sleep(seconds):
        slept.append(seconds)

    async def generate(messages, functions, options):
        return RoutingGeneration(calls=(ToolCall("c1", HEALTH, {}),), model="m")

    selector = ModelBased("model")
    outcome = chain_run([selector], sleep=sleep, context=SelectionContext(generate=generate))
    assert outcome.attempts[0].outcome == Outcome.SELECTED
    assert slept == []
    assert selector.requests[0].deadline_ms is None


def test_a_failure_of_the_gateways_generation_is_not_a_selector_error():
    """RM03, RM04: a budget or model stop in the routing generation ends the chain."""

    class Stop(Exception):
        pass

    async def generate(messages, functions, options):
        raise Stop("budget")

    class Swallowing(ModelBased):
        async def select(self, request, context):
            try:
                await context.generate((), (), GenerationOptions())
            except Exception:
                pass
            return result(self.name)

    for selector in (ModelBased("model"), Swallowing("swallowing")):
        with pytest.raises(Stop):
            chain_run([selector, pick("next")], context=SelectionContext(generate=generate))


@pytest.mark.parametrize("after_cancel", ["propagate", "return", "raise_error"])
def test_a_selector_cannot_replace_gateway_generation_cancellation(after_cancel):
    """RM14, RM28: gateway cancellation ends selection, even if a selector catches it."""
    cancelled = asyncio.CancelledError("gateway stopped the routing generation")

    async def generate(messages, functions, options):
        raise cancelled

    class CatchingCancellation(ModelBased):
        async def select(self, request, context):
            try:
                await context.generate((), (), GenerationOptions())
            except BaseException:
                if after_cancel == "propagate":
                    raise
                if after_cancel == "raise_error":
                    raise RuntimeError("selector cleanup failed") from None
            return result(self.name)

    fallback = pick("next")
    with pytest.raises(asyncio.CancelledError) as caught:
        chain_run(
            [CatchingCancellation("catching"), fallback],
            context=SelectionContext(generate=generate),
        )
    assert caught.value is cancelled
    assert fallback.requests == []


def test_an_exhausted_chain_admits_nothing():
    outcome = chain_run([none_of("a"), pick("b", skill_id="no-such-skill")], sleep=never)
    assert outcome.admitted is None and outcome.selector is None
    record = outcome.selector_record()
    assert record["name"] is None and record["mechanism"] is None
    assert [a["outcome"] for a in record["attempts"]] == ["none", "inadmissible"]


def test_a_result_must_name_its_own_selector_and_no_reserved_mechanism():
    """Section 8: a selector cannot claim another's name, nor a mechanism of the gateway."""
    outcome = chain_run(
        [
            Scripted("a", result("b")),
            Scripted("c", result("c", version="2")),
            Scripted("d", result("d", mechanism="routing_cache")),
            Scripted("e", result("e", mechanism="native_function_calling")),
            Scripted("f", result("f", mechanism=EXHAUSTED_MECHANISM)),
        ],
        sleep=never,
    )
    assert [a.outcome for a in outcome.attempts] == [Outcome.ERROR] * 5


@pytest.mark.parametrize("names", [[], ["a", "a"]])
def test_a_chain_needs_distinct_selectors(names):
    with pytest.raises(ValueError):
        SelectorChain([pick(name) for name in names], timeout_seconds=2)


@pytest.mark.parametrize("timeout", [0, -1, math.inf, -math.inf, math.nan, True, False, "1", None])
def test_a_chain_rejects_invalid_timeout_before_selection(timeout):
    """RM03, RM28: direct construction cannot silently weaken the configured time limit."""
    with pytest.raises(ValueError, match="timeout_seconds"):
        SelectorChain([pick("a")], timeout_seconds=timeout)


# --- model_router outside a gateway run (the injectable generation) -------------------


def demo_router(tmp_path):
    tools = make_demo_tools(tmp_path)
    return Router(make_demo_skills(tools), tools, budgets=LocalBudgets())


def test_model_router_runs_its_routing_generation_through_the_callback(tmp_path):
    """RM02, RM28: model_router wraps the router; the generation is the caller's callback."""
    router = demo_router(tmp_path)
    seen = []

    async def generate(messages, functions, options):
        seen.append((messages, functions, options))
        return RoutingGeneration(
            calls=(ToolCall("call_route", HEALTH, {"language": "en"}),),
            model=LOCAL_MODEL_ID,
            call_id="run_gen1_routing",
        )

    messages = (Message(role=Role.USER, content=PROMPT),)
    selector = ModelRouterSelector(router)
    request = parse_selection_request(request_data())
    raw = asyncio.run(selector.select(request, SelectionContext(messages, generate)))
    parsed = parse_selection_result(raw)
    assert (selector.name, selector.version, selector.model_based) == (MODEL_ROUTER, "1", True)
    assert parsed.candidate.skill_id == HEALTH
    assert parsed.mechanism == "native_function_calling"
    assert parsed.confidence is None
    assert parsed.model_calls == ({"model": LOCAL_MODEL_ID, "call_id": "run_gen1_routing"},)
    # The routing request is exactly the router's (RM24: same prompt, same cache key).
    assert seen == [(router.routing_messages(messages), router.functions(), router.options())]


@pytest.mark.parametrize(
    "calls",
    [
        (),
        (ToolCall("c", "no-such-skill", {"language": "en"}),),
        (ToolCall("c", HEALTH, {"language": "fr"}),),
        (ToolCall("c", HEALTH, {"language": "en", "service": "staging-api"}),),
    ],
    ids=["no-call", "unknown-function", "bad-language", "extra-argument"],
)
def test_model_router_reports_rejected_routing_output_as_a_selector_error(tmp_path, calls):
    """RM02: invalid routing output never becomes a selection."""
    router = demo_router(tmp_path)

    async def generate(messages, functions, options):
        return RoutingGeneration(calls=calls, model=LOCAL_MODEL_ID)

    request = parse_selection_request(request_data())
    with pytest.raises(SelectorError):
        asyncio.run(
            ModelRouterSelector(router).select(
                request, SelectionContext((Message(role=Role.USER, content=PROMPT),), generate)
            )
        )


def test_model_router_turns_a_clarification_call_into_a_clarify_candidate(tmp_path):
    router = demo_router(tmp_path)

    async def generate(messages, functions, options):
        call = clarify_turn("Which service?")[0]
        return RoutingGeneration(calls=(call,), model=LOCAL_MODEL_ID)

    request = parse_selection_request(request_data())
    raw = asyncio.run(
        ModelRouterSelector(router).select(
            request, SelectionContext((Message(role=Role.USER, content="Check it"),), generate)
        )
    )
    candidate = parse_selection_result(raw).candidate
    assert (candidate.kind, candidate.clarification) == ("clarify", "Which service?")


# --- in the gateway (AC22, in-process part) ----------------------------------------------


def selected(events):
    return of_type(events, "route.selected")


def test_the_default_chain_is_model_router_alone_and_route_selected_names_it(tmp_path):
    """RM28: the baseline chain; the decision, actor, and fields of today plus the selector."""
    gateway = build_gateway(tmp_path, [route_turn(HEALTH), *health_tail()], external_turns=[])
    outcome, events = run(gateway, PROMPT)
    assert outcome.state is RunState.COMPLETED
    (route,) = selected(events)
    assert route.actor == "local"
    assert route.payload["mechanism"] == "native_function_calling"
    assert route.payload["model"] == LOCAL_MODEL_ID
    assert route.payload["selector"] == {
        "name": MODEL_ROUTER,
        "version": "1",
        "mechanism": "native_function_calling",
        "elapsed_ms": 0,
        "attempts": [{"name": MODEL_ROUTER, "outcome": "selected"}],
    }
    assert "tools_narrowed" not in route.payload
    (accepted,) = of_type(events, "run.accepted")
    assert accepted.payload["selection"] == [MODEL_ROUTER]
    assert accepted.payload["example_match_score"] == 0.5
    assert accepted.payload["example_match_margin"] == 0.1
    assert accepted.payload["budgets"]["selection.timeout_seconds"] == 2.0


def test_an_unknown_skill_is_inadmissible_and_the_next_selector_is_tried(tmp_path):
    """AC22: the unknown skill falls through; route.selected names every selector tried."""
    first, second = pick("first", skill_id="no-such-skill"), pick("second")
    gateway = build_gateway(tmp_path, health_tail(), external_turns=[], selectors=[first, second])
    outcome, events = run(gateway, PROMPT)
    assert outcome.state is RunState.COMPLETED
    assert purposes(events)[0] == "skill"  # no routing generation ran
    (route,) = selected(events)
    assert route.actor == "gateway"
    assert route.payload["mechanism"] == "scripted_match"
    assert route.payload["decision"]["skill_id"] == HEALTH
    assert route.payload["decision"]["reason_code"] == "simple_skill"
    assert route.payload["selector"]["attempts"] == [
        {"name": "first", "outcome": "inadmissible"},
        {"name": "second", "outcome": "selected"},
    ]
    assert "model" not in route.payload and "call_id" not in route.payload
    (accepted,) = of_type(events, "run.accepted")
    assert accepted.payload["selection"] == ["first", "second"]


def test_the_selection_request_carries_the_conversation_and_the_catalog(tmp_path):
    """RM28: the request names the run, conversation, language, mode, catalog, permissions."""
    selector = pick("only")
    gateway = build_gateway(tmp_path, health_tail(), external_turns=[], selectors=[selector])
    outcome, _ = run(gateway, PROMPT)
    request = selector.requests[0]
    assert request.run_id == outcome.run_id
    assert request.conversation == ({"role": "user", "content": PROMPT},)
    assert (request.language, request.execution_mode) == ("en", "auto")
    assert request.permissions == (READ_PERMISSION,)
    assert request.deadline_ms == 2000
    entries = {entry["id"]: entry for entry in request.catalog}
    assert set(entries) == set(gateway.orchestrator.router.offered)
    health = entries[HEALTH]
    skill = gateway.skills.get(HEALTH)
    assert health["content_hash"] == skill.content_hash
    assert health["allowed_tools"] == ["get_service_health"]
    assert health["local_eligible"] is True
    # The example requests come from the skill's examples.json, in both languages.
    assert PROMPT in health["examples"]
    assert "staging-api 现在健康吗？" in health["examples"]


def test_narrowing_only_removes_tools(tmp_path):
    """RM08, AC22: a tool outside the allowlist is dropped; the skill's tools are only removed."""
    caller = CallerContext(permissions=frozenset({READ_PERMISSION, MUTATE_PERMISSION}))
    selector = pick(
        "narrow", skill_id=DELETE_SKILL, tools=["list_demo_databases", "get_service_health"]
    )
    gateway = build_gateway(
        tmp_path,
        [tool_turn("list_demo_databases"), text_turn("Listed."), text_turn("Listed.")],
        selectors=[selector],
        caller=caller,
    )
    outcome, events = run(gateway, "List the demo-orders-db sandbox database.")
    (route,) = selected(events)
    assert route.payload["tools_narrowed"] == ["list_demo_databases"]
    offered = [e.payload["tools_offered"] for e in of_type(events, "model.started")]
    assert offered[0] == ["list_demo_databases"]
    assert "delete_demo_database" not in {t for tools in offered for t in tools}


def test_a_narrowing_to_no_allowed_tool_leaves_the_skill_no_tool(tmp_path):
    """RM08: narrowing never grants; with nothing left the run ends as capability_missing."""
    selector = pick("narrow", tools=["delete_demo_database"])
    gateway = build_gateway(tmp_path, [], external_turns=[], selectors=[selector])
    outcome, events = run(gateway, PROMPT)
    assert outcome.state is RunState.FAILED
    assert selected(events)[0].payload["tools_narrowed"] == []
    assert executed_tools(events) == []
    (failed,) = of_type(events, "run.failed")
    assert failed.payload["reason_code"] == "capability_missing"


def test_a_selected_mutation_still_waits_for_confirmation(tmp_path):
    """AC22, RM11: a selector choosing a mutating skill executes and grants nothing."""
    caller = CallerContext(permissions=frozenset({READ_PERMISSION, MUTATE_PERMISSION}))
    gateway = build_gateway(
        tmp_path,
        [
            tool_turn("list_demo_databases", call_id="call_list"),
            tool_turn(
                "delete_demo_database",
                call_id="call_delete",
                database_id="demo-orders-db",
                expected_revision=3,
            ),
        ],
        external_turns=[],
        selectors=[pick("mutating", skill_id=DELETE_SKILL)],
        caller=caller,
    )
    outcome, events = run(gateway, "Delete the demo-orders-db sandbox database.")
    assert outcome.state is RunState.WAITING_CONFIRMATION
    assert executed_tools(events) == ["list_demo_databases"]
    assert "demo-orders-db" in [db["database_id"] for db in gateway.sandbox.list_databases()]


def test_a_selector_clarification_is_asked_by_the_gateway(tmp_path):
    """RM02, RM05: a clarify candidate becomes the gateway's clarify decision."""
    selector = Scripted("asks", result("asks", kind="clarify", clarification="Which one?"))
    gateway = build_gateway(tmp_path, [], external_turns=[], selectors=[selector])
    outcome, events = run(gateway, "Check it.")
    assert outcome.state is RunState.WAITING_INPUT
    (route,) = selected(events)
    assert route.payload["decision"]["route"] == "clarify"
    assert route.payload["decision"]["skill_id"] is None
    (required,) = of_type(events, "clarification.required")
    assert required.payload["question"] == "Which one?"
    assert required.payload["missing"] == "skill_selection"


def test_the_gateway_runs_the_identifier_check_on_a_selected_skill(tmp_path):
    """RM05: a selected skill whose identifier is missing gets the gateway's clarification."""
    gateway = build_gateway(tmp_path, [], external_turns=[], selectors=[pick("only")])
    outcome, events = run(gateway, "Is the service healthy?")
    assert outcome.state is RunState.WAITING_INPUT
    (required,) = of_type(events, "clarification.required")
    assert required.payload["skill_id"] == HEALTH
    assert required.payload["missing"] == "service"


def test_none_an_error_and_a_timeout_fall_through(tmp_path):
    """Section 11: the time limit bounds a selector that never answers; the run continues.

    The limit is reached through an injected sleep that ends at once, so the
    test never waits on the wall clock.
    """
    hung = Scripted("hung", result("hung"), hang=True)
    gateway = build_gateway(
        tmp_path,
        health_tail(),
        external_turns=[],
        selection=SelectionSettings(timeout_seconds=0.25),
    )
    gateway.orchestrator.selection_chain = SelectorChain(
        [none_of("nothing"), Scripted("broken", raises=ValueError("x")), hung, pick("ok")],
        timeout_seconds=0.25,
        clock=gateway.clock.monotonic,
        sleep=instant,
    )
    outcome, events = run(gateway, PROMPT)
    assert outcome.state is RunState.COMPLETED
    assert [a["outcome"] for a in selected(events)[0].payload["selector"]["attempts"]] == [
        "none",
        "error",
        "timeout",
        "selected",
    ]
    assert hung.cancelled is True
    (accepted,) = of_type(events, "run.accepted")
    assert accepted.payload["budgets"]["selection.timeout_seconds"] == 0.25


def test_an_exhausted_chain_hands_off_with_the_catalog(tmp_path):
    """RM28, section 11: the chain chose nothing; route external, reason selection_failed."""
    gateway = build_gateway(
        tmp_path,
        [],
        external_turns=[text_turn("I cannot place this request.")],
        selectors=[none_of("nothing")],
    )
    outcome, events = run(gateway, "Tell me a joke.")
    (route,) = selected(events)
    assert route.actor == "gateway"
    assert route.payload["mechanism"] == EXHAUSTED_MECHANISM
    decision = route.payload["decision"]
    assert (decision["route"], decision["skill_id"], decision["reason_code"]) == (
        "external",
        None,
        "selection_failed",
    )
    assert route.payload["selector"]["name"] is None
    assert route.payload["selector"]["attempts"] == [{"name": "nothing", "outcome": "none"}]
    (handoff,) = of_type(events, "handoff.started")
    assert handoff.payload["reason_code"] == "selection_failed"
    assert handoff.payload["skill"] is None
    assert outcome.state is RunState.COMPLETED


def test_an_exhausted_chain_without_dispatch_ends_as_needs_external(tmp_path):
    gateway = build_gateway(tmp_path, [], selectors=[none_of("nothing")])
    outcome, events = run(gateway, "Tell me a joke.")
    assert outcome.state is RunState.FAILED
    (failed,) = of_type(events, "run.failed")
    assert failed.payload["error_code"] == "needs_external"
    assert failed.payload["reason_code"] == "selection_failed"
    assert executed_tools(events) == []


def test_alternatives_are_ignored_by_admission(tmp_path):
    """RM28: the gateway admits the candidate only; the alternatives are evidence."""
    selector = pick("ranked", alternatives=[DELETE_SKILL, "no-such-skill"])
    gateway = build_gateway(tmp_path, health_tail(), external_turns=[], selectors=[selector])
    outcome, events = run(gateway, PROMPT)
    assert outcome.state is RunState.COMPLETED
    assert [e.payload["name"] for e in of_type(events, "skill.loaded")] == [HEALTH]
    assert "alternatives" not in selected(events)[0].payload


def test_a_rejected_model_router_output_still_escalates_with_invalid_output(tmp_path):
    """RM02, RM04: the baseline chain keeps the invalid_output escalation, no route.selected."""
    bad = [ToolCall("call_route", "no-such-skill", {"language": "en"})]
    gateway = build_gateway(tmp_path, [bad])
    outcome, events = run(gateway, PROMPT)
    assert selected(events) == []
    (failed,) = of_type(events, "run.failed")
    assert failed.payload["reason_code"] == "invalid_output"


def test_a_selector_after_a_rejected_model_router_can_still_choose(tmp_path):
    bad = [ToolCall("call_route", "no-such-skill", {"language": "en"})]
    gateway = build_gateway(
        tmp_path,
        [bad, *health_tail()],
        external_turns=[],
    )
    orchestrator = gateway.orchestrator
    orchestrator.selection_chain = SelectorChain(
        [ModelRouterSelector(orchestrator.router), pick("fallback")],
        timeout_seconds=2,
        clock=gateway.clock.monotonic,
    )
    outcome, events = run(gateway, PROMPT)
    assert outcome.state is RunState.COMPLETED
    (route,) = selected(events)
    assert route.actor == "gateway"
    assert [a["outcome"] for a in route.payload["selector"]["attempts"]] == [
        "error",
        "selected",
    ]


def test_model_router_after_a_selector_that_chose_nothing(tmp_path):
    """RM28: model_router later in the chain records the routing generation as today."""
    gateway = build_gateway(tmp_path, [route_turn(HEALTH), *health_tail()], external_turns=[])
    orchestrator = gateway.orchestrator
    orchestrator.selection_chain = SelectorChain(
        [none_of("first"), ModelRouterSelector(orchestrator.router)],
        timeout_seconds=2,
        clock=gateway.clock.monotonic,
    )
    outcome, events = run(gateway, PROMPT)
    assert outcome.state is RunState.COMPLETED
    (route,) = selected(events)
    assert route.actor == "local"
    assert route.payload["mechanism"] == "native_function_calling"
    assert route.payload["call_id"].endswith("_routing")
    assert route.payload["model_language"] == "en"
    assert [a["outcome"] for a in route.payload["selector"]["attempts"]] == ["none", "selected"]


def test_a_skill_chosen_by_another_selector_is_not_stored_in_the_routing_cache(tmp_path):
    """Section 10: an entry is stored only after a routing generation chose the skill."""
    from relaymind.orchestrator.routing_cache import RoutingCache

    cache = RoutingCache()
    gateway = build_gateway(
        tmp_path, health_tail(), external_turns=[], selectors=[pick("only")], routing_cache=cache
    )
    outcome, _ = run(gateway, PROMPT)
    assert outcome.state is RunState.COMPLETED
    assert cache.stores == 0


def test_external_only_and_the_bypass_run_no_selector(tmp_path):
    """Section 7.4, 4.1: gateway decisions made before the chain stay as they were."""
    selector = pick("only")
    gateway = build_gateway(
        tmp_path, [], external_turns=[text_turn(STAGING_OK)], selectors=[selector]
    )
    run(gateway, PROMPT, execution_mode=ExecutionMode.EXTERNAL_ONLY.value)
    assert selector.requests == []
    (tmp_path / "bypass").mkdir()
    gateway = build_gateway(tmp_path / "bypass", [], input_tokens=10_000, selectors=[selector])
    _, events = run(gateway, PROMPT)
    assert selected(events)[0].payload["mechanism"] == "bypass"
    assert selector.requests == []


def test_the_selection_settings_are_checked():
    """Section 14.1: chain items, the time limit, and the two thresholds."""
    assert SelectionSettings().chain == (MODEL_ROUTER,)
    for bad in (
        {"chain": ()},
        {"chain": ("model_router", "model_router")},
        {"chain": ("http",)},
        {"chain": "model_router"},
        {"timeout_seconds": 0},
        {"timeout_seconds": math.inf},
        {"timeout_seconds": True},
        {"example_match_score": 1.5},
        {"example_match_score": -0.1},
        {"example_match_margin": math.nan},
        {"example_match_margin": False},
        {"example_match_score": "0.5"},
    ):
        with pytest.raises(ValueError):
            SelectionSettings(**bad)
    settings = SelectionSettings(chain=["example_match", "model_router"], example_match_score=1)
    assert settings.chain == ("example_match", "model_router")
    assert settings.example_match_score == 1.0
    assert copy.deepcopy(settings.to_json_object()) == {
        "selection": ["example_match", "model_router"],
        "example_match_score": 1.0,
        "example_match_margin": 0.1,
    }


def test_a_parallel_skill_routes_external_when_the_mode_does_not_admit_it(tmp_path):
    """RM02: admission checks the mode; the parallel skill routes external outside its mode."""
    selector = pick("p", skill_id="health-and-diagnostic-checklist")
    gateway = build_gateway(tmp_path, [], selectors=[selector])
    _, events = run(gateway, PROMPT, execution_mode="local_only")
    decision = selected(events)[0].payload["decision"]
    assert decision["route"] == Route.EXTERNAL.value
    assert decision["reason_code"] == "parallel_workflow"
