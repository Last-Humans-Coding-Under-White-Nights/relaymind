"""Model adapter contract (RM18, spec section 6).

Model identity and provider configuration stay independent of
orchestration logic. The orchestrator requests capabilities explicitly
and rejects unsupported combinations.
"""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field, fields
from enum import StrEnum
from types import MappingProxyType


class UnsupportedCapabilityError(Exception):
    """The adapter does not support a capability the caller requires."""


class TokenCountError(Exception):
    """The runtime could not count tokens; no estimate is substituted."""


def _freeze_tuple(obj: object, *field_names: str) -> None:
    """Coerce sequence fields of a frozen dataclass to tuples."""
    for name in field_names:
        value = getattr(obj, name)
        if isinstance(value, str | bytes):
            # tuple("abc") silently decomposes a string into characters.
            raise TypeError(f"{type(obj).__name__}.{name} must be a sequence, not a string")
        object.__setattr__(obj, name, tuple(value))


def _freeze_mapping(obj: object, field_name: str) -> None:
    """Validate and freeze a mapping field of a frozen dataclass."""
    value = getattr(obj, field_name)
    if not isinstance(value, Mapping):
        raise TypeError(
            f"{type(obj).__name__}.{field_name} must be a mapping, got {type(value).__name__}"
        )
    object.__setattr__(obj, field_name, MappingProxyType(dict(value)))


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

    def __post_init__(self) -> None:
        if self.context_limit <= 0:
            raise ValueError("context_limit must be positive")
        _freeze_tuple(self, "reasoning_controls")

    def require(self, *capability_names: str) -> None:
        """Raise unless every named boolean capability is supported.

        Only flag capabilities are accepted here; naming a non-boolean
        field is an error, never a silent pass. Use require_context for
        the context limit and require_reasoning_control for reasoning
        controls.
        """
        unknown = [name for name in capability_names if name not in _FLAG_CAPABILITIES]
        if unknown:
            raise ValueError(f"not boolean capabilities: {', '.join(unknown)}")
        missing = [name for name in capability_names if not getattr(self, name)]
        if missing:
            raise UnsupportedCapabilityError(f"adapter does not support: {', '.join(missing)}")

    def require_reasoning_control(self, control_name: str) -> None:
        """Raise unless the adapter supports the named reasoning control."""
        if control_name not in self.reasoning_controls:
            raise UnsupportedCapabilityError(
                f"adapter does not support reasoning control: {control_name}"
            )

    def require_context(self, tokens: int) -> None:
        """Raise when the input does not fit the adapter's context limit."""
        if tokens < 0:
            raise ValueError("token count must be nonnegative")
        if tokens > self.context_limit:
            raise UnsupportedCapabilityError(
                f"input of {tokens} tokens exceeds context limit {self.context_limit}"
            )


# The boolean capability flags, derived from the record itself so a new
# flag never needs a second edit here. Annotations may be objects or
# strings depending on annotation semantics, so match both.
_FLAG_CAPABILITIES = frozenset(
    f.name for f in fields(AdapterCapabilities) if f.type in (bool, "bool")
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
        if not isinstance(self.call_id, str) or not self.call_id.strip():
            raise ValueError("ToolCall.call_id must be a non-blank string")
        if not isinstance(self.tool_name, str) or not self.tool_name.strip():
            raise ValueError("ToolCall.tool_name must be a non-blank string")
        # A confirmation binds to canonical arguments (RM11): freeze them
        # against in-place mutation after emission. A partial streamed
        # JSON string is not a mapping and is rejected here (RM09).
        _freeze_mapping(self, "arguments")


@dataclass(frozen=True)
class Usage:
    """Token usage for one model call.

    cached_tokens and reasoning_tokens are what the runtime or provider
    reported for this call (spec 0.31 section 6). None means the source
    returned no count: it is absent, never 0, because a default of 0 is
    not a measurement (RM16, finding F9). A reported 0 stays 0.
    """

    input_tokens: int
    output_tokens: int
    cached_tokens: int | None = None
    reasoning_tokens: int | None = None

    def __post_init__(self) -> None:
        if self.input_tokens < 0 or self.output_tokens < 0:
            raise ValueError("token counts must be nonnegative")
        for name in ("cached_tokens", "reasoning_tokens"):
            value = getattr(self, name)
            if value is not None and value < 0:
                raise ValueError("token counts must be nonnegative")


@dataclass(frozen=True)
class FirstToken:
    """The first generated fragment arrived (spec 0.31 section 6, RM25).

    An internal marker, not a model output: it carries no time, because
    the consumer stamps it with its own monotonic clock the moment it
    reads it. A fragment is any non-empty answer text, reasoning text,
    or tool-call piece. A tool call is emitted whole only after the
    finish chunk (RM09), so without this marker the first token of a
    tool-call generation could only be dated at the end of the stream.
    The orchestrator consumes it and never forwards it; an adapter that
    does not send it is dated at its first TextDelta or ToolCall.
    """


@dataclass(frozen=True)
class Completed:
    """The generation finished normally."""

    finish_reason: str


@dataclass(frozen=True)
class GenerationFailed:
    """The generation failed, with a stable error code.

    received is True when the provider sent the failure (an error
    status, an error chunk, or malformed streamed content), so it is a
    transport event (spec 12.1); False when the adapter synthesized it
    without a provider response (timeout, unreachable, no credentials).
    """

    code: str
    message: str
    received: bool = False


AdapterEvent = TextDelta | ToolCall | Usage | FirstToken | Completed | GenerationFailed


class Role(StrEnum):
    """Supported message roles (spec section 7.1)."""

    SYSTEM = "system"
    DEVELOPER = "developer"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass(frozen=True)
class Message:
    """One normalized message.

    A tool message carries the tool_call_id it answers (spec section
    7.1: validate tool-call references). An assistant message may carry
    the tool calls it proposed in an earlier turn, with content None
    when it produced no text.
    """

    role: Role
    content: str | None = None
    tool_call_id: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "role", Role(self.role))
        _freeze_tuple(self, "tool_calls")
        if self.role is Role.TOOL:
            if not self.tool_call_id:
                raise ValueError("a tool message must reference its tool_call_id")
        elif self.tool_call_id is not None:
            raise ValueError("tool_call_id is only valid on tool messages")
        if self.tool_calls and self.role is not Role.ASSISTANT:
            raise ValueError("tool_calls are only valid on assistant messages")
        if self.content is None and not self.tool_calls:
            raise ValueError("a message needs content or tool_calls")


