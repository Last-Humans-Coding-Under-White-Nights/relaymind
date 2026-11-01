"""model_router: the local model's routing generation as a selector (RM02, RM28).

Spec 0.31 RM28: the default chain is this selector alone, the baseline.
It wraps ``relaymind.orchestrator.router.Router``: the routing request is
built from the run's own messages (the context), not from the redacted
conversation of the SelectionRequest, so the prompt, its digest, and the
routing cache key are what they were before the interface existed. The
generation runs through the context's callback, which in a gateway run
is the run's own generation path, so the model call is traced, charged,
and cancellable as before; a caller outside a gateway run (the selection
benchmark, #143) passes its own.

The model is trusted only for the skill choice. Its call is read by
``Router.read_call``; invalid routing output is a SelectorError, and the
gateway then escalates with invalid_output when the chain chooses
nothing else (RM04).
"""

from collections.abc import Mapping
from typing import Any

from relaymind.orchestrator.route_decision import RouteDecisionError
from relaymind.orchestrator.router import Router
from relaymind.selection.contract import (
    SCHEMA_VERSION,
    SelectionContext,
    SelectionRequest,
    SelectorError,
)

__all__ = ["MODEL_ROUTER", "MODEL_ROUTER_VERSION", "ROUTING_MECHANISM", "ModelRouterSelector"]

MODEL_ROUTER = "model_router"
MODEL_ROUTER_VERSION = "1"
ROUTING_MECHANISM = "native_function_calling"


class ModelRouterSelector:
    """The routing generation of the local model, behind the selection interface."""

    name = MODEL_ROUTER
    version = MODEL_ROUTER_VERSION
    model_based = True

    def __init__(self, router: Router) -> None:
        self.router = router

    async def select(
        self, request: SelectionRequest, context: SelectionContext
    ) -> Mapping[str, Any]:
        """Run one routing generation and report its choice as a SelectionResult."""
        if context.generate is None:
            raise SelectorError("model_router needs the routing generation callback")
        router = self.router
        generation = await context.generate(
            router.routing_messages(context.messages), router.functions(), router.options()
        )
        try:
            choice = router.read_call(generation.calls)
        except RouteDecisionError as exc:
            raise SelectorError(str(exc)) from exc
        if choice.skill_id is None:
            candidate = {"kind": "clarify", "clarification": choice.question}
        else:
            candidate = {"kind": "skill", "skill_id": choice.skill_id}
        call = {"model": generation.model}
        if generation.call_id is not None:
            call["call_id"] = generation.call_id
        return {
            "schema_version": SCHEMA_VERSION,
            "candidate": candidate,
            "mechanism": ROUTING_MECHANISM,
            "confidence": None,
            "selector": {"name": self.name, "version": self.version},
            "model_calls": [call],
        }
