"""The live acceptance harness, run over model doubles (#88, #104).

scripts/live_acceptance.py starts the gateway the way an operator does
(``relaymind serve --config``, spec section 14.1) and drives the M3, M4,
and M5 acceptance scenarios over HTTP against the pinned local model.
Spec section 13: real models verify capability, doubles verify the
harness. These tests run each scenario against the app the launcher
builds from the harness's own config file, with scripted model doubles
in place of llama-server, so the checks, their owners, and the export
are tested without a model. A model-quality failure must come out as a
failed model check (a finding), never as a pass (RM16, section 12.3).
"""

import asyncio
import json
import os

import httpx
import pytest

from gateway_helpers import (
    STAGING_OK,
    clarify_turn,
    health_tail,
    route_turn,
    text_turn,
    tool_turn,
)
from helpers import ScriptedAdapter, load_script_module, make_capabilities
from launcher_helpers import make_artifact
from relaymind.adapters import Completed, TextDelta, ToolCall
from relaymind.launcher import build_app, load_config

DELETE_ARGS = {"database_id": "demo-orders-db", "expected_revision": 3}
DELETED = "Deleted the sandbox database demo-orders-db."


@pytest.fixture(scope="module")
def harness():
    return load_script_module("live_acceptance")


def config_text(harness, tmp_path, **options):
    manifest, artifact = make_artifact(tmp_path)
    return harness.gateway_config(
        work=tmp_path / "work",
        manifest=manifest,
        artifact=artifact,
        local_url="http://127.0.0.1:8033",
        port=8099,
        **options,
    )


def serve_doubles(harness, tmp_path, turns, *, external_adapter=None, **options):
    """The launcher's app from the harness config, over model doubles; returns an Api."""
    path = tmp_path / "relaymind.toml"
    path.write_text(config_text(harness, tmp_path, **options), encoding="utf-8")
    config = load_config(path)
    local = turns if isinstance(turns, ScriptedAdapter) else ScriptedAdapter(turns=turns)
    launched = build_app(config, local_adapter=local, external_adapter=external_adapter)
    transport = httpx.ASGITransport(app=launched.app)
    return harness.Api(
        "http://127.0.0.1",
        launched.app.state.application_token,
        sandbox_dir=config.sandbox_dir,
        transport=transport,
    )


def run_scenario(harness, name, api, **kwargs):
    async def go():
        try:
            return await harness.SCENARIOS[name].run(api, **kwargs)
        finally:
            await api.aclose()

    return asyncio.run(go())


def owners_of_failures(result):
    return {(check.owner, check.name) for check in result.checks if not check.ok}


# -- the config file and the launcher output ------------------------------------------


def test_the_config_is_one_the_launcher_accepts(harness, tmp_path):
    """Section 14.1: the harness writes the operator's file; nothing else configures a start."""
    path = tmp_path / "relaymind.toml"
    path.write_text(config_text(harness, tmp_path, routing_cache=True, ui=True), "utf-8")
    config = load_config(path)
    assert config.routing_cache is True
    assert config.ui is not None and config.ui.mode == "live"
    assert config.external is None
    assert config.port == 8099
    assert config.local_url == "http://127.0.0.1:8033"
    assert config.sandbox_dir == tmp_path / "work" / "sandbox"
    # Section 7.4: readiness covers every permitted mode; no external model, no external modes.
    assert {mode.value for mode in config.chat.allowed_modes} == {"auto", "local_only"}


def test_the_cache_and_ui_are_off_unless_named(harness, tmp_path):
    """Section 10: the baseline is cache-off; the demo UI is served only when asked."""
    path = tmp_path / "relaymind.toml"
    path.write_text(config_text(harness, tmp_path), "utf-8")
    config = load_config(path)
    assert config.routing_cache is False
    assert config.ui is None


def test_an_external_endpoint_names_only_the_credential_variable(harness, tmp_path):
    """RM12: the file carries the variable name, never a key."""
    endpoint = harness.ExternalEndpoint("https://api.example.invalid/v1", "model-x", 32768)
    text = config_text(
        harness,
        tmp_path,
        external=endpoint,
        workflows=("health-and-diagnostic-checklist",),
    )
    path = tmp_path / "relaymind.toml"
    path.write_text(text, "utf-8")
    config = load_config(path)
    assert config.external.base_url == "https://api.example.invalid/v1"
    assert config.external.api_key_env == harness.EXTERNAL_KEY_ENV
    assert config.enabled_workflows == {"health-and-diagnostic-checklist"}


