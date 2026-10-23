"""Prefix-cache probe against llama-server (#114, #117; RM27, RM25, spec 0.31 sections 6 and 10).

Finding F9 of the 2026-10-23 analysis in docs/EVIDENCE.md: every
cached-token figure recorded so far is the adapter's default, not a
measurement. This probe asks the pinned runtime directly. It builds the
requests the gateway builds: the routing request with
``Router.build_request`` over the demo skills, and the skill-stage request
with ``skill_stage_messages`` over the offered tools of one skill, both
encoded by ``relaymind.adapters.llamacpp.build_chat_request``. Each body
is sent over raw HTTP with ``cache_prompt`` set to the arm and nothing
else added, so the whole ``timings`` and ``usage`` objects are read back
(cache_n, prompt_n, prompt_ms, predicted_n, predicted_ms, and
prompt_tokens_details.cached_tokens; a value the runtime did not return is
recorded as null, never as 0, RM16).

The pass has four groups, each in its own llama-server process so that no
group inherits the cache of another: the layouts blocked (ten routing
requests, then ten skill-stage requests) and interleaved (they alternate),
each with cache_prompt true and then false. A group starts with the first
request of each purpose after the launch (phase cold), then warm-up rounds
(phase warmup, a text that is not measured), then the measured requests
(phase measured): ten with different user text per purpose. Before each
request the probe counts its prompt tokens through the runtime's own
template and tokenizer, and records how many of them it shares with the
previous request of the same purpose, so cache_n can be read against what
the tokens allow. The constant prefix of a purpose is the longest token
prefix shared by all its measured requests.

Sequential, concurrency one: single-pass observations from one run on one
machine, never p50 or p95 (spec 12.3). ``--runtime-extra-args`` adds
llama-server arguments after the manifest's launch settings (spec 0.31
section 12.2, #117); they are recorded.

Usage:
    python scripts/probes/probe_prefix_cache.py scripts/probes/m7_prefix_cache.json \\
        --out docs/evidence/<date>-prefix-cache-<machine>.json \\
        [--runtime-extra-args='--n-gpu-layers 0 --threads 2']
"""

import argparse
import json
import shutil
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

# Shared launch helpers live in the live acceptance harness one level up.
SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

import live_acceptance as harness  # noqa: E402

from relaymind.adapters.base import (  # noqa: E402
    GenerationRequest,
    Message,
    Role,
    ToolSchema,
)
from relaymind.adapters.llamacpp import build_chat_request, prompt_fields  # noqa: E402
from relaymind.adapters.manifest import launch_adapter_settings  # noqa: E402
from relaymind.adapters.openai_compat import parse_sse_line  # noqa: E402
from relaymind.evaluation.environment import (  # noqa: E402
    application_revision,
    machine_manifest,
    parse_runtime_log,
    runtime_devices,
)
from relaymind.orchestrator.budgets import LocalBudgets  # noqa: E402
from relaymind.orchestrator.local_loop import LOCAL_OPTIONS, skill_stage_messages  # noqa: E402
from relaymind.orchestrator.router import Router  # noqa: E402
from relaymind.orchestrator.workflows import PARALLEL_WORKFLOWS  # noqa: E402
from relaymind.skills import Skill, SkillRegistry  # noqa: E402
from relaymind.skills.predicates import COMPLETION_PREDICATES  # noqa: E402
from relaymind.tools import CallerContext, ToolRegistry  # noqa: E402
from relaymind.tools.registry import OperatorPolicy  # noqa: E402
from relaymind.tools.sandbox import (  # noqa: E402
    DEMO_TOOL_NAMES,
    READ_PERMISSION,
    SandboxDataService,
    register_demo_tools,
)

REPO_ROOT = SCRIPTS_DIR.parent
KIND = "relaymind.probe.prefix_cache"
LABEL = (
    "single-pass observations from one run on one machine; not a benchmark result "
    "(spec 12.3); never p50 or p95"
)
PURPOSES = ("routing", "skill")
LAYOUTS = ("blocked", "interleaved")
# A cold request on a constrained runtime can take long; a request over this is an error row.
REQUEST_TIMEOUT_SECONDS = 180.0


