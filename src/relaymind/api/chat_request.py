"""Validation of a chat completions request body (RM19, AC16, spec 0.14 section 7.1).

``parse_chat_request`` turns a decoded JSON body into a ``ChatRequest``
or raises an ``ApiError``: ``invalid_request`` for a malformed value or
shape, ``unsupported_parameter`` for a field, option, or content part
outside the supported subset. The error message names the field, for
example ``messages[2].content[0].type``. Nothing is dropped silently: a
field is either validated and used, accepted because it is null or its
API default, or rejected by name.

This module knows no web framework and no orchestrator. Ownership of
tools (section 7.2) is checked here as request shape only: the managed
alias refuses client tools, and the client-owned alias checks that
every tool result answers a call. Executing anything is not its job.
"""

import json
import math
import re
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from typing import Any

from relaymind.adapters import GenerationOptions, Message, Role, ToolCall
from relaymind.api.errors import ApiError

__all__ = [
    "CLIENT_TOOLS",
    "MANAGED",
    "SAMPLING_OPTIONS",
    "ChatRequest",
    "ClientTool",
    "ToolChoice",
    "parse_chat_request",
]

MANAGED = "relaymind"
CLIENT_TOOLS = "relaymind-client-tools"
# The options of GenerationOptions that a request can set (section 7.1).
SAMPLING_OPTIONS = frozenset({"max_output_tokens", "temperature", "top_p"})

_SUPPORTED_FIELDS = frozenset(
    {
        "model",
        "messages",
        "stream",
        "n",
        "max_completion_tokens",
        "max_tokens",
        "temperature",
        "top_p",
        "tools",
        "tool_choice",
        "parallel_tool_calls",
        "response_format",
        "user",
    }
)
# Fields outside the table that are accepted at their API default.
_DEFAULTS: dict[str, object] = {
    "presence_penalty": 0,
    "frequency_penalty": 0,
    "logprobs": False,
    "store": False,
}
# The sampling options: their API default, their range check, and its message.
_SAMPLING: tuple[tuple[str, int, Callable[[float], bool], str], ...] = (
    ("temperature", 1, lambda v: 0 <= v <= 2, "temperature must be a number from 0 to 2"),
    ("top_p", 1, lambda v: 0 < v <= 1, "top_p must be a number above 0 and at most 1"),
)
_INSTRUCTION_ROLES = (Role.SYSTEM, Role.DEVELOPER)
_TOOL_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")
_MESSAGE_FIELDS = {
    Role.SYSTEM: frozenset({"role", "content"}),
    Role.DEVELOPER: frozenset({"role", "content"}),
    Role.USER: frozenset({"role", "content"}),
    Role.ASSISTANT: frozenset({"role", "content", "tool_calls"}),
    Role.TOOL: frozenset({"role", "content", "tool_call_id"}),
}


@dataclass(frozen=True)
class ClientTool:
    """A function the caller offers and will execute itself (RM08, section 7.2)."""

    name: str
    description: str | None
    parameters: Mapping[str, Any]


@dataclass(frozen=True)
class ToolChoice:
    """How the client-owned alias may use the offered tools.

    mode is auto, none, required, or function; name is set only for function.
    """

    mode: str
    name: str | None = None


@dataclass(frozen=True)
class ChatRequest:
    """A validated request. final_options apply to the answer generation (section 7.1)."""

    alias: str
    messages: tuple[Message, ...]
    stream: bool
    final_options: GenerationOptions
    tools: tuple[ClientTool, ...] = ()
    tool_choice: ToolChoice | None = None


def _invalid(message: str) -> ApiError:
    return ApiError("invalid_request", message)


def _unsupported(message: str) -> ApiError:
    return ApiError("unsupported_parameter", message)


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _is_default(value: object, default: object) -> bool:
    if isinstance(default, bool):
        return value is default
    return _is_number(value) and value == default