def test_the_launcher_output_is_parsed_and_the_token_never_recorded(harness):
    """Section 14.1: the token is printed once; the evidence must not carry it."""
    lines = [
        "Ended 1 unfinished run(s) from an earlier process as interrupted.",
        "Serving on http://127.0.0.1:8099",
        "Application token: s3cr3t-token-value",
    ]
    output = harness.parse_launch_output(lines)
    assert output.url == "http://127.0.0.1:8099"
    assert output.token == "s3cr3t-token-value"
    assert output.recovered == 1
    assert "s3cr3t-token-value" not in json.dumps(output.recorded_lines)
    assert output.recorded_lines[-1] == "Application token: <not recorded>"


def test_launcher_output_without_a_token_is_incomplete(harness):
    with pytest.raises(ValueError, match="token"):
        harness.parse_launch_output(["Serving on http://127.0.0.1:8099"])


def test_server_sent_events_are_parsed_in_order(harness):
    text = (
        'id: 1\nevent: run.accepted\ndata: {"seq": 1}\n\n'
        ": keep-alive\n\n"
        'id: 2\nevent: run.completed\ndata: {"seq": 2}\n\n'
    )
    assert harness.parse_sse(text) == [{"seq": 1}, {"seq": 2}]


# -- scenarios over doubles ------------------------------------------------------------


def test_ac05_deny_keeps_the_database(harness, tmp_path):
    """AC05, RM11: deny ends the run and nothing is deleted."""
    turns = [
        route_turn("delete-demo-database"),
        tool_turn("list_demo_databases", call_id="call_list"),
        tool_turn("delete_demo_database", call_id="call_delete", **DELETE_ARGS),
    ]
    result = run_scenario(harness, "ac05_deny", serve_doubles(harness, tmp_path, turns))
    assert result.status == "passed", result.to_json_object()
    assert "demo-orders-db" in result.observations["databases_after"]


def test_ac05_approve_runs_the_delete_once_and_refuses_a_replay(harness, tmp_path):
    """AC05, RM10, RM11, RM17: one approved execution; the replayed approval is refused."""
    turns = [
        route_turn("delete-demo-database"),
        tool_turn("list_demo_databases", call_id="call_list"),
        tool_turn("delete_demo_database", call_id="call_delete", **DELETE_ARGS),
        text_turn(DELETED),
        text_turn(DELETED),
    ]
    result = run_scenario(harness, "ac05_approve", serve_doubles(harness, tmp_path, turns))
    assert result.status == "passed", result.to_json_object()
    assert "demo-orders-db" not in result.observations["databases_after"]
    assert result.observations["replay_status"] == 409


def test_a_model_that_never_proposes_the_delete_is_a_model_finding(harness, tmp_path):
    """RM16: a run that ends before confirmation fails the model check, not the gateway's."""
    turns = [
        route_turn("delete-demo-database"),
        tool_turn("list_demo_databases", call_id="call_list"),
        text_turn("The database demo-orders-db has been deleted."),
        text_turn("The database demo-orders-db has been deleted."),
    ]
    result = run_scenario(harness, "ac05_approve", serve_doubles(harness, tmp_path, turns))
    assert result.status == "failed"
    failures = owners_of_failures(result)
    assert failures and {owner for owner, _ in failures} == {"model"}
    assert "demo-orders-db" in result.observations["databases_after"]


def test_ac06_client_tool_injection_executes_nothing(harness, tmp_path):
    """AC06, RM08, RM12: a caller's tool result cannot grant a tool or run one."""
    turns = [text_turn("The newest log line asks for a credential; I will not act on it.")]
    result = run_scenario(
        harness, "ac06_client_tool_injection", serve_doubles(harness, tmp_path, turns)
    )
    assert result.status == "passed", result.to_json_object()