@dataclass(frozen=True)
class ProbeConfig:
    """A validated probe definition."""

    manifest_path: Path
    skills_root: Path
    sandbox_seed: Path
    skill: str
    warmups: int
    warmup_prompts: Mapping[str, str]
    layouts: tuple[str, ...]
    routing_prompts: tuple[str, ...]
    skill_prompts: tuple[str, ...]

    def prompts(self, purpose: str) -> tuple[str, ...]:
        return self.routing_prompts if purpose == "routing" else self.skill_prompts

    def to_json_object(self) -> dict[str, Any]:
        return {
            "manifest": self.manifest_path.name,
            "skill": self.skill,
            "warmups": self.warmups,
            "warmup_prompts": dict(self.warmup_prompts),
            "layouts": list(self.layouts),
            "routing_prompts": list(self.routing_prompts),
            "skill_prompts": list(self.skill_prompts),
        }


def _prompt_list(raw: Mapping[str, Any], key: str) -> tuple[str, ...]:
    value = raw.get(key)
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item.strip() for item in value)
    ):
        raise ValueError(f"'{key}' must be a non-empty list of non-blank strings")
    if len(set(value)) != len(value):
        raise ValueError(f"'{key}' must hold different texts")
    return tuple(value)


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

    warmups = raw.get("warmups")
    if type(warmups) is not int or warmups < 0:
        raise ValueError("'warmups' must be a nonnegative integer")
    skill = raw.get("skill")
    if not isinstance(skill, str) or not skill:
        raise ValueError("'skill' must be a skill name")
    layouts = raw.get("layouts")
    if (
        not isinstance(layouts, list)
        or not layouts
        or any(layout not in LAYOUTS for layout in layouts)
    ):
        raise ValueError(f"'layouts' must be a non-empty list of {', '.join(LAYOUTS)}")
    warmup_prompts = raw.get("warmup_prompts")
    if not isinstance(warmup_prompts, dict) or set(warmup_prompts) != set(PURPOSES):
        raise ValueError("'warmup_prompts' must name a text for each purpose")
    config = ProbeConfig(
        manifest_path=resolved("manifest"),
        skills_root=resolved("skills_root"),
        sandbox_seed=resolved("sandbox_seed"),
        skill=skill,
        warmups=warmups,
        warmup_prompts=warmup_prompts,
        layouts=tuple(layouts),
        routing_prompts=_prompt_list(raw, "routing_prompts"),
        skill_prompts=_prompt_list(raw, "skill_prompts"),
    )
    for purpose in PURPOSES:
        text = warmup_prompts[purpose]
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"'warmup_prompts.{purpose}' must be a non-blank string")
        if text in config.prompts(purpose):
            raise ValueError(
                f"the {purpose} warm-up text is also measured; a repeated text would be "
                "served whole from the cache"
            )
    return config


# -- the requests the gateway builds -------------------------------------------------------------


@dataclass(frozen=True)
class Surfaces:
    """The gateway's routing and skill-stage request builders over the demo skills."""

    router: Router
    skill: Skill
    skill_tools: tuple[ToolSchema, ...]

    def request(self, purpose: str, text: str, generation_id: str) -> GenerationRequest:
        """The request one purpose sends for one user text, as the gateway builds it."""
        messages = [Message(role=Role.USER, content=text)]
        if purpose == "routing":
            return self.router.build_request(messages, generation_id)
        if purpose == "skill":
            return GenerationRequest(
                generation_id=generation_id,
                messages=tuple(
                    skill_stage_messages(self.skill, messages, [t.name for t in self.skill_tools])
                ),
                tool_schemas=self.skill_tools,
                options=LOCAL_OPTIONS,
            )
        raise ValueError(f"unknown purpose: {purpose}")


