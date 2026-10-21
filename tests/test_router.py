"""Router: local-first routing decision by native function calling.

RM02: in managed execution the local model selects among an advertised
skill catalog and the gateway produces a normalized RouteDecision
(section 8). Unknown skills, invalid fields, or unavailable capabilities
never trigger tool execution. Section 4.2: at most eight short skill
descriptions per router prompt. Section 4.1: router input is capped at
2,048 local tokens; a longer input bypasses the fast path and is never
silently truncated. RM05: a missing required identifier asks for
clarification instead of a guess. RM09: internal control actions such as
skill selection are not exposed as application tools. RM04: route and
reason code come from the skill registry entry, not from the model.

Mechanism: native function calling, one function per advertised skill
(docs/ROADMAP.md, decided 2026-10-21 from the #48 routing probe). The
model's language value is not used alone; the decision's language comes
from the conversation (ROADMAP.md, same decision), and an identifier-only
reply keeps it (section 5, spec 0.6). RM05 provenance is user turns and
tool results only (section 5). Section 4.4 (spec 0.6): parallel needs an
enabled workflow and an admitting mode; otherwise external with reason
parallel_workflow. Section 7.4: external_only is a gateway decision.
Every decision passes the section 8 validator with a RoutingContext.
Issue #14.
"""

import dataclasses

import pytest

from helpers import make_demo_skills, make_demo_tools
from relaymind.adapters import Message, Role, ToolCall
from relaymind.orchestrator.budgets import LocalBudgets
from relaymind.orchestrator.route_decision import (
    ExecutionMode,
    Language,
    Route,
    RouteDecisionError,
    parse_route_decision,
)
from relaymind.orchestrator.router import (
    CLARIFY_FUNCTION,
    MAX_ADVERTISED_SKILLS,
    SKILL_SELECTION,
    Router,
    RouterConfigError,
    request_language,
)

CHECKLIST = "health-and-diagnostic-checklist"


@pytest.fixture
def tools(tmp_path):
    return make_demo_tools(tmp_path)


@pytest.fixture
def skills(tools):
    return make_demo_skills(tools)


@pytest.fixture
def router(skills, tools):
    return Router(skills, tools, budgets=LocalBudgets())


def user(text: str) -> tuple[Message, ...]:
    return (Message(role=Role.USER, content=text),)


def skill_call(name: str, **arguments) -> ToolCall:
    return ToolCall(call_id="call_route", tool_name=name, arguments=arguments)


# --- request construction ----------------------------------------------------


def test_one_function_per_advertised_skill_plus_clarification(router, skills):
    request = router.build_request(user("Is staging-api healthy right now?"), "gen_route")
    names = [schema.name for schema in request.tool_schemas]
    assert names == [*skills.names(), CLARIFY_FUNCTION]
    descriptions = {schema.name: schema.description for schema in request.tool_schemas}
    for entry in skills.catalog():
        assert descriptions[entry.name] == entry.description


def test_the_router_request_carries_the_routing_output_budget(router):
    request = router.build_request(user("Is staging-api healthy?"), "gen_route")
    assert request.options.max_output_tokens == 128
    assert request.generation_id == "gen_route"
    assert request.messages[0].role is Role.SYSTEM
    assert request.messages[-1].content == "Is staging-api healthy?"


def test_more_than_eight_skills_is_a_configuration_error(skills, tools):
    assert MAX_ADVERTISED_SKILLS == 8
    with pytest.raises(RouterConfigError):
        Router(skills, tools, budgets=LocalBudgets(), offered=list(skills.names()) * 2)


def test_offering_an_unregistered_skill_is_a_configuration_error(skills, tools):
    with pytest.raises(RouterConfigError):
        Router(skills, tools, budgets=LocalBudgets(), offered=["no-such-skill"])


def test_routing_functions_are_not_application_tools(router, tools):
    # RM09: skill selection is an internal control action.
    request = router.build_request(user("Is staging-api healthy?"), "gen_route")
    assert not {schema.name for schema in request.tool_schemas} & tools.names()


# --- decisions -----------------------------------------------------------------


def decide(router, calls, messages, **kwargs):
    return router.decide(calls, messages, **kwargs).decision


def test_a_selected_local_skill_gives_a_local_decision(router):
    decision = decide(
        router,
        [skill_call("check-service-health", language="en")],
        user("Is staging-api healthy right now?"),
    )
    assert decision.route is Route.LOCAL
    assert decision.skill_id == "check-service-health"
    assert decision.reason_code == "simple_skill"
    assert decision.clarification is None
    assert decision.language is Language.EN


def test_the_decision_passes_the_published_boundary_validator(router, skills):
    decision = decide(
        router, [skill_call("check-service-health", language="en")], user("Is staging-api healthy?")
    )
    context = router.context(ExecutionMode.AUTO)
    validated = parse_route_decision(
        decision.to_json_object(), skill_ids=skills.names(), context=context
    )
    assert validated == decision


def checklist(router, mode):
    return decide(
        router,
        [skill_call(CHECKLIST, language="en")],
        user("Run the health checklist for staging-api."),
        mode=mode,
    )


