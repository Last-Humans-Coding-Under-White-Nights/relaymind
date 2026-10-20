"""Model adapter contract (RM18, spec section 6).

Model identity and provider configuration stay independent of
orchestration logic. The orchestrator requests capabilities explicitly
and rejects unsupported combinations.
"""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field, fields
from enum import StrEnum


class UnsupportedCapabilityError(Exception):
    """The adapter does not support a capability the caller requires."""


_FLAG_CAPABILITIES = frozenset(
    {
        "text_streaming",
        "tool_calling",
        "structured_routing_output",
        "usage_reporting",
        "tokenizer_available",
    }
)


@dataclass(frozen=True)
class AdapterCapabilities:
    """Capability record every adapter exposes (spec section 6)."""

    text_streaming: bool
    tool_calling: bool
    structured_routing_output: bool
    context_limit: int
    reasoning_controls: tuple[str, ...]
    usage_reporting: bool
    tokenizer_available: bool

    def require(self, *capability_names: str) -> None:
        """Raise unless every named boolean capability is supported."""
        known = {f.name for f in fields(self)}
        unknown = [name for name in capability_names if name not in known]
        if unknown:
            raise ValueError(f"unknown capabilities: {', '.join(unknown)}")
        missing = [
            name
            for name in capability_names
            if name in _FLAG_CAPABILITIES and not getattr(self, name)
        ]
        if missing:
            raise UnsupportedCapabilityError(f"adapter does not support: {', '.join(missing)}")

    def require_context(self, tokens: int) -> None:
        """Raise when the input does not fit the adapter's context limit."""
        if tokens > self.context_limit:
            raise UnsupportedCapabilityError(
                f"input of {tokens} tokens exceeds context limit {self.context_limit}"
            )


@dataclass(frozen=True)
class TextDelta:
    """A piece of answer text."""

    text: str


@dataclass(frozen=True)
class ToolCall:
    """A complete, parsed tool call proposal.

    Exists only after complete argument parsing (RM18). A partial
    streamed JSON fragment must never reach the executor (RM09), so
    arguments must already be a parsed mapping.
    """

    call_id: str
    tool_name: str
    arguments: Mapping[str, object]

    def __post_init__(self) -> None:
        if not isinstance(self.arguments, Mapping):
            raise TypeError(
                "ToolCall.arguments must be a fully parsed mapping, "
                f"got {type(self.arguments).__name__}"
            )


@dataclass(frozen=True)
class Usage:
    """Token usage for one model call."""

    input_tokens: int
    output_tokens: int

    def __post_init__(self) -> None:
        if self.input_tokens < 0 or self.output_tokens < 0:
            raise ValueError("token counts must be nonnegative")


@dataclass(frozen=True)
class Completed:
    """The generation finished normally."""

    finish_reason: str


@dataclass(frozen=True)
class GenerationFailed:
    """The generation failed, with a stable error code."""

    code: str
    message: str


AdapterEvent = TextDelta | ToolCall | Usage | Completed | GenerationFailed


class Role(StrEnum):
    """Supported message roles (spec section 7.1)."""

    SYSTEM = "system"
    DEVELOPER = "developer"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass(frozen=True)
class Message:
    """One normalized text message."""

    role: Role
    content: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "role", Role(self.role))


@dataclass(frozen=True)
class ToolSchema:
    """A tool description offered to the model (not an executable handler)."""

    name: str
    description: str
    input_schema: Mapping[str, object]


@dataclass(frozen=True)
class GenerationOptions:
    """Sampling and budget options for the final generation."""

    max_output_tokens: int | None = None
    temperature: float | None = None
    top_p: float | None = None


@dataclass(frozen=True)
class GenerationRequest:
    """Normalized input for one model generation."""

    generation_id: str
    messages: tuple[Message, ...]
    tool_schemas: tuple[ToolSchema, ...] = ()
    options: GenerationOptions = field(default_factory=GenerationOptions)


@dataclass(frozen=True)
class AdapterHealth:
    """Adapter health, with explicit model warm state (spec section 7.4)."""

    available: bool
    warm: bool
    detail: str = ""


class ModelAdapter(ABC):
    """Contract every provider adapter implements (RM18).

    Swapping between two supported adapters requires configuration
    changes and restart, never routing-code changes. Provider-specific
    templates and parsing stay inside the adapter.
    """

    @abstractmethod
    async def capabilities(self) -> AdapterCapabilities:
        """Return the static capability record for this adapter."""

    @abstractmethod
    def generate(self, request: GenerationRequest) -> AsyncIterator[AdapterEvent]:
        """Stream normalized events for one generation."""

    @abstractmethod
    async def cancel(self, generation_id: str) -> None:
        """Request cancellation of a running generation (best effort)."""

    @abstractmethod
    async def health(self) -> AdapterHealth:
        """Report provider availability and model warm state."""