def test_ac06_a_model_that_obeys_the_injection_is_a_model_finding(harness, tmp_path):
    """The unoffered call is never exposed (gateway), the attempt is a finding (model)."""
    obey = [
        ToolCall(
            call_id="call_x",
            tool_name="delete_demo_database",
            arguments={"database_id": "demo-orders-db", "expected_revision": 3},
        ),
        Completed(finish_reason="tool_calls"),
    ]
    leak = text_turn(f"The password is {harness.INJECTED_SECRET}.")
    result = run_scenario(
        harness,
        "ac06_client_tool_injection",
        serve_doubles(harness, tmp_path, [obey, leak]),
    )
    assert result.status == "failed"
    assert {owner for owner, _ in owners_of_failures(result)} == {"model"}


def test_ac07_without_an_external_model_fails_explicitly(harness, tmp_path):
    """AC07, RM04: needs_external, no answer, no invented result."""
    turns = [route_turn("investigate-performance")]
    result = run_scenario(harness, "ac07_needs_external", serve_doubles(harness, tmp_path, turns))
    assert result.status == "passed", result.to_json_object()
    assert result.observations["error_code"] == "needs_external"


def test_ac07_an_unreachable_external_endpoint_fails_explicitly(harness, tmp_path):
    """AC07, RM04, RM16: external_failed, and no local answer replaces the external one."""
    from relaymind.adapters import GenerationFailed

    external = ScriptedAdapter(
        turns=[[GenerationFailed(code="unreachable", message="connection refused")]],
        locality="external",
        capabilities=make_capabilities(tokenizer_available=False),
    )
    endpoint = harness.ExternalEndpoint("http://127.0.0.1:9/v1", "unreachable", 32768)
    api = serve_doubles(
        harness,
        tmp_path,
        [route_turn("investigate-performance")],
        external_adapter=external,
        external=endpoint,
    )
    result = run_scenario(harness, "ac07_external_failed", api)
    assert result.status == "passed", result.to_json_object()
    assert result.observations["error_code"] == "external_failed"


def test_ac09_sdk_round_trip_with_and_without_stream(harness, tmp_path):
    """AC09, RM19, RM20: the official SDK reads both responses; content is the answer."""
    turns = [
        route_turn("check-service-health"),
        *health_tail(),
        route_turn("check-service-health"),
        *health_tail(),
    ]
    result = run_scenario(harness, "ac09_sdk", serve_doubles(harness, tmp_path, turns))
    assert result.status == "passed", result.to_json_object()
    assert result.observations["stream_false"]["content"] == STAGING_OK
    assert result.observations["stream_true"]["content"] == STAGING_OK


def test_ac10_client_owned_call_then_result(harness, tmp_path):
    """AC10, RM08, RM19: the caller executes; the gateway executes nothing."""
    call = [
        ToolCall(
            call_id="call_status",
            tool_name="get_service_status",
            arguments={"service": "staging-api"},
        ),
        Completed(finish_reason="tool_calls"),
    ]
    answer = text_turn("staging-api is healthy (checked at 2026-10-20T02:58:00Z).")
    result = run_scenario(
        harness, "ac10_client_tools", serve_doubles(harness, tmp_path, [call, answer])
    )
    assert result.status == "passed", result.to_json_object()
    assert len(result.run_ids) == 2


def test_ac11_replay_and_idempotent_resubmission(harness, tmp_path):
    """AC11, RM17, RM21, RM22: replay in order; the same key creates no new work."""
    turns = [route_turn("check-service-health"), *health_tail()]
    result = run_scenario(
        harness, "ac11_replay_idempotency", serve_doubles(harness, tmp_path, turns)
    )
    assert result.status == "passed", result.to_json_object()
    assert result.observations["conflict_status"] == 409


class HangingAdapter(ScriptedAdapter):
    """A local double whose generation never ends until it is cancelled."""

    async def generate(self, request):
        self.requests.append(request)
        await asyncio.sleep(60)
        yield TextDelta(text="never")


def test_ac12_cancel_during_a_generation(harness, tmp_path):
    """AC12, RM14: the cancel lands while the model call is in flight; dispatch stops."""
    api = serve_doubles(harness, tmp_path, HangingAdapter(turns=[]))
    result = run_scenario(harness, "ac12_cancel_generation", api)
    assert result.status == "passed", result.to_json_object()
    assert result.observations["in_flight_calls"]


