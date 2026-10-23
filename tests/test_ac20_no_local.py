"""AC20: operation without a usable local model (#126; RM01, RM05, RM11, RM21, RM26, RM28, RM29).

Spec 0.31 RM26 and sections 7.4, 8, 11, and 14.1. In each class, absent
(no local_model table), unavailable (the configured model cannot run),
and unsuitable (it runs too slowly to help), auto is managed external
execution: the selector chain runs without a model call, the selected
skill is loaded with its body and completion predicate, prefetch runs,
the gateway's clarification and the confirmation apply, every
generation runs on the external model, and a request no selector places
hands off with the catalog. local_only and parallel_demo are forbidden
while the model is absent or unavailable; while it is unsuitable
local_only still runs locally. Readiness and run.accepted report the
class and its cause. None of this is degraded mode (section 11), and
external_only is unchanged.

Deterministic: scripted model doubles and a fake clock (section 13). The
UI clause of AC20 ("the UI says no local model") is #127.
"""

import asyncio

import pytest

from api_helpers import RUN_ID_HEADER, call, chat, make_app
from gateway_helpers import (
    EXTERNAL_MODEL_ID,
    LOCAL_MODEL_ID,
    STAGING_OK,
    build_gateway,
    executed_tools,
    health_tail,
    route_turn,
    text_turn,
    tool_turn,
)
from helpers import of_type
from relaymind.adapters import AdapterHealth, Message, Role
from relaymind.api.app import ChatSettings
from relaymind.api.chat import ClientToolsChat
from relaymind.api.chat_request import CLIENT_TOOLS
from relaymind.orchestrator.client_loop import ToolOffer, run_client_request
from relaymind.orchestrator.local_loop import ExecutionModeForbiddenError
from relaymind.orchestrator.local_status import LocalModelClass, LocalStatus
from relaymind.orchestrator.performance import PerformanceSettings
from relaymind.orchestrator.route_decision import ExecutionMode
from relaymind.orchestrator.run_state import RunState
from relaymind.tools import CallerContext
from relaymind.tools.sandbox import MUTATE_PERMISSION, READ_PERMISSION

CLASSES = {
    "absent": LocalStatus.absent(),
    "unavailable": LocalStatus(LocalModelClass.UNAVAILABLE, "health check failed: ConnectError"),
    "unsuitable": LocalStatus(
        LocalModelClass.UNSUITABLE, "the routing warm-up took 5200 ms; the stage limit is 4 s"
    ),
}
SIMPLE = "Is staging-api healthy right now?"
NO_SERVICE = "Is the service healthy right now?"
DELETE_PROMPT = "Delete the sandbox database demo-orders-db."
DELETE = {"database_id": "demo-orders-db", "expected_revision": 3}
UNPLACED = "Write a haiku about autumn leaves."
HAIKU = "Leaves drift in cold wind."
MUTATING_CALLER = CallerContext(permissions=frozenset({READ_PERMISSION, MUTATE_PERMISSION}))


@pytest.fixture(params=list(CLASSES))
def status(request) -> LocalStatus:
    return CLASSES[request.param]


def no_local(tmp_path, status, external_turns, **options):
    """A gateway in this class; the local double, when one is configured, has no turns."""
    return build_gateway(
        tmp_path, [], local_status=status, external_turns=external_turns, **options
    )


def run(gateway, prompt=SIMPLE, **options):
    outcome = asyncio.run(gateway.orchestrator.run_text(prompt, **options))
    return outcome, gateway.store.events(outcome.run_id)


def assert_no_local_call(gateway, events):
    """RM26: every generation ran on the external model; the local double saw nothing."""
    assert gateway.adapter.requests == []
    assert {e.payload["model"] for e in of_type(events, "model.started")} <= {EXTERNAL_MODEL_ID}


# -- a simple request -----------------------------------------------------------------


