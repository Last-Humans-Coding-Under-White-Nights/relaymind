"""Skill selection behind one published interface (RM02, RM28; spec 0.31).

A selector proposes a skill, a clarification, or nothing for a request
from the advertised catalog; the gateway admits it. The package holds
the published request and result shapes and their boundary checks
(``contract``), the chain with its time limit (``chain``), the admission
step (``admission``), the two built-in selectors (``model_router``, the
default, and ``example_match``), and the ``[selection]`` settings
(``settings``). See docs/ARCHITECTURE.md.
"""

from relaymind.selection.admission import Admission, admit
from relaymind.selection.catalog import catalog_entries
from relaymind.selection.chain import (
    EXHAUSTED_MECHANISM,
    Attempt,
    ChainResult,
    InadmissibleError,
    Outcome,
    SelectorChain,
)
from relaymind.selection.contract import (
    SELECTION_REQUEST_SCHEMA,
    SELECTION_RESULT_SCHEMA,
    Candidate,
    CandidateKind,
    RoutingGeneration,
    SelectionContext,
    SelectionError,
    SelectionRequest,
    SelectionResult,
    Selector,
    SelectorError,
    parse_selection_request,
    parse_selection_result,
)
from relaymind.selection.example_match import (
    EXAMPLE_MATCH,
    EXAMPLE_MATCH_VERSION,
    ExampleMatchSelector,
)
from relaymind.selection.model_router import (
    MODEL_ROUTER,
    MODEL_ROUTER_VERSION,
    ModelRouterSelector,
)
from relaymind.selection.settings import BUILT_IN_SELECTORS, SelectionSettings

__all__ = [
    "BUILT_IN_SELECTORS",
    "EXAMPLE_MATCH",
    "EXAMPLE_MATCH_VERSION",
    "EXHAUSTED_MECHANISM",
    "MODEL_ROUTER",
    "MODEL_ROUTER_VERSION",
    "SELECTION_REQUEST_SCHEMA",
    "SELECTION_RESULT_SCHEMA",
    "Admission",
    "Attempt",
    "Candidate",
    "CandidateKind",
    "ChainResult",
    "ExampleMatchSelector",
    "InadmissibleError",
    "ModelRouterSelector",
    "Outcome",
    "RoutingGeneration",
    "SelectionContext",
    "SelectionError",
    "SelectionRequest",
    "SelectionResult",
    "SelectionSettings",
    "Selector",
    "SelectorChain",
    "SelectorError",
    "admit",
    "catalog_entries",
    "parse_selection_request",
    "parse_selection_result",
]
