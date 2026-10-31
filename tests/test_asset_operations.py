"""RM38/AC29: deterministic observations for the operational protocol."""

import asyncio
import shutil
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from recommendation_helpers import BODY, client_for, setup
from relaymind.assets.catalog import acquisition_budgets
from relaymind.assets.service import AssetCatalogService
from relaymind.assets.settings import AssetSettings, SourceConfig
from relaymind.evaluation.asset_operations import (
    BatchSample,
    ImportBatch,
    MemoryObservation,
    RequestSample,
    StartupSample,
    VisibilityProbe,
    WorkloadSamples,
    measure_startups,
    request_sample,
    run_workload,
    summarize_operations,
)
from relaymind.store.recommendation_event import validate_view
from test_asset_sources import capture, skill


@pytest.fixture(scope="module")
def validated_request_cohort(tmp_path_factory):
    """Real API evidence with synthetic timestamps; never AC29 measurement."""

    async def collect():
        app, service, runs, _ = setup(tmp_path_factory.mktemp("operations-cohort"))
        await service.start()
        try:
            async with await client_for(app) as client:
                records = [
                    await request_sample(client, BODY, category="scheduled", ordinal=i)
                    for i in range(120)
                ]
            return [
                replace(
                    record,
                    scheduled_at=i / 2,
                    started_at=i / 2,
                    finished_at=i / 2 + record.client_elapsed_ms / 1000,
                )
                for i, record in enumerate(records)
            ]
        finally:
            await service.stop()
            runs.close()

    return asyncio.run(collect())


def _measured_workload(records):
    return WorkloadSamples("measured", requests=list(records), origin_at=0.0)


def test_schedule_requires_fixed_origin_cadence_and_single_request_slot(
    validated_request_cohort,
    production_batch_samples,
):
    """RM38/AC29: 120 valid API responses cannot relabel a 1 Hz or overlapping run."""
    original = _measured_workload(validated_request_cohort)
    assert summarize_operations([], original, None)["scheduled"]["passes"] is True
    one_hz = _measured_workload(
        [
            replace(
                record,
                scheduled_at=float(i),
                started_at=float(i),
                finished_at=i + record.client_elapsed_ms / 1000,
            )
            for i, record in enumerate(validated_request_cohort)
        ]
    )
    assert summarize_operations([], one_hz, None)["scheduled"]["passes"] is False
    assert (
        summarize_operations([], replace(original, origin_at=1.0), None)["scheduled"]["passes"]
        is False
    )
    wrong_mapping = _measured_workload(validated_request_cohort)
    wrong_mapping.requests[0] = replace(
        wrong_mapping.requests[0],
        scheduled_at=0.25,
        started_at=0.25,
        finished_at=0.25 + wrong_mapping.requests[0].client_elapsed_ms / 1000,
    )
    assert summarize_operations([], wrong_mapping, None)["scheduled"]["passes"] is False
    # A clock whose two reads share a tick ends every response at its start.
    # Zero-duration responses still pass. A probe placed during the first
    # slot does not overlap them, because they end before the probe starts.
    zero_clock = _measured_workload(
        [replace(record, finished_at=record.started_at) for record in validated_request_cohort]
    )
    assert all(record.client_elapsed_ms == 0 for record in zero_clock.requests)
    assert summarize_operations([], zero_clock, None)["scheduled"]["passes"] is True
    probe = replace(production_batch_samples[0].probes[0], started_at=0.005, finished_at=0.015)
    zero_clock.batches = [replace(production_batch_samples[0], probes=(probe,))]
    assert summarize_operations([], zero_clock, None)["scheduled"]["passes"] is True
    # A response that runs for a synthetic 20 ms overlaps the same probe and
    # fails. The interval is set by hand, so the check does not depend on the
    # clock's resolution. A probe that starts as the response ends passes.
    overlapping = _measured_workload(validated_request_cohort)
    first = overlapping.requests[0]
    overlapping.requests[0] = replace(first, finished_at=first.started_at + 0.020)
    overlapping.batches = [replace(production_batch_samples[0], probes=(probe,))]
    assert summarize_operations([], overlapping, None)["scheduled"]["passes"] is False
    touching = replace(probe, started_at=0.020, finished_at=0.030)
    overlapping.batches = [replace(production_batch_samples[0], probes=(touching,))]
    assert summarize_operations([], overlapping, None)["scheduled"]["passes"] is True


def test_measured_label_adversarial_delay_fails(validated_request_cohort):
    """RM38/AC29: 499 ms of dispatch lateness cannot pass a 2 Hz schedule."""
    workload = _measured_workload(validated_request_cohort)
    assert summarize_operations([], workload, None)["scheduled"]["passes"] is True
    workload.requests = [
        replace(
            record,
            started_at=record.scheduled_at + 0.499,
            finished_at=record.scheduled_at + 0.499 + record.client_elapsed_ms / 1000,
        )
        for record in workload.requests
    ]
    assert summarize_operations([], workload, None)["scheduled"]["passes"] is False