def test_a_simple_request_is_selected_without_a_model_and_answered_externally(tmp_path, status):
    """AC20, RM26, RM28: example_match selects; the external model runs the loaded skill."""
    gateway = no_local(
        tmp_path,
        status,
        [tool_turn("get_service_health", service="staging-api"), text_turn(STAGING_OK)],
    )
    outcome, events = run(gateway)

    assert outcome.state is RunState.COMPLETED and outcome.answer == STAGING_OK
    (route,) = of_type(events, "route.selected")
    assert route.actor == "gateway"
    assert route.payload["mechanism"] == "example_match"
    assert route.payload["decision"]["route"] == "external"
    assert route.payload["decision"]["skill_id"] == "check-service-health"
    assert route.payload["decision"]["reason_code"] == "no_local_model"
    # RM26: the cause of the managed external route is on route.selected.
    assert route.payload["local_model_status"] == status.status.value
    # The chain ran without model_router: no routing generation.
    assert route.payload["selector"]["attempts"] == [
        {"name": "example_match", "outcome": "selected"}
    ]
    (loaded,) = of_type(events, "skill.loaded")
    assert loaded.payload["name"] == "check-service-health"
    (handoff,) = of_type(events, "handoff.started")
    assert handoff.payload["reason_code"] == "no_local_model"
    assert handoff.payload["skill"]["name"] == "check-service-health"
    # Section 11: a gateway without a usable local model is not in degraded mode.
    assert "degraded_mode" not in handoff.payload and "local_failure" not in handoff.payload
    system = gateway.external.requests[0].messages[0].content
    assert "Skill instructions:" in system and "# Check service health" in system
    assert executed_tools(events) == ["get_service_health"]
    (completed,) = of_type(events, "run.completed")
    assert completed.payload["final_answer_model"] == EXTERNAL_MODEL_ID
    assert completed.payload["external_calls"] == 2
    assert_no_local_call(gateway, events)


def test_the_skill_predicate_checks_the_external_answer(tmp_path, status):
    """AC20, RM06: an answer that misses the completion predicate gets a correction."""
    gateway = no_local(
        tmp_path,
        status,
        [
            tool_turn("get_service_health", service="staging-api"),
            text_turn("All good."),
            text_turn(STAGING_OK),
        ],
    )
    outcome, events = run(gateway)

    assert outcome.state is RunState.COMPLETED and outcome.answer == STAGING_OK
    assert len(gateway.external.requests) == 3
    assert_no_local_call(gateway, events)


def test_prefetch_runs_before_the_first_external_generation(tmp_path, status):
    """AC20, RM29: the declared call runs as a gateway call; the first generation answers."""
    gateway = no_local(
        tmp_path,
        status,
        [text_turn(STAGING_OK)],
        performance=PerformanceSettings(prefetch=True),
    )
    outcome, events = run(gateway)

    assert outcome.state is RunState.COMPLETED and outcome.answer == STAGING_OK
    (proposed,) = of_type(events, "tool.proposed")
    assert proposed.actor == "gateway" and proposed.payload["mechanism"] == "prefetch"
    order = [e.event_type for e in events]
    assert order.index("handoff.started") < order.index("tool.proposed")
    assert order.index("tool.finished") < order.index("model.started")
    # Every tool the stage offers has a result, so the first generation answers.
    (request,) = gateway.external.requests
    assert request.tool_schemas == ()
    assert_no_local_call(gateway, events)


# -- a missing identifier --------------------------------------------------------------


def test_a_missing_identifier_gets_the_gateway_clarification(tmp_path, status):
    """AC20, RM05: the gateway asks; on the answer the run goes external with the skill."""
    gateway = no_local(
        tmp_path,
        status,
        [tool_turn("get_service_health", service="staging-api"), text_turn(STAGING_OK)],
    )
    waiting, events = run(gateway, NO_SERVICE)

    assert waiting.state is RunState.WAITING_INPUT
    (route,) = of_type(events, "route.selected")
    decision = route.payload["decision"]
    assert (decision["route"], decision["reason_code"]) == ("clarify", "missing_input")
    assert decision["skill_id"] == "check-service-health"
    assert decision["clarification"].startswith("Which service")
    assert route.actor == "gateway" and route.payload["local_model_status"] == status.status.value
    assert gateway.external.requests == []

    version = gateway.store.get_run(waiting.run_id).version
    outcome = asyncio.run(gateway.orchestrator.resume(waiting.run_id, version, "staging-api"))

    events = gateway.store.events(waiting.run_id)
    assert outcome.state is RunState.COMPLETED and outcome.answer == STAGING_OK
    resumed = of_type(events, "route.selected")[1]
    assert resumed.payload["mechanism"] == "clarification_resume"
    assert resumed.payload["decision"]["route"] == "external"
    assert resumed.payload["decision"]["reason_code"] == "no_local_model"
    assert resumed.payload["local_model_status"] == status.status.value
    assert_no_local_call(gateway, events)