def parse_chat_request(
    body: object,
    *,
    served: Collection[str] = (MANAGED, CLIENT_TOOLS),
    supported_options: Collection[str] = SAMPLING_OPTIONS,
) -> ChatRequest:
    """Validate a decoded request body (spec 0.14 section 7.1).

    served is the set of aliases this gateway runs; supported_options
    is the operator's declaration of which sampling and budget options
    the configured adapters accept.
    """
    if not isinstance(body, dict):
        raise _invalid("the request body must be a JSON object")
    fields = {name: value for name, value in body.items() if value is not None}
    alias = fields.get("model")
    if not isinstance(alias, str):
        raise _invalid("model is required and must be a string")
    if alias not in served:
        raise _invalid(f"model {alias!r} is not served; use one of: {', '.join(sorted(served))}")
    for name, value in fields.items():
        if name in _SUPPORTED_FIELDS:
            continue
        if name in _DEFAULTS and _is_default(value, _DEFAULTS[name]):
            continue
        raise _unsupported(f"{name} is not supported by this gateway")

    stream = fields.get("stream", False)
    if not isinstance(stream, bool):
        raise _invalid("stream must be a boolean")
    _check_n(fields.get("n", 1))
    if "user" in fields and not isinstance(fields["user"], str):
        raise _invalid("user must be a string")
    _check_response_format(fields.get("response_format", {"type": "text"}))
    options = _final_options(fields, set(supported_options))
    messages = _messages(fields.get("messages"), alias)
    tools = _tools(fields.get("tools"), alias)
    choice = _tool_choice(fields.get("tool_choice"), tools, alias)
    _parallel_tool_calls(fields.get("parallel_tool_calls", False))
    return ChatRequest(
        alias=alias,
        messages=messages,
        stream=stream,
        final_options=options,
        tools=tools,
        tool_choice=choice,
    )


def _check_n(n: object) -> None:
    if not isinstance(n, int) or isinstance(n, bool):
        raise _invalid("n must be an integer")
    if n != 1:
        raise _unsupported("n is not supported; only n=1 is")


def _check_response_format(value: object) -> None:
    if not isinstance(value, dict) or not isinstance(value.get("type"), str):
        raise _invalid("response_format must be an object with a string type")
    if value["type"] != "text":
        raise _unsupported(f"response_format type {value['type']!r} is not supported; only text is")


def _final_options(fields: Mapping[str, Any], supported: set[str]) -> GenerationOptions:
    budget_names = [n for n in ("max_completion_tokens", "max_tokens") if n in fields]
    if len(budget_names) == 2:
        raise _invalid("max_tokens cannot be sent together with max_completion_tokens")
    values: dict[str, Any] = {}
    for name in budget_names:
        value = fields[name]
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise _invalid(f"{name} must be a positive integer")
        _require_option("max_output_tokens", name, supported)
        values["max_output_tokens"] = value
    for name, default, in_range, message in _SAMPLING:
        value = fields.get(name)
        if value is None:
            continue
        if not _is_number(value) or not in_range(value):
            raise _invalid(message)
        # The API default of an option the adapters do not accept is still accepted.
        if name in supported or value != default:
            _require_option(name, name, supported)
            values[name] = value
    return GenerationOptions(**values)


def _require_option(option: str, field: str, supported: set[str]) -> None:
    if option not in supported:
        raise _unsupported(f"{field} is not accepted by the configured model adapters")


# -- messages -------------------------------------------------------------------


def _messages(raw: object, alias: str) -> tuple[Message, ...]:
    if not isinstance(raw, list) or not raw:
        raise _invalid("messages must be a nonempty array")
    messages = tuple(_message(index, item, alias) for index, item in enumerate(raw))
    _check_order(messages, alias)
    return messages


