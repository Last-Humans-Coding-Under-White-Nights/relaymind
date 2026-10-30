"""Internal RM38/AC29 operational observations; no qualification claim."""

import asyncio
import math
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager as AsyncContextManager
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx

from relaymind.evaluation.metrics import nearest_rank
from relaymind.store.index_job_event import validate_job_event
from relaymind.store.recommendation_event import validate_event, validate_readiness, validate_view

_ENDPOINT = "/relay/v1/recommendations"
_REQUIRED_PHASES = frozenset({"startup", "rebuild", "import", "query"})
_REQUIRED_COVERAGE = frozenset({"gateway", "children", "embedding_device"})


@dataclass(frozen=True)
class RequestSample:
    category: str
    ordinal: int
    scheduled_at: float | None
    started_at: float | None
    finished_at: float | None
    status_code: int | None
    outcome: str
    view: Mapping[str, object] | None
    events: tuple[Mapping[str, object], ...]
    error: str | None

    @property
    def client_elapsed_ms(self) -> float | None:
        if self.started_at is None or self.finished_at is None:
            return None
        return (self.finished_at - self.started_at) * 1000

    @property
    def scheduling_delay_ms(self) -> float | None:
        if self.scheduled_at is None or self.started_at is None:
            return None
        return (self.started_at - self.scheduled_at) * 1000


@dataclass(frozen=True)
class VisibilityProbe:
    body: Mapping[str, object]
    expected: tuple[tuple[str, str, str | None], ...]


@dataclass(frozen=True)
class ImportBatch:
    batch_id: str
    offset_seconds: float
    probes: tuple[VisibilityProbe, ...]
    receive: Callable[[], Awaitable[float]]
    rescan: Callable[[], Awaitable[object]]


@dataclass(frozen=True)
class BatchSample:
    batch_id: str
    offset_seconds: float
    received_at: float | None
    first_full_visibility_at: float | None
    outcome: str
    error: str | None
    probes: tuple[RequestSample, ...]
    expected_count: int
    observed_count: int
    expected: tuple[tuple[str, str, str | None], ...] = ()


@dataclass(frozen=True)
class StartupSample:
    ordinal: int
    process_started_at: float
    first_useful_at: float | None
    pid: int | None
    attempts: tuple[RequestSample, ...]
    outcome: str
    error: str | None
    readiness: Mapping[str, object] | None = None
    provenance: str = "fixture"


@dataclass(frozen=True)
class MemoryObservation:
    baseline_bytes: int | None
    peak_bytes: int | None
    counter_source: str
    coverage: tuple[str, ...]
    phases: tuple[str, ...]
    complete: bool