# -- a mutation -------------------------------------------------------------------------


def test_a_mutation_waits_for_confirmation_in_the_external_stage(tmp_path, status):
    """AC20, RM11: the external proposal of a write suspends until it is approved."""
    gateway = no_local(
        tmp_path,
        status,
        [
            tool_turn("list_demo_databases", call_id="e_list"),
            tool_turn("delete_demo_database", call_id="e_delete", **DELETE),
            text_turn("Deleted demo-orders-db."),
        ],
        caller=MUTATING_CALLER,
    )
    waiting, events = run(gateway, DELETE_PROMPT)

    assert waiting.state is RunState.WAITING_CONFIRMATION
    (route,) = of_type(events, "route.selected")
    assert route.payload["decision"]["skill_id"] == "delete-demo-database"
    (required,) = of_type(events, "confirmation.required")
    assert required.payload["model"] == EXTERNAL_MODEL_ID
    assert "demo-orders-db" in [db["database_id"] for db in gateway.sandbox.list_databases()]

    request = waiting.confirmation
    version = gateway.store.get_run(waiting.run_id).version
    outcome = asyncio.run(
        gateway.orchestrator.resume_confirmation(
            waiting.run_id,
            version,
            approval_id=request.approval_id,
            token=request.token,
            approve=True,
        )
    )
    assert outcome.state is RunState.COMPLETED
    assert "demo-orders-db" not in [db["database_id"] for db in gateway.sandbox.list_databases()]
    assert_no_local_call(gateway, gateway.store.events(waiting.run_id))


# -- a request no selector places -------------------------------------------------------


def test_a_request_no_selector_places_hands_off_with_the_catalog(tmp_path, status):
    """AC20, RM28, section 4.4: the chain chose nothing; the external model gets the catalog."""
    gateway = no_local(tmp_path, status, [text_turn(HAIKU)])
    outcome, events = run(gateway, UNPLACED)

    assert outcome.state is RunState.COMPLETED and outcome.answer == HAIKU
    (route,) = of_type(events, "route.selected")
    assert route.payload["mechanism"] == "selector_chain"
    assert route.payload["decision"]["reason_code"] == "selection_failed"
    assert route.payload["local_model_status"] == status.status.value
    (handoff,) = of_type(events, "handoff.started")
    assert handoff.payload["skill"] is None
    assert "Available skills:" in gateway.external.requests[0].messages[0].content
    assert_no_local_call(gateway, events)


# -- run.accepted -----------------------------------------------------------------------


def test_run_accepted_records_the_class_and_its_cause(tmp_path, status):
    """AC20, section 8: local_model is null only when absent; every run records its class."""
    gateway = no_local(tmp_path, status, [text_turn(HAIKU)])
    _, events = run(gateway, UNPLACED)

    accepted = events[0].payload
    expected_model = None if status.status == LocalModelClass.ABSENT else LOCAL_MODEL_ID
    assert accepted["local_model"] == expected_model
    assert accepted["local_model_status"] == status.status.value
    assert accepted["local_model_reason"] == status.reason


def test_a_ready_local_model_is_recorded_as_ready(tmp_path):
    """Section 8: a usable local model is recorded with its ID, ready, and no cause."""
    gateway = build_gateway(tmp_path, [route_turn("check-service-health"), *health_tail()])
    _, events = run(gateway)
    accepted = events[0].payload
    assert (accepted["local_model"], accepted["local_model_status"]) == (LOCAL_MODEL_ID, "ready")
    assert accepted["local_model_reason"] is None
    (route,) = of_type(events, "route.selected")
    assert "local_model_status" not in route.payload


# -- execution modes (section 7.4) -------------------------------------------------------


def native_run(app, mode):
    return call(
        app,
        "POST",
        "/relay/v1/runs",
        json_body={"messages": [{"role": "user", "content": SIMPLE}], "execution_mode": mode},
    )