def test_cache_prompts_label_hits_and_misses(harness, tmp_path):
    """AC13, RM24, section 10: the second round hits; the hit skips the routing call."""
    prompts = (("Is staging-api healthy right now?", "en", "staging-api"),)
    turns = [route_turn("check-service-health"), *health_tail(), *health_tail()]
    api = serve_doubles(harness, tmp_path, turns, routing_cache=True)
    result = run_scenario(harness, "routing_cache", api, prompts=prompts, rounds=2, warmup=None)
    assert result.status == "passed", result.to_json_object()
    summary = result.observations["cache"]
    assert (summary["hits"], summary["misses"]) == (1, 1)
    assert summary["routing_cache"] == "on"


def test_cache_off_records_no_hit(harness, tmp_path):
    prompts = (("Is staging-api healthy right now?", "en", "staging-api"),)
    turns = [route_turn("check-service-health"), *health_tail()] * 2
    api = serve_doubles(harness, tmp_path, turns)
    result = run_scenario(harness, "routing_cache", api, prompts=prompts, rounds=2, warmup=None)
    assert result.status == "passed", result.to_json_object()
    summary = result.observations["cache"]
    assert (summary["hits"], summary["misses"], summary["routing_cache"]) == (0, 0, "off")


def test_a_clarifying_model_on_a_health_prompt_is_a_model_finding(harness, tmp_path):
    """A run that asks instead of answering fails the model check of the cache scenario."""
    prompts = (("Is staging-api healthy right now?", "en", "staging-api"),)
    api = serve_doubles(harness, tmp_path, [clarify_turn()])
    result = run_scenario(harness, "routing_cache", api, prompts=prompts, rounds=1, warmup=None)
    assert result.status == "failed"
    assert {owner for owner, _ in owners_of_failures(result)} == {"model"}


# -- external scenarios and the export -------------------------------------------------


def test_external_scenarios_are_not_run_without_an_endpoint(harness):
    """#88, #104: without a configured endpoint they are recorded as not run, never passed."""
    results = harness.not_run_external(None, environ={})
    names = {r.name for r in results}
    assert names == {*harness.EXTERNAL_SCENARIOS, "ac06_managed_logs", "ac08_external_swap"}
    assert {"ac02_handoff", "parallel_demo", "external_only"} <= {r.name for r in results}
    assert all(r.status == "not_run" and r.reason for r in results)


def test_external_scenarios_need_the_credential_too(harness):
    endpoint = harness.ExternalEndpoint("https://api.example.invalid/v1", "model-x", 32768)
    results = harness.not_run_external(endpoint, environ={})
    assert all(r.status == "not_run" for r in results)
    assert harness.not_run_external(endpoint, environ={harness.EXTERNAL_KEY_ENV: "k"}) == []


def test_stats_records_come_from_the_trace(harness):
    """RM25: per-call records from model.finished; completion only for a final answer."""
    events = [
        {
            "event_type": "model.started",
            "timestamp": "2026-10-22T10:00:00Z",
            "payload": {"call_id": "c1", "model": "m", "purpose": "routing"},
        },
        {
            "event_type": "model.finished",
            "payload": {
                "call_id": "c1",
                "model": "m",
                "duration_ms": 300,
                "finish_reason": "tool_calls",
                "error": None,
                "usage": {"input_tokens": 900, "output_tokens": 20},
            },
        },
        {
            "event_type": "model.started",
            "timestamp": "2026-10-22T10:00:01Z",
            "payload": {"call_id": "c2", "model": "m", "purpose": "final"},
        },
        {
            "event_type": "model.finished",
            "payload": {
                "call_id": "c2",
                "model": "m",
                "duration_ms": 650,
                "finish_reason": "stop",
                "error": None,
                "usage": None,
            },
        },
    ]
    records = harness.generation_records(events, prompt="p")
    assert [r.generation_id for r in records] == ["c1", "c2"]
    assert records[0].completion_seconds is None and records[0].input_tokens == 900
    assert records[1].completion_seconds == 0.65
    assert records[1].started_at_utc == "2026-10-22T10:00:01Z"
    assert all(r.first_model_action_seconds is None for r in records)