@pytest.mark.parametrize(
    ("mode", "in_auto"), [(ExecutionMode.PARALLEL_DEMO, False), (ExecutionMode.AUTO, True)]
)
def test_parallel_needs_an_enabled_workflow_and_an_admitting_mode(skills, tools, mode, in_auto):
    # Section 4.4 (spec 0.6): route and reason come from the registry and
    # the run, never from the model.
    router = Router(
        skills,
        tools,
        budgets=LocalBudgets(),
        enabled_workflows={CHECKLIST},
        parallel_in_auto=in_auto,
    )
    decision = checklist(router, mode)
    assert (decision.route, decision.reason_code) == (Route.PARALLEL, "parallel_workflow")
    assert decision.skill_id == CHECKLIST


@pytest.mark.parametrize(
    ("enabled", "mode", "in_auto"),
    [
        ((), ExecutionMode.PARALLEL_DEMO, True),
        ({CHECKLIST}, ExecutionMode.AUTO, False),
        ({CHECKLIST}, ExecutionMode.LOCAL_ONLY, True),
    ],
    ids=["not-enabled", "auto-without-policy", "local-only"],
)
def test_a_parallel_skill_the_run_does_not_admit_routes_external(
    skills, tools, enabled, mode, in_auto
):
    # Section 4.4 (spec 0.6): otherwise route external, reason parallel_workflow.
    router = Router(
        skills, tools, budgets=LocalBudgets(), enabled_workflows=enabled, parallel_in_auto=in_auto
    )
    decision = checklist(router, mode)
    assert (decision.route, decision.reason_code) == (Route.EXTERNAL, "parallel_workflow")
    assert decision.skill_id == CHECKLIST


def test_external_only_is_a_gateway_decision(router):
    # Section 7.4 (spec 0.6): route external, skill_id null, execution_mode.
    decision = router.execution_mode_decision(user("staging-api 现在健康吗？"))
    assert decision.to_json_object() == {
        "schema_version": 1,
        "route": "external",
        "skill_id": None,
        "language": "zh",
        "reason_code": "execution_mode",
        "clarification": None,
    }


def test_a_skill_that_is_not_local_eligible_routes_external(skills, tools):
    health = skills.get("check-service-health")
    patched = dataclasses.replace(health, local_eligible=False)
    skills._skills["check-service-health"] = patched
    decision = decide(
        Router(skills, tools, budgets=LocalBudgets()),
        [skill_call("check-service-health", language="en")],
        user("Is staging-api healthy?"),
    )
    assert decision.route is Route.EXTERNAL
    assert decision.reason_code == "complex_skill"


@pytest.mark.parametrize(
    ("text", "model_language", "expected"),
    [
        ("staging-api 现在健康吗？", "en", Language.ZH),
        ("Is staging-api healthy right now?", "zh", Language.EN),
    ],
)
def test_the_language_comes_from_the_request_text(router, text, model_language, expected):
    # The #48 probe: the model's language was wrong for 5 of 6 Chinese prompts.
    decision = decide(
        router, [skill_call("check-service-health", language=model_language)], user(text)
    )
    assert decision.language is expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Is staging-api healthy?", Language.EN),
        ("staging-api 现在健康吗？", Language.ZH),
        ("检查 payments-api", Language.ZH),
        ("payments-api ok?", Language.EN),
    ],
)
def test_request_language(text, expected):
    assert request_language(user(text)) is expected


def test_request_language_reads_the_latest_user_message_with_words():
    messages = (
        Message(role=Role.USER, content="你好"),
        Message(role=Role.ASSISTANT, content="Hello"),
        Message(role=Role.USER, content="Is staging-api healthy?"),
    )
    assert request_language(messages) is Language.EN


@pytest.mark.parametrize(
    "reply", ["staging-api", "payments-api。", " demo-orders-db, 3 ", "auth", "redis"]
)
def test_an_identifier_only_reply_keeps_the_conversation_language(reply):
    # Section 5 (spec 0.6): an identifier-only reply does not re-detect it.
    messages = (
        Message(role=Role.USER, content="帮我检查服务健康"),
        Message(role=Role.ASSISTANT, content="请问是哪个服务？可选：staging-api、payments-api。"),
        Message(role=Role.USER, content=reply),
    )
    assert request_language(messages) is Language.ZH


def test_a_model_clarification_gives_a_clarify_decision(router):
    routing = router.decide(
        [skill_call(CLARIFY_FUNCTION, language="en", question="Which service do you mean?")],
        user("Is it healthy?"),
    )
    decision = routing.decision
    # Section 8 (spec 0.6): no skill was chosen, so it waits for one.
    assert routing.missing == SKILL_SELECTION
    assert decision.route is Route.CLARIFY
    assert decision.skill_id is None
    assert decision.reason_code == "missing_input"
    assert decision.clarification == "Which service do you mean?"