def test_delayed_valid_responses_still_report_all_service_latency(
    validated_request_cohort,
):
    """RM38/AC29: bounded dispatch jitter passes and stays in client latency."""
    workload = _measured_workload(
        [
            replace(
                record,
                started_at=record.scheduled_at + 0.001,
                finished_at=record.scheduled_at + 0.001 + record.client_elapsed_ms / 1000,
            )
            for record in validated_request_cohort
        ]
    )
    scheduled = summarize_operations([], workload, None)["scheduled"]
    assert scheduled["passes"] is True
    assert scheduled["useful"] == 120
    assert scheduled["delayed"] == 120
    assert scheduled["service_observed_count"] == 120
    assert scheduled["client_elapsed_count"] == 120
    assert scheduled["p95_observed_ms"] is not None
    assert scheduled["p95_client_elapsed_ms"] is not None
    assert scheduled["scheduled_elapsed_count"] == 120
    assert scheduled["p95_scheduled_elapsed_ms"] == pytest.approx(
        scheduled["p95_client_elapsed_ms"] + 1
    )
    assert scheduled["scheduled_elapsed_ms"] == pytest.approx(
        [value + 1 for value in scheduled["client_elapsed_ms"]]
    )


def test_dispatch_delay_counts_toward_scheduled_latency_gate(validated_request_cohort):
    """RM38/AC29: fast service cannot conceal a delayed client schedule."""
    workload = _measured_workload(
        [
            replace(
                record,
                started_at=record.scheduled_at + 0.35,
                finished_at=record.scheduled_at + 0.351,
            )
            for record in validated_request_cohort
        ]
    )
    scheduled = summarize_operations([], workload, None)["scheduled"]
    assert scheduled["useful"] == scheduled["scheduled_elapsed_count"] == 120
    assert scheduled["p95_client_elapsed_ms"] == pytest.approx(1)
    assert scheduled["p95_scheduled_elapsed_ms"] == pytest.approx(351)
    assert scheduled["passes"] is False


def test_response_overhead_counts_toward_complete_request_latency(validated_request_cohort):
    """RM38/AC29: low selector time cannot hide a 450 ms full response."""
    workload = _measured_workload(
        [
            replace(record, finished_at=record.scheduled_at + 0.45)
            for record in validated_request_cohort
        ]
    )
    scheduled = summarize_operations([], workload, None)["scheduled"]
    assert scheduled["useful"] == scheduled["scheduled_elapsed_count"] == 120
    assert all(record.finished_at <= record.scheduled_at + 0.5 for record in workload.requests)
    assert scheduled["p95_observed_ms"] < 300
    assert scheduled["p95_client_elapsed_ms"] == pytest.approx(450)
    assert scheduled["p95_scheduled_elapsed_ms"] == pytest.approx(450)
    assert scheduled["passes"] is False


@pytest.mark.parametrize("started,finished", [(-0.001, 0.01), (0.499, 0.501)])
def test_each_dispatch_and_response_stays_in_original_slot(
    validated_request_cohort, started, finished
):
    """RM38/AC29: one early dispatch or late response fails even below p95."""
    workload = _measured_workload(validated_request_cohort)
    workload.requests[0] = replace(workload.requests[0], started_at=started, finished_at=finished)
    scheduled = summarize_operations([], workload, None)["scheduled"]
    assert scheduled["p95_scheduled_elapsed_ms"] < 300
    assert scheduled["passes"] is False


def test_slow_in_slot_request_remains_in_latency_cohort(validated_request_cohort):
    """RM27, RM38/AC29: p95 keeps an occasional delayed but in-slot response."""
    workload = _measured_workload(validated_request_cohort)
    workload.requests[0] = replace(workload.requests[0], started_at=0.4, finished_at=0.5)
    scheduled = summarize_operations([], workload, None)["scheduled"]
    assert scheduled["scheduled_elapsed_count"] == 120
    assert scheduled["scheduled_elapsed_ms"][0] == 500
    assert scheduled["passes"] is True


def test_delayed_http_and_timeout_keep_original_failure_and_elapsed(
    validated_request_cohort,
):
    """RM38/AC29: failed completed attempts remain in latency coverage."""
    workload = _measured_workload(validated_request_cohort)
    for i in range(10):
        original = workload.requests[i]
        workload.requests[i] = replace(
            original,
            started_at=original.scheduled_at + 0.001,
            finished_at=original.scheduled_at + 0.401,
            status_code=503 if i % 2 else None,
            outcome="http_error" if i % 2 else "timeout",
            error="http_status" if i % 2 else "deadline_exceeded",
            view=None,
            events=(),
        )
    scheduled = summarize_operations([], workload, None)["scheduled"]
    assert scheduled["passes"] is False
    assert scheduled["failed"] == 10
    assert scheduled["client_elapsed_count"] == 120
    assert scheduled["service_observed_count"] == 110
    assert scheduled["p95_client_elapsed_ms"] >= 399.9
    assert scheduled["scheduled_elapsed_count"] == 120
    assert scheduled["p95_scheduled_elapsed_ms"] >= 400.9