def test_first_model_action_skips_gateway_routes(harness):
    """Section 9 timing: a gateway-made route (a cache hit) is not a model action."""
    events = [
        {"event_type": "route.selected", "actor": "gateway", "elapsed_ms": 1, "payload": {}},
        {
            "event_type": "tool.proposed",
            "actor": "local",
            "elapsed_ms": 400,
            "payload": {"admissible": False},
        },
        {
            "event_type": "tool.proposed",
            "actor": "local",
            "elapsed_ms": 700,
            "payload": {"admissible": True},
        },
    ]
    assert harness.first_model_action_ms(events) == 700


def test_the_export_hides_local_paths(harness, tmp_path):
    """Committed evidence names <repo> and <work>, not this machine's directories."""
    path = tmp_path / "out.json"
    harness.write_json(
        path, {"a": f"{harness.REPO_ROOT}/skills", "b": f"{tmp_path}/x"}, work=tmp_path
    )
    data = json.loads(path.read_text("utf-8"))
    assert data == {"a": "<repo>/skills", "b": "<work>/x"}
    assert os.fspath(tmp_path) not in path.read_text("utf-8")


def test_ui_session_reads_the_live_label(harness, tmp_path):
    """RM23: the session drives the real page and records the declared data mode."""
    from browser_helpers import served
    from ui_helpers import health_gateway

    shot = tmp_path / "ui.png"
    with served(health_gateway(tmp_path), mode="live") as (base, token):
        api = harness.Api(base, token, sandbox_dir=tmp_path)
        result = run_scenario(harness, "ui_live_session", api, screenshot=shot)
    assert result.status == "passed", result.to_json_object()
    labels = result.observations["labels"]
    assert labels and all("LIVE" in label for label in labels)
    assert shot.is_file() and shot.stat().st_size > 0


# -- the local-preparation launch and the external scenarios ---------------------------

INVESTIGATE_TOOLS = [
    tool_turn("get_latest_deployment", call_id="c1", service="payments-api"),
    tool_turn("get_recent_logs", call_id="c2", service="payments-api", limit=3),
    tool_turn("get_metrics", call_id="c3", service="payments-api", window_minutes=15),
]
EVIDENCE_ANSWER = (
    "Observations:\n- get_metrics: p95 latency rose to 3020 ms.\n\n"
    "Hypotheses:\n- The upstream ledger-db may be slow.\n"
)


def local_prep(harness, tmp_path, turns, **options):
    work = tmp_path / "work"
    return serve_doubles(
        harness,
        tmp_path,
        turns,
        skill_root=harness.local_prep_skills(work),
        seed=harness.poisoned_seed(work),
        **options,
    )


def test_ac07_local_budget_escalates_after_two_tools(harness, tmp_path):
    """AC07, RM03, RM04: the third tool exceeds the local budget; needs_external."""
    turns = [route_turn("investigate-performance"), *INVESTIGATE_TOOLS]
    result = run_scenario(harness, "ac07_local_budget", local_prep(harness, tmp_path, turns))
    assert result.status == "passed", result.to_json_object()
    assert result.observations["reason_code"] == "local_budget"
    assert result.observations["executed"] == ["get_latest_deployment", "get_recent_logs"]


def test_ac06_managed_logs_refuses_the_injected_delete(harness, tmp_path):
    """AC06, RM08, RM12: the model obeys the log entry; the gateway still refuses."""
    obey = tool_turn("delete_demo_database", call_id="c3", **DELETE_ARGS)
    turns = [route_turn("investigate-performance"), *INVESTIGATE_TOOLS[:2], obey, obey]
    result = run_scenario(harness, "ac06_managed_logs", local_prep(harness, tmp_path, turns))
    assert result.observations["injection_reached_the_model"] is True
    assert {owner for owner, _ in owners_of_failures(result)} <= {"model"}
    assert result.observations["rejected_proposals"]
    assert not any(p["admissible"] for p in result.observations["rejected_proposals"])


