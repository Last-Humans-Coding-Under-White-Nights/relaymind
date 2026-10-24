"""RM32/AC19: deterministic statement gates, never provider calls."""

import asyncio

import pytest

from gateway_helpers import STAGING_OK, build_gateway, health_tail, route_turn, run, text_turn
from helpers import of_type
from relaymind.adapters import Completed, TextDelta
from relaymind.orchestrator.performance import PerformanceSettings
from relaymind.orchestrator.run_state import RunState
from relaymind.skills import predicates as p
from test_completion_predicates import UNAVAILABLE, T


def test_live_configuration_is_implemented():
    """RM32: live is opt-in; buffered remains the default."""
    assert PerformanceSettings(answer_stream="live").answer_stream == "live"
    assert PerformanceSettings().answer_stream == "buffered"


def test_gate_holds_ascii_lookahead_and_rejects_later_statement():
    """AC19: chunk boundaries never turn punctuation into an early EOF."""
    gate = p.StatementGate("health_status_preserved", [UNAVAILABLE])
    first = f"payments-api is unavailable at {T}."
    assert gate.feed(first) == ""
    assert gate.feed(" Next sentence; ") == first + " Next sentence;"
    with pytest.raises(p.StatementRejected):
        gate.feed("payments-api is healthy. ")


@pytest.mark.parametrize("width", [1, 2, 7, 1000])
@pytest.mark.parametrize(
    "answer",
    [
        f"payments-api unavailable at {T}. More; text!",
        f"payments-api unavailable（不可用）{T}。更多！",
        f"payments-api unavailable\r\n{T}",
    ],
)
def test_gate_preserves_raw_text_and_boundaries(answer, width):
    """RM12/RM32: accepted output preserves delimiters and CRLF byte for byte."""
    gate = p.StatementGate("health_status_preserved", [UNAVAILABLE])
    parts = [gate.feed(answer[i : i + width]) for i in range(0, len(answer), width)]
    parts.append(gate.finish())
    assert "".join(parts) == answer


def test_local_live_late_failure_is_terminal(tmp_path):
    """AC19/RM20: exact final generation commits; no later correction/handoff."""
    gateway = build_gateway(
        tmp_path,
        [
            route_turn("check-service-health"),
            *health_tail()[:-1],
            [
                TextDelta(STAGING_OK + " "),
                TextDelta("staging-api is unavailable. "),
                Completed("stop"),
            ],
        ],
        performance=PerformanceSettings(answer_stream="live"),
        external_turns=[text_turn(STAGING_OK)],
    )
    outcome, events = run(gateway)
    assert outcome.state is RunState.FAILED
    assert outcome.error_code == "predicate_unmet"
    deltas = of_type(events, "answer.delta")
    assert "".join(e.payload["text"] for e in deltas) == STAGING_OK
    final = of_type(events, "model.started")[-1]
    assert {e.payload["call_id"] for e in deltas} == {final.payload["call_id"]}
    assert gateway.external.requests == []
    assert of_type(events, "run.accepted")[0].payload["answer_stream_effective"] == "live"


def test_native_live_publishes_before_provider_finishes(tmp_path):
    """RM32: native output persists while its exact final call is still running."""

    async def scenario():
        gateway = build_gateway(
            tmp_path,
            [route_turn("check-service-health"), *health_tail()],
            performance=PerformanceSettings(answer_stream="live"),
        )
        original = gateway.adapter.generate
        ready, release = asyncio.Event(), asyncio.Event()

        async def generate(request):
            if request.generation_id.endswith("_final"):
                yield TextDelta(STAGING_OK + " ")
                ready.set()
                await release.wait()
                yield Completed("stop")
            else:
                async for event in original(request):
                    yield event

        gateway.adapter.generate = generate
        from relaymind.adapters import Message, Role

        started = gateway.orchestrator.start(
            (Message(role=Role.USER, content="Is staging-api healthy?"),)
        )
        try:
            await asyncio.wait_for(ready.wait(), 1)
            assert not started.task.done()
            assert of_type(gateway.store.events(started.run_id), "answer.delta")
        finally:
            release.set()
            await started.task

    asyncio.run(scenario())