@pytest.mark.parametrize("failure", ["timeout", "transport"])
def test_computing_poll_failure_keeps_full_attempt_latency(validated_request_cohort, failure):
    """RM38/AC29: a computing POST cannot truncate a failed poll or its p95."""
    computing = dict(
        validated_request_cohort[0].view,
        state="computing",
        state_version=0,
        ready_at=None,
        choice_expires_at=None,
        decision=None,
        displayed_candidates=[],
        result=None,
    )
    validate_view(computing)

    async def go():
        times = {}

        async def respond(request):
            if request.method == "POST":
                return httpx.Response(200, json=computing)
            assert request.url.path == (
                f"/relay/v1/recommendations/{computing['recommendation_id']}"
            )
            try:
                await asyncio.sleep(1 if failure == "timeout" else 0.03)
                raise httpx.ConnectError("poll unavailable", request=request)
            finally:
                times["poll_finished"] = time.monotonic()

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(respond), base_url="http://test"
        ) as client:
            began = time.monotonic()
            sample = await request_sample(
                client,
                BODY,
                category="scheduled",
                ordinal=0,
                scheduled_at=began - 0.001,
                deadline_at=began + (0.05 if failure == "timeout" else 1),
            )
        assert sample.outcome == ("timeout" if failure == "timeout" else "failed")
        assert sample.error == (
            "deadline_exceeded" if failure == "timeout" else "invalid_or_unavailable_evidence"
        )
        assert sample.view["state"] == "computing"
        assert sample.finished_at >= times["poll_finished"]
        minimum_elapsed_ms = (times["poll_finished"] - sample.started_at) * 1000
        scheduled = summarize_operations([], WorkloadSamples("fixture", requests=[sample]), None)[
            "scheduled"
        ]
        assert scheduled["denominator"] == 120
        assert scheduled["observed"] == scheduled["failed"] == scheduled["delayed"] == 1
        assert scheduled["missing"] == 119
        assert scheduled["client_elapsed_count"] == 1
        assert scheduled["service_observed_count"] == 0
        assert scheduled["p95_client_elapsed_ms"] >= minimum_elapsed_ms
        assert scheduled["passes"] is False

    asyncio.run(go())


@pytest.mark.parametrize("failure", ["timeout", "transport"])
def test_terminal_response_keeps_timestamp_when_evidence_fetch_fails(
    validated_request_cohort, failure
):
    """RM38/AC29: slow failed evidence does not extend a terminal response."""
    record = validated_request_cohort[0]

    async def go():
        times = {}

        async def respond(request):
            if request.method == "POST":
                times["terminal_response"] = time.monotonic()
                return httpx.Response(200, json=record.view)
            assert request.url.path.endswith("/events")
            times["evidence_started"] = time.monotonic()
            try:
                await asyncio.sleep(1 if failure == "timeout" else 0.03)
                raise httpx.ConnectError("evidence unavailable", request=request)
            finally:
                times["evidence_finished"] = time.monotonic()

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(respond), base_url="http://test"
        ) as client:
            sample = await request_sample(
                client,
                BODY,
                category="scheduled",
                ordinal=0,
                deadline_at=time.monotonic() + (0.05 if failure == "timeout" else 1),
            )
        assert sample.outcome == "failed"
        assert sample.error == (
            "evidence_timeout" if failure == "timeout" else "invalid_or_unavailable_evidence"
        )
        assert times["terminal_response"] <= sample.finished_at <= times["evidence_started"]
        assert sample.finished_at < times["evidence_finished"]
        scheduled = summarize_operations([], WorkloadSamples("fixture", requests=[sample]), None)[
            "scheduled"
        ]
        assert scheduled["client_elapsed_count"] == scheduled["service_observed_count"] == 1
        assert (
            scheduled["p95_client_elapsed_ms"]
            < (times["evidence_finished"] - sample.started_at) * 1000
        )
        assert scheduled["passes"] is False

    asyncio.run(go())


@pytest.mark.parametrize(
    "change",
    [
        lambda sample: replace(sample, status_code=500),
        lambda sample: replace(sample, error="hidden_failure"),
        lambda sample: replace(sample, view={"result": {"observed_ms": 1}}),
        lambda sample: replace(sample, view="invalid"),
        lambda sample: replace(sample, events=()),
        lambda sample: replace(sample, finished_at=sample.started_at - 1),
        lambda sample: replace(sample, started_at=float("nan")),
        lambda sample: replace(sample, view=sample.view | {"state": "failed"}),
    ],
)
def test_measured_cohort_rejects_one_invalid_record(validated_request_cohort, change):
    """RM38/AC29: one bad observation keeps its scheduled denominator and fails."""
    workload = _measured_workload(validated_request_cohort)
    workload.requests[0] = change(workload.requests[0])
    assert summarize_operations([], workload, None)["scheduled"]["passes"] is False


def test_replayed_valid_recommendation_cannot_fill_120_distinct_slots(
    validated_request_cohort,
):
    """RM38/AC29: a copied response does not stand for fresh requests."""
    first = validated_request_cohort[0]
    copies = [
        replace(
            first,
            ordinal=i,
            scheduled_at=i / 2,
            started_at=i / 2,
            finished_at=i / 2 + first.client_elapsed_ms / 1000,
        )
        for i in range(120)
    ]
    assert (
        summarize_operations([], WorkloadSamples("measured", requests=copies), None)["scheduled"][
            "passes"
        ]
        is False
    )


def test_startup_requires_pid_readiness_and_valid_attempt(validated_request_cohort, tmp_path):
    """RM38/AC29: a claimed first useful timestamp needs process and API evidence."""

    async def ready():
        app, service, runs, _ = setup(tmp_path)
        await service.start()
        try:
            async with await client_for(app) as client:
                return (await client.get("/relay/v1/recommendations/readiness")).json()
        finally:
            await service.stop()
            runs.close()

    readiness = asyncio.run(ready())
    records = [
        StartupSample(
            i,
            i * 2.0,
            i * 2.0 + 0.1,
            100 + i,
            (
                replace(
                    validated_request_cohort[i],
                    category="startup",
                    started_at=i * 2.0 + 0.05,
                    finished_at=i * 2.0 + 0.1,
                    scheduled_at=None,
                ),
            ),
            "useful",
            None,
            readiness,
            "measured",
        )
        for i in range(10)
    ]
    assert summarize_operations(records, WorkloadSamples("fixture"), None)["startup"]["passes"]
    for mutated in (
        replace(records[0], pid=None),
        replace(records[0], readiness=None),
        replace(records[0], attempts=()),
        replace(records[0], first_useful_at=records[0].process_started_at - 1),
    ):
        changed = [mutated, *records[1:]]
        assert (
            summarize_operations(changed, WorkloadSamples("fixture"), None)["startup"]["passes"]
            is False
        )


