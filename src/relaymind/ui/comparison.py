"""The comparison of auto and external_only on reset fixtures (RM23, spec 0.20 section 9).

The same input runs in each mode, one run after the other, because one
run is active at a time (section 11). The fixtures are reset before
each run, so the second run never sees a change the first one made. A
run that comes to rest waiting for input or confirmation is cancelled:
a comparison collects no input and must not change the fixtures after
the reset. Each run is an ordinary run with its own trace.
"""

import asyncio
from collections.abc import Sequence
from typing import Any

from relaymind.adapters import Message
from relaymind.orchestrator.local_loop import LocalOrchestrator
from relaymind.orchestrator.route_decision import ExecutionMode
from relaymind.orchestrator.run_state import TERMINAL_STATES, WAITING_STATES, RunState
from relaymind.ui.settings import UiSettings

__all__ = ["COMPARED_MODES", "run_comparison"]

COMPARED_MODES: tuple[ExecutionMode, ...] = (ExecutionMode.AUTO, ExecutionMode.EXTERNAL_ONLY)


async def run_comparison(
    orchestrator: LocalOrchestrator,
    settings: UiSettings,
    messages: Sequence[Message],
    *,
    cancel_wait_seconds: float,
) -> dict[str, Any]:
    """Run messages in each compared mode against freshly reset fixtures.

    Raises whatever the orchestrator raises for the start of a run, such
    as SlotUnavailableError when another run holds the execution slot.
    """
    fixtures = settings.fixtures
    assert fixtures is not None, "the caller checks that a fixture service is configured"
    arms = []
    for mode in COMPARED_MODES:
        fixtures.reset()
        started = orchestrator.start(messages, execution_mode=mode.value)
        record = await _until_rest(orchestrator, started.run_id, settings, cancel_wait_seconds)
        arms.append(
            {
                "execution_mode": mode.value,
                "run_id": record.run_id,
                "state": record.state,
                "state_version": record.version,
                "fixture": {"label": fixtures.label, "scenario": fixtures.scenario},
            }
        )
    return {"arms": arms}


async def _until_rest(
    orchestrator: LocalOrchestrator, run_id: str, settings: UiSettings, cancel_wait_seconds: float
) -> Any:
    """Wait for the run to end or wait; cancel it when it waits or takes too long."""
    store = orchestrator.store
    loop = asyncio.get_running_loop()
    deadline = loop.time() + settings.arm_wait_seconds
    while True:
        record = store.get_run(run_id)
        state = RunState(record.state)
        if state in TERMINAL_STATES:
            return record
        if state in WAITING_STATES or loop.time() >= deadline:
            await orchestrator.cancel(run_id, record.version, wait_seconds=cancel_wait_seconds)
            return store.get_run(run_id)
        await asyncio.sleep(settings.poll_seconds)
