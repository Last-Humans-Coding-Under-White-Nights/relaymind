"""RouteDecision schema and boundary validation (spec section 8; RM02, RM05).

Section 8: RouteDecision has schema_version=1, a route enum, skill_id or
null, language en or zh, a reason_code, and clarification or null;
unknown fields are rejected. Implementations publish an equivalent
versioned JSON Schema and validate at the boundary. RM02: unknown
skills or invalid fields never trigger tool execution, so the validator
checks skill_id against the advertised catalog. Part of issue #14.
"""

import importlib.util
import json
from pathlib import Path

import jsonschema
import pytest

from relaymind.orchestrator.route_decision import (
    REASON_CODES,
    ROUTE_DECISION_SCHEMA,
    ExecutionMode,
    Language,
    Route,
    RouteDecision,
    RouteDecisionError,
    RoutingContext,
    parse_route_decision,
    parse_route_decision_json,
)

CATALOG = frozenset({"check-service-health", "lookup-latest-deployment"})

VALID = {
    "schema_version": 1,
    "route": "local",
    "skill_id": "check-service-health",
    "language": "en",
    "reason_code": "simple_skill",
    "clarification": None,
}


def test_a_valid_decision_parses_into_typed_fields():
    decision = parse_route_decision(VALID, skill_ids=CATALOG)
    assert decision == RouteDecision(
        route=Route.LOCAL,
        skill_id="check-service-health",
        language=Language.EN,
        reason_code="simple_skill",
        clarification=None,
    )
    assert decision.schema_version == 1


def test_a_clarification_decision_without_a_skill_parses():
    # RM05: a missing identifier yields a clarification, not a guess.
    data = {
        **VALID,
        "route": "clarify",
        "skill_id": None,
        "language": "zh",
        "reason_code": "missing_input",
        "clarification": "请问是哪个服务？",
    }
    decision = parse_route_decision(data, skill_ids=CATALOG)
    assert decision.route is Route.CLARIFY
    assert decision.skill_id is None
    assert decision.language is Language.ZH
    assert decision.clarification == "请问是哪个服务？"


def test_the_route_enum_is_the_four_spec_decisions():
    # RM02: local, external, clarify, and parallel.
    assert {route.value for route in Route} == {"local", "external", "clarify", "parallel"}


def test_reason_codes_are_the_section_8_labels():
    assert REASON_CODES == {
        "simple_skill",
        "complex_skill",
        "missing_input",
        "local_budget",
        "invalid_output",
        "tool_failure",
        "context_limit",
        "capability_missing",
        "parallel_workflow",
        "execution_mode",
        # Spec 0.31 section 8 (RM26, RM27, RM28).
        "local_slow",
        "no_local_model",
        "selection_failed",
    }


def test_unknown_fields_are_rejected_by_name():
    with pytest.raises(RouteDecisionError, match="confidence"):
        parse_route_decision({**VALID, "confidence": 0.9}, skill_ids=CATALOG)


@pytest.mark.parametrize("field", sorted(VALID))
def test_every_field_is_required(field):
    data = {key: value for key, value in VALID.items() if key != field}
    with pytest.raises(RouteDecisionError, match=field):
        parse_route_decision(data, skill_ids=CATALOG)


def test_a_skill_outside_the_advertised_catalog_is_rejected():
    # RM02: an unknown skill must never reach tool execution.
    with pytest.raises(RouteDecisionError, match="skill_id"):
        parse_route_decision({**VALID, "skill_id": "delete-demo-database"}, skill_ids=CATALOG)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", 2),
        ("schema_version", "1"),
        ("schema_version", True),
        ("schema_version", 1.0),
        ("route", "remote"),
        ("route", None),
        ("skill_id", ""),
        ("skill_id", "   "),
        ("skill_id", 7),
        ("language", "fr"),
        ("language", "EN"),
        ("reason_code", "because"),
        ("reason_code", None),
        ("clarification", ""),
        ("clarification", "   "),
        ("clarification", ["which service?"]),
    ],
)
def test_invalid_field_values_are_rejected_by_name(field, value):
    with pytest.raises(RouteDecisionError, match=field):
        parse_route_decision({**VALID, field: value}, skill_ids=CATALOG)


@pytest.mark.parametrize("data", [None, [], "local", 1])
def test_a_non_object_is_rejected(data):
    with pytest.raises(RouteDecisionError, match="object"):
        parse_route_decision(data, skill_ids=CATALOG)