def test_batch_requires_exact_identity_probe_evidence(validated_request_cohort):
    """RM38/AC29: counts and a visibility timestamp alone cannot prove 25 records."""
    workload = WorkloadSamples("measured")
    workload.batches = [
        BatchSample(str(i), float(i), 10.0 + i, 9.0 + i, "visible", None, (), 25, 25)
        for i in range(4)
    ]
    assert summarize_operations([], workload, None)["batches"]["passes"] is False


def _event_page(view, events, *, more=False, after=None):
    return {
        "schema_version": 1,
        "recommendation_id": view["recommendation_id"],
        "events": list(events),
        "next_after_seq": after if after is not None else (events[-1]["seq"] if events else 0),
        "has_more": more,
    }


def test_event_pages_require_complete_bound_terminal_evidence(validated_request_cohort):
    """RM38/AC29: empty, unrelated, duplicate and partial pages fail closed."""

    async def observe(pages):
        record = validated_request_cohort[0]

        class PageClient:
            def __init__(self):
                self.calls = 0

            async def post(self, *args, **kwargs):
                return httpx.Response(200, json=record.view)

            async def get(self, *args, **kwargs):
                index = self.calls
                self.calls += 1
                page = pages[min(index, len(pages) - 1)]
                return httpx.Response(page[0], json=page[1])

        return await request_sample(PageClient(), BODY, category="startup", ordinal=0)

    record = validated_request_cohort[0]
    view, events = record.view, record.events
    valid = _event_page(view, events)
    assert asyncio.run(observe([(200, valid)])).outcome == "useful"
    wrong_event = dict(
        events[0], recommendation_id=validated_request_cohort[1].view["recommendation_id"]
    )
    cases = [
        [(200, _event_page(view, ()))],
        [(200, {"events": list(events)})],
        [(200, _event_page(view, (wrong_event, *events[1:])))],
        [(200, _event_page(view, events[:2], more=True)), (200, _event_page(view, events[:2]))],
    ]
    for pages in cases:
        assert asyncio.run(observe(pages)).outcome == "failed"
    partial = asyncio.run(
        observe(
            [
                (200, _event_page(view, events[:2], more=True)),
                (500, {"error": {"code": "unavailable"}}),
            ]
        )
    )
    assert partial.outcome == "failed"
    assert len(partial.events) == 2


def test_event_pages_keep_one_absolute_deadline(validated_request_cohort):
    """RM38/AC29: fast paginated responses cannot reset a spent deadline."""

    class TickingClock:
        now = 0.0

        def __call__(self):
            self.now += 0.001
            return self.now

    async def go():
        record = validated_request_cohort[0]
        clock = TickingClock()

        class EndlessPages:
            calls = 0

            async def post(self, *args, **kwargs):
                return httpx.Response(200, json=record.view)

            async def get(self, *args, **kwargs):
                self.calls += 1
                event = dict(record.events[-1], seq=self.calls)
                return httpx.Response(
                    200, json=_event_page(record.view, (event,), more=self.calls < 100)
                )

        client = EndlessPages()
        sample = await request_sample(
            client,
            BODY,
            category="startup",
            ordinal=0,
            clock=clock,
            deadline_at=0.04,
        )
        assert sample.outcome == "failed"
        assert client.calls < 30
        assert sample.events

    asyncio.run(go())


def test_bare_job_overlap_flag_cannot_claim_contention():
    """RM38/AC29: scoped boolean without job events or timeline is unknown."""
    workload = WorkloadSamples("measured", identities={"job_ids": ["fake"]})
    workload.jobs.append({"job_id": "fake", "overlap_observed": True})
    assert summarize_operations([], workload, None)["contention_observed"] is None


def test_production_observations_require_nonempty_lexical_and_new_keys(tmp_path):
    """RM38/AC29: authenticated production responses and stored attempts prove usefulness."""

    async def go():
        app, service, runs, _ = setup(tmp_path)
        await service.start()
        try:
            async with await client_for(app) as client:
                first = await request_sample(client, BODY, category="startup", ordinal=0)
                second = await request_sample(client, BODY, category="startup", ordinal=1)
            assert first.outcome == second.outcome == "useful"
            assert first.view["recommendation_id"] != second.view["recommendation_id"]
            assert first.finished_at >= first.started_at
            assert first.events and first.view["result"]["attempts"]
            assert first.view["result"]["final_candidates"]
            assert first.view["result"]["observed_ms"] >= 0
        finally:
            await service.stop()
            runs.close()

    asyncio.run(go())


def test_invalid_or_empty_result_is_not_useful(tmp_path):
    """RM38/AC29: an empty recommendation and an HTTP error remain failures."""

    async def go():
        app, service, runs, _ = setup(tmp_path, populate=False)
        await service.start()
        try:
            async with await client_for(app) as client:
                sample = await request_sample(client, BODY, category="startup", ordinal=0)
            assert sample.outcome != "useful"
            assert sample.error is not None
        finally:
            await service.stop()
            runs.close()

    asyncio.run(go())