def build_surfaces(config: ProbeConfig, *, work_dir: Path) -> Surfaces:
    """The router and the skill stage over the checked-in demo skills and tools (RM06, RM08)."""
    tools = ToolRegistry()
    register_demo_tools(tools, SandboxDataService(config.sandbox_seed, work_dir=work_dir))
    skills = SkillRegistry.load(
        [config.skills_root],
        tools=tools,
        predicates=COMPLETION_PREDICATES.keys(),
        workflows=PARALLEL_WORKFLOWS,
    )
    skill = skills.get(config.skill)
    available = tools.available_tools(
        skill_allowlist=skill.allowed_tools,
        policy=OperatorPolicy(enabled_tools=DEMO_TOOL_NAMES),
        caller=CallerContext(permissions=frozenset({READ_PERMISSION})),
    )
    return Surfaces(
        router=Router(skills, tools, budgets=LocalBudgets()),
        skill=skill,
        skill_tools=tools.tool_schemas(name for name in skill.allowed_tools if name in available),
    )


def request_body(
    request: GenerationRequest, template_kwargs: Mapping[str, object], cache_prompt: bool
) -> dict[str, object]:
    """The body sent: the adapter's encoding of the request, with cache_prompt set to the arm."""
    return {
        **build_chat_request(request, chat_template_kwargs=template_kwargs),
        "cache_prompt": cache_prompt,
    }


# -- the plan --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Step:
    phase: str  # cold, warmup, or measured
    purpose: str
    index: int | None  # position among the measured requests of the purpose
    text: str


def plan_group(config: ProbeConfig, layout: str) -> list[Step]:
    """The requests of one group in send order (the same for both arms)."""
    steps = [Step("cold", purpose, None, config.warmup_prompts[purpose]) for purpose in PURPOSES]
    for _ in range(config.warmups):
        steps += [Step("warmup", p, None, config.warmup_prompts[p]) for p in PURPOSES]
    count = min(len(config.prompts(purpose)) for purpose in PURPOSES)
    if layout == "blocked":
        order = [(p, i) for p in PURPOSES for i in range(count)]
    elif layout == "interleaved":
        order = [(p, i) for i in range(count) for p in PURPOSES]
    else:
        raise ValueError(f"unknown layout: {layout}")
    return steps + [Step("measured", p, i, config.prompts(p)[i]) for p, i in order]


# -- sending and reading ---------------------------------------------------------------------------


def _number(value: object) -> int | float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def row_fields(*, timings: Mapping[str, Any] | None, usage: Mapping[str, Any] | None) -> dict:
    """The counts one response carries; a value the runtime did not return is None (RM16)."""
    timings = timings or {}
    details = (usage or {}).get("prompt_tokens_details")
    cached = details.get("cached_tokens") if isinstance(details, Mapping) else None
    return {
        "timings": None if not timings else dict(timings),
        "usage": None if not usage else dict(usage),
        "cache_n": _number(timings.get("cache_n")),
        "prompt_n": _number(timings.get("prompt_n")),
        "prompt_ms": _number(timings.get("prompt_ms")),
        "predicted_n": _number(timings.get("predicted_n")),
        "predicted_ms": _number(timings.get("predicted_ms")),
        "cached_tokens": _number(cached),
        "prompt_tokens": _number((usage or {}).get("prompt_tokens")),
        "completion_tokens": _number((usage or {}).get("completion_tokens")),
    }