@dataclass
class WorkloadSamples:
    provenance: str
    identities: Mapping[str, object] = field(default_factory=dict)
    environment: Mapping[str, object] = field(default_factory=dict)
    requests: list[RequestSample] = field(default_factory=list)
    batches: list[BatchSample] = field(default_factory=list)
    rebuild: Mapping[str, object] | None = None
    jobs: list[Mapping[str, object]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    origin_at: float | None = None
    origin_utc: datetime | None = None


def _useful(view: Mapping[str, Any]) -> bool:
    result = view.get("result")
    if view.get("state") != "ready" or not isinstance(result, Mapping):
        return False
    if result.get("decision") not in ("confident", "choice_required"):
        return False
    if result.get("admission", {}).get("outcome") != "accepted":
        return False
    if not result.get("final_candidates"):
        return False
    return any(
        attempt.get("selector", {}).get("name") == "lexical"
        and attempt.get("selector", {}).get("version")
        and attempt.get("mechanism", "").startswith("lexical_")
        and attempt.get("outcome") == "ranked"
        for attempt in result.get("attempts", ())
    )


def _finite_ordered(start: object, finish: object) -> bool:
    return (
        isinstance(start, (int, float))
        and not isinstance(start, bool)
        and isinstance(finish, (int, float))
        and not isinstance(finish, bool)
        and math.isfinite(start)
        and math.isfinite(finish)
        and start <= finish
    )


def _valid_request(sample: RequestSample) -> bool:
    if (
        sample.status_code != 200
        or sample.outcome != "useful"
        or sample.error is not None
        or not _finite_ordered(sample.started_at, sample.finished_at)
        or not isinstance(sample.view, Mapping)
        or not sample.events
    ):
        return False
    try:
        view = validate_view(sample.view)
        if not _useful(view):
            return False
        observed_ms = view["result"]["observed_ms"]
        if not math.isfinite(observed_ms):
            return False
        event_list = [validate_event(event) for event in sample.events]
        if any(
            event["recommendation_id"] != view["recommendation_id"] or event["seq"] != index
            for index, event in enumerate(event_list, 1)
        ):
            return False
        return (
            event_list[0]["event_type"] == "accepted"
            and event_list[-1]["event_type"] == "ready"
            and event_list[-1]["payload"]["result"] == view["result"]
        )
    except Exception:
        return False


def _valid_startup(sample: StartupSample) -> bool:
    if (
        sample.outcome != "useful"
        or sample.error is not None
        or type(sample.pid) is not int
        or sample.pid <= 0
        or not _finite_ordered(sample.process_started_at, sample.first_useful_at)
        or sample.first_useful_at - sample.process_started_at > 1
        or not sample.attempts
    ):
        return False
    try:
        validate_readiness(sample.readiness)
    except Exception:
        return False
    final = sample.attempts[-1]
    return (
        final.category == "startup"
        and _valid_request(final)
        and final.finished_at == sample.first_useful_at
        and all(
            attempt.category == "startup"
            and _finite_ordered(attempt.started_at, attempt.finished_at)
            and sample.process_started_at <= attempt.started_at
            and attempt.finished_at <= sample.first_useful_at
            for attempt in sample.attempts
        )
    )


def _valid_batch(sample: BatchSample) -> bool:
    try:
        expected = set(sample.expected)
    except (TypeError, ValueError):
        return False
    if (
        sample.outcome != "visible"
        or sample.error is not None
        or not _finite_ordered(sample.received_at, sample.first_full_visibility_at)
        or sample.first_full_visibility_at - sample.received_at > 1
        or sample.expected_count != 25
        or sample.observed_count != 25
        or not isinstance(sample.offset_seconds, (int, float))
        or not math.isfinite(sample.offset_seconds)
        or not 0 <= sample.offset_seconds < 60
        or len(sample.expected) != 25
        or len(expected) != 25
        or not sample.probes
    ):
        return False
    seen = set()
    for probe in sample.probes:
        if (
            probe.category != "visibility"
            or not _valid_request(probe)
            or probe.started_at < sample.received_at
            or probe.finished_at > sample.first_full_visibility_at
        ):
            continue
        seen.update(
            (candidate["asset_id"], candidate["content_hash"], candidate["source_version"])
            for candidate in probe.view["result"]["final_candidates"]
        )
    return expected <= seen


def _result(sample: RequestSample) -> Mapping[str, object]:
    if isinstance(sample.view, Mapping):
        result = sample.view.get("result")
        if isinstance(result, Mapping):
            return result
    return {}


def _service_observed_ms(sample: RequestSample) -> float | None:
    if sample.status_code != 200 or not isinstance(sample.view, Mapping):
        return None
    try:
        result = validate_view(sample.view)["result"]
        value = result["observed_ms"] if result is not None else None
        return value if isinstance(value, (int, float)) and math.isfinite(value) else None
    except Exception:
        return None


def _attempts(sample: RequestSample) -> tuple[Mapping[str, object], ...]:
    raw = _result(sample).get("attempts")
    if not isinstance(raw, (tuple, list)):
        return ()
    return tuple(item for item in raw if isinstance(item, Mapping))


def _costs(attempt: Mapping[str, object]) -> tuple[Mapping[str, object], ...]:
    raw = attempt.get("cost_observations")
    if not isinstance(raw, (tuple, list)):
        return ()
    return tuple(item for item in raw if isinstance(item, Mapping))


async def request_sample(
    client: httpx.AsyncClient,
    body: Mapping[str, object],
    *,
    category: str,
    ordinal: int,
    scheduled_at: float | None = None,
    deadline_at: float | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
) -> RequestSample:
    """Time the production response, then collect separately validated evidence."""
    if category not in ("startup", "scheduled", "visibility"):
        raise ValueError("invalid_category")
    started = clock()
    if deadline_at is None:
        deadline_at = started + 1.0
    status = None
    view = None
    events: tuple[Mapping[str, object], ...] = ()
    outcome, error = "failed", None
    finished = None
    collecting_events = False
    try:
        remaining = deadline_at - clock()
        if remaining <= 0:
            return RequestSample(
                category,
                ordinal,
                scheduled_at,
                started,
                clock(),
                None,
                "timeout",
                None,
                (),
                "deadline_exceeded",
            )
        response = await asyncio.wait_for(
            client.post(_ENDPOINT, json=dict(body), headers={"Idempotency-Key": uuid4().hex}),
            timeout=remaining,
        )
        finished = clock()
        status = response.status_code
        if status != 200:
            return RequestSample(
                category,
                ordinal,
                scheduled_at,
                started,
                finished,
                status,
                "http_error",
                None,
                (),
                "http_status",
            )
        view = validate_view(response.json())
        while view["state"] == "computing":
            if clock() >= deadline_at:
                return RequestSample(
                    category,
                    ordinal,
                    scheduled_at,
                    started,
                    clock(),
                    status,
                    "timeout",
                    view,
                    (),
                    "deadline_exceeded",
                )
            await sleep(min(0.01, max(0.0, deadline_at - clock())))
            remaining = deadline_at - clock()
            if remaining <= 0:
                raise TimeoutError
            response = await asyncio.wait_for(
                client.get(f"{_ENDPOINT}/{view['recommendation_id']}"),
                timeout=remaining,
            )
            finished = clock()
            status = response.status_code
            if status != 200:
                return RequestSample(
                    category,
                    ordinal,
                    scheduled_at,
                    started,
                    finished,
                    status,
                    "http_error",
                    view,
                    (),
                    "http_status",
                )
            view = validate_view(response.json())
        outcome = "useful" if _useful(view) else "not_useful"
        if outcome != "useful":
            error = "no_useful_lexical_result"
        collecting_events = True
        after = 0
        pages = []
        while True:
            remaining = deadline_at - clock()
            if remaining <= 0:
                raise TimeoutError
            response = await asyncio.wait_for(
                client.get(
                    f"{_ENDPOINT}/{view['recommendation_id']}/events",
                    params={"after_seq": after, "limit": 1000},
                ),
                timeout=remaining,
            )
            if response.status_code != 200:
                raise ValueError("events_http_status")
            page = response.json()
            if (
                not isinstance(page, dict)
                or set(page)
                != {"schema_version", "recommendation_id", "events", "next_after_seq", "has_more"}
                or page["schema_version"] != 1
                or page["recommendation_id"] != view["recommendation_id"]
                or not isinstance(page["events"], list)
                or type(page["next_after_seq"]) is not int
                or type(page["has_more"]) is not bool
            ):
                raise ValueError("events_shape")
            for event in page["events"]:
                checked = validate_event(event)
                if (
                    checked["recommendation_id"] != view["recommendation_id"]
                    or checked["seq"] != after + 1
                ):
                    raise ValueError("events_sequence")
                pages.append(checked)
                after = checked["seq"]
                events = tuple(pages)
            if page["next_after_seq"] != after or (page["has_more"] and not page["events"]):
                raise ValueError("events_cursor")
            if clock() > deadline_at:
                raise TimeoutError
            if not page["has_more"]:
                break
        events = tuple(pages)
        if (
            not pages
            or pages[0]["event_type"] != "accepted"
            or view["state"] == "ready"
            and (
                pages[-1]["event_type"] != "ready"
                or pages[-1]["payload"]["result"] != view["result"]
            )
        ):
            raise ValueError("events_terminal")
    except TimeoutError:
        outcome, error = (
            ("failed", "evidence_timeout")
            if collecting_events
            else ("timeout", "deadline_exceeded")
        )
        finished = finished if collecting_events and finished is not None else clock()
    except asyncio.CancelledError:
        raise
    except (httpx.HTTPError, ValueError, TypeError, KeyError, IndexError):
        outcome, error = "failed", "invalid_or_unavailable_evidence"
        finished = finished if collecting_events and finished is not None else clock()
    return RequestSample(
        category, ordinal, scheduled_at, started, finished, status, outcome, view, events, error
    )


def _validate_plan(bodies: Sequence[Mapping[str, object]], batches: Sequence[ImportBatch]) -> None:
    if len(bodies) != 120 or len(batches) != 4:
        raise ValueError("invalid_workload_size")
    if len({b.batch_id for b in batches}) != 4:
        raise ValueError("duplicate_batch")
    all_expected = set()
    for batch in batches:
        if not math.isfinite(batch.offset_seconds) or not 0 <= batch.offset_seconds < 60:
            raise ValueError("invalid_batch_offset")
        expected = [identity for probe in batch.probes for identity in probe.expected]
        if any(
            not isinstance(identity, tuple)
            or len(identity) != 3
            or any(not isinstance(part, str) or not part for part in identity[:2])
            or identity[2] is not None
            and (not isinstance(identity[2], str) or not identity[2])
            for identity in expected
        ):
            raise ValueError("invalid_batch_identity")
        if len(set(expected)) != 25 or len(expected) != 25:
            raise ValueError("invalid_batch_coverage")
        all_expected.update(expected)
    if len(all_expected) != 100:
        raise ValueError("duplicate_asset_identity")


def _missed(ordinal: int, scheduled: float, reason: str) -> RequestSample:
    return RequestSample(
        "scheduled", ordinal, scheduled, None, None, None, "missed", None, (), reason
    )


async def run_workload(
    client: httpx.AsyncClient,
    bodies: Sequence[Mapping[str, object]],
    *,
    batches: Sequence[ImportBatch],
    rebuild: Callable[[], Awaitable[object]],
    job_evidence: Callable[[], Sequence[Mapping[str, object]]],
    samples: WorkloadSamples,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
    wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> WorkloadSamples:
    """Run one fixed 60-second schedule with caller-owned source and rebuild hooks."""
    _validate_plan(bodies, batches)
    origin = clock()
    samples.origin_at = origin
    samples.origin_utc = wall_clock()
    slot = asyncio.Lock()
    owned: list[asyncio.Task] = []
    completed_batches: dict[str, BatchSample] = {}

    async def run_rebuild():
        began = clock()
        try:
            remaining = origin + 60 - clock()
            if remaining <= 0:
                raise TimeoutError
            await asyncio.wait_for(rebuild(), timeout=remaining)
            samples.rebuild = {"started_at": began, "finished_at": clock(), "outcome": "returned"}
        except asyncio.CancelledError:
            samples.rebuild = {"started_at": began, "finished_at": clock(), "outcome": "cancelled"}
            samples.errors.append("rebuild_cancelled")
            raise
        except TimeoutError:
            samples.rebuild = {"started_at": began, "finished_at": clock(), "outcome": "timeout"}
            samples.errors.append("rebuild_timeout")
        except Exception:
            samples.rebuild = {"started_at": began, "finished_at": clock(), "outcome": "failed"}
            samples.errors.append("rebuild_failed")

    async def run_batch(batch: ImportBatch):
        received = None
        probe_samples = []
        observed = set()
        expected = {identity for probe in batch.probes for identity in probe.expected}
        outcome, error, visible = "failed", None, None
        try:
            await sleep(max(0.0, origin + batch.offset_seconds - clock()))
            remaining = origin + 60 - clock()
            if remaining <= 0:
                raise TimeoutError
            received = await asyncio.wait_for(batch.receive(), timeout=remaining)
            if (
                not isinstance(received, (int, float))
                or isinstance(received, bool)
                or not math.isfinite(received)
                or received > clock()
            ):
                raise ValueError("invalid_receipt")
            if clock() >= received + 1:
                error = "receipt_deadline"
                return
            remaining = min(origin + 60, received + 1) - clock()
            if remaining <= 0:
                error = "receipt_deadline"
                return
            await asyncio.wait_for(batch.rescan(), timeout=remaining)
            deadline = received + 1
            index = 0
            while clock() < deadline and observed != expected:
                for probe in batch.probes:
                    if clock() >= deadline:
                        break
                    # Do not take a due scheduled slot. The lock also forbids two POSTs.
                    next_slot = origin + min(120, len(samples.requests)) / 2
                    if slot.locked() or clock() >= next_slot:
                        await sleep(min(0.01, max(0.0, deadline - clock())))
                        continue
                    async with slot:
                        item = await request_sample(
                            client,
                            probe.body,
                            category="visibility",
                            ordinal=index,
                            deadline_at=min(deadline, next_slot),
                            clock=clock,
                            sleep=sleep,
                        )
                    probe_samples.append(item)
                    index += 1
                    if item.outcome == "useful" and item.view:
                        for candidate in item.view["result"]["final_candidates"]:
                            identity = (
                                candidate["asset_id"],
                                candidate["content_hash"],
                                candidate["source_version"],
                            )
                            if identity in expected:
                                observed.add(identity)
                if observed != expected:
                    await sleep(min(0.01, max(0.0, deadline - clock())))
            if observed == expected:
                visible = clock()
                outcome = "visible"
            else:
                error = "visibility_deadline"
        except asyncio.CancelledError:
            error = "cancelled"
            raise
        except TimeoutError:
            error = "batch_timeout"
        except Exception:
            error = "batch_failed"
        finally:
            if error:
                samples.errors.append(error)
            completed_batches[batch.batch_id] = BatchSample(
                batch.batch_id,
                batch.offset_seconds,
                received,
                visible,
                outcome,
                error,
                tuple(probe_samples),
                len(expected),
                len(observed),
                tuple(expected),
            )

    try:
        owned.append(asyncio.create_task(run_rebuild()))
        owned.extend(asyncio.create_task(run_batch(batch)) for batch in batches)
        for ordinal, body in enumerate(bodies):
            due = origin + ordinal / 2
            await sleep(max(0.0, due - clock()))
            now = clock()
            if now >= due + 0.5 or slot.locked():
                samples.requests.append(_missed(ordinal, due, "delayed_or_occupied"))
                continue
            async with slot:
                try:
                    item = await request_sample(
                        client,
                        body,
                        category="scheduled",
                        ordinal=ordinal,
                        scheduled_at=due,
                        deadline_at=min(origin + 60, due + 0.5),
                        clock=clock,
                        sleep=sleep,
                    )
                except asyncio.CancelledError:
                    samples.requests.append(
                        RequestSample(
                            "scheduled",
                            ordinal,
                            due,
                            now,
                            clock(),
                            None,
                            "cancelled",
                            None,
                            (),
                            "cancelled",
                        )
                    )
                    raise
            samples.requests.append(item)
        await asyncio.wait(owned, timeout=max(0.0, origin + 60 - clock()))
    except asyncio.CancelledError:
        samples.errors.append("cancelled")
        raise
    finally:
        for task in owned:
            if not task.done():
                task.cancel()
        results = await asyncio.gather(*owned, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                samples.errors.append("owned_task_failed")
        recorded = {s.ordinal for s in samples.requests}
        samples.requests.extend(
            _missed(i, origin + i / 2, "cancelled_or_unreached")
            for i in range(120)
            if i not in recorded
        )
        samples.requests.sort(key=lambda sample: sample.ordinal)
        for batch in batches:
            samples.batches.append(
                completed_batches.get(batch.batch_id)
                or BatchSample(
                    batch.batch_id,
                    batch.offset_seconds,
                    None,
                    None,
                    "failed",
                    "cancelled_or_unreached",
                    (),
                    25,
                    0,
                )
            )
        try:
            for job in job_evidence():
                value = dict(job)
                if "events" in value:
                    value["events"] = tuple(validate_job_event(event) for event in value["events"])
                samples.jobs.append(value)
        except Exception:
            samples.errors.append("job_evidence_unavailable")
    return samples


async def measure_startups(
    launch: Callable[[], AsyncContextManager[tuple[int, httpx.AsyncClient]]],
    body: Mapping[str, object],
    *,
    samples: list[StartupSample],
    clock: Callable[[], float] = time.monotonic,
    provenance: str = "fixture",
) -> None:
    """Measure ten independent process-context entries through first useful POST."""
    for ordinal in range(10):
        began = clock()
        pid = None
        readiness = None
        attempts = []
        useful = None
        error = None
        try:
            async with AsyncExitStack() as cleanup:
                async with asyncio.timeout(max(0.0, began + 1 - clock())):
                    pid, client = await cleanup.enter_async_context(launch())
                    response = await client.get(f"{_ENDPOINT}/readiness")
                    if response.status_code != 200:
                        raise ValueError("readiness_http_status")
                    readiness = validate_readiness(response.json())
                    while clock() < began + 1:
                        attempt = await request_sample(
                            client,
                            body,
                            category="startup",
                            ordinal=len(attempts),
                            deadline_at=began + 1,
                            clock=clock,
                        )
                        attempts.append(attempt)
                        if attempt.outcome == "useful":
                            useful = attempt.finished_at
                            break
                        if attempt.outcome in ("timeout", "http_error", "failed"):
                            break
        except asyncio.CancelledError:
            samples.append(
                StartupSample(
                    ordinal,
                    began,
                    useful,
                    pid,
                    tuple(attempts),
                    "cancelled",
                    "cancelled",
                    readiness,
                    provenance,
                )
            )
            raise
        except TimeoutError:
            error = "deadline_exceeded"
        except Exception:
            error = "startup_failed"
        samples.append(
            StartupSample(
                ordinal,
                began,
                useful,
                pid,
                tuple(attempts),
                "useful"
                if error is None and useful is not None and useful - began <= 1
                else "failed",
                error or (None if useful is not None else "no_useful_lexical_result"),
                readiness,
                provenance,
            )
        )


def _job_interval(job: Mapping[str, object], *, kind: str) -> tuple[datetime, datetime] | None:
    """Require a store outcome backed by its complete, matching event chain."""
    try:
        if job["state"] != "succeeded" or type(job["overlap_observed"]) is not bool:
            return None
        started = datetime.fromisoformat(job["started_at"])
        finished = datetime.fromisoformat(job["finished_at"])
        if started.tzinfo is None or finished.tzinfo is None or started > finished:
            return None
        events = [validate_job_event(event) for event in job["events"]]
        if (
            len(events) < 3
            or [event["event_type"] for event in events[:2]] != ["queued", "started"]
            or events[-1]["event_type"] != "published"
            or any(event["event_type"] != "batch.finished" for event in events[2:-1])
            or any(
                event["job_id"] != job["job_id"] or event["seq"] != i
                for i, event in enumerate(events, 1)
            )
            or events[0]["payload"]["job_kind"] != kind
            or events[0]["payload"]["source_id"] != job["source_id"]
        ):
            return None
        stamps = [datetime.fromisoformat(event["timestamp"]) for event in events]
        if any(stamp.tzinfo is None for stamp in stamps) or stamps != sorted(stamps):
            return None
        if stamps[0] < started or stamps[-1] > finished:
            return None
        if (kind == "refresh") != (job["source_id"] is not None):
            return None
        return started, finished
    except (KeyError, ValueError, TypeError, IndexError):
        return None


def _overlap(a: tuple[datetime, datetime], b: tuple[datetime, datetime]) -> bool:
    return max(a[0], b[0]) <= min(a[1], b[1])


def _contention_observed(workload: WorkloadSamples) -> bool | None:
    """A bare overlap flag is never enough to bind jobs to this run."""
    origin = workload.origin_at
    wall = workload.origin_utc
    rebuild = workload.rebuild
    identities = workload.identities
    rebuild_id = identities.get("rebuild_job_id")
    batch_ids = identities.get("batch_job_ids")
    if (
        workload.provenance != "measured"
        or workload.errors
        or not _finite_ordered(origin, origin)
        or not isinstance(wall, datetime)
        or wall.tzinfo is None
        or not isinstance(rebuild, Mapping)
        or rebuild.get("outcome") != "returned"
        or not _finite_ordered(rebuild.get("started_at"), rebuild.get("finished_at"))
        or not isinstance(rebuild_id, str)
        or not isinstance(batch_ids, Mapping)
        or len(batch_ids) != 4
        or any(not isinstance(value, str) or not value for value in batch_ids.values())
        or len(workload.batches) != 4
    ):
        return None

    def at(value: float) -> datetime:
        return wall + timedelta(seconds=value - origin)

    callback = (at(rebuild["started_at"]), at(rebuild["finished_at"]))
    jobs = {
        job["job_id"]: job
        for job in workload.jobs
        if isinstance(job, Mapping) and isinstance(job.get("job_id"), str)
    }
    derived = jobs.get(rebuild_id)
    if not derived:
        return None
    derived_interval = _job_interval(derived, kind="lexical") or _job_interval(
        derived, kind="embedding"
    )
    run_interval = (wall, at(origin + 60))
    if (
        derived_interval is None
        or not run_interval[0] <= derived_interval[0] <= derived_interval[1] <= run_interval[1]
        or not _overlap(callback, derived_interval)
    ):
        return None
    if len({rebuild_id, *batch_ids.values()}) != 5:
        return None
    for batch in workload.batches:
        if not _valid_batch(batch):
            return None
        source_id = batch_ids.get(batch.batch_id)
        source_job = jobs.get(source_id)
        if not source_job or source_job.get("overlap_observed") is not True:
            return None
        source_interval = _job_interval(source_job, kind="refresh")
        receipt_interval = (at(batch.received_at), at(batch.first_full_visibility_at))
        if (
            source_interval is None
            or not run_interval[0] <= source_interval[0] <= source_interval[1] <= run_interval[1]
            or not _overlap(source_interval, receipt_interval)
            or not _overlap(source_interval, derived_interval)
        ):
            return None
        if not any(
            _valid_request(query)
            and query.category == "scheduled"
            and _finite_ordered(query.started_at, query.finished_at)
            and _overlap((at(query.started_at), at(query.finished_at)), source_interval)
            and _overlap((at(query.started_at), at(query.finished_at)), derived_interval)
            and _overlap((at(query.started_at), at(query.finished_at)), receipt_interval)
            for query in workload.requests
        ):
            return None
    return True


def _scheduled_protocol(workload: WorkloadSamples, scheduled: Sequence[RequestSample]) -> bool:
    origin = workload.origin_at
    if not _finite_ordered(origin, origin) or len(scheduled) != 120:
        return False
    if any(
        type(sample.ordinal) is not int
        or not 0 <= sample.ordinal < 120
        or not _finite_ordered(sample.scheduled_at, sample.scheduled_at)
        or sample.scheduled_at != origin + sample.ordinal / 2
        for sample in scheduled
    ):
        return False
    probes = [probe for batch in workload.batches for probe in batch.probes]
    started = [sample for sample in (*scheduled, *probes) if sample.started_at is not None]
    if any(not _finite_ordered(sample.started_at, sample.finished_at) for sample in started):
        return False
    intervals = sorted((sample.started_at, sample.finished_at) for sample in started)
    return all(
        previous[1] <= current[0]
        for previous, current in zip(intervals, intervals[1:], strict=False)
    )


def summarize_operations(
    startups: Sequence[StartupSample],
    workload: WorkloadSamples,
    memory: MemoryObservation | None,
) -> Mapping[str, object]:
    """Report component evidence only; fixture numbers never qualify AC29."""
    scheduled = [x for x in workload.requests if x.category == "scheduled"]
    unique = {x.ordinal: x for x in scheduled if 0 <= x.ordinal < 120}
    useful = [x for x in unique.values() if _valid_request(x)]
    on_time = [
        x
        for x in useful
        if x.scheduled_at == x.started_at and x.finished_at <= x.scheduled_at + 0.5
    ]
    observed = [value for x in unique.values() if (value := _service_observed_ms(x)) is not None]
    elapsed = [
        x.client_elapsed_ms for x in unique.values() if _finite_ordered(x.started_at, x.finished_at)
    ]
    delayed = sum(
        x.scheduling_delay_ms is not None and x.scheduling_delay_ms > 0 for x in unique.values()
    )
    batch_good = [x for x in workload.batches if _valid_batch(x)]
    batch_identities = {identity for batch in batch_good for identity in batch.expected}
    batch_probe_ids = [
        probe.view["recommendation_id"]
        for batch in batch_good
        for probe in batch.probes
        if _valid_request(probe)
    ]
    startup_good = [x for x in startups if _valid_startup(x)]
    complete_memory = bool(
        memory
        and memory.complete
        and bool(memory.counter_source)
        and _REQUIRED_COVERAGE <= set(memory.coverage)
        and _REQUIRED_PHASES <= set(memory.phases)
        and type(memory.baseline_bytes) is int
        and type(memory.peak_bytes) is int
        and 0 <= memory.baseline_bytes <= memory.peak_bytes
    )
    increment = memory.peak_bytes - memory.baseline_bytes if complete_memory else None
    costs = [
        cost
        for x in unique.values()
        for attempt in _attempts(x)
        for cost in _costs(attempt)
        if cost.get("provenance") in ("observed", "reported")
    ]
    return {
        "provenance": workload.provenance,
        "identities": workload.identities,
        "environment": workload.environment,
        "startup": {
            "denominator": 10,
            "observed": len(startups),
            "useful": len(startup_good),
            "missing": max(0, 10 - len(startups)),
            "elapsed_ms": tuple(
                (x.first_useful_at - x.process_started_at) * 1000
                if _finite_ordered(x.process_started_at, x.first_useful_at)
                else None
                for x in startups
            ),
            "passes": len(startups) == len(startup_good) == 10
            and all(x.provenance == "measured" for x in startups)
            and {x.ordinal for x in startups} == set(range(10))
            and len({x.attempts[-1].view["recommendation_id"] for x in startup_good}) == 10,
        },
        "scheduled": {
            "denominator": 120,
            "observed": len(unique),
            "missing": 120 - len(unique),
            "useful": len(useful),
            "failed": len(unique) - len(useful),
            "delayed": delayed,
            "service_observed_count": len(observed),
            "client_elapsed_count": len(elapsed),
            "client_elapsed_ms": tuple(x.client_elapsed_ms for x in scheduled),
            "scheduling_delay_ms": tuple(x.scheduling_delay_ms for x in scheduled),
            "p95_observed_ms": nearest_rank(observed, 0.95),
            "p95_client_elapsed_ms": nearest_rank(elapsed, 0.95),
            "passes": workload.provenance == "measured"
            and _scheduled_protocol(workload, scheduled)
            and len(scheduled) == len(unique) == len(useful) == len(on_time) == 120
            and len({x.view["recommendation_id"] for x in useful}) == 120
            and nearest_rank(observed, 0.95) <= 300
            and not workload.errors,
        },
        "batches": {
            "denominator": 4,
            "observed": len(workload.batches),
            "visible": len(batch_good),
            "coverage": len(batch_identities),
            "reported_coverage": sum(x.observed_count for x in workload.batches),
            "receipt_to_visibility_ms": tuple(
                (x.first_full_visibility_at - x.received_at) * 1000
                if _finite_ordered(x.received_at, x.first_full_visibility_at)
                else None
                for x in workload.batches
            ),
            "passes": workload.provenance == "measured"
            and len(workload.batches) == len(batch_good) == 4
            and len({x.batch_id for x in workload.batches}) == 4
            and len(batch_identities) == 100
            and len(batch_probe_ids) == len(set(batch_probe_ids)),
        },
        "memory": {
            "incremental_bytes": increment,
            "passes": (
                increment <= 1024**3
                if increment is not None and workload.provenance == "measured"
                else None
            ),
            "counter_source": memory.counter_source if memory else None,
            "coverage": memory.coverage if memory else (),
            "phases": memory.phases if memory else (),
        },
        "cost_observations": {
            "observed": sum(c["provenance"] == "observed" for c in costs),
            "reported": sum(c["provenance"] == "reported" for c in costs),
            "unknown_attempts": sum(
                not attempt.get("cost_observations")
                for x in unique.values()
                for attempt in _attempts(x)
            ),
        },
        "fallbacks": sum(bool(_result(x).get("fallback_reason")) for x in unique.values()),
        "rebuild": workload.rebuild,
        "jobs": tuple(workload.jobs),
        "contention_observed": _contention_observed(workload),
        "errors": tuple(workload.errors),
    }