@dataclass(frozen=True)
class ToolSchema:
    """A tool description offered to the model (not an executable handler)."""

    name: str
    description: str
    input_schema: Mapping[str, object]

    def __post_init__(self) -> None:
        _freeze_mapping(self, "input_schema")


@dataclass(frozen=True)
class GenerationOptions:
    """Sampling and budget options for one generation."""

    max_output_tokens: int | None = None
    temperature: float | None = None
    top_p: float | None = None

    def __post_init__(self) -> None:
        if self.max_output_tokens is not None and self.max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be positive")
        if self.temperature is not None and self.temperature < 0:
            raise ValueError("temperature must be nonnegative")
        if self.top_p is not None and not 0 < self.top_p <= 1:
            raise ValueError("top_p must be in (0, 1]")


@dataclass(frozen=True)
class GenerationRequest:
    """Normalized input for one model generation."""

    generation_id: str
    messages: tuple[Message, ...]
    tool_schemas: tuple[ToolSchema, ...] = ()
    options: GenerationOptions = field(default_factory=GenerationOptions)

    def __post_init__(self) -> None:
        _freeze_tuple(self, "messages", "tool_schemas")


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

    locality says where the adapter runs inference: ``local`` for a
    runtime on this host, ``external`` for a remote provider. The trace
    labels every model call with it, so the count of external calls is
    measured from the adapters actually called (RM01, RM16). An adapter
    that does not declare it leaves it None, and the orchestrator
    refuses it rather than guess.
    """

    locality: str | None = None
    # The provider request options the operator declared, sent with every
    # request and recorded in model.started (spec 0.31 section 6). None
    # for an adapter that has none to declare (a local runtime).
    request_options: Mapping[str, object] | None = None

    @abstractmethod
    async def capabilities(self) -> AdapterCapabilities:
        """Return the capability record for this adapter.

        Async because some adapters discover capabilities from their
        runtime (a llama-server /props call, a provider model listing).
        The orchestrator fetches this once at startup and caches the
        record; routing decisions read the cached copy and make no
        network calls (RM01).
        """

    @abstractmethod
    def generate(self, request: GenerationRequest) -> AsyncIterator[AdapterEvent]:
        """Stream normalized events for one generation."""

    @abstractmethod
    async def cancel(self, generation_id: str) -> None:
        """Request cancellation of a running generation (best effort)."""

    @abstractmethod
    async def health(self) -> AdapterHealth:
        """Report provider availability and model warm state."""

    async def count_tokens(
        self,
        messages: Sequence[Message],
        tool_schemas: Sequence[ToolSchema] = (),
    ) -> int:
        """Count the input tokens a generation of these messages would use.

        Only adapters whose capability record reports
        tokenizer_available implement this (spec section 6). The
        default refuses explicitly: an adapter without a local tokenizer
        never estimates a count (RM16).
        """
        raise UnsupportedCapabilityError("adapter does not support: tokenizer")