def observe(client: httpx.Client, body: Mapping[str, object]) -> dict[str, Any]:
    """Send one streamed chat completion and read its timings, usage, and output.

    Times are the probe's monotonic clock: first_delta_ms to the first
    content, reasoning, or tool-call delta, wall_ms to the end of the
    stream. A transport failure or an error status is recorded in the row.
    """
    started = time.monotonic()
    timings = usage = finish_reason = first_delta_ms = error = status = None
    text: list[str] = []
    calls: dict[int, dict[str, str]] = {}
    try:
        with client.stream("POST", "/v1/chat/completions", json=dict(body)) as response:
            status = response.status_code
            if status != 200:
                error = f"status {status}: {response.read().decode(errors='replace')[:200]}"
            else:
                for line in response.iter_lines():
                    chunk = parse_sse_line(line)
                    if chunk is None:
                        continue
                    if isinstance(chunk.get("timings"), Mapping):
                        timings = chunk["timings"]
                    if isinstance(chunk.get("usage"), Mapping):
                        usage = chunk["usage"]
                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}
                        if delta.get("content"):
                            text.append(delta["content"])
                        for fragment in delta.get("tool_calls") or []:
                            call = calls.setdefault(
                                int(fragment.get("index") or 0), {"name": "", "arguments": ""}
                            )
                            function = fragment.get("function") or {}
                            call["name"] += str(function.get("name") or "")
                            call["arguments"] += str(function.get("arguments") or "")
                        if first_delta_ms is None and (
                            delta.get("content")
                            or delta.get("reasoning_content")
                            or delta.get("tool_calls")
                        ):
                            first_delta_ms = (time.monotonic() - started) * 1000
                        if choice.get("finish_reason"):
                            finish_reason = choice["finish_reason"]
    except httpx.HTTPError as exc:
        error = f"{type(exc).__name__}: {exc}"
    return {
        "http_status": status,
        "error": error,
        "finish_reason": finish_reason,
        "first_delta_ms": first_delta_ms,
        "wall_ms": (time.monotonic() - started) * 1000,
        "output": {"text": "".join(text) or None, "tool_calls": [calls[i] for i in sorted(calls)]},
        **row_fields(timings=timings, usage=usage),
    }


def count_prompt_tokens(
    client: httpx.Client, request: GenerationRequest, template_kwargs: Mapping[str, object]
) -> list[int] | None:
    """The request's prompt tokens through the runtime's own template and tokenizer.

    The same two calls the adapter's count_tokens makes (spec 4.1); None
    when the runtime does not answer them, and nothing is estimated.
    """
    fields = prompt_fields(request.messages, request.tool_schemas, template_kwargs)
    try:
        templated = client.post("/apply-template", json=fields)
        prompt = templated.json().get("prompt") if templated.status_code == 200 else None
        if not isinstance(prompt, str):
            return None
        tokenized = client.post(
            "/tokenize", json={"content": prompt, "add_special": True, "parse_special": True}
        )
        tokens = tokenized.json().get("tokens") if tokenized.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None
    return tokens if isinstance(tokens, list) else None


def common_prefix_length(token_lists: Sequence[Sequence[int]]) -> int:
    """The length of the longest token prefix that all the lists share."""
    if not token_lists:
        return 0
    shortest = min(len(tokens) for tokens in token_lists)
    for position in range(shortest):
        if any(tokens[position] != token_lists[0][position] for tokens in token_lists):
            return position
    return shortest


def run_group(
    client: httpx.Client,
    surfaces: Surfaces,
    config: ProbeConfig,
    template_kwargs: Mapping[str, object],
    layout: str,
    cache_prompt: bool,
) -> list[dict[str, Any]]:
    """Send one group's requests in order; return one row per request.

    A request that fails is a row with its error, and the pass goes on.
    """
    rows: list[dict[str, Any]] = []
    previous: dict[str, list[int] | None] = {}
    arm = "on" if cache_prompt else "off"
    for position, step in enumerate(plan_group(config, layout)):
        generation_id = f"{layout}_{arm}_{position:02d}_{step.purpose}"
        request = surfaces.request(step.purpose, step.text, generation_id)
        tokens = count_prompt_tokens(client, request, template_kwargs)
        shared = None
        before = previous.get(step.purpose)
        if tokens is not None and before is not None:
            shared = common_prefix_length([before, tokens])
        previous[step.purpose] = tokens
        observed = observe(client, request_body(request, template_kwargs, cache_prompt))
        rows.append(
            {
                "generation_id": generation_id,
                "layout": layout,
                "cache_prompt": cache_prompt,
                "phase": step.phase,
                "purpose": step.purpose,
                "index": step.index,
                "user_text": step.text,
                "prompt_tokens_counted": None if tokens is None else len(tokens),
                "shared_with_previous": shared,
                **observed,
            }
        )
    return rows