def test_ac02_handoff_carries_the_local_evidence(harness, tmp_path):
    """AC02, RM13, RM14: the handoff carries completed operations; none is replayed."""
    external = ScriptedAdapter(
        turns=[
            tool_turn("get_metrics", call_id="e1", service="payments-api", window_minutes=15),
            text_turn(EVIDENCE_ANSWER),
        ],
        locality="external",
    )
    endpoint = harness.ExternalEndpoint("https://api.example.invalid/v1", "model-x", 32768)
    turns = [route_turn("investigate-performance"), *INVESTIGATE_TOOLS]
    api = local_prep(harness, tmp_path, turns, external_adapter=external, external=endpoint)
    result = run_scenario(harness, "ac02_handoff", api)
    assert result.status == "passed", result.to_json_object()
    assert len(result.observations["local_operations_before_handoff"]) == 2


def test_parallel_demo_records_the_branch_and_synthesis_timings(harness, tmp_path):
    """AC14, RM15, section 12.1: the workflow summary and the synthesis after both branches."""
    external = ScriptedAdapter(
        turns=[
            text_turn("1. Check the upstream dependencies."),
            text_turn(
                "Observation: payments-api is unavailable (checked at 2026-10-20T02:59:00Z).\n\n"
                "Diagnostic checklist (draft, not log-backed):\n- Check the upstream dependencies."
            ),
        ],
        locality="external",
    )
    endpoint = harness.ExternalEndpoint("https://api.example.invalid/v1", "model-x", 32768)
    turns = [
        route_turn("health-and-diagnostic-checklist"),
        tool_turn("get_service_health", service="payments-api"),
    ]
    api = serve_doubles(
        harness,
        tmp_path,
        turns,
        external_adapter=external,
        external=endpoint,
        workflows=("health-and-diagnostic-checklist",),
        modes=harness.ALL_MODES,
    )
    result = run_scenario(harness, "parallel_demo", api)
    assert result.status == "passed", result.to_json_object()
    workflow = result.observations["workflow"]
    assert workflow["extra_call_ms"] is not None
    assert {"local_health_ms", "external_checklist_ms", "branch_wall_ms"} <= set(workflow)


def test_the_plans_run_external_scenarios_only_with_an_endpoint_and_a_key(harness):
    endpoint = harness.ExternalEndpoint("https://api.example.invalid/v1", "model-x", 32768)
    names = [plan.name for plan in harness.launch_plans(None, {})]
    assert names == ["local", "local-prep", "unreachable-external", "cache-off", "cache-on"]
    assert "external" not in [plan.name for plan in harness.launch_plans(endpoint, {})]
    with_key = harness.launch_plans(endpoint, {harness.EXTERNAL_KEY_ENV: "k"})
    external = [plan for plan in with_key if plan.name.startswith("external")]
    assert [plan.name for plan in external] == ["external", "external-poisoned"]
    assert all(plan.external == endpoint for plan in external)
    cache = {plan.name: plan.routing_cache for plan in with_key if plan.name.startswith("cache")}
    assert cache == {"cache-off": False, "cache-on": True}


def test_ac12_restart_during_a_generation(harness, tmp_path):
    """AC12, RM17, section 5: a new gateway over the same store ends the run as interrupted."""
    api = serve_doubles(harness, tmp_path, HangingAdapter(turns=[]))
    config = load_config(tmp_path / "relaymind.toml")

    async def restart():
        launched = build_app(config, local_adapter=ScriptedAdapter(turns=[]))
        transport = httpx.ASGITransport(app=launched.app)
        token = launched.app.state.application_token
        fresh = harness.Api(
            "http://127.0.0.1", token, sandbox_dir=config.sandbox_dir, transport=transport
        )
        return fresh, launched.recovered

    result = run_scenario(harness, "ac12_restart", api, restart=restart)
    assert result.status == "passed", result.to_json_object()
    assert result.observations["recovered_runs"] == 1
    assert result.observations["in_flight_calls"]


def external_double(*turns):
    return ScriptedAdapter(turns=list(turns), locality="external")


ENDPOINT_FIELDS = ("https://api.example.invalid/v1", "model-x", 32768)


def test_external_only_calls_only_the_external_model(harness, tmp_path):
    """Section 7.4, AC08: the gateway fixes the route; every model call is external."""
    external = external_double(
        tool_turn("get_service_health", service="staging-api"), text_turn(STAGING_OK)
    )
    api = serve_doubles(
        harness,
        tmp_path,
        [],
        external_adapter=external,
        external=harness.ExternalEndpoint(*ENDPOINT_FIELDS),
        modes=harness.ALL_MODES,
    )
    result = run_scenario(harness, "external_only", api)
    assert result.status == "passed", result.to_json_object()
    assert result.observations["executed"] == ["get_service_health"]


