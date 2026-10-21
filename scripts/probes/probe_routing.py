"""Live routing probe spike (issue #48; RM02, RM05, spec sections 6, 8, 12.1).

Throwaway instrumentation, not production code. It asks the pinned
local model to route twelve short prompts (six English, six Simplified
Chinese) among the six demo skill descriptions from spec section 9,
with each mechanism spec section 6 allows for internal local control:

- native_function_calling: one function per skill plus a
  request_clarification function; the gateway maps the call to a
  RouteDecision (route and reason code come from the skill's
  configuration, language from the call).
- json_envelope: the model writes a RouteDecision JSON object as text.
- json_envelope_grammar: the same prompt, with llama-server
  grammar-constrained decoding (response_format json_schema) built from
  the published RouteDecision schema closed over the advertised catalog.

Every output is judged by the production validator
(relaymind.orchestrator.route_decision); invalid output is recorded,
never repaired. The probe has no run, so the validator applies the
context-free rules of spec section 8 only; the run-dependent rules
(execution mode, enabled parallel workflow) are the router's. Router
input is measured with the adapter's local tokenizer (#47) and the
2,048-token cap of spec section 4.1 is applied: an over-cap prompt
takes the bypass, sends no routing generation, and is marked in its
row. First model action is the time from request start to the
validated decision (spec 12.1), including the tokenizer round trips
that precede the generation: the tool-call event for native calls, the
end of the stream for an envelope; None when the output did not
validate.

Sequential, concurrency one, temperature and the runtime's prompt-cache
setting from the probe definition (recorded in the export, spec 10 and
12.2), a single measured pass after warm-ups per mechanism:
observations, never p50 or p95 (spec 12.3).

Usage:
    python scripts/probes/probe_routing.py scripts/probes/m2_routing.json \\
        --out docs/evidence/<date>-m2-routing-<machine>.json [--model <gguf>]
"""

import argparse
import asyncio
import copy
import json
import shutil
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import httpx

# Shared launch helpers live in the M1 timing probe one level up.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from probe_timing import _launch_server, _runtime_banner, _stop  # noqa: E402

from relaymind.adapters.base import (  # noqa: E402
    AdapterEvent,
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
)
from relaymind.adapters.llamacpp import LlamaCppAdapter, LlamaCppConfig  # noqa: E402
from relaymind.adapters.manifest import (  # noqa: E402
    ManifestError,
    launch_adapter_settings,
    launch_settings_argv,
    load_manifest,
    verify_artifact,
)
from relaymind.orchestrator.route_decision import (  # noqa: E402
    REASON_CODES,
    ROUTE_DECISION_SCHEMA,
    SCHEMA_VERSION,
    RouteDecision,
    RouteDecisionError,
    parse_route_decision,
    parse_route_decision_json,
)
from relaymind.stats import (  # noqa: E402
    AdapterIdentity,
    GenerationStats,
    TimingCollector,
    export_stats,
)

MECHANISMS = ("native_function_calling", "json_envelope", "json_envelope_grammar")
CLARIFY_FUNCTION = "request_clarification"
# Spec section 4.1: the fast router's complete input, in local tokens.
ROUTER_INPUT_TOKEN_CAP = 2048

LANGUAGE_PROPERTY = {"enum": ["en", "zh"], "description": "en for English, zh for Chinese."}


@dataclass(frozen=True)
class Skill:
    id: str
    description: str
    route: str
    reason_code: str


@dataclass(frozen=True)
class Prompt:
    text: str
    language: str
    expected_route: str
    expected_skill: str | None


@dataclass(frozen=True)
class RoutingConfig:
    manifest_path: Path
    warmups: int
    max_output_tokens: int
    temperature: float
    # llama-server cache_prompt: reuse the KV cache of a matching prompt
    # prefix. Recorded with every run (spec 10, 12.2).
    cache_prompt: bool
    skills: tuple[Skill, ...]
    prompts: tuple[Prompt, ...]

    @property
    def skill_ids(self) -> tuple[str, ...]:
        return tuple(skill.id for skill in self.skills)