@pytest.mark.parametrize("early", [False, True])
def test_managed_chat_streams_live_and_keeps_predicate_error(tmp_path, early):
    """AC19: role/content before completion, exact error, no DONE or retry."""
    from api_helpers import chat, make_app

    gateway = build_gateway(
        tmp_path,
        [
            route_turn("check-service-health"),
            *health_tail()[:-1],
            [
                TextDelta(STAGING_OK + " "),
                TextDelta("staging-api is unavailable. "),
                Completed("stop"),
            ],
        ],
        performance=PerformanceSettings(answer_stream="live", early_headers=early),
    )
    response = chat(make_app(gateway), stream=True, raise_app_exceptions=False)
    assert response.status_code == 200
    assert "[DONE]" not in response.text
    assert '"code": "predicate_unmet"' in response.text
    assert "staging-api is unavailable" not in response.text


def test_nonstream_chat_selects_buffered_policy(tmp_path):
    """RM32: stream=false retains the final/correction path before acceptance."""
    from api_helpers import chat, make_app

    gateway = build_gateway(
        tmp_path,
        [route_turn("check-service-health"), *health_tail()],
        performance=PerformanceSettings(answer_stream="live"),
    )
    response = chat(make_app(gateway))
    assert response.status_code == 200
    run_id = response.headers["x-relaymind-run-id"]
    accepted = of_type(gateway.store.events(run_id), "run.accepted")[0]
    assert accepted.payload["answer_stream"] == "live"
    assert accepted.payload["answer_stream_effective"] == "buffered"


@pytest.mark.parametrize(
    "tail,code",
    [("staging-api is unavailable. ", "predicate_unmet"), ("Missing time", "predicate_unmet")],
)
def test_incomplete_end_never_publishes_tail(tmp_path, tail, code):
    """AC19: completeness failure keeps earlier verified text and drops EOF tail."""
    gateway = build_gateway(
        tmp_path,
        [
            route_turn("check-service-health"),
            *health_tail()[:-1],
            text_turn("staging-api is healthy. ", tail),
        ],
        performance=PerformanceSettings(answer_stream="live"),
    )
    outcome, events = run(gateway)
    assert outcome.error_code == code
    assert (
        "".join(e.payload["text"] for e in of_type(events, "answer.delta"))
        == "staging-api is healthy."
    )


@pytest.mark.parametrize(
    "suffix",
    [
        [],
        [Completed("stop")],
        [Completed("stop"), "raise"],
        [Completed("stop"), "failure"],
        ["failure"],
        ["tool"],
    ],
)
def test_provider_terminal_markers_and_late_errors(tmp_path, suffix):
    """RM20/RM32: Completed is provisional; exceptions/missing marker are failures."""
    from relaymind.adapters import GenerationFailed, ToolCall

    async def scenario():
        gateway = build_gateway(
            tmp_path,
            [route_turn("check-service-health"), *health_tail()[:-1]],
            performance=PerformanceSettings(answer_stream="live"),
        )
        original = gateway.adapter.generate

        async def generate(request):
            if not request.generation_id.endswith("_final"):
                async for event in original(request):
                    yield event
                return
            yield TextDelta(STAGING_OK + " ")
            for event in suffix:
                if event == "raise":
                    raise RuntimeError("provider secret")
                if event == "failure":
                    event = GenerationFailed(code="unavailable", message="provider failed")
                if event == "tool":
                    event = ToolCall(
                        call_id="bad",
                        tool_name="get_service_health",
                        arguments={"service": "staging-api"},
                    )
                yield event

        gateway.adapter.generate = generate
        outcome = await gateway.orchestrator.run_text("Is staging-api healthy?")
        events = gateway.store.events(outcome.run_id)
        if suffix == [Completed("stop")]:
            assert outcome.state is RunState.COMPLETED
        else:
            assert outcome.error_code == "external_failed"
            assert of_type(events, "model.finished")[-1].payload["error"] is not None
            assert len(of_type(events, "tool.started")) == 1

    asyncio.run(scenario())