@pytest.mark.parametrize(
    "name, mode",
    [
        ("absent", "local_only"),
        ("absent", "parallel_demo"),
        ("unavailable", "local_only"),
        ("unavailable", "parallel_demo"),
        # While unsuitable local_only stays permitted (the next tests); parallel_demo does not.
        ("unsuitable", "parallel_demo"),
    ],
)
def test_local_modes_are_forbidden_without_a_usable_local_model(tmp_path, name, mode):
    """AC20, section 7.4: 403 execution_mode_forbidden over the native and the chat API."""
    status = CLASSES[name]
    gateway = no_local(tmp_path, status, [], enabled_workflows={"health-and-diagnostic-checklist"})
    app = make_app(gateway)

    native = native_run(app, mode)
    chat_response = chat(app, headers={"X-RelayMind-Execution-Mode": mode})

    for response in (native, chat_response):
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "execution_mode_forbidden"
        assert status.status.value in response.json()["error"]["message"]
    assert gateway.store.active_run_id() is None
    assert gateway.adapter.requests == [] and gateway.external.requests == []


@pytest.mark.parametrize("mode", [ExecutionMode.LOCAL_ONLY, ExecutionMode.PARALLEL_DEMO])
def test_the_orchestrator_refuses_a_forbidden_mode_before_creating_a_run(tmp_path, mode):
    """Section 7.4: a caller that skips the API check gets the same refusal, and no run."""
    gateway = no_local(tmp_path, CLASSES["unavailable"], [])
    with pytest.raises(ExecutionModeForbiddenError) as caught:
        asyncio.run(gateway.orchestrator.run_text(SIMPLE, execution_mode=mode.value))
    assert (caught.value.http_status, caught.value.code) == (403, "execution_mode_forbidden")
    assert gateway.store.active_run_id() is None
    assert gateway.store.unfinished_runs(()) == []


def test_local_only_runs_locally_while_the_model_is_unsuitable(tmp_path):
    """RM26, section 7.4: the operator asked for local_only, so it runs locally."""
    gateway = build_gateway(
        tmp_path,
        [route_turn("check-service-health"), *health_tail()],
        local_status=CLASSES["unsuitable"],
        external_turns=[],
    )
    outcome, events = run(gateway, execution_mode="local_only")

    assert outcome.state is RunState.COMPLETED and outcome.answer == STAGING_OK
    (route,) = of_type(events, "route.selected")
    assert route.payload["mechanism"] == "native_function_calling"
    assert gateway.external.requests == []
    assert events[0].payload["local_model_status"] == "unsuitable"


def test_external_only_is_unchanged(tmp_path, status):
    """RM26: external_only keeps its own decision and route.selected, the baseline of 9 and 12."""
    gateway = no_local(tmp_path, status, [text_turn(HAIKU)])
    outcome, events = run(gateway, UNPLACED, execution_mode="external_only")

    assert outcome.state is RunState.COMPLETED
    (route,) = of_type(events, "route.selected")
    assert route.payload["mechanism"] == "execution_mode"
    assert route.payload["decision"]["reason_code"] == "execution_mode"
    assert "local_model_status" not in route.payload and "selector" not in route.payload


def test_degraded_mode_does_not_apply_without_a_usable_local_model(tmp_path, status):
    """Section 11: a gateway without a usable local model is not in degraded mode."""
    gateway = no_local(
        tmp_path,
        status,
        [tool_turn("get_service_health", service="staging-api"), text_turn(STAGING_OK)],
        degraded_mode=True,
    )
    outcome, events = run(gateway)

    assert outcome.state is RunState.COMPLETED
    assert [e.payload["mechanism"] for e in of_type(events, "route.selected")] == ["example_match"]
    (handoff,) = of_type(events, "handoff.started")
    assert handoff.payload["reason_code"] == "no_local_model"
    assert "degraded_mode" not in handoff.payload


def test_without_an_external_model_the_run_needs_external(tmp_path):
    """RM04: managed external execution needs a configured external model."""
    gateway = build_gateway(tmp_path, [], local_status=CLASSES["absent"])
    outcome, events = run(gateway)

    assert outcome.state is RunState.FAILED and outcome.error_code == "needs_external"
    assert outcome.reason_code == "no_local_model"
    assert not of_type(events, "model.started")