def load_routing_config(path: Path) -> RoutingConfig:
    """Read a probe definition; a malformed one fails loudly (spike)."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    manifest_path = (path.parent / raw["manifest"]).resolve()
    if not manifest_path.is_file():
        raise ValueError(f"manifest not found: {raw['manifest']}")
    if not isinstance(raw["cache_prompt"], bool):
        raise ValueError("'cache_prompt' must be true or false")
    return RoutingConfig(
        manifest_path=manifest_path,
        warmups=int(raw["warmups"]),
        max_output_tokens=int(raw["max_output_tokens"]),
        temperature=float(raw["temperature"]),
        cache_prompt=raw["cache_prompt"],
        skills=tuple(Skill(**skill) for skill in raw["skills"]),
        prompts=tuple(Prompt(**prompt) for prompt in raw["prompts"]),
    )


def _native_tools(config: RoutingConfig) -> tuple[ToolSchema, ...]:
    tools = [
        ToolSchema(
            name=skill.id,
            description=skill.description,
            input_schema={
                "type": "object",
                "properties": {"language": LANGUAGE_PROPERTY},
                "required": ["language"],
                "additionalProperties": False,
            },
        )
        for skill in config.skills
    ]
    tools.append(
        ToolSchema(
            name=CLARIFY_FUNCTION,
            description=(
                "Ask the user one short question when the request does not name the "
                "service or resource it needs."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "language": LANGUAGE_PROPERTY,
                    "question": {"type": "string"},
                },
                "required": ["language", "question"],
                "additionalProperties": False,
            },
        )
    )
    return tuple(tools)


def _native_system_prompt() -> str:
    return (
        "You are a request router. Call exactly one function for the user's request. "
        "Each function is a skill. If the request does not name the service or "
        f"resource it needs, call {CLARIFY_FUNCTION} instead. Never guess a service "
        "or resource name. Set language to en for an English request and zh for a "
        "Chinese request."
    )


def _envelope_system_prompt(config: RoutingConfig) -> str:
    catalog = "\n".join(f"- {skill.id}: {skill.description}" for skill in config.skills)
    return (
        "You are a request router. Skills:\n"
        f"{catalog}\n\n"
        "Reply with only one JSON object and no other text. It has exactly these keys:\n"
        '- "schema_version": always 1\n'
        '- "route": "local" for a simple skill task, "external" for a complex '
        'investigation, "clarify" when the request does not name the service or '
        'resource it needs, "parallel" for the fixed checklist workflow\n'
        '- "skill_id": one skill id from the list, or null\n'
        '- "language": "en" for an English request, "zh" for a Chinese request\n'
        f'- "reason_code": one of {", ".join(sorted(REASON_CODES))}\n'
        '- "clarification": one short question when route is "clarify", otherwise null\n'
        "Never guess a service or resource name."
    )


def build_request(
    mechanism: str, prompt: Prompt, config: RoutingConfig, generation_id: str
) -> GenerationRequest:
    """The router request one mechanism sends for one prompt."""
    options = GenerationOptions(
        max_output_tokens=config.max_output_tokens, temperature=config.temperature
    )
    if mechanism == "native_function_calling":
        system, tools = _native_system_prompt(), _native_tools(config)
    elif mechanism in ("json_envelope", "json_envelope_grammar"):
        system, tools = _envelope_system_prompt(config), ()
    else:
        raise ValueError(f"unknown mechanism: {mechanism}")
    return GenerationRequest(
        generation_id=generation_id,
        messages=(
            Message(role=Role.SYSTEM, content=system),
            Message(role=Role.USER, content=prompt.text),
        ),
        tool_schemas=tools,
        options=options,
    )


def grammar_schema(config: RoutingConfig) -> dict[str, object]:
    """The published RouteDecision schema, closed over this catalog."""
    schema = copy.deepcopy(ROUTE_DECISION_SCHEMA)
    for key in ("$schema", "$id", "title", "description"):
        schema.pop(key, None)
    schema["properties"]["skill_id"] = {"enum": [*config.skill_ids, None]}
    # The grammar constrains the shape only. The cross-field if/then and
    # the non-blank patterns are outside what the runtime's grammar
    # converter is known to accept; the validator judges them afterwards.
    schema.pop("allOf", None)
    schema["properties"]["clarification"].pop("pattern", None)
    return schema


def _native_decision(
    calls: Sequence[ToolCall], config: RoutingConfig
) -> tuple[RouteDecision | None, str | None]:
    if len(calls) != 1:
        return None, f"expected one function call, got {len(calls)}"
    call = calls[0]
    arguments = dict(call.arguments)
    language = arguments.pop("language", None)
    if call.tool_name == CLARIFY_FUNCTION:
        question = arguments.pop("question", None)
        if question is None:
            # Declared required in the function schema (RM09).
            return None, "missing required argument: question"
        data = {
            "route": "clarify",
            "skill_id": None,
            "reason_code": "missing_input",
            "clarification": question,
        }
    else:
        skill = next((s for s in config.skills if s.id == call.tool_name), None)
        if skill is None:
            return None, f"unknown function: {call.tool_name}"
        data = {
            "route": skill.route,
            "skill_id": skill.id,
            "reason_code": skill.reason_code,
            "clarification": None,
        }
    if arguments:
        return None, f"unexpected arguments: {', '.join(sorted(arguments))}"
    try:
        decision = parse_route_decision(
            {"schema_version": SCHEMA_VERSION, "language": language, **data},
            skill_ids=config.skill_ids,
        )
    except RouteDecisionError as exc:
        return None, str(exc)
    return decision, None


def decide(
    mechanism: str, events: Sequence[AdapterEvent], config: RoutingConfig
) -> tuple[RouteDecision | None, str | None]:
    """Judge one generation's output; (decision, None) or (None, why)."""
    failed = next((event for event in events if isinstance(event, GenerationFailed)), None)
    if failed is not None:
        # A transport or runtime failure is not model output (RM16).
        return None, f"generation failed ({failed.code}): {failed.message}"
    calls = [event for event in events if isinstance(event, ToolCall)]
    text = "".join(event.text for event in events if isinstance(event, TextDelta))
    if mechanism == "native_function_calling":
        if not calls:
            return None, f"no function call; text: {text[:200]!r}"
        return _native_decision(calls, config)
    if calls:
        return None, "unexpected function call in envelope mode"
    try:
        return parse_route_decision_json(text, skill_ids=config.skill_ids), None
    except RouteDecisionError as exc:
        return None, str(exc)