def test_precommit_failure_can_handoff_then_external_correct(tmp_path):
    """AC19: invalid first statement does not commit, so bounded recovery remains."""
    gateway = build_gateway(
        tmp_path,
        [
            route_turn("check-service-health"),
            *health_tail()[:-1],
            text_turn("staging-api is unavailable. "),
        ],
        external_turns=[text_turn("staging-api is unavailable. "), text_turn(STAGING_OK)],
        performance=PerformanceSettings(answer_stream="live"),
    )
    outcome, events = run(gateway)
    assert outcome.state is RunState.COMPLETED
    assert outcome.answer == STAGING_OK
    assert len(gateway.external.requests) == 2
    assert all(not req.tool_schemas for req in gateway.external.requests)
    assert {e.actor for e in of_type(events, "answer.delta")} == {"external"}
    assert len(gateway.adapter.cancelled) == 1
    assert len(gateway.external.cancelled) == 1


@pytest.mark.parametrize(
    "secrets", [{"secret; phrase"}, {"1", "secret; phrase", "secret"}, {"healthy (checked at"}]
)
def test_redaction_equivalence_across_statements_and_chunks(secrets):
    """RM12: buffer known secrets to preserve longest-first full-text semantics."""
    from relaymind.redaction import redact_text

    answer = f"payments-api unavailable at {T}. secret; phrase and secretX; 10 and 1."
    gate = p.StatementGate("health_status_preserved", [UNAVAILABLE], hold=True)
    for char in answer:
        assert gate.feed(char) == ""
    assert redact_text(gate.finish(), secrets) == redact_text(answer, secrets)


def test_managed_redaction_uses_raw_predicate_then_full_scrub(tmp_path):
    """RM12/RM32: secret-bearing evidence still verifies raw text; store is scrubbed."""
    from relaymind.adapters import Message, Role

    answer = STAGING_OK + " secret; phrase. secretX."
    gateway = build_gateway(
        tmp_path,
        [route_turn("check-service-health"), *health_tail()[:-1], text_turn(answer)],
        performance=PerformanceSettings(answer_stream="live"),
    )

    async def scenario():
        accepted = gateway.orchestrator.accept(
            (Message(role=Role.USER, content="Is staging-api healthy?"),)
        )
        accepted._run.secrets.update({"secret", "secret; phrase"})
        return await accepted.start(lambda outcome: outcome)

    outcome = asyncio.run(scenario())
    events = gateway.store.events(outcome.run_id)
    assert outcome.state is RunState.COMPLETED
    assert outcome.answer == STAGING_OK + " [redacted]. secretX."
    assert len(of_type(events, "answer.delta")) == 1


