"""Operator settings of the demo UI (RM23, spec 0.20 section 9).

The data mode has no default. The UI is never served without a declared
mode, and the mode is shown in every view, so a recorded replay or a
deterministic double can never be taken for a live model. The gateway
cannot tell a double from a live adapter; the label is the operator's
declaration, and each trace also names the models its own events
recorded.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

__all__ = ["MODE_LABELS", "DataMode", "FixtureService", "UiSettings"]


class DataMode(StrEnum):
    """Where the decisions on screen come from (RM23)."""

    LIVE = "live"
    REPLAY = "replay"
    DOUBLE = "double"


# The visible label of each mode: a short word and a plain sentence.
MODE_LABELS: dict[DataMode, tuple[str, str]] = {
    DataMode.LIVE: ("LIVE", "Model decisions come from the configured live models."),
    DataMode.DOUBLE: ("DOUBLE", "Scripted deterministic model doubles. This is not a live model."),
    DataMode.REPLAY: ("REPLAY", "Recorded runs. No model is called to show them."),
}


class FixtureService(Protocol):
    """A fixture service the comparison can reset (the sandbox data service fits)."""

    @property
    def label(self) -> str: ...

    @property
    def scenario(self) -> str: ...

    def reset(self) -> None: ...


@dataclass(frozen=True)
class UiSettings:
    """What the operator declares when the UI is enabled.

    mode is required. fixtures is the sandbox the comparison resets
    before each run (without it the comparison is unavailable).
    arm_wait_seconds bounds how long a comparison waits for one run to
    come to rest; poll_seconds is how often it looks.
    """

    mode: DataMode
    fixtures: FixtureService | None = None
    arm_wait_seconds: float = 120.0
    poll_seconds: float = 0.02

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "mode", DataMode(self.mode))
        except ValueError:
            allowed = ", ".join(m.value for m in DataMode)
            raise ValueError(f"mode must be one of: {allowed}") from None
        if not self.arm_wait_seconds > 0 or not self.poll_seconds > 0:
            raise ValueError("arm_wait_seconds and poll_seconds must be positive")

    @property
    def interactive(self) -> bool:
        """Whether the UI may create, resume, or cancel runs (not in replay mode)."""
        return self.mode is not DataMode.REPLAY