def test_event_fetch_timeout_does_not_extend_measured_response(tmp_path):
    """RM38/AC29: evidence collection is bounded but excluded from service latency."""

    async def go():
        app, service, runs, _ = setup(tmp_path)
        await service.start()
        try:
            async with await client_for(app) as production:
                view = (await production.post("/relay/v1/recommendations", json=BODY)).json()

            class SlowEvidence:
                async def post(self, *args, **kwargs):
                    return httpx.Response(200, json=view)

                async def get(self, *args, **kwargs):
                    await asyncio.sleep(0.05)
                    return httpx.Response(200, json={})

            sample = await request_sample(
                SlowEvidence(),
                BODY,
                category="startup",
                ordinal=0,
                deadline_at=time.monotonic() + 0.01,
            )
            assert sample.error == "evidence_timeout"
            assert sample.client_elapsed_ms < 10
        finally:
            await service.stop()
            runs.close()

    asyncio.run(go())


def test_startup_counts_context_entry_and_keeps_ten_failures(tmp_path):
    """RM38/AC29: process creation and token handover precede the first useful answer."""

    async def go():
        app, service, runs, _ = setup(tmp_path)
        await service.start()
        calls = []

        @asynccontextmanager
        async def launch():
            calls.append("enter")
            await asyncio.sleep(0.05)
            async with await client_for(app) as client:
                yield 1234, client
            calls.append("exit")

        samples = []
        try:
            await measure_startups(launch, BODY, samples=samples)
            assert len(samples) == 10
            assert all(x.provenance == "fixture" for x in samples)
            assert all(x.pid == 1234 and x.outcome == "useful" for x in samples)
            assert all(x.first_useful_at > x.process_started_at for x in samples)
            assert calls == [item for _ in range(10) for item in ("enter", "exit")]
        finally:
            await service.stop()
            runs.close()

    asyncio.run(go())


def test_launch_and_cleanup_failures_keep_ten_independent_samples(tmp_path):
    """RM38/AC29: an OS launch or cleanup error is one failed launch, not abort."""

    async def go():
        app, service, runs, _ = setup(tmp_path)
        await service.start()
        enters = []

        @asynccontextmanager
        async def launch():
            ordinal = len(enters)
            enters.append(ordinal)
            if ordinal == 0:
                raise OSError("sensitive process path")
            async with await client_for(app) as client:
                yield 1234, client
            if ordinal == 1:
                raise OSError("sensitive cleanup path")

        samples = []
        try:
            await measure_startups(launch, BODY, samples=samples)
            assert len(samples) == 10 and enters == list(range(10))
            assert [sample.outcome for sample in samples[:2]] == ["failed", "failed"]
            assert all(sample.outcome == "useful" for sample in samples[2:])
            assert all("sensitive" not in str(sample.error) for sample in samples)
        finally:
            await service.stop()
            runs.close()

    asyncio.run(go())


def test_startup_entry_timeout_keeps_original_deadline_and_later_launches(tmp_path):
    """RM38/AC29: token handover delay consumes the one-second launch budget."""

    async def go():
        app, service, runs, _ = setup(tmp_path)
        await service.start()
        count = 0

        @asynccontextmanager
        async def launch():
            nonlocal count
            count += 1
            if count == 1:
                await asyncio.sleep(1.1)
            async with await client_for(app) as client:
                yield 1234, client

        samples = []
        try:
            await measure_startups(launch, BODY, samples=samples)
            assert len(samples) == 10 and count == 10
            assert samples[0].outcome == "failed"
            assert samples[0].first_useful_at is None
            assert all(sample.outcome == "useful" for sample in samples[1:])
        finally:
            await service.stop()
            runs.close()

    asyncio.run(go())


def test_startup_response_deadline_excludes_normal_slow_process_cleanup(tmp_path):
    """RM38/AC29: a useful response is timed before drain and termination."""

    async def go():
        app, service, runs, _ = setup(tmp_path)
        await service.start()
        exits = []

        @asynccontextmanager
        async def launch():
            ordinal = len(exits)
            async with await client_for(app) as client:
                yield 1234, client
            if ordinal == 0:
                await asyncio.sleep(1.1)
            exits.append(ordinal)

        samples = []
        try:
            await measure_startups(launch, BODY, samples=samples)
            assert len(samples) == 10
            assert all(sample.outcome == "useful" for sample in samples)
            assert samples[0].first_useful_at - samples[0].process_started_at < 1
            assert exits == list(range(10))
        finally:
            await service.stop()
            runs.close()

    asyncio.run(go())


def test_startup_entry_cancellation_records_partial_and_cleans_context():
    """RM38/AC29: cancellation during launch does not start another process."""

    async def go():
        entered = asyncio.Event()
        cleaned = []
        gate = asyncio.Event()

        @asynccontextmanager
        async def launch():
            try:
                entered.set()
                await gate.wait()
                raise AssertionError("unreachable")
                yield
            finally:
                cleaned.append(True)

        samples = []
        task = asyncio.create_task(measure_startups(launch, BODY, samples=samples))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cleaned == [True]
        assert len(samples) == 1 and samples[0].outcome == "cancelled"

    asyncio.run(go())