@pytest.mark.parametrize("early", [False, True])
@pytest.mark.parametrize("leave_before_content", [False, True])
def test_live_response_owns_task_through_disconnect(tmp_path, early, leave_before_content):
    """RM14/AC12/AC19: disconnect cancels provider; late resistant text never publishes."""
    from api_helpers import make_app
    from relaymind.api.native import NativeSettings
    from test_chat_disconnect_cancel import chat_client, until

    async def scenario():
        gateway = build_gateway(
            tmp_path,
            [route_turn("check-service-health"), *health_tail()[:-1]],
            performance=PerformanceSettings(answer_stream="live", early_headers=early),
        )
        original = gateway.adapter.generate
        entered = asyncio.Event()

        async def generate(request):
            if not request.generation_id.endswith("_final"):
                async for event in original(request):
                    yield event
                return
            if not leave_before_content:
                yield TextDelta(STAGING_OK + " ")
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                # Cancellation wins even when resistant cleanup exceeds every deadline.
                gateway.clock.advance(1000)
                yield TextDelta("staging-api is unavailable. ")
                yield Completed("stop")

        gateway.adapter.generate = generate
        client = chat_client(
            make_app(gateway, native=NativeSettings(heartbeat_seconds=0.01)), stream=True
        ).start()
        await asyncio.wait_for(entered.wait(), 1)
        if not leave_before_content:
            await until(lambda: any(STAGING_OK.encode() in m.get("body", b"") for m in client.sent))
        await client.leave()
        run_id = gateway.adapter.requests[0].generation_id.split("_gen")[0]
        record = gateway.store.get_run(run_id)
        events = gateway.store.events(record.run_id)
        assert record.state == "cancelled"
        assert not of_type(events, "run.failed")
        assert not any("unavailable" in e.payload["text"] for e in of_type(events, "answer.delta"))
        assert len(gateway.adapter.cancelled) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "bounds, expected_budget, expected_reason",
    [
        ({}, "placement.deadline", "local_slow"),
        ({"active_seconds": 0.2}, "placement.deadline", "local_slow"),
        ({"run_active_seconds": 0.2, "active_seconds": 0.2}, "run_active_seconds", "local_budget"),
    ],
)
def test_postcommit_deadline_precedence(tmp_path, bounds, expected_budget, expected_reason):
    """AC19/AC21: run wins its placement tie; applied placement wins stage tie."""
    from gateway_helpers import LOCAL_MODEL_ID
    from relaymind.orchestrator.budgets import LocalBudgets
    from relaymind.orchestrator.placement import PlacementSettings
    from test_placement import seed

    async def scenario():
        gateway = build_gateway(
            tmp_path,
            [route_turn("check-service-health"), *health_tail()[:-1]],
            external_turns=[],
            performance=PerformanceSettings(answer_stream="live"),
            placement=PlacementSettings(policy="device_aware"),
            input_tokens=100,
            budgets=LocalBudgets(**bounds),
        )
        seed(gateway.store, model=LOCAL_MODEL_ID, purpose="final")
        original = gateway.adapter.generate

        async def generate(request):
            if not request.generation_id.endswith("_final"):
                async for event in original(request):
                    yield event
                return
            yield TextDelta(STAGING_OK + " ")
            gateway.clock.advance(0.25)
            yield TextDelta("staging-api is unavailable. ")
            yield Completed("stop")

        gateway.adapter.generate = generate
        outcome = await gateway.orchestrator.run_text("Is staging-api healthy?")
        events = gateway.store.events(outcome.run_id)
        assert outcome.error_code == "external_failed"
        assert outcome.budget == expected_budget
        assert outcome.reason_code == expected_reason
        assert gateway.external.requests == []
        assert of_type(events, "model.finished")[-1].payload["deadline_missed"] is True
        assert len(gateway.adapter.cancelled) == 1

    asyncio.run(scenario())


def test_exact_generation_ownership_blocks_same_model_and_handoff():
    """RM20: another call of the same actor is still an impermissible replacement."""
    from relaymind.orchestrator.handoff import DispatchAuthority, FinalAnswerOwnedError

    owner = DispatchAuthority()
    owner.claim_final_answer("local", generation=("call1", "model", "local"))
    owner.claim_final_answer("local", generation=("call1", "model", "local"))
    with pytest.raises(FinalAnswerOwnedError):
        owner.claim_final_answer("local", generation=("call2", "model", "local"))
    with pytest.raises(FinalAnswerOwnedError):
        owner.hand_off_to_external()


def test_workflow_streams_only_post_join_synthesis(tmp_path):
    """RM15/RM32: no-tools checklist branch is provisional, synthesis is the answer."""
    from test_parallel_workflow import parallel_gateway, run_parallel

    gateway = parallel_gateway(tmp_path, performance=PerformanceSettings(answer_stream="live"))
    outcome, events = run_parallel(gateway)
    assert outcome.state is RunState.COMPLETED
    synthesis = [
        e for e in of_type(events, "model.started") if e.payload.get("branch") == "synthesis"
    ][0]
    deltas = of_type(events, "answer.delta")
    assert deltas
    assert {e.payload["call_id"] for e in deltas} == {synthesis.payload["call_id"]}
    assert all(e.seq > synthesis.seq for e in deltas)
    assert synthesis.payload["tools_offered"] == []


@pytest.mark.parametrize(
    "predicate, names, results, expected",
    [
        ("health_status_preserved", ["get_service_health"], [], False),
        ("health_status_preserved", ["get_service_health"], [UNAVAILABLE], True),
    ],
)
def test_pending_evidence_requires_each_latest_outcome(predicate, names, results, expected):
    """RM32: no result is not successful evidence; stale successes never revive."""
    from dataclasses import replace

    from relaymind.tools import ToolStatus

    assert p.pending_evidence_complete(names, predicate, results) is expected
    if results:
        failed = replace(results[-1], status=ToolStatus.FAILED, data=None)
        truncated = replace(results[-1], truncated=True)
        assert not p.pending_evidence_complete(names, predicate, [*results, failed])
        assert not p.pending_evidence_complete(names, predicate, [truncated])
        assert p.pending_evidence_complete(names, "performance_evidence_separated", [truncated])