# -- the client-owned alias ---------------------------------------------------------------


def test_the_client_owned_alias_runs_on_the_external_model(tmp_path, status):
    """Section 7.4: without a usable local model the alias runs as in external_only."""
    gateway = no_local(tmp_path, status, [text_turn(HAIKU)])
    outcome = asyncio.run(
        run_client_request(
            gateway.orchestrator,
            [Message(role=Role.USER, content=UNPLACED)],
            ToolOffer(),
        )
    )
    events = gateway.store.events(outcome.run_id)

    assert outcome.state is RunState.COMPLETED and outcome.answer == HAIKU
    (route,) = of_type(events, "route.selected")
    assert route.payload["mechanism"] == "client_owned"
    assert route.payload["decision"]["route"] == "external"
    assert route.payload["decision"]["reason_code"] == "no_local_model"
    assert route.payload["local_model_status"] == status.status.value
    (handoff,) = of_type(events, "handoff.started")
    assert handoff.payload["reason_code"] == "no_local_model"
    assert_no_local_call(gateway, events)


def test_the_client_owned_alias_over_http_without_a_local_model(tmp_path):
    """Section 7.4: the alias answers through the external model over the chat endpoint."""
    gateway = no_local(tmp_path, CLASSES["absent"], [text_turn(HAIKU)])
    app = make_app(gateway, client_tools=ClientToolsChat(gateway.orchestrator))
    response = chat(app, model=CLIENT_TOOLS, prompt=UNPLACED)

    assert response.status_code == 200, response.text
    assert response.json()["choices"][0]["message"]["content"] == HAIKU
    assert gateway.store.get_run(response.headers[RUN_ID_HEADER]).state == "completed"


# -- readiness (section 7.4) ----------------------------------------------------------------

AUTO_AND_EXTERNAL = ChatSettings(
    allowed_modes=frozenset({ExecutionMode.AUTO, ExecutionMode.EXTERNAL_ONLY})
)


def readyz(app):
    return call(app, "GET", "/readyz")


def down(detail="connection refused"):
    async def health():
        return AdapterHealth(available=False, warm=False, detail=detail)

    return health


def test_readiness_reports_the_class_and_cause_and_auto_needs_the_external_model(tmp_path, status):
    """AC20, section 7.4: auto is ready when the external model is; the local class is shown."""
    gateway = no_local(tmp_path, status, [])
    if status.status == LocalModelClass.UNAVAILABLE:
        gateway.adapter.health = down()
    app = make_app(gateway, settings=AUTO_AND_EXTERNAL)

    response = readyz(app)
    body = response.json()
    assert response.status_code == 200, body
    expected_model = None if status.status == LocalModelClass.ABSENT else LOCAL_MODEL_ID
    assert body["local_model"] == {
        "model_id": expected_model,
        "status": status.status.value,
        "reason": status.reason,
    }
    assert body["modes"]["auto"] == {"ready": True, "reason": None}
    if status.status == LocalModelClass.ABSENT:
        assert body["models"]["local"] is None
    if status.status == LocalModelClass.UNAVAILABLE:
        # An unavailable model is never reported ready (section 12).
        assert body["models"]["local"]["available"] is False


def test_auto_is_not_ready_when_the_external_model_is_down(tmp_path, status):
    """Section 7.4: without a usable local model, auto needs the external model."""
    gateway = no_local(tmp_path, status, [])
    gateway.external.health = down()
    app = make_app(gateway, settings=AUTO_AND_EXTERNAL)

    response = readyz(app)
    assert response.status_code == 503
    assert response.json()["modes"]["auto"] == {
        "ready": False,
        "reason": "the external model is not ready",
    }


def test_readiness_reports_a_forbidden_mode_as_not_ready(tmp_path):
    """Section 7.4: local_only cannot run while the model is unavailable."""
    gateway = no_local(tmp_path, CLASSES["unavailable"], [])
    gateway.adapter.health = down()
    app = make_app(gateway)

    body = readyz(app).json()
    assert body["modes"]["local_only"]["ready"] is False
    assert "unavailable" in body["modes"]["local_only"]["reason"]