def _message(index: int, item: object, alias: str) -> Message:
    where = f"messages[{index}]"
    if not isinstance(item, dict):
        raise _invalid(f"{where} must be an object")
    try:
        role = Role(item.get("role"))
    except ValueError:
        raise _invalid(f"{where}.role must be one of: {', '.join(r.value for r in Role)}") from None
    for name, value in item.items():
        if name not in _MESSAGE_FIELDS[role] and value is not None:
            raise _unsupported(f"{where}.{name} is not supported")
    calls = item.get("tool_calls") or []
    if alias == MANAGED and (calls or role is Role.TOOL):
        raise _invalid(
            f"{where}: client tool calls and results are not accepted with the {MANAGED} alias"
        )
    if role is Role.TOOL:
        call_id = item.get("tool_call_id")
        if not isinstance(call_id, str) or not call_id.strip():
            raise _invalid(f"{where}.tool_call_id is required for a tool message")
        return Message(
            role=role,
            content=_content(where, item.get("content"), blank=True),
            tool_call_id=call_id,
        )
    if role is Role.ASSISTANT and calls:
        text = _content(where, item.get("content"), blank=True, optional=True)
        return Message(role=role, content=text or None, tool_calls=_tool_calls(where, calls))
    return Message(role=role, content=_content(where, item.get("content"), blank=False))


def _content(where: str, value: object, *, blank: bool, optional: bool = False) -> str | None:
    """The text of a message: a string or an array of text parts, joined."""
    if value is None and optional:
        return None
    if isinstance(value, str):
        text = value
    elif isinstance(value, list):
        pieces = []
        for part_index, part in enumerate(value):
            part_where = f"{where}.content[{part_index}]"
            if not isinstance(part, dict):
                raise _invalid(f"{part_where} must be an object")
            if part.get("type") != "text":
                raise _unsupported(f"{part_where}.type {part.get('type')!r} is not supported")
            if not isinstance(part.get("text"), str):
                raise _invalid(f"{part_where}.text must be a string")
            pieces.append(part["text"])
        text = "".join(pieces)
    else:
        raise _invalid(f"{where}.content must be a string or an array of text parts")
    if not blank and not text.strip():
        raise _invalid(f"{where}.content must not be blank")
    return text