@pytest.mark.parametrize(
    "kind,payload",
    [
        ("run.accepted", {"answer_stream_effective": "invalid"}),
        ("answer.delta", {"call_id": " "}),
        ("answer.delta", {"call_id": 3}),
    ],
)
def test_new_trace_fields_have_matching_python_json_validation(kind, payload):
    """RM22/RM32: new optional fields are typed by both published validators."""
    import json
    from pathlib import Path

    import jsonschema

    from relaymind.store.trace_event import TraceEventError, validate_trace_event
    from test_trace_event import accepted_with

    if kind == "run.accepted":
        event = accepted_with(**payload)
    else:
        event = {
            "schema_version": 1,
            "run_id": "r",
            "seq": 2,
            "timestamp": "2026-10-24T00:00:00Z",
            "elapsed_ms": 0,
            "event_type": kind,
            "actor": "local",
            "payload": {"text": "hello", "model": "m", **payload},
        }
    with pytest.raises(TraceEventError):
        validate_trace_event(event)
    schema = json.loads(Path("src/relaymind/store/schemas/trace_event.v1.json").read_text())
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(event, schema)


@pytest.mark.parametrize("fail_delta", [1, 2])
def test_persistence_failure_is_internal_and_never_forwards_unstored_text(tmp_path, fail_delta):
    """RM12/RM17/RM32: append failure never forwards bytes or changes terminal code."""
    from api_helpers import chat, make_app
    from relaymind.store import EventType

    gateway = build_gateway(
        tmp_path,
        [
            route_turn("check-service-health"),
            *health_tail()[:-1],
            text_turn(STAGING_OK + " More details; "),
        ],
        performance=PerformanceSettings(answer_stream="live"),
    )
    original = gateway.store.append_event
    count = 0

    def append(run_id, event_type, actor, payload):
        nonlocal count
        if event_type == EventType.ANSWER_DELTA:
            count += 1
            if count == fail_delta:
                raise RuntimeError("database internal secret")
        return original(run_id, event_type, actor, payload)

    gateway.store.append_event = append
    response = chat(make_app(gateway), stream=True, raise_app_exceptions=False)
    assert response.status_code == (500 if fail_delta == 1 else 200)
    assert '"code": "internal_error"' in response.text or '"code":"internal_error"' in response.text
    assert "More details" not in response.text
    assert "[DONE]" not in response.text
    assert "database internal secret" not in response.text


def test_failed_completeness_retains_successful_provider_timing(tmp_path):
    """RM16/RM27: provider completion and task completion are separate results."""
    gateway = build_gateway(
        tmp_path,
        [
            route_turn("check-service-health"),
            *health_tail()[:-1],
            text_turn("staging-api is healthy. Missing time"),
        ],
        performance=PerformanceSettings(answer_stream="live"),
    )
    outcome, events = run(gateway)
    assert outcome.error_code == "predicate_unmet"
    last = of_type(events, "model.finished")[-1].payload
    assert last["finish_reason"] == "stop"
    assert last["error"] is None
    assert gateway.store.model_timings()[-1].succeeded


def test_live_client_owned_stays_buffered(tmp_path):
    """RM19/RM32: configured live never turns client tool proposals into partial output."""
    from api_helpers import chat, make_app

    gateway = build_gateway(
        tmp_path,
        [text_turn("Client answer")],
        performance=PerformanceSettings(answer_stream="live"),
    )
    from relaymind.api.chat import ClientToolsChat

    response = chat(
        make_app(gateway, client_tools=ClientToolsChat(gateway.orchestrator)),
        model="relaymind-client-tools",
        stream=True,
    )
    assert response.status_code == 200
    events = gateway.store.events(response.headers["x-relaymind-run-id"])
    assert of_type(events, "run.accepted")[0].payload["answer_stream_effective"] == "buffered"
    assert all("call_id" not in e.payload for e in of_type(events, "answer.delta"))