async def run_mechanism(
    adapter: ModelAdapter,
    mechanism: str,
    config: RoutingConfig,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[list[dict[str, object]], list[GenerationStats]]:
    """Warm-ups then one measured pass; returns (rows, timing records).

    The clock starts before the token count, so first model action
    includes the tokenizer round trips the router pays (spec 12.1). A
    count over ROUTER_INPUT_TOKEN_CAP takes the bypass: no generation is
    sent and the row is marked (spec 4.1). An adapter exception (token
    count or generation) is recorded in the row's "error" field and the
    pass continues; nothing is judged for that row, and no synthetic
    text is added to the model's output (RM16).
    """
    collector = TimingCollector(adapter, clock=clock)
    plan = [(True, f"warmup_{mechanism}_{i}", config.prompts[0]) for i in range(config.warmups)]
    plan += [(False, f"{mechanism}_{i:02d}", p) for i, p in enumerate(config.prompts)]
    rows: list[dict[str, object]] = []
    for warmup, generation_id, prompt in plan:
        request = build_request(mechanism, prompt, config, generation_id)
        input_tokens: int | None = None
        events: list[AdapterEvent] = []
        action_at: float | None = None
        adapter_error: str | None = None
        bypass = False
        started = clock()
        try:
            input_tokens = await adapter.count_tokens(request.messages, request.tool_schemas)
            if input_tokens > ROUTER_INPUT_TOKEN_CAP:
                bypass = True
            else:
                async for event in collector.generate(request):
                    events.append(event)
                    if isinstance(event, ToolCall) and action_at is None:
                        action_at = clock() - started
                    elif isinstance(event, Completed) and mechanism != "native_function_calling":
                        action_at = clock() - started
        except Exception as exc:  # noqa: BLE001 - recorded, the pass continues
            adapter_error = f"{type(exc).__name__}: {exc}"
        if adapter_error is None and not bypass:
            decision, error = decide(mechanism, events, config)
        else:
            decision, error = None, None
        raw_text = "".join(e.text for e in events if isinstance(e, TextDelta)) or None
        raw_calls = [
            {"tool_name": e.tool_name, "arguments": dict(e.arguments)}
            for e in events
            if isinstance(e, ToolCall)
        ]
        rows.append(
            {
                "generation_id": generation_id,
                "mechanism": mechanism,
                "warmup": warmup,
                "prompt": prompt.text,
                "language": prompt.language,
                "expected_route": prompt.expected_route,
                "expected_skill": prompt.expected_skill,
                "router_input_tokens": input_tokens,
                "bypass": bypass,
                "valid": decision is not None,
                "validation_error": error,
                "error": adapter_error,
                "decision": decision.to_json_object() if decision else None,
                "route": decision.route.value if decision else None,
                "skill_id": decision.skill_id if decision else None,
                "route_correct": bool(decision and decision.route.value == prompt.expected_route),
                "skill_correct": bool(decision and decision.skill_id == prompt.expected_skill),
                "language_correct": bool(decision and decision.language.value == prompt.language),
                "first_model_action_seconds": action_at if decision else None,
                "raw_text": raw_text,
                "raw_tool_calls": raw_calls,
            }
        )
    return rows, collector.drain()


class ProbeLlamaCppAdapter(LlamaCppAdapter):
    """LlamaCppAdapter with the probe's explicit cache setting (spike only).

    Every generation body carries cache_prompt, so the runtime's
    prefix-cache behavior is a recorded setting and not a default
    (spec 10, 12.2). With a schema it also adds the response_format
    json_schema for the grammar mechanism. Neither field shapes the
    prompt, so the token count is unchanged.
    """

    def __init__(
        self,
        config: LlamaCppConfig,
        *,
        cache_prompt: bool,
        schema: Mapping[str, object] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        super().__init__(config, transport=transport)
        self._cache_prompt = cache_prompt
        self._schema = dict(schema) if schema is not None else None

    def _chat_body(self, request: GenerationRequest) -> dict[str, object]:
        body = super()._chat_body(request)
        body["cache_prompt"] = self._cache_prompt
        if self._schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "route_decision", "schema": self._schema},
            }
        return body