def test_json_text_is_parsed_then_validated():
    decision = parse_route_decision_json(json.dumps(VALID), skill_ids=CATALOG)
    assert decision.skill_id == "check-service-health"


@pytest.mark.parametrize(
    "text",
    [
        "",
        '{"schema_version": 1, "route": "local"',  # partial streamed JSON (RM09)
        "Sure! " + json.dumps(VALID),  # prose around the envelope
        json.dumps(VALID) + json.dumps(VALID),
        '{"schema_version": NaN}',
    ],
)
def test_text_that_is_not_exactly_one_json_object_is_rejected(text):
    with pytest.raises(RouteDecisionError):
        parse_route_decision_json(text, skill_ids=CATALOG)


def test_duplicate_keys_are_rejected():
    # A second "route" key must not silently override the first.
    text = json.dumps(VALID)[:-1] + ', "route": "external"}'
    with pytest.raises(RouteDecisionError, match="duplicate"):
        parse_route_decision_json(text, skill_ids=CATALOG)


def test_the_published_schema_is_versioned_and_closed():
    schema = ROUTE_DECISION_SCHEMA
    assert schema["$id"].endswith("route_decision.v1.json")
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(VALID)
    assert set(schema["properties"]) == set(VALID)
    assert schema["properties"]["schema_version"]["const"] == 1


def test_the_published_schema_matches_the_validator_enums():
    # One contract: the JSON Schema and the Python validator agree.
    props = ROUTE_DECISION_SCHEMA["properties"]
    assert set(props["route"]["enum"]) == {route.value for route in Route}
    assert set(props["language"]["enum"]) == {language.value for language in Language}
    assert set(props["reason_code"]["enum"]) == REASON_CODES
    assert props["skill_id"]["type"] == ["string", "null"]
    assert props["clarification"]["type"] == ["string", "null"]


def _schema_accepts(data: dict) -> bool:
    return jsonschema.Draft202012Validator(ROUTE_DECISION_SCHEMA).is_valid(data)


CLARIFY = {
    **VALID,
    "route": "clarify",
    "skill_id": None,
    "reason_code": "missing_input",
    "clarification": "Which service?",
}


@pytest.mark.parametrize(
    ("data", "accepted"),
    [
        (VALID, True),
        (CLARIFY, True),
        ({**VALID, "skill_id": "   "}, False),  # 53.9: blank skill_id
        ({**CLARIFY, "clarification": "   "}, False),  # 53.9: blank clarification
        ({**CLARIFY, "clarification": None}, False),  # clarify needs a question
        ({**VALID, "clarification": "Which service?"}, False),  # others need null
        ({**VALID, "route": "external", "clarification": "Why?"}, False),
    ],
    ids=["valid", "clarify", "blank-skill", "blank-question", "no-question", "local-q", "ext-q"],
)
def test_the_published_schema_enforces_non_blank_text_and_the_clarification_rule(data, accepted):
    # Spec section 8 cross-field rule, mirrored in the schema with if/then
    # so a grammar or an external validator rejects the same documents
    # the Python validator does.
    assert _schema_accepts(data) is accepted
    if accepted:
        parse_route_decision(data, skill_ids=CATALOG)
    else:
        with pytest.raises(RouteDecisionError):
            parse_route_decision(data, skill_ids=CATALOG)


def test_a_decision_serializes_back_to_the_schema_shape():
    decision = parse_route_decision(VALID, skill_ids=CATALOG)
    assert decision.to_json_object() == VALID


