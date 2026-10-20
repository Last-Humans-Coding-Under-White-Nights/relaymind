"""Shared OpenAI-compatible chat-completions wire protocol.

The local llama.cpp adapter and the external Chat Completions adapter
speak the same streamed chat-completions protocol. Message encoding,
SSE parsing, and stream accumulation live here; endpoint-specific
request shaping and lifecycle stay in each adapter.
"""

import asyncio
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from relaymind.adapters.base import (
    AdapterEvent,
    Completed,
    GenerationFailed,
    Message,
    Role,
    TextDelta,
    ToolCall,
    ToolSchema,
    Usage,
)


def encode_message(message: Message) -> dict[str, object]:
    """Encode one normalized message in OpenAI chat format.

    Tool turns carry their references: a tool message names the call it
    answers, an assistant message carries the calls it proposed. The
    developer role maps to system: standard chat templates drop roles
    they do not know.
    """
    role = "system" if message.role is Role.DEVELOPER else message.role.value
    encoded: dict[str, object] = {"role": role, "content": message.content}
    if message.tool_call_id is not None:
        encoded["tool_call_id"] = message.tool_call_id
    if message.tool_calls:
        encoded["tool_calls"] = [
            {
                "id": call.call_id,
                "type": "function",
                "function": {
                    "name": call.tool_name,
                    "arguments": json.dumps(dict(call.arguments)),
                },
            }
            for call in message.tool_calls
        ]
    return encoded


def encode_tools(tool_schemas: Sequence[ToolSchema]) -> list[dict[str, object]]:
    """Encode tool schemas as OpenAI function tools."""
    return [
        {
            "type": "function",
            "function": {
                "name": schema.name,
                "description": schema.description,
                # input_schema is a MappingProxyType; the wire body
                # must be plain-JSON serializable.
                "parameters": dict(schema.input_schema),
            },
        }
        for schema in tool_schemas
    ]


@dataclass
class _PartialToolCall:
    call_id: str = ""
    tool_name: str = ""
    arguments_json: str = ""


@dataclass
class StreamAccumulator:
    """Turn streamed chat-completion chunks into normalized events.

    Tool-call argument fragments are buffered; a ToolCall event exists
    only after the stream finishes and the full argument JSON parses
    (RM09: partial streamed argument JSON must never execute).
    """

    _tool_calls: dict[int, _PartialToolCall] = field(default_factory=dict)
    # True once the stream outcome is settled (finish, failure, or
    # provider error chunk); the end-of-stream flush then adds nothing.
    _closed: bool = False

    def feed(self, chunk: Mapping[str, object]) -> list[AdapterEvent]:
        error = chunk.get("error")
        if error is not None:
            # Providers stream runtime errors (context exhausted, busy
            # slots) as an error chunk without choices or usage.
            self._closed = True
            self._tool_calls.clear()
            message = error.get("message") if isinstance(error, Mapping) else None
            return [GenerationFailed(code="provider_error", message=str(message or error))]
        events: list[AdapterEvent] = []
        usage = chunk.get("usage")
        if isinstance(usage, Mapping):
            events.append(
                Usage(
                    input_tokens=int(usage.get("prompt_tokens") or 0),
                    output_tokens=int(usage.get("completion_tokens") or 0),
                )
            )
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            content = delta.get("content")
            if content:
                events.append(TextDelta(text=content))
            for fragment in delta.get("tool_calls") or []:
                self._absorb_tool_fragment(fragment)
            finish_reason = choice.get("finish_reason")
            if finish_reason:
                events.extend(self._finish(finish_reason))
        return events

    def flush(self) -> list[AdapterEvent]:
        """Settle a stream that ended without an explicit finish chunk.

        [DONE] or EOF can arrive before any finish_reason; buffered tool
        calls and the Completed event must still come out.
        """
        if self._closed:
            return []
        return self._finish("end_of_stream")

    def _absorb_tool_fragment(self, fragment: Mapping[str, object]) -> None:
        partial = self._tool_calls.setdefault(int(fragment.get("index") or 0), _PartialToolCall())
        if fragment.get("id"):
            partial.call_id = str(fragment["id"])
        function = fragment.get("function") or {}
        if function.get("name"):
            # Names can be streamed in fragments, like arguments.
            partial.tool_name += str(function["name"])
        partial.arguments_json += str(function.get("arguments") or "")

    def _finish(self, finish_reason: str) -> list[AdapterEvent]:
        try:
            events: list[AdapterEvent] = []
            for index in sorted(self._tool_calls):
                partial = self._tool_calls[index]
                try:
                    # A tool without parameters may stream no argument
                    # fragments at all; that is an empty object, not an
                    # error.
                    raw_arguments = partial.arguments_json.strip()
                    arguments = json.loads(raw_arguments) if raw_arguments else {}
                    if not isinstance(arguments, dict):
                        raise ValueError("arguments are not a JSON object")
                    events.append(
                        ToolCall(
                            # A provider may omit the id; synthesize one
                            # rather than emit an invalid call.
                            call_id=partial.call_id or f"call_{index}",
                            tool_name=partial.tool_name,
                            arguments=arguments,
                        )
                    )
                except ValueError as exc:
                    return [
                        GenerationFailed(
                            code="invalid_tool_arguments",
                            message=(
                                f"tool call {partial.call_id or index} for "
                                f"{partial.tool_name or 'unknown tool'}: {exc}"
                            ),
                        )
                    ]
            events.append(Completed(finish_reason=finish_reason))
            return events
        finally:
            self._tool_calls.clear()
            self._closed = True


def parse_sse_line(line: str) -> Mapping[str, object] | None:
    """Decode one server-sent-events line into a chunk, if it is one."""
    line = line.strip()
    if not line.startswith("data:"):
        return None
    payload = line[len("data:") :].strip()
    if payload == "[DONE]":
        return None
    try:
        chunk = json.loads(payload)
    except ValueError:
        return None
    return chunk if isinstance(chunk, Mapping) else None


async def next_line_or_cancel(lines, cancel_event: asyncio.Event) -> str | None:
    """The next stream line, or None on end of stream or cancellation."""
    next_line = asyncio.ensure_future(anext(lines))
    cancelled = asyncio.ensure_future(cancel_event.wait())
    try:
        done, _pending = await asyncio.wait(
            {next_line, cancelled}, return_when=asyncio.FIRST_COMPLETED
        )
        if cancel_event.is_set():
            return None
        try:
            return next_line.result()
        except StopAsyncIteration:
            return None
    finally:
        for task in (next_line, cancelled):
            task.cancel()
        await asyncio.gather(next_line, cancelled, return_exceptions=True)
