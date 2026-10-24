"""The [selection] launch table: the selector chain and its limits (RM28, section 14.1).

Spec 0.31 and 0.32 section 14.1. The default chain is model_router alone,
the baseline configuration of section 12 and the behavior before spec
0.31. timeout_seconds is the selection time limit of a selector that is
not a model (section 11). The two example_match thresholds are finite
numbers in [0, 1]; their defaults (0.5 and 0.1) are provisional product
choices, not accuracy claims: the matcher's accuracy is measured in
section 12, never assumed. HTTP items carry a fixed configured identity
and a loopback URL (spec 0.40).
"""

import math
from collections.abc import Mapping
from dataclasses import dataclass

from relaymind.selection.example_match import EXAMPLE_MATCH
from relaymind.selection.http import HttpSelectorConfig
from relaymind.selection.model_router import MODEL_ROUTER

__all__ = ["BUILT_IN_SELECTORS", "SelectionSettings"]

BUILT_IN_SELECTORS = (MODEL_ROUTER, EXAMPLE_MATCH)


def _number(name: str, value: object) -> float:
    # A flag is never a number, even though True == 1.
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    return float(value)


@dataclass(frozen=True)
class SelectionSettings:
    """The selector chain in order, the selection time limit, and the matcher thresholds."""

    chain: tuple[str | HttpSelectorConfig, ...] = (MODEL_ROUTER,)
    timeout_seconds: float = 2.0
    example_match_score: float = 0.5
    example_match_margin: float = 0.1

    def __post_init__(self) -> None:
        chain = self.chain
        if not isinstance(chain, (list, tuple)):
            raise ValueError("chain must be a list of selector names")
        if not chain:
            raise ValueError("chain must name at least one selector")
        normalized = []
        for item in chain:
            if isinstance(item, Mapping):
                if set(item) != {"transport", "name", "version", "url"}:
                    raise ValueError(
                        "selection HTTP item requires only transport, name, version, url"
                    )
                item = HttpSelectorConfig(**item)
            if not isinstance(item, HttpSelectorConfig) and (
                not isinstance(item, str) or item not in BUILT_IN_SELECTORS
            ):
                raise ValueError("chain item must name a built-in selector or configure HTTP")
            normalized.append(item)
        names = [item.name if isinstance(item, HttpSelectorConfig) else item for item in normalized]
        if len(set(names)) != len(names):
            raise ValueError("chain names a selector more than once")
        object.__setattr__(self, "chain", tuple(normalized))
        timeout = _number("timeout_seconds", self.timeout_seconds)
        if not timeout > 0:
            raise ValueError("timeout_seconds must be positive")
        object.__setattr__(self, "timeout_seconds", timeout)
        for name in ("example_match_score", "example_match_margin"):
            value = _number(name, getattr(self, name))
            if not 0 <= value <= 1:
                raise ValueError(f"{name} must be between 0 and 1")
            object.__setattr__(self, name, value)

    def to_json_object(self) -> dict[str, object]:
        """The selection fields of run.accepted (section 8)."""
        return {
            "selection": [i if isinstance(i, str) else i.name for i in self.chain],
            "example_match_score": self.example_match_score,
            "example_match_margin": self.example_match_margin,
        }
