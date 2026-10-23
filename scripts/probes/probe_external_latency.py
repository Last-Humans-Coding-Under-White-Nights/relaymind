"""External latency probe against a Chat Completions endpoint (#116).

RM25, RM18, spec 0.31 sections 6 and 12.1.

Finding F3 of the 2026-10-23 analysis in docs/EVIDENCE.md: an external
call has a fixed cost, and nothing recorded yet says what it is made of.
This probe sends streamed Chat Completions requests over raw HTTP, so a
provider's own request field can be sent without changing the adapter, and
records per request the time to the response headers, to the first
reasoning delta (``reasoning_content``), to the first content or tool-call
delta, and to the end of the stream, with the whole usage object (a
reasoning-token count the provider did not return is recorded as null,
never as 0, RM16).

Two request shapes, both built by the gateway's own code: ``answer`` is an
answer generation, the conversation holding a health tool exchange and no
tool offered; ``tools`` is a tool-offering generation, the tools of
investigate-performance offered and no exchange yet. Both go through
``orchestrator.handoff.external_messages`` over a synthetic handoff
envelope and are encoded by the external adapter's own
``chat_completions.build_chat_request``. Four arms of five requests: the
shape times the provider's reasoning default (the field is not sent) or
off (the provider's documented switch, from the definition, with the
documentation URLs checked on the day). The arms are interleaved in
rounds, the order of the arms rotating each round, so a drift in the
provider is spread over them; every arm sends the same five user texts,
each request has a run ID of its own, and no call is retried (every retry
would be another provider call). The plan is refused when it is over
``--max-calls``.

The endpoint, the model, and the name of the environment variable that
holds the key are arguments (the defaults are the RELAYMIND_EXTERNAL_*
variables). The key is read from the environment, sent in the
Authorization header, and is never printed or written: a provider that
echoes it in an error has it replaced in the row.

Sequential, concurrency one: single-pass observations from one run at one
time of day, never p50 or p95 (spec 12.3). The first call of the run also
pays for the connection, and its row says so.

Usage:
    source ~/.config/relaymind/external.env
    python scripts/probes/probe_external_latency.py scripts/probes/m7_external_latency.json \\
        --out docs/evidence/<date>-external-latency-<provider>.json
"""

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from relaymind.adapters.base import GenerationRequest, Message, Role, ToolSchema
from relaymind.adapters.chat_completions import build_chat_request
from relaymind.adapters.openai_compat import parse_sse_line
from relaymind.evaluation.environment import application_revision, machine_manifest
from relaymind.orchestrator.budgets import LocalBudgets
from relaymind.orchestrator.handoff import build_handoff_envelope, external_messages
from relaymind.orchestrator.local_loop import ANSWER_LANGUAGE, EXTERNAL_OPTIONS
from relaymind.orchestrator.route_decision import Language
from relaymind.orchestrator.workflows import PARALLEL_WORKFLOWS
from relaymind.skills import Skill, SkillRegistry
from relaymind.skills.predicates import COMPLETION_PREDICATES
from relaymind.tools import CallerContext, ToolRegistry
from relaymind.tools.registry import OperatorPolicy
from relaymind.tools.sandbox import (
    DEMO_TOOL_NAMES,
    READ_PERMISSION,
    SandboxDataService,
    register_demo_tools,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
KIND = "relaymind.probe.external_latency"
LABEL = (
    "single-pass observations from one run at one time of day against one endpoint; not a "
    "benchmark result (spec 12.3); never p50 or p95"
)
SHAPES = ("answer", "tools")
CHAT_PATH = "/chat/completions"
# The owner's cap for the live run of this probe (the whole PR): four arms of five requests.
DEFAULT_MAX_CALLS = 20
# The external stage is bounded at 30 s (spec 11.2); the probe waits a little longer so a slow
# call is a measured row and not an abort.
REQUEST_TIMEOUT_SECONDS = 60.0
REDACTED = "[redacted]"
# Response headers that are never recorded.
SECRET_HEADERS = frozenset({"set-cookie", "authorization", "proxy-authorization", "cookie"})


@dataclass(frozen=True)
class Arm:
    """One arm: a request shape and the provider's reasoning setting."""

    shape: str  # answer or tools
    reasoning: str  # default (no field sent) or off (the provider's documented field)

    @property
    def name(self) -> str:
        return f"{self.shape}_{self.reasoning}"


ARMS = (
    Arm("answer", "default"),
    Arm("answer", "off"),
    Arm("tools", "default"),
    Arm("tools", "off"),
)


@dataclass(frozen=True)
class ProbeConfig:
    """A validated probe definition."""

    skills_root: Path
    sandbox_seed: Path
    repetitions: int
    answer_skill: str
    tools_skill: str
    answer_requests: tuple[Mapping[str, str], ...]
    tools_requests: tuple[Mapping[str, str], ...]
    reasoning_off: Mapping[str, Any]

    def requests(self, shape: str) -> tuple[Mapping[str, str], ...]:
        return self.answer_requests if shape == "answer" else self.tools_requests

    def to_json_object(self) -> dict[str, Any]:
        return {
            "repetitions": self.repetitions,
            "answer": {"skill": self.answer_skill, "requests": list(self.answer_requests)},
            "tools": {"skill": self.tools_skill, "requests": list(self.tools_requests)},
        }


def load_probe_config(path: Path) -> ProbeConfig:
    """Read and validate a probe definition; ValueError names the bad field."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read the definition: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("the definition must be a JSON object")

    def resolved(key: str) -> Path:
        value = raw.get(key)
        if not isinstance(value, str) or not value:
            raise ValueError(f"'{key}' must be a path")
        target = (path.parent / value).resolve()
        if not target.exists():
            raise ValueError(f"'{key}' not found: {value}")
        return target

    repetitions = raw.get("repetitions")
    if type(repetitions) is not int or repetitions < 1:
        raise ValueError("'repetitions' must be a positive integer")
    seed = resolved("sandbox_seed")
    try:
        services = set(json.loads(seed.read_text(encoding="utf-8"))["services"])
    except (OSError, ValueError, KeyError) as exc:
        raise ValueError(f"cannot read the services of the sandbox seed: {exc}") from exc

    def section(shape: str) -> tuple[str, tuple[Mapping[str, str], ...]]:
        entry = raw.get(shape)
        if not isinstance(entry, dict):
            raise ValueError(f"'{shape}' must be an object")
        skill = entry.get("skill")
        if not isinstance(skill, str) or not skill:
            raise ValueError(f"'{shape}.skill' must be a skill name")
        requests = entry.get("requests")
        if not isinstance(requests, list) or len(requests) != repetitions:
            raise ValueError(f"'{shape}.requests' must hold one request for each repetition")
        for item in requests:
            if not isinstance(item, dict) or not str(item.get("text", "")).strip():
                raise ValueError(f"'{shape}.requests' entries need a non-blank text")
            if shape == "answer" and item.get("service") not in services:
                raise ValueError(
                    f"'answer.requests' names an unknown service: {item.get('service')}"
                )
        texts = [item["text"] for item in requests]
        if len(set(texts)) != len(texts):
            raise ValueError(f"'{shape}.requests' must hold different texts")
        return skill, tuple(dict(item) for item in requests)

    answer_skill, answer_requests = section("answer")
    tools_skill, tools_requests = section("tools")
    reasoning_off = raw.get("reasoning_off")
    if (
        not isinstance(reasoning_off, dict)
        or not isinstance(reasoning_off.get("fields"), dict)
        or not reasoning_off["fields"]
    ):
        raise ValueError("'reasoning_off.fields' must name the request field that switches it off")
    documentation = reasoning_off.get("documentation")
    if not isinstance(documentation, list) or not documentation:
        raise ValueError("'reasoning_off.documentation' must list the documentation URLs")
    return ProbeConfig(
        skills_root=resolved("skills_root"),
        sandbox_seed=seed,
        repetitions=repetitions,
        answer_skill=answer_skill,
        tools_skill=tools_skill,
        answer_requests=answer_requests,
        tools_requests=tools_requests,
        reasoning_off=reasoning_off,
    )


# -- the requests the gateway builds ---------------------------------------------------------------


@dataclass(frozen=True)
class Shapes:
    """The two request shapes over the demo skills, tools, and sandbox."""

    config: ProbeConfig
    answer_skill: Skill
    tools_skill: Skill
    tool_schemas: tuple[ToolSchema, ...]
    sandbox: SandboxDataService

    def request(self, shape: str, index: int, run_id: str) -> GenerationRequest:
        """The external request of one shape for the index-th user text."""
        entry = self.config.requests(shape)[index]
        user = [Message(role=Role.USER, content=entry["text"])]
        budgets = LocalBudgets()
        if shape == "answer":
            skill = self.answer_skill
            operation = self._health_operation(entry["service"], run_id)
            envelope = build_handoff_envelope(
                run_id=run_id,
                messages=user,
                skill=_skill_ref(skill),
                operations=[operation],
                proposals=[],
                allowed_tools=[],
                pending_work=[{"kind": "final_answer", "tool_name": None}],
                reason_code="invalid_output",
                # Two generations and one tool call used, as after a local tool stage.
                remaining_budgets=_remaining(budgets, generations=2, tool_calls=1),
                language=Language.EN.value,
            )
            offered: tuple[ToolSchema, ...] = ()
        elif shape == "tools":
            skill = self.tools_skill
            names = [schema.name for schema in self.tool_schemas]
            envelope = build_handoff_envelope(
                run_id=run_id,
                messages=user,
                skill=_skill_ref(skill),
                operations=[],
                proposals=[],
                allowed_tools=names,
                pending_work=[
                    *({"kind": "tool_call", "tool_name": name} for name in names),
                    {"kind": "final_answer", "tool_name": None},
                ],
                reason_code="complex_skill",
                # The routing generation is the one generation used before the handoff.
                remaining_budgets=_remaining(budgets, generations=1, tool_calls=0),
                language=Language.EN.value,
            )
            offered = self.tool_schemas
        else:
            raise ValueError(f"unknown shape: {shape}")
        messages = external_messages(
            envelope,
            skill_body=skill.body,
            catalog=(),
            answer_instruction=ANSWER_LANGUAGE[Language.EN],
        )
        return GenerationRequest(
            generation_id=f"{run_id}_{shape}",
            messages=messages,
            tool_schemas=offered,
            options=EXTERNAL_OPTIONS,
        )

    def _health_operation(self, service: str, run_id: str) -> dict[str, Any]:
        """A completed get_service_health operation with the sandbox's own output."""
        operation_id = f"op_{run_id}"
        data = self.sandbox.service_health(service)
        return {
            "operation_id": operation_id,
            "tool_name": "get_service_health",
            "arguments": {"service": service},
            "status": "succeeded",
            "result": {
                "operation_id": operation_id,
                "tool_name": "get_service_health",
                "status": "succeeded",
                "data": data,
                "error": None,
                "observed_at": data["fixture"]["observed_at"],
                "duration_ms": 3,
                "source_ref": f"tool:get_service_health/{operation_id}",
                "truncated": False,
            },
        }


def _skill_ref(skill: Skill) -> dict[str, str]:
    return {"name": skill.name, "version": skill.version, "content_hash": skill.content_hash}


def _remaining(budgets: LocalBudgets, *, generations: int, tool_calls: int) -> dict[str, float]:
    """The budgets a handoff preserves (RM13) after the stated local work, at the defaults."""
    return {
        "run_generations": budgets.run_generations - generations,
        "run_tool_executions": budgets.run_tool_executions - tool_calls,
        "run_active_seconds": budgets.run_active_seconds,
        "external_generations": budgets.external_generations,
        "external_seconds": float(budgets.external_seconds),
    }


def build_shapes(config: ProbeConfig, *, work_dir: Path) -> Shapes:
    """The shapes over the checked-in demo skills and a sandbox working copy (RM06, RM08)."""
    sandbox = SandboxDataService(config.sandbox_seed, work_dir=work_dir)
    tools = ToolRegistry()
    register_demo_tools(tools, sandbox)
    skills = SkillRegistry.load(
        [config.skills_root],
        tools=tools,
        predicates=COMPLETION_PREDICATES.keys(),
        workflows=PARALLEL_WORKFLOWS,
    )
    tools_skill = skills.get(config.tools_skill)
    available = tools.available_tools(
        skill_allowlist=tools_skill.allowed_tools,
        policy=OperatorPolicy(enabled_tools=DEMO_TOOL_NAMES),
        caller=CallerContext(permissions=frozenset({READ_PERMISSION})),
    )
    return Shapes(
        config=config,
        answer_skill=skills.get(config.answer_skill),
        tools_skill=tools_skill,
        tool_schemas=tools.tool_schemas(
            name for name in tools_skill.allowed_tools if name in available
        ),
        sandbox=sandbox,
    )


def request_body(
    request: GenerationRequest,
    *,
    model: str,
    reasoning_off: Mapping[str, object],
    arm: Arm,
) -> dict[str, object]:
    """The body sent: the external adapter's encoding, plus the provider's field when off."""
    body = build_chat_request(request, model=model, supported_options=())
    if arm.reasoning == "off":
        body.update(reasoning_off)
    return body


# -- sending and reading ---------------------------------------------------------------------------


def _number(value: object) -> int | float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def usage_fields(usage: Mapping[str, Any] | None) -> dict[str, Any]:
    """The counts one usage object carries; a count the provider did not return is None (RM16)."""
    usage = usage or {}
    prompt_details = usage.get("prompt_tokens_details")
    completion_details = usage.get("completion_tokens_details")
    return {
        "usage": dict(usage) if usage else None,
        "prompt_tokens": _number(usage.get("prompt_tokens")),
        "completion_tokens": _number(usage.get("completion_tokens")),
        "cached_tokens": _number(
            prompt_details.get("cached_tokens") if isinstance(prompt_details, Mapping) else None
        ),
        "reasoning_tokens": _number(
            completion_details.get("reasoning_tokens")
            if isinstance(completion_details, Mapping)
            else None
        ),
    }


def observe_call(
    client: httpx.Client,
    path: str,
    body: Mapping[str, object],
    headers: Mapping[str, str],
    *,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Send one streamed chat completion and time it.

    Times are in milliseconds of the monotonic clock from just before the
    request is sent (RM25): headers_ms to the response status and headers,
    first_reasoning_ms to the first reasoning delta, first_output_ms to the
    first content or tool-call delta, end_ms to the end of the stream. The
    clock is read once for each, in that order. A transport failure or an
    error status is recorded in the row, never raised.
    """
    content = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    sent_headers = {**headers, "content-type": "application/json"}
    started = clock()
    status = error = finish_reason = response_model = None
    headers_ms = first_reasoning_ms = first_output_ms = None
    usage: Mapping[str, Any] | None = None
    response_headers: dict[str, str] = {}
    text: list[str] = []
    reasoning_chars = 0
    calls: dict[int, dict[str, str]] = {}
    try:
        with client.stream("POST", path, content=content, headers=sent_headers) as response:
            headers_ms = (clock() - started) * 1000
            status = response.status_code
            response_headers = {
                name.lower(): value
                for name, value in response.headers.items()
                if name.lower() not in SECRET_HEADERS
            }
            if status != 200:
                error = f"status {status}: {response.read().decode(errors='replace')[:300]}"
            else:
                for line in response.iter_lines():
                    chunk = parse_sse_line(line)
                    if chunk is None:
                        continue
                    if isinstance(chunk.get("model"), str):
                        response_model = chunk["model"]
                    if isinstance(chunk.get("usage"), Mapping):
                        usage = chunk["usage"]
                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}
                        if delta.get("reasoning_content"):
                            reasoning_chars += len(delta["reasoning_content"])
                            if first_reasoning_ms is None:
                                first_reasoning_ms = (clock() - started) * 1000
                        output = False
                        if delta.get("content"):
                            text.append(delta["content"])
                            output = True
                        for fragment in delta.get("tool_calls") or []:
                            call = calls.setdefault(
                                int(fragment.get("index") or 0), {"name": "", "arguments": ""}
                            )
                            function = fragment.get("function") or {}
                            call["name"] += str(function.get("name") or "")
                            call["arguments"] += str(function.get("arguments") or "")
                            output = True
                        if output and first_output_ms is None:
                            first_output_ms = (clock() - started) * 1000
                        if choice.get("finish_reason"):
                            finish_reason = choice["finish_reason"]
    except httpx.HTTPError as exc:
        error = f"{type(exc).__name__}: {exc}"
    return {
        "http_status": status,
        "error": error,
        "finish_reason": finish_reason,
        "response_model": response_model,
        "headers_ms": headers_ms,
        "first_reasoning_ms": first_reasoning_ms,
        "first_output_ms": first_output_ms,
        "end_ms": (clock() - started) * 1000,
        "request_bytes": len(content),
        "response_headers": response_headers,
        "output": {
            "text": "".join(text) or None,
            "reasoning_chars": reasoning_chars,
            "tool_calls": [calls[i] for i in sorted(calls)],
        },
        **usage_fields(usage),
    }


def scrub(value: Any, secret: str) -> Any:
    """The value with the credential replaced in every string, however deep."""
    if isinstance(value, str):
        return value.replace(secret, REDACTED) if secret else value
    if isinstance(value, Mapping):
        return {scrub(key, secret): scrub(item, secret) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub(item, secret) for item in value]
    return value


def plan_calls(config: ProbeConfig) -> list[tuple[int, Arm]]:
    """The calls in send order: rounds of the four arms, the arm order rotating each round."""
    plan = []
    for round_number in range(config.repetitions):
        turn = round_number % len(ARMS)
        plan += [(round_number, arm) for arm in ARMS[turn:] + ARMS[:turn]]
    return plan


def run_probe(
    config: ProbeConfig,
    shapes: Shapes,
    *,
    base_url: str,
    model: str,
    api_key: str,
    reasoning_off: Mapping[str, object],
    max_calls: int = DEFAULT_MAX_CALLS,
    transport: httpx.BaseTransport | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Send the planned calls one after the other; return the rows and the call count.

    A plan over max_calls is refused before any call. A call that fails is
    a row with its error and is not retried. The credential is scrubbed
    from every row as it is made.
    """
    plan = plan_calls(config)
    if len(plan) > max_calls:
        raise ValueError(f"the plan has {len(plan)} calls, over the cap of {max_calls}")
    headers = {"authorization": f"Bearer {api_key}"}
    rows: list[dict[str, Any]] = []
    # The body of the first request of each arm, as sent: the options used are in the record.
    bodies: dict[str, Mapping[str, object]] = {}
    with httpx.Client(
        base_url=base_url, timeout=REQUEST_TIMEOUT_SECONDS, transport=transport
    ) as client:
        for position, (round_number, arm) in enumerate(plan):
            run_id = f"run_probe_{arm.name}_{round_number}"
            request = shapes.request(arm.shape, round_number, run_id)
            body = request_body(request, model=model, reasoning_off=reasoning_off, arm=arm)
            bodies.setdefault(arm.name, body)
            observed = observe_call(client, CHAT_PATH, body, headers, clock=clock)
            rows.append(
                scrub(
                    {
                        "position": position,
                        "round": round_number,
                        "arm": arm.name,
                        "shape": arm.shape,
                        "reasoning": arm.reasoning,
                        "index": round_number,
                        "run_id": run_id,
                        "user_text": config.requests(arm.shape)[round_number]["text"],
                        "first_call_of_run": position == 0,
                        "sent_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
                        **observed,
                    },
                    api_key,
                )
            )
            print(
                f"{position + 1}/{len(plan)} {arm.name} #{round_number}: "
                f"status={observed['http_status']} headers={_fmt(observed['headers_ms'])} "
                f"reasoning={_fmt(observed['first_reasoning_ms'])} "
                f"output={_fmt(observed['first_output_ms'])} end={_fmt(observed['end_ms'])}",
                flush=True,
            )
    return {
        "calls_made": len(rows),
        "provider": {"base_url": base_url, "model": model},
        "example_bodies": bodies,
        "rows": rows,
    }


def _fmt(value: float | None) -> str:
    return "-" if value is None else f"{value:.0f} ms"


# -- the export --------------------------------------------------------------------------------


def _range(values: Sequence[float | int | None]) -> dict[str, float | int] | None:
    present = [v for v in values if v is not None]
    return {"min": min(present), "max": max(present)} if present else None


def summarize(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Per arm: each metric as the values in send order and their range.

    These are the five observations of one pass, never percentiles.
    """
    summary = []
    for arm in ARMS:
        group = [row for row in rows if row["arm"] == arm.name]
        entry: dict[str, Any] = {
            "arm": arm.name,
            "shape": arm.shape,
            "reasoning": arm.reasoning,
            "requests": len(group),
            "errors": sum(1 for row in group if row["error"]),
            "with_reasoning_delta": sum(
                1 for row in group if row["first_reasoning_ms"] is not None
            ),
            "with_tool_call": sum(1 for row in group if row["output"]["tool_calls"]),
        }
        for metric in (
            "headers_ms",
            "first_reasoning_ms",
            "first_output_ms",
            "end_ms",
            "prompt_tokens",
            "completion_tokens",
            "reasoning_tokens",
            "cached_tokens",
        ):
            values = [row[metric] for row in group]
            entry[metric] = {"values": values, "range": _range(values)}
        summary.append(entry)
    return summary


def build_export(
    result: Mapping[str, Any],
    *,
    config: ProbeConfig,
    command: Sequence[str],
    provider: Mapping[str, Any] | None = None,
    timing: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The export document: what was asked, what came back, and the options used.

    provider adds to what the run knows of the endpoint (the name of the
    key variable, the region); timing holds the times, the machine, and
    the application revision.
    """
    rows = result["rows"]
    return {
        "kind": KIND,
        "label": LABEL,
        "command": " ".join(command),
        **(timing or {}),
        "provider": {**result["provider"], **(provider or {})},
        "reasoning_off": dict(config.reasoning_off),
        "calls_made": result["calls_made"],
        "definition": config.to_json_object(),
        "example_bodies": dict(result["example_bodies"]),
        "summary": summarize(rows),
        "rows": list(rows),
    }


# -- the run -------------------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("config", type=Path, help="probe definition JSON")
    parser.add_argument("--out", type=Path, required=True, help="export file path")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("RELAYMIND_EXTERNAL_BASE_URL"),
        help="the endpoint, with its version prefix (default: $RELAYMIND_EXTERNAL_BASE_URL)",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("RELAYMIND_EXTERNAL_MODEL"),
        help="the model identifier (default: $RELAYMIND_EXTERNAL_MODEL)",
    )
    parser.add_argument(
        "--api-key-env",
        default="RELAYMIND_EXTERNAL_API_KEY",
        help="the NAME of the environment variable that holds the key",
    )
    parser.add_argument("--region", help="the endpoint's region, when it is known")
    parser.add_argument("--max-calls", type=int, default=DEFAULT_MAX_CALLS)
    args = parser.parse_args(argv)
    try:
        config = load_probe_config(args.config)
    except ValueError as exc:
        parser.error(str(exc))
    if not args.base_url or not args.model:
        parser.error("the base URL and the model are required (arguments or RELAYMIND_EXTERNAL_*)")
    api_key = (os.environ.get(args.api_key_env) or "").strip()
    if not api_key:
        parser.error(f"environment variable {args.api_key_env} is not set")

    # Read before the run writes its export into the checkout.
    application = application_revision(REPO_ROOT)
    started = datetime.now(UTC)
    work = Path(tempfile.mkdtemp(prefix="relaymind-external-latency-"))
    try:
        shapes = build_shapes(config, work_dir=work / "sandbox")
        result = run_probe(
            config,
            shapes,
            base_url=args.base_url,
            model=args.model,
            api_key=api_key,
            reasoning_off=config.reasoning_off["fields"],
            max_calls=args.max_calls,
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)

    host = urlsplit(args.base_url)
    document = build_export(
        result,
        config=config,
        command=["python", *sys.argv],
        provider={
            "host": host.hostname,
            "api_key_env": args.api_key_env,
            "region": args.region,
            "region_known": args.region is not None,
        },
        timing={
            "started_at_utc": started.isoformat(timespec="seconds"),
            "started_at_local": started.astimezone().isoformat(timespec="seconds"),
            "finished_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
            "machine": machine_manifest(),
            "application": application,
        },
    )
    text = json.dumps(scrub(document, api_key), indent=2, ensure_ascii=False) + "\n"
    if api_key in text:
        raise SystemExit("refusing to write: the export would hold the credential")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, "utf-8")
    for entry in document["summary"]:
        print(
            f"{entry['arm']:15} errors={entry['errors']} "
            f"headers={entry['headers_ms']['range']} "
            f"first_output={entry['first_output_ms']['range']} end={entry['end_ms']['range']}"
        )
    print(f"calls made: {result['calls_made']}")
    print(f"export: {args.out}")


if __name__ == "__main__":
    main()