def summarize(rows: Sequence[Mapping[str, object]]) -> dict[str, dict[str, str]]:
    """Exact numerators and denominators per mechanism and language."""
    summary: dict[str, dict[str, str]] = {}
    for mechanism in MECHANISMS:
        for language in ("en", "zh"):
            group = [
                r
                for r in rows
                if r["mechanism"] == mechanism and r["language"] == language and not r["warmup"]
            ]
            if not group:
                continue
            n = len(group)
            summary[f"{mechanism}/{language}"] = {
                key: f"{sum(1 for r in group if r[key])}/{n}"
                for key in ("valid", "route_correct", "skill_correct", "language_correct", "bypass")
            }
    return summary


async def _measure(
    base_url: str,
    ctx_size: int,
    template_kwargs: Mapping[str, object] | None,
    config: RoutingConfig,
) -> tuple[list[dict[str, object]], list[GenerationStats]]:
    llama_config = LlamaCppConfig(
        base_url=base_url, context_limit=ctx_size, chat_template_kwargs=template_kwargs
    )
    rows: list[dict[str, object]] = []
    records = []
    for mechanism in MECHANISMS:
        schema = grammar_schema(config) if mechanism == "json_envelope_grammar" else None
        adapter = ProbeLlamaCppAdapter(
            llama_config, cache_prompt=config.cache_prompt, schema=schema
        )
        try:
            mech_rows, mech_records = await run_mechanism(adapter, mechanism, config)
        finally:
            await adapter.aclose()
        rows += mech_rows
        records += mech_records
    return rows, records


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("config", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--model", type=Path, default=None)
    args = parser.parse_args()

    config = load_routing_config(args.config)
    manifest = load_manifest(config.manifest_path)
    if shutil.which("llama-server") is None:
        parser.error("llama-server is not on PATH")
    model_path = args.model or config.manifest_path.parent / manifest.file_name
    try:
        verify_artifact(manifest, model_path)
    except ManifestError as exc:
        parser.error(f"artifact failed verification: {exc}")

    server_argv = launch_settings_argv(manifest.launch_settings)
    ctx_size, template_kwargs = launch_adapter_settings(manifest.launch_settings)
    log_path = args.out.with_suffix(".server.log")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    process, base_url, cold_start = _launch_server(model_path, server_argv, log_path)
    try:
        rows, records = asyncio.run(_measure(base_url, ctx_size, template_kwargs, config))
    finally:
        _stop(process)
        log_path.unlink(missing_ok=True)

    run_info = {
        "command": " ".join(["python", *sys.argv]),
        "passes": 1,
        "warmups_per_mechanism": config.warmups,
        "cold_start_seconds": cold_start,
        "manifest": config.manifest_path.name,
        "model_file": model_path.name,
        "server_argv": server_argv,
        "runtime_banner": _runtime_banner(),
        "temperature": config.temperature,
        "max_output_tokens": config.max_output_tokens,
        "cache_prompt": config.cache_prompt,
        "router_input_token_cap": ROUTER_INPUT_TOKEN_CAP,
        "mechanisms": list(MECHANISMS),
        "native_system_prompt": _native_system_prompt(),
        "native_tools": [
            {"name": t.name, "description": t.description, "input_schema": dict(t.input_schema)}
            for t in _native_tools(config)
        ],
        "envelope_system_prompt": _envelope_system_prompt(config),
        "grammar_schema": grammar_schema(config),
        "skills": [skill.__dict__ for skill in config.skills],
        "prompts": [prompt.__dict__ for prompt in config.prompts],
        "rows": rows,
        "summary": summarize(rows),
    }
    identity = AdapterIdentity(
        adapter="llamacpp",
        model_id=manifest.model_id,
        quantization=manifest.quantization,
        runtime_version=manifest.runtime_version,
    )
    export_stats(args.out, identity=identity, records=records, run_info=run_info)
    print(f"cold start: {cold_start:.2f} s")
    for row in rows:
        if row["warmup"]:
            continue
        action = row["first_model_action_seconds"]
        shown = f"{action * 1000:.0f} ms" if action is not None else "-"
        status = "bypass" if row["bypass"] else f"valid={row['valid']}"
        print(
            f"{row['generation_id']}: tokens={row['router_input_tokens']} {status} "
            f"route={row['route']} skill={row['skill_id']} action={shown} "
            f"err={row['validation_error'] or row['error']}"
        )
    print(json.dumps(run_info["summary"], indent=2))
    print(f"export: {args.out}")


if __name__ == "__main__":
    main()