def test_aggregation_retains_denominators_and_unknown_memory():
    """RM38/AC29: missing slots and incomplete counters cannot become a pass."""
    workload = WorkloadSamples(provenance="fixture", identities={"catalog": "fixture"})
    workload.requests.append(
        RequestSample(
            "scheduled",
            0,
            0.0,
            0.0,
            0.1,
            200,
            "useful",
            {"result": {"observed_ms": 30, "attempts": [], "fallback_reason": None}},
            (),
            None,
        )
    )
    summary = summarize_operations([], workload, None)
    assert summary["scheduled"]["denominator"] == 120
    assert summary["scheduled"]["missing"] == 119
    assert summary["scheduled"]["p95_observed_ms"] is None
    assert summary["scheduled"]["passes"] is False
    assert summary["memory"]["incremental_bytes"] is None
    assert summary["memory"]["passes"] is None
    incomplete = MemoryObservation(100, 200, "rss", ("gateway",), ("query",), False)
    assert summarize_operations([], workload, incomplete)["memory"]["passes"] is None
    complete = MemoryObservation(
        100,
        200,
        "platform counters",
        ("gateway", "children", "embedding_device"),
        ("startup", "rebuild", "import", "query"),
        True,
    )
    assert (
        summarize_operations([], WorkloadSamples("measured"), complete)["memory"]["passes"] is True
    )
    assert (
        summarize_operations(
            [],
            WorkloadSamples("measured"),
            replace(
                complete,
                baseline_bytes=None,
            ),
        )["memory"]["passes"]
        is None
    )
    assert (
        summarize_operations([], WorkloadSamples("measured"), replace(complete, counter_source=""))[
            "memory"
        ]["passes"]
        is None
    )


def test_duplicate_scheduled_record_cannot_make_cohort_pass():
    """RM38/AC29: an extra duplicate is not one of the 120 unique slots."""
    workload = WorkloadSamples("fixture")
    for ordinal in range(120):
        workload.requests.append(
            RequestSample(
                "scheduled",
                ordinal,
                ordinal / 2,
                ordinal / 2,
                ordinal / 2 + 0.01,
                200,
                "useful",
                {"result": {"observed_ms": 10, "attempts": []}},
                (),
                None,
            )
        )
    workload.requests.append(replace(workload.requests[0], status_code=500))
    assert summarize_operations([], workload, None)["scheduled"]["passes"] is False


def test_workload_rejects_malformed_plan_before_side_effects():
    """RM38/AC29: the 120-slot, four-batch protocol is fixed."""

    async def go():
        called = []

        async def rebuild():
            called.append("rebuild")

        async with httpx.AsyncClient() as client:
            with pytest.raises(ValueError):
                await run_workload(
                    client,
                    [BODY] * 119,
                    batches=[],
                    rebuild=rebuild,
                    job_evidence=lambda: (),
                    samples=WorkloadSamples("fixture"),
                )
        assert called == []

    asyncio.run(go())


def _batches(clock, calls):
    batches = []
    for number in range(4):
        expected = tuple((f"asset-{number}-{i}", "a" * 64, str(number)) for i in range(25))

        async def receive(number=number):
            calls.append(("receive", number))
            return clock()

        async def rescan(number=number):
            calls.append(("rescan", number))

        batches.append(
            ImportBatch(
                str(number), number + 0.1, (VisibilityProbe(BODY, expected),), receive, rescan
            )
        )
    return batches


def test_workload_keeps_all_slots_and_caller_owned_batch_callbacks():
    """RM38/AC29: four imports and 120 fixed slots retain failures and order."""

    class FastClock:
        now = 0.0

        def __call__(self):
            return self.now

        async def sleep(self, seconds):
            self.now += max(0, seconds) + 0.001
            await asyncio.sleep(0)

    async def go():
        clock = FastClock()
        calls = []
        active = 0
        peak = 0

        async def handler(request):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0)
            active -= 1
            return httpx.Response(503, json={"error": {"code": "unavailable"}})

        async def rebuild():
            calls.append(("rebuild", None))

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://127.0.0.1"
        ) as client:
            samples = await run_workload(
                client,
                [BODY] * 120,
                batches=_batches(clock, calls),
                rebuild=rebuild,
                job_evidence=lambda: (),
                samples=WorkloadSamples("fixture"),
                clock=clock,
                sleep=clock.sleep,
            )
        assert [x.ordinal for x in samples.requests] == list(range(120))
        assert [x.scheduled_at for x in samples.requests] == [i / 2 for i in range(120)]
        assert sum(x.status_code == 503 for x in samples.requests) >= 100
        assert any(
            sample.scheduling_delay_ms is not None
            and sample.scheduling_delay_ms > 0
            and sample.status_code == 503
            and sample.outcome == "http_error"
            and sample.error == "http_status"
            for sample in samples.requests
        )
        assert len(samples.batches) == 4
        assert calls.count(("rebuild", None)) == 1
        assert samples.rebuild["outcome"] == "returned"
        assert all(calls.index(("receive", i)) < calls.index(("rescan", i)) for i in range(4))
        assert peak <= 1
        assert not summarize_operations([], samples, None)["scheduled"]["passes"]

    asyncio.run(go())


def test_cancelled_workload_retains_planned_samples_and_drains_callbacks():
    """RM38/AC29: cancellation leaves no source callback running or lost slot."""

    async def go():
        calls = []
        gate = asyncio.Event()

        async def rebuild():
            calls.append("rebuild")
            await gate.wait()

        async def receive():
            calls.append("receive")
            return time.monotonic()

        async def rescan():
            calls.append("rescan")

        batches = [
            ImportBatch(
                str(i),
                float(i + 1),
                (VisibilityProbe(BODY, tuple((f"a{i}-{n}", "a" * 64, None) for n in range(25))),),
                receive,
                rescan,
            )
            for i in range(4)
        ]
        samples = WorkloadSamples("fixture")
        async with httpx.AsyncClient() as client:
            task = asyncio.create_task(
                run_workload(
                    client,
                    [BODY] * 120,
                    batches=batches,
                    rebuild=rebuild,
                    job_evidence=lambda: (),
                    samples=samples,
                )
            )
            await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert len(samples.requests) == 120
        assert len(samples.batches) == 4
        assert samples.rebuild["outcome"] == "cancelled"
        before = list(calls)
        await asyncio.sleep(0.01)
        assert calls == before

    asyncio.run(go())


