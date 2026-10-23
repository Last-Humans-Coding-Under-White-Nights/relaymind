"""The performance options and the draft answer guards (RM31, RM32, spec 0.32 section 14.1).

``PerformanceSettings`` holds the launch-file ``[performance]`` table.
Every default is the baseline configuration of section 12 and the
behavior before spec 0.31, so a gateway that sets nothing runs exactly
as it did. The orchestrator records the options in force in each run's
``run.accepted`` event (section 8), so a measurement names its
configuration.

An option that the gateway cannot honor is refused, never accepted and
ignored. ``answer_stream = "live"`` is a spec value (RM32), but gated
answer streaming is not implemented yet, so it stops startup. The
options ``calls_per_generation`` and ``prefetch`` are stored and
recorded here; the orchestrator acts on them in a later change (RM29,
section 4.3), and until then they change nothing but the record.

The guards at the end decide whether a validated draft may be published
as the answer without a second generation (RM31).
"""

import re
from dataclasses import dataclass

from relaymind.orchestrator.identifiers import has_cjk
from relaymind.orchestrator.route_decision import Language

__all__ = [
    "ANSWER_PASSES",
    "ANSWER_STREAMS",
    "CALLS_PER_GENERATION",
    "PerformanceSettings",
    "draft_in_language",
    "holds_tool_call_json",
]

ANSWER_PASSES = ("final", "draft")
CALLS_PER_GENERATION = ("one", "many")
# Every value the spec names, and the ones the gateway implements.
ANSWER_STREAMS = ("buffered", "live")
_IMPLEMENTED_STREAMS = ("buffered",)


def _choice(name: str, value: object, allowed: tuple[str, ...]) -> None:
    # A bool or a number is never a choice, even when it compares equal to text.
    if not isinstance(value, str) or value not in allowed:
        raise ValueError(f"{name} must be one of: {', '.join(allowed)}")


def _flag(name: str, value: object) -> None:
    if type(value) is not bool:
        raise ValueError(f"{name} must be true or false")


@dataclass(frozen=True)
class PerformanceSettings:
    """The performance options (section 14.1). The defaults are the baseline.

    answer_pass: final (a second generation with tools disabled) or
    draft (publish the validated skill-stage draft when the RM31 guards
    pass). calls_per_generation: one, or many (the admitted-calls rule
    of section 4.3). prefetch: run the read-only calls a skill declares
    before its first generation (RM29). answer_stream: buffered; live
    (RM32) is refused until it exists. early_headers: send the status
    line, the run ID, and the events URL of a streamed chat request as
    soon as the run is accepted (section 7.3).
    """

    answer_pass: str = "final"
    calls_per_generation: str = "one"
    prefetch: bool = False
    answer_stream: str = "buffered"
    early_headers: bool = False

    def __post_init__(self) -> None:
        _choice("answer_pass", self.answer_pass, ANSWER_PASSES)
        _choice("calls_per_generation", self.calls_per_generation, CALLS_PER_GENERATION)
        _flag("prefetch", self.prefetch)
        _choice("answer_stream", self.answer_stream, ANSWER_STREAMS)
        if self.answer_stream not in _IMPLEMENTED_STREAMS:
            raise ValueError(
                f"answer_stream {self.answer_stream} is not implemented yet"
                " (gated answer streaming, RM32); use buffered"
            )
        _flag("early_headers", self.early_headers)

    def to_json_object(self) -> dict[str, str | bool]:
        """The options in force, as run.accepted records them (section 8)."""
        return {
            "answer_pass": self.answer_pass,
            "answer_stream": self.answer_stream,
            "calls_per_generation": self.calls_per_generation,
            "prefetch": self.prefetch,
            "early_headers": self.early_headers,
        }


# -- the draft guards (RM31) ---------------------------------------------------------

_TOOL_CALL_TAG = re.compile(r"</?tool_call>", re.IGNORECASE)
_TOOL_NAME_KEY = re.compile(r'"(?:tool_name|name|function)"\s*:')
_TOOL_ARGUMENTS_KEY = re.compile(r'"(?:arguments|parameters)"\s*:')


def holds_tool_call_json(text: str) -> bool:
    """A tool-call tag, or JSON that names a tool and its arguments (RM31 guard).

    A model that writes its call as text instead of a native call must
    not have that text published as the answer.
    """
    return bool(
        _TOOL_CALL_TAG.search(text)
        or (_TOOL_NAME_KEY.search(text) and _TOOL_ARGUMENTS_KEY.search(text))
    )


def draft_in_language(text: str, language: Language | str) -> bool:
    """Whether a draft is in the run's language (RM31 guard).

    The rule is the one the router reads a message by
    (``identifiers.has_cjk``): a Chinese run needs Chinese characters in
    the draft, and an English run needs none. A Chinese run with an
    English draft, or an English run with a Chinese one, takes the
    final pass, which carries the language instruction. The language
    compares by value, so a plain "zh" is the Chinese run; a value
    outside spec section 1 raises ValueError.
    """
    return has_cjk(text) == (Language(language) == Language.ZH)