def test_a_model_that_comes_back_is_used_again(tmp_path):
    """RM26: readiness repeats the availability check; an available model is ready again."""
    gateway = build_gateway(
        tmp_path,
        [route_turn("check-service-health"), *health_tail()],
        local_status=CLASSES["unavailable"],
        external_turns=[],
        detect=True,
    )
    readyz(make_app(gateway, settings=AUTO_AND_EXTERNAL))

    assert gateway.orchestrator.local_status == LocalStatus.ready()
    outcome, events = run(gateway)
    assert outcome.state is RunState.COMPLETED
    (route,) = of_type(events, "route.selected")
    assert route.payload["mechanism"] == "native_function_calling"
    assert gateway.external.requests == []


def test_a_model_that_stops_answering_is_unavailable_again(tmp_path):
    """RM26: a ready model whose runtime stops answering is classified unavailable."""
    gateway = build_gateway(tmp_path, [], external_turns=[], detect=True)
    gateway.adapter.health = down("connection refused")
    readyz(make_app(gateway, settings=AUTO_AND_EXTERNAL))

    status = gateway.orchestrator.local_status
    assert status.status == LocalModelClass.UNAVAILABLE
    assert "connection refused" in status.reason


@pytest.mark.parametrize("initial", [LocalStatus.ready(), CLASSES["unsuitable"]])
@pytest.mark.parametrize("detail", ["loading model", None, "   "])
def test_a_loading_model_routes_auto_externally_until_warm(tmp_path, initial, detail):
    """AC20, RM26: a responding but unloaded model is unavailable, including on recovery."""
    gateway = build_gateway(
        tmp_path,
        [],
        local_status=initial,
        external_turns=[text_turn(STAGING_OK)],
        detect=True,
        performance=PerformanceSettings(prefetch=True),
    )
    warm_health = gateway.adapter.health

    async def loading():
        return AdapterHealth(available=True, warm=False, detail=detail)

    gateway.adapter.health = loading
    app = make_app(gateway, settings=AUTO_AND_EXTERNAL)
    response = readyz(app)
    body = response.json()

    assert response.status_code == 200
    assert body["local_model"]["status"] == "unavailable"
    assert "loading" in body["local_model"]["reason"]
    assert "stopped answering" not in body["local_model"]["reason"]
    assert body["models"]["local"]["available"] is True
    assert body["models"]["local"]["warm"] is False
    assert body["modes"]["auto"] == {"ready": True, "reason": None}

    outcome, events = run(gateway)
    assert outcome.state is RunState.COMPLETED and outcome.answer == STAGING_OK
    assert events[0].payload["local_model_status"] == "unavailable"
    assert events[0].payload["local_model_reason"] == body["local_model"]["reason"]
    (route,) = of_type(events, "route.selected")
    assert route.payload["decision"]["reason_code"] == "no_local_model"
    assert_no_local_call(gateway, events)

    # A repeated cold check must not restore readiness before the model loaded.
    assert readyz(app).json()["local_model"]["status"] == "unavailable"
    gateway.adapter.health = warm_health
    assert readyz(app).json()["local_model"]["status"] == initial.status.value
    assert gateway.orchestrator.local_status == initial


def test_an_unsuitable_model_that_comes_back_stays_unsuitable(tmp_path):
    """RM26: the measured class is kept; availability alone does not make a slow model fit."""
    gateway = build_gateway(
        tmp_path, [], local_status=CLASSES["unsuitable"], external_turns=[], detect=True
    )
    app = make_app(gateway, settings=AUTO_AND_EXTERNAL)
    real_health = gateway.adapter.health
    gateway.adapter.health = down()
    readyz(app)
    assert gateway.orchestrator.local_status.status == LocalModelClass.UNAVAILABLE

    gateway.adapter.health = real_health
    readyz(app)
    assert gateway.orchestrator.local_status == CLASSES["unsuitable"]


def test_without_detection_readiness_never_changes_the_class(tmp_path):
    """Section 14.1: with placement.detect false an unavailable model is only not ready."""
    gateway = build_gateway(tmp_path, [], external_turns=[])
    gateway.adapter.health = down()
    response = readyz(make_app(gateway, settings=AUTO_AND_EXTERNAL))

    assert response.status_code == 503
    assert gateway.orchestrator.local_status == LocalStatus.ready()
    assert response.json()["modes"]["auto"]["reason"] == "the local model is not ready"
