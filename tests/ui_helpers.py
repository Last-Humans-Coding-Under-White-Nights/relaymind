"""Scenario builders for the demo UI tests (RM23, spec 0.20 section 9).

Every run here comes from the real orchestrator over model doubles and a
disposable sandbox copy (spec section 13). The clock advances on every
generation, so timing values in a trace are not all zero.
"""

import asyncio
from dataclasses import replace
from html.parser import HTMLParser

from gateway_helpers import (
    STAGING_OK,
    build_gateway,
    clarify_turn,
    health_tail,
    route_turn,
    text_turn,
    tool_turn,
)
from helpers import FakeClock
from relaymind.tools import CallerContext
from relaymind.tools.sandbox import MUTATE_PERMISSION, READ_PERMISSION

GENERATION_SECONDS = 0.25
HEALTH_PROMPT = "Is staging-api healthy right now?"
DELETE_PROMPT = "Delete the demo-orders-db sandbox database."
DELETE_ARGS = {"database_id": "demo-orders-db", "expected_revision": 3}
DELETE_ANSWER = "Deleted demo-orders-db."
MUTATING_CALLER = CallerContext(permissions=frozenset({READ_PERMISSION, MUTATE_PERMISSION}))


def external_health_turns():
    """The external model's script for the health task: one tool call, then the answer."""
    return [
        tool_turn("get_service_health", call_id="e1", service="staging-api"),
        text_turn(STAGING_OK),
    ]


def database_ids(gateway):
    return [db["database_id"] for db in gateway.sandbox.list_databases()]


def without_original_messages(accepted):
    """The run.accepted event as a run stored before spec 0.20 wrote it."""
    payload = {k: v for k, v in accepted.payload.items() if k != "original_messages"}
    return replace(accepted, payload=payload)


def timed_gateway(tmp_path, turns, **options):
    """A gateway whose clock moves GENERATION_SECONDS on each model generation."""
    clock = FakeClock()
    options.setdefault("on_generate", lambda request: clock.advance(GENERATION_SECONDS))
    return build_gateway(tmp_path, turns, clock=clock, **options)


def health_gateway(tmp_path, **options):
    """A local health run: route, tool call, answer."""
    options.setdefault("external_turns", [])
    return timed_gateway(tmp_path, [route_turn("check-service-health"), *health_tail()], **options)


def external_health_gateway(tmp_path, **options):
    """The same task for external_only: one tool call, then the answer."""
    gateway = timed_gateway(tmp_path, [], external_turns=external_health_turns(), **options)
    clock = gateway.clock
    gateway.external.on_generate = lambda request: clock.advance(GENERATION_SECONDS)
    return gateway


def delete_gateway(tmp_path, *tail, **options):
    """A delete run that waits for confirmation; tail scripts what follows an approval."""
    options.setdefault("caller", MUTATING_CALLER)
    options.setdefault("external_turns", [])
    turns = [
        route_turn("delete-demo-database"),
        tool_turn("list_demo_databases", call_id="call_list"),
        tool_turn("delete_demo_database", call_id="call_delete", **DELETE_ARGS),
        *tail,
    ]
    return timed_gateway(tmp_path, turns, **options)


def clarifying_gateway(tmp_path, **options):
    """A run that asks which service, because the model asks for clarification."""
    options.setdefault("external_turns", [])
    return timed_gateway(tmp_path, [clarify_turn()], **options)


def run_to_rest(gateway, prompt=HEALTH_PROMPT, **options):
    """Run one request to a terminal or waiting state; return the outcome."""
    return asyncio.run(gateway.orchestrator.run_text(prompt, **options))


class Controls:
    """The elements of a rendered trace that carry a data-action (spec 0.20 section 9)."""

    def __init__(self, fragment: str) -> None:
        found: list[dict[str, str]] = []

        class Collector(HTMLParser):
            def handle_starttag(self, tag, attrs):
                data = {name: value or "" for name, value in attrs}
                if "data-action" in data:
                    found.append({"tag": tag, **data})

        Collector().feed(fragment)
        self.found = found

    def actions(self) -> list[str]:
        return [item["data-action"] for item in self.found]

    def get(self, action: str) -> dict[str, str]:
        (item,) = [item for item in self.found if item["data-action"] == action]
        return item


class SpyFixtures:
    """A fixture service that records resets, in order, with the sandbox it wraps."""

    def __init__(self, sandbox, log: list[str]) -> None:
        self.sandbox = sandbox
        self.log = log

    def reset(self) -> None:
        self.log.append("reset")
        self.sandbox.reset()

    @property
    def label(self) -> str:
        return self.sandbox.label

    @property
    def scenario(self) -> str:
        return self.sandbox.scenario


def spy_on_starts(gateway, log: list[str]) -> None:
    """Record every run start with the state of the previous run at that moment."""
    orchestrator = gateway.orchestrator
    real_start = orchestrator.start
    started: list[str] = []

    def start(messages, **kwargs):
        previous = None if not started else gateway.store.get_run(started[-1]).state
        log.append(f"start:{kwargs.get('execution_mode')}:after={previous}")
        result = real_start(messages, **kwargs)
        started.append(result.run_id)
        return result

    orchestrator.start = start