def test_failed_rebuild_is_retained_with_safe_error():
    """RM38/AC29: callback failure remains in the caller-owned collector."""

    async def go():
        calls = []

        async def rebuild():
            calls.append("rebuild")
            raise OSError("private rebuild path")

        async def receive():
            return time.monotonic()

        async def rescan():
            return None

        batches = [
            ImportBatch(
                str(i),
                float(i + 1),
                (VisibilityProbe(BODY, tuple((f"a{i}-{n}", "a" * 64, None) for n in range(25))),),
                receive,
                rescan,
            )
            for i in range(4)
        ]
        samples = WorkloadSamples("fixture")
        async with httpx.AsyncClient() as client:
            task = asyncio.create_task(
                run_workload(
                    client,
                    [BODY] * 120,
                    batches=batches,
                    rebuild=rebuild,
                    job_evidence=lambda: (),
                    samples=samples,
                )
            )
            await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert calls == ["rebuild"]
        assert samples.rebuild["outcome"] == "failed"
        assert "rebuild_failed" in samples.errors
        assert "private" not in str(samples.errors)
        assert len(samples.requests) == 120 and len(samples.batches) == 4

    asyncio.run(go())


@pytest.mark.parametrize("failure", ["receive", "rescan"])
def test_failed_batch_callback_keeps_its_planned_sample(failure):
    """RM38/AC29: local receipt or rescan failure remains visible on shutdown."""

    async def go():
        async def rebuild():
            await asyncio.Event().wait()

        async def receive():
            if failure == "receive":
                raise OSError("private source path")
            return time.monotonic()

        async def rescan():
            if failure == "rescan":
                raise OSError("private source path")

        batches = [
            ImportBatch(
                str(i),
                float(i),
                (VisibilityProbe(BODY, tuple((f"a{i}-{n}", "a" * 64, None) for n in range(25))),),
                receive,
                rescan,
            )
            for i in range(4)
        ]
        samples = WorkloadSamples("fixture")
        async with httpx.AsyncClient() as client:
            task = asyncio.create_task(
                run_workload(
                    client,
                    [BODY] * 120,
                    batches=batches,
                    rebuild=rebuild,
                    job_evidence=lambda: (),
                    samples=samples,
                )
            )
            await asyncio.sleep(0.02)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert len(samples.requests) == 120 and len(samples.batches) == 4
        assert samples.batches[0].error == "batch_failed"
        if failure == "receive":
            assert samples.batches[0].received_at is None
        else:
            assert samples.batches[0].received_at is not None
        assert "private" not in str(samples.batches[0])

    asyncio.run(go())


@pytest.fixture(scope="module")
def production_batch_samples(tmp_path_factory):
    """Actual local publication and API evidence; still only a test fixture."""

    async def go():
        tmp_path = tmp_path_factory.mktemp("operations-visibility")
        template = tmp_path / "template"
        for number in range(100):
            skill(
                template / f"p{number}",
                name=f"Skill{number}",
                description=f"uniquevalue{number} independent",
            )
        identities = {
            document.record.identity["package_id"]: (
                document.record.asset_id,
                document.record.content_hash,
                document.record.identity["package_version"],
            )
            for document in capture(template).documents
        }
        app, service, runs, catalog = setup(tmp_path, populate=False)
        source = tmp_path / "source"
        worker = AssetCatalogService(
            AssetSettings((SourceConfig("example", "local_directory", source),)),
            catalog,
            after_change=service.refresh_catalog,
        )
        batches = []
        await service.start()
        worker.start()
        try:
            async with await client_for(app) as client:
                for batch_number in range(4):
                    begin = batch_number * 25
                    for number in range(begin, begin + 25):
                        shutil.copytree(template / f"p{number}", source / f"p{number}")
                    receipt = time.monotonic()
                    await worker.rescan("example")
                    probes = []
                    expected = []
                    for first in range(begin, begin + 25, 10):
                        last = min(first + 10, begin + 25)
                        group = tuple(identities[f"Skill{i}"] for i in range(first, last))
                        expected.extend(group)
                        probes.append(
                            await request_sample(
                                client,
                                BODY
                                | {
                                    "task_text": " ".join(
                                        f"uniquevalue{i}" for i in range(first, last)
                                    )
                                },
                                category="visibility",
                                ordinal=len(probes),
                            )
                        )
                    seen = {
                        (
                            candidate["asset_id"],
                            candidate["content_hash"],
                            candidate["source_version"],
                        )
                        for probe in probes
                        if probe.view and probe.view["result"]
                        for candidate in probe.view["result"]["final_candidates"]
                    }
                    visible = max(probe.finished_at for probe in probes)
                    batches.append(
                        BatchSample(
                            str(batch_number),
                            float(batch_number),
                            receipt,
                            visible,
                            "visible" if set(expected) <= seen else "failed",
                            None,
                            tuple(probes),
                            25,
                            len(set(expected) & seen),
                            tuple(expected),
                        )
                    )
            assert [batch.outcome for batch in batches] == ["visible"] * 4
            assert all(batch.observed_count == 25 for batch in batches)
            # Measured here is an adversarial aggregation label over fixture data.
            workload = WorkloadSamples("measured", batches=batches)
            assert summarize_operations([], workload, None)["batches"]["passes"] is True
            changed = list(batches)
            original = changed[0].expected[0]
            changed[0] = replace(
                changed[0],
                expected=((original[0], "0" * 64, original[2]), *changed[0].expected[1:]),
            )
            assert (
                summarize_operations([], WorkloadSamples("measured", batches=changed), None)[
                    "batches"
                ]["passes"]
                is False
            )
            return batches
        finally:
            await worker.stop()
            await service.stop()
            runs.close()

    return asyncio.run(go())