def test_ac09_and_ac10_run_with_the_external_only_header(harness, tmp_path):
    """AC09, AC10: the SDK sends the mode header; the external model answers."""
    external = external_double(
        tool_turn("get_service_health", service="staging-api"),
        text_turn(STAGING_OK),
        tool_turn("get_service_health", service="staging-api"),
        text_turn(STAGING_OK),
    )
    endpoint = harness.ExternalEndpoint(*ENDPOINT_FIELDS)
    api = serve_doubles(
        harness, tmp_path, [], external_adapter=external, external=endpoint, modes=harness.ALL_MODES
    )
    result = run_scenario(harness, "ac09_sdk_external", api)
    assert result.status == "passed", result.to_json_object()
    call = [
        ToolCall(
            call_id="call_status",
            tool_name="get_service_status",
            arguments={"service": "staging-api"},
        ),
        Completed(finish_reason="tool_calls"),
    ]
    external = external_double(call, text_turn("staging-api is healthy."))
    (tmp_path / "b").mkdir()
    api = serve_doubles(
        harness,
        tmp_path / "b",
        [],
        external_adapter=external,
        external=endpoint,
        modes=harness.ALL_MODES,
    )
    result = run_scenario(harness, "ac10_client_tools_external", api)
    assert result.status == "passed", result.to_json_object()


def _swap_trace(model):
    skills = [{"name": "s", "version": "1", "content_hash": "sha256:a"}]
    return [
        {"event_type": "run.accepted", "payload": {"skills_offered": skills}},
        {"event_type": "model.started", "payload": {"model": model}},
    ]


def _swap_config(model, port):
    return (
        f'port = {port}\npath = "/w/{model}"\n[external_model]\nmodel = "{model}"\n'
        'api_key_env = "RELAYMIND_EXTERNAL_API_KEY"\n'
    )


def _swap_result(harness, launch, run_id):
    result = harness.Result("external_only", ("AC08",), launch=launch, run_ids=[run_id])
    result.check("completed", "model", True)
    return result


def test_ac08_external_swap_needs_only_the_model_line_changed(harness):
    """AC08, RM18: same skills, each run names its own model, only the model line differs."""
    first = _swap_result(harness, "external", "r1")
    second = _swap_result(harness, "external-second", "r2")
    traces = {"r1": _swap_trace("model-a"), "r2": _swap_trace("model-b")}
    configs = {
        "external": _swap_config("model-a", 1),
        "external-second": _swap_config("model-b", 2),
    }
    models = {"external": "model-a", "external-second": "model-b"}
    result = harness.ac08_external_swap(
        first, second, traces=traces, configs=configs, models=models
    )
    assert result.status == "passed", result.to_json_object()
    configs["external-second"] += "context_limit = 8\n"
    result = harness.ac08_external_swap(
        first, second, traces=traces, configs=configs, models=models
    )
    assert result.status == "failed"


def test_a_second_external_model_adds_the_swap_plan(harness):
    endpoint = harness.ExternalEndpoint(*ENDPOINT_FIELDS)
    plans = harness.launch_plans(endpoint, {harness.EXTERNAL_KEY_ENV: "k"}, "model-y")
    by_name = {plan.name: plan for plan in plans}
    assert by_name["external-second"].external.model == "model-y"
    assert by_name["external-second"].external.base_url == endpoint.base_url
    assert by_name["external-poisoned"].poisoned and not by_name["external"].poisoned


def test_only_cut_off_generations_count_as_in_flight(harness):
    """AC12: a call that failed on its own budget is not a call a cancel cut off."""

    def call(call_id, error=None, finished=True):
        started = {"event_type": "model.started", "payload": {"call_id": call_id}}
        done = {"event_type": "model.finished", "payload": {"call_id": call_id, "error": error}}
        return [started, done] if finished else [started]

    events = [
        *call("ok"),
        *call("budget", {"code": "time_budget"}),
        *call("cut", {"code": "cancelled"}),
        *call("open", finished=False),
    ]
    assert harness.in_flight_calls(events) == ["cut", "open"]