def test_the_schema_loads_when_the_module_is_imported_outside_its_package():
    # Spec section 8: the schema is published inside the package. Loading
    # the module standalone (where __package__ is empty) must find it too.
    path = Path(importlib.util.find_spec("relaymind.orchestrator.route_decision").origin)
    spec = importlib.util.spec_from_file_location("route_decision_standalone", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.ROUTE_DECISION_SCHEMA == ROUTE_DECISION_SCHEMA


def _typed_valid() -> dict[str, object]:
    return {
        "route": Route.LOCAL,
        "skill_id": "check-service-health",
        "language": Language.EN,
        "reason_code": "simple_skill",
        "clarification": None,
    }


def test_a_decision_built_from_strings_is_coerced_to_its_enums():
    # RM02: a decision is one typed shape however it was built, so the
    # published JSON form never depends on the caller passing enums.
    decision = RouteDecision(**{**_typed_valid(), "route": "clarify", "language": "zh"})
    assert decision.route is Route.CLARIFY
    assert decision.language is Language.ZH
    assert decision.to_json_object()["route"] == "clarify"
    assert decision.to_json_object()["language"] == "zh"


@pytest.mark.parametrize(("field", "value"), [("route", "remote"), ("language", "fr")])
def test_a_decision_built_from_an_unknown_value_is_rejected(field, value):
    with pytest.raises(ValueError):
        RouteDecision(**{**_typed_valid(), field: value})


# --- Cross-field rules (spec 0.6 section 8; RM02) ---------------------------


def test_a_clarify_route_needs_a_clarification():
    with pytest.raises(RouteDecisionError, match="clarification"):
        parse_route_decision({**CLARIFY, "clarification": None}, skill_ids=CATALOG)


@pytest.mark.parametrize("route", ["local", "external", "parallel"])
def test_other_routes_need_a_null_clarification(route):
    data = {**VALID, "route": route, "clarification": "Which service?"}
    with pytest.raises(RouteDecisionError, match="clarification"):
        parse_route_decision(data, skill_ids=CATALOG)


def test_context_free_parsing_leaves_the_run_dependent_rules_to_the_router():
    # Without a run context the validator cannot know the mode or the
    # catalog ownership, so only the context-free rules apply here.
    decision = parse_route_decision({**VALID, "skill_id": None}, skill_ids=CATALOG)
    assert decision.skill_id is None


MANAGED_AUTO = RoutingContext()


def test_managed_local_needs_a_skill():
    with pytest.raises(RouteDecisionError, match="skill_id"):
        parse_route_decision({**VALID, "skill_id": None}, skill_ids=CATALOG, context=MANAGED_AUTO)


@pytest.mark.parametrize("route", ["external", "clarify"])
def test_external_and_clarify_allow_a_null_skill(route):
    base = (
        CLARIFY if route == "clarify" else {**VALID, "route": route, "reason_code": "complex_skill"}
    )
    decision = parse_route_decision(
        {**base, "skill_id": None}, skill_ids=CATALOG, context=MANAGED_AUTO
    )
    assert decision.skill_id is None


PARALLEL = {
    **VALID,
    "route": "parallel",
    "skill_id": "health-and-diagnostic-checklist",
    "reason_code": "parallel_workflow",
}
WORKFLOWS = {"health-and-diagnostic-checklist": "health_checklist_v1"}
PARALLEL_CATALOG = CATALOG | {"health-and-diagnostic-checklist"}


def parallel_context(**overrides) -> RoutingContext:
    base = dict(
        execution_mode="parallel_demo",
        parallel_workflows=WORKFLOWS,
        enabled_workflows={"health_checklist_v1"},
    )
    base.update(overrides)
    return RoutingContext(**base)


def test_parallel_is_admissible_in_parallel_demo_with_an_enabled_workflow():
    decision = parse_route_decision(
        PARALLEL, skill_ids=PARALLEL_CATALOG, context=parallel_context()
    )
    assert decision.route is Route.PARALLEL


@pytest.mark.parametrize("workflows", ["health_checklist_v1", b"health_checklist_v1"])
def test_routing_context_rejects_scalar_enabled_workflows(workflows):
    # RM02, spec section 8: workflow IDs must not become individual characters.
    with pytest.raises(TypeError, match="enabled_workflows"):
        parallel_context(enabled_workflows=workflows)


@pytest.mark.parametrize("collection", [list, tuple, set, frozenset])
def test_parallel_accepts_workflow_id_collections(collection):
    # RM02, spec section 8: the complete enabled workflow ID admits parallel.
    context = parallel_context(enabled_workflows=collection(["health_checklist_v1"]))
    decision = parse_route_decision(PARALLEL, skill_ids=PARALLEL_CATALOG, context=context)
    assert decision.route is Route.PARALLEL


@pytest.mark.parametrize("mode", ["local_only", "external_only"])
def test_parallel_is_invalid_in_local_only_and_external_only(mode):
    with pytest.raises(RouteDecisionError, match="parallel"):
        parse_route_decision(
            PARALLEL, skill_ids=PARALLEL_CATALOG, context=parallel_context(execution_mode=mode)
        )


def test_parallel_in_auto_needs_operator_policy():
    # Spec 4.4: by default auto does not run the workflow.
    with pytest.raises(RouteDecisionError, match="parallel"):
        parse_route_decision(
            PARALLEL, skill_ids=PARALLEL_CATALOG, context=parallel_context(execution_mode="auto")
        )
    decision = parse_route_decision(
        PARALLEL,
        skill_ids=PARALLEL_CATALOG,
        context=parallel_context(execution_mode="auto", parallel_in_auto=True),
    )
    assert decision.route is Route.PARALLEL


def test_parallel_needs_a_skill_with_an_enabled_workflow():
    with pytest.raises(RouteDecisionError, match="skill_id"):
        parse_route_decision(
            {**PARALLEL, "skill_id": None}, skill_ids=PARALLEL_CATALOG, context=parallel_context()
        )
    # The skill declares no workflow.
    with pytest.raises(RouteDecisionError, match="parallel"):
        parse_route_decision(
            {**PARALLEL, "skill_id": "check-service-health"},
            skill_ids=PARALLEL_CATALOG,
            context=parallel_context(),
        )
    # The declared workflow is not enabled by the operator.
    with pytest.raises(RouteDecisionError, match="parallel"):
        parse_route_decision(
            PARALLEL, skill_ids=PARALLEL_CATALOG, context=parallel_context(enabled_workflows=())
        )


def test_client_owned_mode_has_no_skills_and_no_parallel():
    # Spec 7.2: the offered catalog is empty for the client-owned alias.
    client = RoutingContext(managed=False)
    with pytest.raises(RouteDecisionError, match="skill_id"):
        parse_route_decision(
            {**VALID, "route": "external", "reason_code": "complex_skill"},
            skill_ids=CATALOG,
            context=client,
        )
    with pytest.raises(RouteDecisionError, match="parallel"):
        parse_route_decision(
            {**PARALLEL, "skill_id": None}, skill_ids=(), context=parallel_context(managed=False)
        )
    decision = parse_route_decision(
        {**VALID, "skill_id": None, "route": "external", "reason_code": "complex_skill"},
        skill_ids=(),
        context=client,
    )
    assert decision.route is Route.EXTERNAL


def test_a_gateway_external_only_decision_validates_under_the_same_rules():
    # Spec 7.4: the gateway's own decision carries reason code
    # execution_mode and follows every rule.
    data = {**VALID, "route": "external", "skill_id": None, "reason_code": "execution_mode"}
    context = RoutingContext(execution_mode=ExecutionMode.EXTERNAL_ONLY)
    decision = parse_route_decision(data, skill_ids=CATALOG, context=context)
    assert decision.reason_code == "execution_mode"


def test_the_routing_context_rejects_an_unknown_mode():
    with pytest.raises(ValueError):
        RoutingContext(execution_mode="turbo")


# --- Every decoding failure is a RouteDecisionError (53.5) ------------------


@pytest.mark.parametrize(
    "text",
    ["1" * 5000, '{"schema_version": 1e999}', "[" * 100_000],
    ids=["huge-int", "overflowing-float", "deep-nesting"],
)
def test_every_decoding_failure_is_reported_as_invalid_routing_output(text):
    # RM02: whatever the model wrote, the router sees one error type.
    with pytest.raises(RouteDecisionError):
        parse_route_decision_json(text, skill_ids=CATALOG)


# --- The reason code follows the route (spec 0.31 section 8; RM26 to RM28) ----


@pytest.mark.parametrize("reason", ["selection_failed", "no_local_model", "local_slow"])
def test_the_phase_2_reasons_are_external_routes(reason):
    """Section 8: route=external carries selection_failed, no_local_model, or local_slow."""
    data = {**VALID, "route": "external", "skill_id": None, "reason_code": reason}
    decision = parse_route_decision(data, skill_ids=CATALOG, context=MANAGED_AUTO)
    assert decision.reason_code == reason
    assert _schema_accepts(data)


@pytest.mark.parametrize(
    ("route", "reason"),
    [
        ("local", "selection_failed"),
        ("local", "complex_skill"),
        ("external", "simple_skill"),
        ("external", "local_budget"),
        ("external", "invalid_output"),
        ("external", "tool_failure"),
        ("external", "missing_input"),
    ],
)
def test_a_reason_that_does_not_follow_the_route_is_refused(route, reason):
    """Section 8: local, invalid_output, and tool_failure are escalation reasons, not routes."""
    data = {**VALID, "route": route, "reason_code": reason}
    with pytest.raises(RouteDecisionError, match="reason_code"):
        parse_route_decision(data, skill_ids=CATALOG)
    assert not _schema_accepts(data)


def test_a_clarify_route_carries_missing_input():
    data = {**CLARIFY, "reason_code": "selection_failed"}
    with pytest.raises(RouteDecisionError, match="reason_code"):
        parse_route_decision(data, skill_ids=CATALOG)
    assert not _schema_accepts(data)