def test_four_real_source_batches_become_exactly_visible(production_batch_samples):
    """RM38/AC29: four local publications expose all 100 exact versions."""
    assert len(production_batch_samples) == 4


def test_four_replayed_batches_cannot_claim_100_distinct_versions(
    production_batch_samples,
):
    """RM38/AC29: repeating one 25-asset publication is not four imports."""
    original = WorkloadSamples("measured", batches=list(production_batch_samples))
    assert summarize_operations([], original, None)["batches"]["passes"] is True
    copied = [
        replace(production_batch_samples[0], batch_id=str(i), offset_seconds=float(i))
        for i in range(4)
    ]
    result = summarize_operations([], WorkloadSamples("measured", batches=copied), None)
    assert result["batches"]["denominator"] == 4
    assert result["batches"]["coverage"] == 25
    assert result["batches"]["passes"] is False


def _synthetic_valid_job(job_id, source_id, kind, start, finish, base):
    """Schema-valid unit evidence; never an actual measured store job."""

    def stamp(seconds):
        return (base + timedelta(seconds=seconds)).isoformat()

    events = (
        dict(
            schema_version=1,
            job_id=job_id,
            seq=1,
            timestamp=stamp(start),
            event_type="queued",
            payload=dict(
                source_id=source_id,
                catalog_revision=0,
                config_digest="a" * 64,
                job_kind=kind,
                budgets=acquisition_budgets(),
            ),
        ),
        dict(
            schema_version=1,
            job_id=job_id,
            seq=2,
            timestamp=stamp(start + 0.01),
            event_type="started",
            payload=dict(catalog_revision=0, index_revision=None),
        ),
        dict(
            schema_version=1,
            job_id=job_id,
            seq=3,
            timestamp=stamp(finish),
            event_type="published",
            payload=dict(
                catalog_revision=1,
                index_revision="index-v1" if source_id is None else None,
                accepted_count=25,
                rejected_count=0,
                duration_ms=(finish - start) * 1000,
            ),
        ),
    )
    return dict(
        job_id=job_id,
        source_id=source_id,
        state="succeeded",
        started_at=stamp(start),
        finished_at=stamp(finish),
        overlap_observed=True,
        events=events,
    )


def test_contention_needs_scoped_valid_jobs_and_temporal_overlap(
    production_batch_samples,
    validated_request_cohort,
):
    """RM38/AC29: sequential jobs each need bound evidence and foreground overlap."""
    base = datetime(2026, 10, 30, tzinfo=UTC)
    batches = []
    queries = [
        replace(
            validated_request_cohort[4],
            scheduled_at=0.5,
            started_at=0.501,
            finished_at=0.51,
        )
    ]
    jobs = [_synthetic_valid_job("rebuild", None, "lexical", 0, 1, base)]
    for number, original in enumerate(production_batch_samples):
        receipt = 5 + number * 10
        probes = tuple(
            replace(probe, started_at=receipt + i * 0.02, finished_at=receipt + i * 0.02 + 0.01)
            for i, probe in enumerate(original.probes)
        )
        batches.append(
            replace(
                original, received_at=receipt, first_full_visibility_at=receipt + 0.2, probes=probes
            )
        )
        query = validated_request_cohort[number]
        queries.append(
            replace(
                query,
                scheduled_at=receipt + 0.05,
                started_at=receipt + 0.05,
                finished_at=receipt + 0.06,
            )
        )
        jobs.append(
            _synthetic_valid_job(
                f"source{number}",
                "example",
                "refresh",
                receipt - 0.01,
                receipt + 0.15,
                base,
            )
        )
    workload = WorkloadSamples(
        "measured",
        identities={
            "rebuild_job_id": "rebuild",
            "batch_job_ids": {str(i): f"source{i}" for i in range(4)},
        },
        requests=queries,
        batches=batches,
        jobs=jobs,
        rebuild={"started_at": 0.0, "finished_at": 1.0, "outcome": "returned"},
        origin_at=0.0,
        origin_utc=base,
    )
    assert summarize_operations([], workload, None)["contention_observed"] is True
    for change_index, changed in enumerate(
        (
            replace(workload, requests=queries[1:]),
            replace(workload, requests=[queries[0], *queries[2:]]),
            replace(workload, jobs=[jobs[0] | {"overlap_observed": False}, *jobs[1:]]),
            replace(workload, jobs=[jobs[0], jobs[1] | {"overlap_observed": False}, *jobs[2:]]),
            replace(workload, jobs=[*jobs[:1], jobs[1] | {"state": "failed"}, *jobs[2:]]),
            replace(workload, jobs=[*jobs[:1], jobs[1] | {"events": ()}, *jobs[2:]]),
            replace(
                workload,
                jobs=[jobs[0] | {"started_at": (base - timedelta(days=1)).isoformat()}, *jobs[1:]],
            ),
            replace(
                workload,
                identities={
                    "rebuild_job_id": "unrelated",
                    "batch_job_ids": workload.identities["batch_job_ids"],
                },
            ),
        )
    ):
        assert summarize_operations([], changed, None)["contention_observed"] is None, change_index
