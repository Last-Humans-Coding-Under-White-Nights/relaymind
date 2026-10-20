"""Model adapters (RM18). See AGENTS.md and docs/ARCHITECTURE.md."""

from relaymind.adapters.base import (
    AdapterCapabilities,
    AdapterEvent,
    AdapterHealth,
    Completed,
    GenerationFailed,
    GenerationOptions,
    GenerationRequest,
    Message,
    ModelAdapter,
    Role,
    TextDelta,
    ToolCall,
    ToolSchema,
    UnsupportedCapabilityError,
    Usage,
)

__all__ = [
    "AdapterCapabilities",
    "AdapterEvent",
    "AdapterHealth",
    "Completed",
    "GenerationFailed",
    "GenerationOptions",
    "GenerationRequest",
    "Message",
    "ModelAdapter",
    "Role",
    "TextDelta",
    "ToolCall",
    "ToolSchema",
    "UnsupportedCapabilityError",
    "Usage",
]
