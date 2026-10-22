"""Trace contract of the routing cache: cache.hit and run.accepted (RM22, RM24; spec 0.26)."""

import copy

import jsonschema
import pytest

from helpers import ACCEPTED_PAYLOAD
from relaymind.store.trace_event import TRACE_EVENT_SCHEMA, TraceEventError, validate_trace_event

HIT = {
    "originating_version": "0.1.1",
    "originating_run_id": "run_1",
    "skill": {"name": "check-service-health", "version": "0.1.1", "content_hash": "sha256:aa"},
    "age_ms": 1200,
    "key": "ab" * 32,
}


def event(event_type, payload, seq=2):
    return {
        "schema_version": 1,
        "run_id": "run_2",
        "seq": seq,
        "timestamp": "2026-10-20T03:00:00Z",
        "elapsed_ms": 5,
        "event_type": event_type,
        "actor": "gateway",
        "payload": payload,
    }


def both_accept(data):
    validate_trace_event(data)
    jsonschema.Draft202012Validator(TRACE_EVENT_SCHEMA).validate(data)


def both_reject(data):
    with pytest.raises(TraceEventError):
        validate_trace_event(data)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(TRACE_EVENT_SCHEMA).validate(data)


def test_the_full_cache_hit_payload_validates():
    """RM22: cache.hit carries the originating version and its provenance."""
    both_accept(event("cache.hit", HIT))
    both_accept(event("cache.hit", {"originating_version": "v1"}))


@pytest.mark.parametrize(
    "change",
    [
        {"originating_run_id": " "},
        {"age_ms": -1},
        {"age_ms": 1.5},
        {"key": ""},
        {"skill": {"name": "x"}},
        {"skill": "x"},
    ],
)
def test_a_malformed_cache_hit_payload_is_refused(change):
    both_reject(event("cache.hit", {**copy.deepcopy(HIT), **change}))


def test_run_accepted_may_label_the_routing_cache():
    """RM24: cache-on runs are labeled in run.accepted."""
    for label in (True, False):
        both_accept(event("run.accepted", {**ACCEPTED_PAYLOAD, "routing_cache": label}, seq=1))


@pytest.mark.parametrize("label", ["yes", 1, None])
def test_the_routing_cache_label_must_be_a_boolean(label):
    both_reject(event("run.accepted", {**ACCEPTED_PAYLOAD, "routing_cache": label}, seq=1))


def test_exported_statistics_name_the_routing_cache_setting(tmp_path):
    """RM24, RM25: cache-on figures are labeled and never pass as the baseline."""
    import json

    from relaymind.stats import AdapterIdentity, export_stats

    identity = AdapterIdentity(adapter="llamacpp", model_id="m")
    off, on = tmp_path / "off.json", tmp_path / "on.json"
    export_stats(off, identity=identity, records=[])
    export_stats(on, identity=identity, records=[], routing_cache=True)
    assert json.loads(off.read_text("utf-8"))["routing_cache"] == "off"
    assert json.loads(on.read_text("utf-8"))["routing_cache"] == "on"