def _tool_calls(where: str, raw: object) -> tuple[ToolCall, ...]:
    if not isinstance(raw, list):
        raise _invalid(f"{where}.tool_calls must be an array")
    calls = []
    for index, item in enumerate(raw):
        here = f"{where}.tool_calls[{index}]"
        function = item.get("function") if isinstance(item, dict) else None
        if (
            not isinstance(item, dict)
            or item.get("type") != "function"
            or not isinstance(function, dict)
            or not isinstance(item.get("id"), str)
            or not item["id"].strip()
            or not isinstance(function.get("name"), str)
            or not function["name"].strip()
        ):
            raise _invalid(f"{here} must be a function call with an id and a name")
        try:
            arguments = json.loads(function.get("arguments"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            arguments = None
        if not isinstance(arguments, dict):
            raise _invalid(f"{here}.function.arguments must be a string holding a JSON object")
        calls.append(ToolCall(call_id=item["id"], tool_name=function["name"], arguments=arguments))
    return tuple(calls)


def _check_order(messages: tuple[Message, ...], alias: str) -> None:
    """Section 7.1 ordering and tool-call references."""
    position = 0
    while position < len(messages) and messages[position].role in _INSTRUCTION_ROLES:
        position += 1
    for later in range(position, len(messages)):
        if messages[later].role in _INSTRUCTION_ROLES:
            raise _invalid(
                f"messages[{later}]: system and developer messages must come before the others"
            )
    if position == len(messages) or messages[position].role is not Role.USER:
        where = "messages" if position == len(messages) else f"messages[{position}]"
        raise _invalid(f"{where}: the conversation must start with a user message")
    seen_ids: set[str] = set()
    unanswered: set[str] = set()
    owner = -1
    for index, message in enumerate(messages):
        if unanswered and message.role is not Role.TOOL:
            raise _invalid(
                f"messages[{index}]: tool calls of messages[{owner}] are not answered yet"
            )
        if message.role is Role.TOOL:
            assert message.tool_call_id is not None
            if message.tool_call_id not in unanswered:
                raise _invalid(
                    f"messages[{index}].tool_call_id {message.tool_call_id!r} does not "
                    "answer an open tool call"
                )
            unanswered.discard(message.tool_call_id)
        for call_index, call in enumerate(message.tool_calls):
            if call.call_id in seen_ids:
                raise _invalid(
                    f"messages[{index}].tool_calls[{call_index}].id {call.call_id!r} "
                    "was used before"
                )
            seen_ids.add(call.call_id)
            unanswered.add(call.call_id)
            owner = index
    last = len(messages) - 1
    if unanswered:
        raise _invalid(f"messages[{owner}]: its tool calls are not answered")
    allowed = (Role.USER,) if alias == MANAGED else (Role.USER, Role.TOOL)
    if messages[last].role not in allowed:
        raise _invalid(
            f"messages[{last}]: the last message must have role "
            f"{' or '.join(r.value for r in allowed)}"
        )


# -- tools ----------------------------------------------------------------------


def _tools(raw: object, alias: str) -> tuple[ClientTool, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise _invalid("tools must be an array")
    if not raw:
        return ()
    if alias == MANAGED:
        raise _invalid(f"tools are not accepted with the {MANAGED} alias; it runs managed tools")
    tools: list[ClientTool] = []
    for index, item in enumerate(raw):
        where = f"tools[{index}]"
        if not isinstance(item, dict):
            raise _invalid(f"{where} must be an object")
        if item.get("type") != "function":
            raise _unsupported(f"{where}.type {item.get('type')!r} is not supported")
        function = item.get("function")
        if not isinstance(function, dict):
            raise _invalid(f"{where}.function must be an object")
        name = function.get("name")
        if not isinstance(name, str) or not _TOOL_NAME.fullmatch(name):
            raise _invalid(
                f"{where}.function.name must be 1 to 64 letters, digits, underscores, or hyphens"
            )
        if any(tool.name == name for tool in tools):
            raise _invalid(f"{where}.function.name {name!r} is used twice")
        description = function.get("description")
        if description is not None and not isinstance(description, str):
            raise _invalid(f"{where}.function.description must be a string")
        parameters = function.get("parameters")
        if parameters is None:
            parameters = {"type": "object", "properties": {}}
        if not isinstance(parameters, dict):
            raise _invalid(f"{where}.function.parameters must be a JSON Schema object")
        if function.get("strict"):
            raise _unsupported(f"{where}.function.strict is not supported")
        tools.append(ClientTool(name=name, description=description, parameters=parameters))
    return tuple(tools)


def _tool_choice(raw: object, tools: tuple[ClientTool, ...], alias: str) -> ToolChoice | None:
    if raw is None:
        return ToolChoice("auto") if tools else None
    if alias == MANAGED:
        raise _invalid(f"tool_choice is not accepted with the {MANAGED} alias")
    if raw in ("auto", "none", "required"):
        choice = ToolChoice(str(raw))
    elif (
        isinstance(raw, dict)
        and raw.get("type") == "function"
        and isinstance(raw.get("function"), dict)
        and isinstance(raw["function"].get("name"), str)
    ):
        choice = ToolChoice("function", raw["function"]["name"])
        if not any(tool.name == choice.name for tool in tools):
            raise _invalid(f"tool_choice names {choice.name!r}, which tools does not supply")
    else:
        raise _invalid(
            "tool_choice must be auto, none, required, or a function naming a supplied tool"
        )
    if choice.mode in ("auto", "required") and not tools:
        raise _invalid(f"tool_choice {choice.mode} needs a nonempty tools array")
    return choice


def _parallel_tool_calls(value: object) -> None:
    if not isinstance(value, bool):
        raise _invalid("parallel_tool_calls must be a boolean")
    if value:
        raise _unsupported("parallel_tool_calls true is not supported; use false or omit it")