@pytest.mark.parametrize(
    ("text", "language", "fragment"),
    [
        ("Is the service healthy?", Language.EN, "staging-api"),
        ("这个服务健康吗？", Language.ZH, "payments-api"),
    ],
)
def test_a_missing_service_identifier_asks_instead_of_guessing(router, text, language, fragment):
    # RM05 and the #48 probe: the model picked a skill for these prompts.
    routing = router.decide([skill_call("check-service-health", language="en")], user(text))
    decision = routing.decision
    assert decision.route is Route.CLARIFY
    # Section 8 (spec 0.6): the clarify names the skill and the missing argument.
    assert decision.skill_id == "check-service-health"
    assert routing.missing == "service"
    assert decision.reason_code == "missing_input"
    assert decision.language is language
    assert fragment in decision.clarification


def test_a_gateway_question_that_lists_ids_is_not_provenance(router):
    # RM05 / section 5 (spec 0.6): only user turns and tool results count.
    messages = (
        Message(role=Role.USER, content="Is my service healthy?"),
        Message(
            role=Role.ASSISTANT,
            content="Which service do you mean? Options: staging-api, payments-api.",
        ),
        Message(role=Role.USER, content="I don't know, you pick."),
    )
    decision = decide(router, [skill_call("check-service-health", language="en")], messages)
    assert decision.route is Route.CLARIFY


def test_the_clarification_label_comes_from_the_schema(skills, tools):
    # The label is the property's schema label, not a hard-coded name.
    tool = tools.get("get_service_health")
    schema = dict(tool.declaration.input_schema)
    service = {
        **schema["properties"]["service"],
        "x-relaymind-label": {"en": "service ID", "zh": "服务编号"},
    }
    schema["properties"] = {"service": service}
    declaration = dataclasses.replace(tool.declaration, input_schema=schema)
    tools._tools["get_service_health"] = dataclasses.replace(tool, declaration=declaration)
    router = Router(skills, tools, budgets=LocalBudgets())
    decision = decide(router, [skill_call("check-service-health", language="en")], user("Ok?"))
    assert decision.clarification.startswith("Which service ID do you mean?")


@pytest.mark.parametrize(
    ("text", "question"),
    [
        (
            "Is the service healthy?",
            "Which service do you mean? Options: staging-api, payments-api.",
        ),
        ("这个服务健康吗？", "请问是哪个服务？可选：staging-api、payments-api。"),
    ],
)
def test_the_clarification_uses_the_label_in_the_run_language(router, text, question):
    """Section 5: the question is in the run's language, so a Chinese run
    names the input with the schema's Chinese label, not "service" (#55
    x-relaymind-label; live AC01 read "请问是哪个 service？")."""
    routing = router.decide([skill_call("check-service-health", language="en")], user(text))
    assert routing.decision.clarification == question


def test_an_identifier_from_earlier_conversation_counts(router):
    messages = (
        Message(role=Role.USER, content="I am looking at payments-api."),
        Message(role=Role.ASSISTANT, content="OK."),
        Message(role=Role.USER, content="Is it healthy?"),
    )
    decision = decide(router, [skill_call("check-service-health", language="en")], messages)
    assert decision.route is Route.LOCAL


def test_a_skill_without_identifier_tools_is_not_asked_for_one(router):
    # delete-demo-database resolves its target with list_demo_databases.
    decision = decide(
        router,
        [skill_call("delete-demo-database", language="en")],
        user("Delete the demo database demo-orders."),
    )
    assert decision.route is Route.LOCAL
    assert decision.skill_id == "delete-demo-database"


@pytest.mark.parametrize(
    "calls",
    [
        [],
        [skill_call("check-service-health", language="en")] * 2,
        [skill_call("get_service_health", service="staging-api")],
        [skill_call("no-such-skill", language="en")],
        [skill_call("check-service-health")],
        [skill_call("check-service-health", language="fr")],
        [skill_call("check-service-health", language="en", service="staging-api")],
        [skill_call(CLARIFY_FUNCTION, language="en")],
        [skill_call(CLARIFY_FUNCTION, language="en", question="  ")],
    ],
    ids=[
        "no-call",
        "two-calls",
        "application-tool",
        "unknown-skill",
        "missing-language",
        "bad-language",
        "undeclared-argument",
        "clarify-without-question",
        "blank-question",
    ],
)
def test_invalid_routing_output_is_rejected(router, calls):
    with pytest.raises(RouteDecisionError):
        router.decide(calls, user("Is staging-api healthy?"))


def test_a_skill_outside_the_offered_catalog_is_rejected(skills, tools):
    router = Router(skills, tools, budgets=LocalBudgets(), offered=["lookup-latest-deployment"])
    with pytest.raises(RouteDecisionError):
        router.decide(
            [skill_call("check-service-health", language="en")], user("Is staging-api healthy?")
        )


# --- input cap -------------------------------------------------------------------


def test_input_within_the_cap_takes_the_fast_path(router):
    assert router.within_input_cap(2048) is True


def test_oversized_input_takes_the_bypass_decision(router):
    assert router.within_input_cap(2049) is False
    decision = router.bypass_decision(user("staging-api 现在健康吗？"), ExecutionMode.AUTO)
    assert decision.route is Route.EXTERNAL
    assert decision.skill_id is None
    assert decision.reason_code == "context_limit"
    assert decision.language is Language.ZH