def constant_prefixes(
    client: httpx.Client,
    surfaces: Surfaces,
    config: ProbeConfig,
    template_kwargs: Mapping[str, object],
) -> dict[str, dict[str, Any]]:
    """Per purpose: the prefix of tokens shared by all measured requests, and their lengths.

    None for a purpose when the runtime would not count a request.
    """
    found: dict[str, dict[str, Any]] = {}
    for purpose in PURPOSES:
        lists = [
            count_prompt_tokens(client, surfaces.request(purpose, text, "count"), template_kwargs)
            for text in config.prompts(purpose)
        ]
        if any(tokens is None for tokens in lists):
            found[purpose] = {"constant_prefix_tokens": None, "prompt_tokens": None}
            continue
        sizes = [len(tokens) for tokens in lists if tokens is not None]
        found[purpose] = {
            "constant_prefix_tokens": common_prefix_length([t for t in lists if t is not None]),
            "prompt_tokens": {"min": min(sizes), "max": max(sizes)},
        }
    return found


# -- the summary ---------------------------------------------------------------------------------


def _range(values: Sequence[float | int | None]) -> dict[str, float | int] | None:
    present = [v for v in values if v is not None]
    return {"min": min(present), "max": max(present)} if present else None


def summarize(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Per layout, arm, and purpose: the measured requests as ranges and the reused share.

    reused_share is the cached prompt tokens over all prompt tokens
    (cache_n over cache_n plus prompt_n), None when no request returned
    both. These are the range of one pass, never percentiles.
    """
    groups: dict[tuple[str, bool, str], list[Mapping[str, Any]]] = {}
    for row in rows:
        if row["phase"] == "measured":
            groups.setdefault((row["layout"], row["cache_prompt"], row["purpose"]), []).append(row)
    summary = []
    for (layout, cache_prompt, purpose), group in groups.items():
        counted = [r for r in group if r["cache_n"] is not None and r["prompt_n"] is not None]
        total = sum(r["cache_n"] + r["prompt_n"] for r in counted)
        summary.append(
            {
                "layout": layout,
                "cache_prompt": cache_prompt,
                "purpose": purpose,
                "requests": len(group),
                "errors": sum(1 for r in group if r["error"]),
                "reused_share": round(sum(r["cache_n"] for r in counted) / total, 4)
                if total
                else None,
                "cache_n": _range([r["cache_n"] for r in group]),
                "prompt_n": _range([r["prompt_n"] for r in group]),
                "prompt_ms": _range([r["prompt_ms"] for r in group]),
                "predicted_ms": _range([r["predicted_ms"] for r in group]),
                "wall_ms": _range([r["wall_ms"] for r in group]),
                "first_delta_ms": _range([r["first_delta_ms"] for r in group]),
                "cached_tokens_reported": sum(1 for r in group if r["cached_tokens"] is not None),
                "cached_tokens": _range([r["cached_tokens"] for r in group]),
                "shared_with_previous": _range([r["shared_with_previous"] for r in group]),
                "prompt_tokens_counted": _range([r["prompt_tokens_counted"] for r in group]),
            }
        )
    return summary


def cold_requests(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The first request of each purpose after a launch, per group (phase cold)."""
    keys = ("cache_n", "prompt_n", "prompt_ms", "first_delta_ms", "wall_ms", "error")
    return [
        {
            "layout": r["layout"],
            "cache_prompt": r["cache_prompt"],
            "purpose": r["purpose"],
            **{key: r[key] for key in keys},
        }
        for r in rows
        if r["phase"] == "cold"
    ]


# -- the run -------------------------------------------------------------------------------------


def _server_props(client: httpx.Client) -> dict[str, Any] | None:
    try:
        data = client.get("/props").json()
    except (httpx.HTTPError, ValueError):
        return None
    settings = data.get("default_generation_settings") or {}
    return {
        "n_ctx": settings.get("n_ctx"),
        "total_slots": data.get("total_slots"),
        "build_info": data.get("build_info"),
    }


def _format_range(entry: Mapping[str, Any] | None) -> str:
    return "-" if entry is None else f"{entry['min']:g}..{entry['max']:g}"


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("config", type=Path, help="probe definition JSON")
    parser.add_argument("--out", type=Path, required=True, help="export file path")
    harness.add_runtime_extra_args(parser)
    args = parser.parse_args(argv)
    try:
        config = load_probe_config(args.config)
    except ValueError as exc:
        parser.error(str(exc))
    runtime = harness.check_runtime(config.manifest_path, extra_args=args.runtime_extra_args)
    # Read before the run writes its export into the checkout.
    application = application_revision(REPO_ROOT)
    template_kwargs = launch_adapter_settings(runtime.manifest.launch_settings)[1]
    started_at = datetime.now(UTC).isoformat(timespec="seconds")
    work = Path(tempfile.mkdtemp(prefix="relaymind-prefix-cache-"))
    surfaces = build_surfaces(config, work_dir=work / "sandbox")
    rows: list[dict[str, Any]] = []
    launches: list[dict[str, Any]] = []
    prefixes: dict[str, Any] | None = None
    try:
        for layout in config.layouts:
            for cache_prompt in (True, False):
                log = work / f"llama-{layout}-{cache_prompt}.log"
                process, base_url, cold_start = runtime.launch(log)
                try:
                    with httpx.Client(
                        base_url=base_url, timeout=REQUEST_TIMEOUT_SECONDS, trust_env=False
                    ) as client:
                        props = _server_props(client)
                        if prefixes is None:
                            prefixes = constant_prefixes(client, surfaces, config, template_kwargs)
                        print(f"{layout} cache_prompt={cache_prompt}", flush=True)
                        rows += run_group(
                            client, surfaces, config, template_kwargs, layout, cache_prompt
                        )
                finally:
                    harness._stop(process)
                launches.append(
                    {
                        "layout": layout,
                        "cache_prompt": cache_prompt,
                        "cold_start_seconds": round(cold_start, 3),
                        "props": props,
                        "runtime_log": parse_runtime_log(log.read_text("utf-8", errors="replace")),
                    }
                )
    finally:
        shutil.rmtree(work, ignore_errors=True)

    summary = summarize(rows)
    document = {
        "kind": KIND,
        "label": LABEL,
        "command": " ".join(["python", *sys.argv]),
        "started_at_utc": started_at,
        "finished_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "machine": machine_manifest(),
        "application": application,
        "runtime_banner": runtime.banner,
        "runtime_devices": runtime_devices(),
        "llama_server": runtime.server_record(launches[0]["cold_start_seconds"]),
        **runtime.model_record(),
        "definition": config.to_json_object(),
        "request_options": {
            "routing": surfaces.router.options().__dict__,
            "skill": LOCAL_OPTIONS.__dict__,
        },
        "chat_template_kwargs": dict(template_kwargs),
        "example_bodies": {
            purpose: request_body(
                surfaces.request(purpose, config.prompts(purpose)[0], "example"),
                template_kwargs,
                True,
            )
            for purpose in PURPOSES
        },
        "constant_prefixes": prefixes,
        "launches": launches,
        "cold_requests": cold_requests(rows),
        "summary": summary,
        "rows": rows,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", "utf-8")
    print(json.dumps(prefixes, indent=2))
    for entry in summary:
        print(
            f"{entry['layout']:11} cache_prompt={str(entry['cache_prompt']):5} "
            f"{entry['purpose']:7} reused_share={entry['reused_share']} "
            f"cache_n={_format_range(entry['cache_n'])} "
            f"prompt_n={_format_range(entry['prompt_n'])} "
            f"prompt_ms={_format_range(entry['prompt_ms'])} "
            f"cached_tokens_reported={entry['cached_tokens_reported']}/{entry['requests']}"
        )
    print(f"export: {args.out}")


if __name__ == "__main__":
    main()