def test_known_predicate_limitations_remain_accepted():
    """RM16/RM32: live must not silently become a stronger factual verifier."""
    from test_completion_predicates import METRICS, NO_DEPLOYMENT

    for predicate, answer, results in [
        ("deployment_reported", "payments-api checked.", [NO_DEPLOYMENT]),
        (
            "health_status_preserved",
            f"Healthy: payments-api; payments-api is unavailable at {T}.",
            [UNAVAILABLE],
        ),
        (
            "performance_evidence_separated",
            "Observations: payments-api is healthy because aliens fixed it.\nHypotheses: None.",
            [METRICS],
        ),
    ]:
        gate = p.StatementGate(predicate, results)
        assert "".join(gate.feed(c) for c in answer) + gate.finish() == answer


def test_checklist_multiline_labels_and_status_context_are_chunk_independent():
    """RM32: first checklist label/scope and unresolved gloss/resource context survive splits."""
    observed = f"Observation: payments-api unavailable at {T}.\n"
    label = "Diagnostic checklist\n(\ndraft,\nnot log-backed\n)\n"
    answers = [
        observed + label + "payments-api healthy.\nObservation: payments-api unavailable.\n"
        "Diagnostic checklist (draft, not log-backed)\npayments-api healthy.",
        observed + label + "- Observation: payments-api healthy.",
        f"观察：payments-api unavailable（不可用）{T}。\n"
        "诊断检查清单\n（草稿，\n未基于日志）\npayments-api healthy。",
    ]
    for answer in answers:
        expected = p.COMPLETION_PREDICATES["health_checklist_separated"](answer, [UNAVAILABLE])
        for split in range(len(answer) + 1):
            gate = p.StatementGate("health_checklist_separated", [UNAVAILABLE])
            try:
                output = gate.feed(answer[:split]) + gate.feed(answer[split:]) + gate.finish()
            except p.StatementRejected:
                assert not expected
            else:
                assert expected
                assert output == answer


@pytest.mark.parametrize("early", [False, True])
def test_stubborn_live_cleanup_obeys_configured_cancel_wait(tmp_path, early):
    """RM14/AC12: response waits only the native cancel bound while provider retains slot."""
    from api_helpers import make_app
    from relaymind.api.native import NativeSettings
    from test_chat_disconnect_cancel import chat_client, run_ended, until

    async def scenario():
        gateway = build_gateway(
            tmp_path,
            [route_turn("check-service-health"), *health_tail()[:-1]],
            performance=PerformanceSettings(answer_stream="live", early_headers=early),
        )
        original = gateway.adapter.generate
        release = asyncio.Event()

        async def generate(request):
            if not request.generation_id.endswith("_final"):
                async for event in original(request):
                    yield event
                return
            yield TextDelta(STAGING_OK + " ")
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    continue
            yield Completed("stop")

        gateway.adapter.generate = generate
        client = chat_client(
            make_app(
                gateway, native=NativeSettings(heartbeat_seconds=0.01, cancel_wait_seconds=0.01)
            ),
            stream=True,
        ).start()
        try:
            await until(lambda: any(STAGING_OK.encode() in m.get("body", b"") for m in client.sent))
            run_id = gateway.store.active_run_id()
            await asyncio.wait_for(client.leave(), 0.8)
            assert gateway.store.active_run_id() == run_id
            assert len(gateway.adapter.cancelled) == 1
        finally:
            release.set()
            if gateway.store.active_run_id() is not None:
                await run_ended(gateway, gateway.store.active_run_id())

    asyncio.run(scenario())


@pytest.mark.parametrize("early", [False, True])
def test_sdk_receives_verified_prefix_then_predicate_error(tmp_path, early):
    """AC19: standard SDK parses the real socket stream error after verified content."""
    import httpx
    import openai

    from api_helpers import RunningServer, make_app, token_of

    gateway = build_gateway(
        tmp_path,
        [
            route_turn("check-service-health"),
            *health_tail()[:-1],
            text_turn(STAGING_OK + " ", "staging-api is unavailable. "),
        ],
        performance=PerformanceSettings(answer_stream="live", early_headers=early),
    )
    app = make_app(gateway)
    pieces = []
    with RunningServer(app) as server:
        with openai.OpenAI(
            base_url=server.base_url,
            api_key=token_of(app),
            http_client=httpx.Client(trust_env=False),
            max_retries=0,
            timeout=10,
        ) as client:
            stream = client.chat.completions.create(
                model="relaymind",
                messages=[{"role": "user", "content": "Is staging-api healthy?"}],
                stream=True,
            )
            with stream, pytest.raises(openai.APIError) as caught:
                for chunk in stream:
                    pieces.append(chunk.choices[0].delta.content or "")
            assert caught.value.code == "predicate_unmet"
    assert "".join(pieces) == STAGING_OK


def test_completed_live_idempotent_replay_is_stored_and_exact(tmp_path):
    """RM17/RM32: replay delivers identical persisted chunks and no extra generation."""
    from api_helpers import chat, make_app, sse_events

    gateway = build_gateway(
        tmp_path,
        [route_turn("check-service-health"), *health_tail()],
        performance=PerformanceSettings(answer_stream="live"),
    )
    app = make_app(gateway)
    headers = {"Idempotency-Key": "live-replay"}
    first = chat(app, stream=True, headers=headers)
    count = len(gateway.adapter.requests)
    second = chat(app, stream=True, headers=headers)

    def content(response):
        return [
            item["choices"][0]["delta"] for item in sse_events(response) if isinstance(item, dict)
        ]

    assert content(first) == content(second)
    assert len(gateway.adapter.requests) == count
    from relaymind.api.idempotency import REPLAYED_HEADER

    assert second.headers[REPLAYED_HEADER] == "true"


def test_client_owned_python_entry_is_buffered_without_alias(tmp_path):
    """RM19/RM32: client ownership, not its optional alias label, selects buffering."""
    from relaymind.adapters import Message, Role
    from relaymind.orchestrator.client_loop import ToolOffer, run_client_request

    gateway = build_gateway(
        tmp_path,
        [text_turn("Client answer")],
        performance=PerformanceSettings(answer_stream="live"),
    )
    outcome = asyncio.run(
        run_client_request(
            gateway.orchestrator, [Message(role=Role.USER, content="Hello")], ToolOffer(())
        )
    )
    accepted = of_type(gateway.store.events(outcome.run_id), "run.accepted")[0]
    assert accepted.payload["answer_stream_effective"] == "buffered"


@pytest.mark.parametrize("eof_only", [False, True])
def test_verification_time_cannot_publish_past_generation_deadline(tmp_path, monkeypatch, eof_only):
    """RM03/RM27/RM32: a predicate consumes time too; recheck before persisted bytes."""
    from gateway_helpers import LOCAL_MODEL_ID
    from relaymind.adapters import GenerationFailed
    from relaymind.orchestrator.placement import PlacementSettings
    from test_placement import seed

    answer = STAGING_OK if eof_only else STAGING_OK + " More details; "
    gateway = build_gateway(
        tmp_path,
        [route_turn("check-service-health"), *health_tail()[:-1], text_turn(answer)],
        external_turns=[[GenerationFailed(code="unavailable", message="test")]],
        performance=PerformanceSettings(answer_stream="live"),
        placement=PlacementSettings(policy="device_aware"),
        input_tokens=100,
    )
    seed(gateway.store, model=LOCAL_MODEL_ID, purpose="final")
    original = p.completion_parts

    def verify(predicate, text, results):
        if eof_only or "More details" in text:
            gateway.clock.advance(0.25)
        return original(predicate, text, results)

    monkeypatch.setattr(p, "completion_parts", verify)
    outcome, events = run(gateway)
    assert outcome.state is RunState.FAILED
    published = "".join(e.payload["text"] for e in of_type(events, "answer.delta"))
    assert published == ("" if eof_only else STAGING_OK)
    final = [e for e in of_type(events, "model.finished") if e.payload["purpose"] == "final"][0]
    assert final.payload["deadline_missed"] is True
    assert final.payload["error"]["code"] == "deadline_exceeded"


def test_live_external_role_can_publish_with_local_adapter_locality(tmp_path):
    """RM14/RM16/RM32: authority is the external role; trace locality stays local."""
    from relaymind.adapters import Message, Role

    async def scenario():
        gateway = build_gateway(
            tmp_path,
            [
                route_turn("check-service-health"),
                *health_tail()[:-1],
                text_turn("staging-api is unavailable. "),
            ],
            external_turns=[text_turn(STAGING_OK + " ")],
            performance=PerformanceSettings(answer_stream="live"),
        )
        gateway.external.locality = "local"
        accepted = gateway.orchestrator.accept(
            (Message(role=Role.USER, content="Is staging-api healthy?"),)
        )
        outcome = await accepted.start(lambda result: result)
        assert outcome.state is RunState.COMPLETED
        assert outcome.answer == STAGING_OK + " "
        events = gateway.store.events(outcome.run_id)
        generated = of_type(events, "model.started")[-1]
        assert generated.payload["purpose"] == "external"
        assert generated.actor == "local"
        deltas = of_type(events, "answer.delta")
        assert {delta.actor for delta in deltas} == {"local"}
        assert {delta.payload["call_id"] for delta in deltas} == {generated.payload["call_id"]}
        assert accepted._run.authority.answer_generation == (
            generated.payload["call_id"],
            generated.payload["model"],
            "external",
        )
        assert accepted._run.authority.answer_owner == "external"
        assert of_type(events, "run.completed")[0].payload["external_calls"] == 0

    asyncio.run(scenario())


class IteratorOnly:
    """The published adapter contract promises AsyncIterator, without aclose (RM18)."""

    def __init__(self, source):
        self.source = source

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await anext(self.source)


@pytest.mark.parametrize("answer_stream", ["buffered", "live"])
def test_adapter_iterator_without_close_completes_normally(tmp_path, answer_stream):
    """RM18/RM32: lawful iterator-only EOF is not an interrupted model call."""
    gateway = build_gateway(
        tmp_path,
        [route_turn("check-service-health"), *health_tail()],
        performance=PerformanceSettings(answer_stream=answer_stream),
    )
    generate = gateway.adapter.generate
    gateway.adapter.generate = lambda request: IteratorOnly(generate(request))
    outcome, events = run(gateway)
    assert outcome.state is RunState.COMPLETED
    assert outcome.answer == STAGING_OK
    final = of_type(events, "model.finished")[-1]
    assert final.payload["finish_reason"] == "stop"
    assert final.payload["error"] is None
    assert gateway.store.model_timings()[-1].succeeded


def test_early_rejection_awaits_optional_iterator_cleanup_before_next_dispatch(tmp_path):
    """RM14/RM18/RM32: cancellation/close completes before correction or handoff."""

    async def scenario():
        gateway = build_gateway(
            tmp_path,
            [
                route_turn("check-service-health"),
                *health_tail()[:-1],
                text_turn("staging-api is unavailable. "),
            ],
            external_turns=[text_turn(STAGING_OK)],
            performance=PerformanceSettings(answer_stream="live"),
        )
        closing, release = asyncio.Event(), asyncio.Event()
        sequence = []
        generate = gateway.adapter.generate

        async def final_source(request):
            try:
                async for event in generate(request):
                    yield event
            finally:
                sequence.append("source closed")

        class CloseableIterator(IteratorOnly):
            async def aclose(self):
                sequence.append("close requested")
                closing.set()
                await release.wait()
                await self.source.aclose()
                sequence.append("close complete")

        def streamed(request):
            if request.generation_id.endswith("_final"):
                return CloseableIterator(final_source(request))
            return generate(request)

        gateway.adapter.generate = streamed
        gateway.external.on_generate = lambda request: sequence.append("external dispatched")
        task = asyncio.create_task(gateway.orchestrator.run_text("Is staging-api healthy?"))
        try:
            await asyncio.wait_for(closing.wait(), 5)
            run_id = gateway.store.active_run_id()
            assert run_id is not None
            assert not task.done()
            assert gateway.external.requests == []
            assert len(gateway.adapter.cancelled) == 1
            events = gateway.store.events(run_id)
            assert not of_type(events, "handoff.started")
            assert not of_type(events, "answer.delta")
            assert all(
                event.payload["purpose"] != "final" for event in of_type(events, "model.finished")
            )
        finally:
            release.set()
            outcome = await asyncio.wait_for(task, 5)
        assert outcome.state is RunState.COMPLETED
        assert sequence == [
            "close requested",
            "source closed",
            "close complete",
            "external dispatched",
        ]
        assert len(gateway.adapter.cancelled) == 1

    asyncio.run(scenario())
