"""Frozen selection benchmarks, without tool execution (RM02, RM08, RM09, RM25, RM28).

These are selection measurements in office/coding and web API domains, not the
ops correctness corpus. All errors remain in the score denominator (§12.4).
"""

import asyncio
import hashlib
import json
import random
import re
import time
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

from relaymind.adapters import GenerationRequest, Message, Role, ToolCall
from relaymind.adapters.base import Completed, GenerationFailed, Usage
from relaymind.canonical import canonical_digest
from relaymind.orchestrator.budgets import LocalBudgets
from relaymind.orchestrator.handoff import measure_request
from relaymind.orchestrator.local_loop import EXTERNAL_OPTIONS, LOCAL_OPTIONS, skill_stage_messages
from relaymind.orchestrator.router import Router
from relaymind.selection.contract import (
    RoutingGeneration,
    SelectionContext,
    SelectionError,
    SelectionRequest,
    SelectorError,
    parse_selection_request,
    parse_selection_result,
)
from relaymind.selection.model_router import ModelRouterSelector
from relaymind.skills import Skill, SkillRegistry
from relaymind.tools import SideEffect, ToolDeclaration, ToolRegistry, ToolRejectedError

TOOLRET_DATASET = "mangopy/ToolRet-Training-20w"
TOOLRET_REVISION = "fdf5a317455b1e60785de7ba587496aa6cc878e4"


def file_digest(path: Path) -> str:
    """SHA-256 of exact file bytes, not a reserialized JSON object."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_locked(path: Path, lock_path: Path) -> dict[str, Any]:
    """Refuse a changed benchmark file before a selector sees it."""
    lock = json.loads(lock_path.read_text())
    if file_digest(path) != lock["sha256"]:
        raise ValueError("benchmark file digest differs from lock")
    return json.loads(path.read_text())


def set_metrics(predicted: Iterable[str], expected: Iterable[str]) -> dict[str, float]:
    """Set precision, recall and Jaccard; empty predictions score zero."""
    p, e = set(predicted), set(expected)
    overlap = len(p & e)
    return {
        "precision": overlap / len(p) if p else 0.0,
        "recall": overlap / len(e) if p and e else 0.0,
        "jaccard": overlap / len(p | e) if p else 0.0,
    }


def summarize(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Macro and micro scores, retaining errors, timeouts and bypasses."""
    counts = {"intersection": 0, "predicted": 0, "expected": 0, "union": 0}
    total = dict.fromkeys(("precision", "recall", "jaccard"), 0.0)
    for row in rows:
        p, e = set(row["predicted"]), set(row["expected"])
        for key, value in set_metrics(p, e).items():
            total[key] += value
        for key, value in [
            ("intersection", len(p & e)),
            ("predicted", len(p)),
            ("expected", len(e)),
            ("union", len(p | e)),
        ]:
            counts[key] += value
    n = len(rows)
    proposals = sum(r.get("proposal_count", 0) for r in rows)
    valid_proposals = sum(r.get("proposal_count", 0) - len(r.get("rejections", ())) for r in rows)
    bypasses = sum(r.get("status") == "bypass" for r in rows)
    return {
        "count": n,
        "macro": {k: v / n if n else 0 for k, v in total.items()},
        "micro": {
            k: counts["intersection"] / counts[d] if counts[d] else 0
            for k, d in [("precision", "predicted"), ("recall", "expected"), ("jaccard", "union")]
        },
        "none_rate": sum(r.get("none", False) for r in rows) / n if n else 0,
        "invalid_output_rate": sum(r.get("status") == "invalid_output" for r in rows) / n
        if n
        else 0,
        "bypasses": bypasses,
        "bypass_rate": bypasses / n if n else 0,
        "schema_validity": valid_proposals / proposals if proposals else 0,
        "errors": sum(r.get("status") in ("error", "timeout") for r in rows),
        "completeness": sum(set(r["expected"]) <= set(r["predicted"]) for r in rows) / n
        if n
        else 0,
    }


def skill_catalog(
    data: Mapping[str, Any], case: Mapping[str, Any], *, seed: int, cap: int | None = None
) -> list[dict[str, Any]]:
    """Leave-one-task-out examples, with seeded expected-preserving router cap."""
    entries = list(data["catalog"])
    if cap is not None:
        expected = set(case["expected"])
        if len(expected) > cap:
            raise ValueError("expected skills exceed router catalog cap")
        wanted = [s for s in entries if s["id"] in expected]
        others = [s for s in entries if s["id"] not in expected]
        rng = random.Random(f"{seed}:{case['id']}")
        entries = wanted + rng.sample(others, min(cap - len(wanted), len(others)))
        rng.shuffle(entries)
    catalog = []
    for entry in entries:
        examples = [
            c["request"]
            for c in data["cases"]
            if c["id"] != case["id"] and entry["id"] in c["expected"]
        ]
        catalog.append(catalog_entry(entry["id"], entry["description"], examples=examples))
    return catalog


def catalog_entry(
    name: str, description: str, *, tools: Sequence[str] = (), examples: Sequence[str] = ()
) -> dict[str, Any]:
    """A benchmark skill in the published RM28 request shape."""
    body = {
        "id": name,
        "version": "1",
        "description": description,
        "languages": ["en"],
        "local_eligible": True,
        "allowed_tools": list(tools),
        "examples": list(examples),
    }
    return {**body, "content_hash": canonical_digest(body)}


def selection_request(
    case: Mapping[str, Any], catalog: Sequence[Mapping[str, Any]], timeout: float = 30
) -> SelectionRequest:
    """The source instruction is deliberately absent from the conversation."""
    return parse_selection_request(
        {
            "schema_version": 1,
            "run_id": f"benchmark:{case['id']}",
            "conversation": [{"role": "user", "content": case["request"]}],
            "language": "en",
            "execution_mode": "auto",
            "catalog": list(catalog),
            "permissions": ["benchmark"],
            "deadline_ms": max(1, int(timeout * 1000)),
        }
    )


def synthetic_skill(entry: Mapping[str, Any]) -> Skill:
    """An in-memory package; no dataset instruction is executable."""
    return Skill(
        name=entry["id"],
        description=entry["description"],
        version="1",
        content_hash=entry["content_hash"],
        body="Select the tools needed for the request.",
        languages=("en",),
        local_eligible=True,
        allowed_tools=tuple(entry["allowed_tools"]),
        completion_predicate="tool_result",
        parallel_workflow=None,
        package_dir=Path("."),
        resource_texts={},
    )


class InputBypass(Exception):
    """Measured input exceeds a configured cap; this case stays in the report."""


async def collect_generation(
    adapter: Any, request: GenerationRequest
) -> tuple[list[ToolCall], dict]:
    """Collect one generation. Never dispatch any of its proposed tools."""
    calls, usage, completed = [], {}, False
    try:
        async for event in adapter.generate(request):
            if isinstance(event, ToolCall):
                calls.append(event)
            elif isinstance(event, Usage):
                usage = {"input_tokens": event.input_tokens, "output_tokens": event.output_tokens}
            elif isinstance(event, GenerationFailed):
                if event.code == "invalid_tool_arguments":
                    raise SelectorError(event.message)
                if event.code == "provider_timeout":
                    raise TimeoutError(event.message)
                raise RuntimeError(f"{event.code}: {event.message}")
            elif isinstance(event, Completed):
                completed = True
    except BaseException:
        await adapter.cancel(request.generation_id)
        raise
    if not completed:
        raise RuntimeError("generation ended without completion")
    return calls, usage


class RouterBenchmarkSelector:
    """Build the production router for each catalog and count its actual input."""

    name, version, model_based = "model_router", "1", True

    def __init__(self, adapter: Any, model: str, *, budgets: LocalBudgets | None = None):
        self.adapter, self.model = adapter, model
        self.budgets = budgets or LocalBudgets()
        self.diagnostics: dict[str, Any] = {}

    async def select(
        self, request: SelectionRequest, context: SelectionContext
    ) -> Mapping[str, Any]:
        self.diagnostics = {}
        skills = SkillRegistry((), {e["id"]: synthetic_skill(e) for e in request.catalog})
        router = Router(skills, ToolRegistry(), budgets=self.budgets)
        messages = tuple(Message(Role.USER, c["content"]) for c in request.conversation)
        tokens = await self.adapter.count_tokens(
            router.routing_messages(messages), router.functions()
        )
        self.diagnostics["input_tokens"] = tokens
        if not router.within_input_cap(tokens):
            raise InputBypass("routing_input_tokens")

        async def generate(messages, functions, options):
            calls, usage = await collect_generation(
                self.adapter,
                GenerationRequest(request.run_id, tuple(messages), tuple(functions), options),
            )
            self.diagnostics["usage"] = usage
            return RoutingGeneration(tuple(calls), self.model)

        return await ModelRouterSelector(router).select(
            request, SelectionContext(messages, generate)
        )


async def run_skills(
    data: Mapping[str, Any],
    selector: Any,
    *,
    seed: int,
    timeout: float = 30,
    case_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Score every skill case, including failures, through the RM28 boundary."""
    rows = []
    for case in data["cases"]:
        if case_ids is not None and case["id"] not in case_ids:
            continue
        row = {k: case.get(k) for k in ("id", "category", "difficulty", "expected")}
        row.update(predicted=[], status="ok", none=False)
        started = time.monotonic()
        try:
            if selector.name == "model_router" and len(set(case["expected"])) > 8:
                raise InputBypass("expected skill set exceeds router catalog cap")
            catalog = skill_catalog(
                data, case, seed=seed, cap=8 if selector.name == "model_router" else None
            )
            row["catalog_ids"] = [e["id"] for e in catalog]
            request = selection_request(case, catalog, timeout)
            raw = await asyncio.wait_for(selector.select(request, SelectionContext()), timeout)
            result = parse_selection_result(raw)
            row["selection"] = result.to_json_object()
            if result.candidate.kind == "skill":
                row["predicted"] = sorted({result.candidate.skill_id, *result.alternatives})
                if not set(row["predicted"]) <= {e["id"] for e in catalog}:
                    raise SelectionError("selector returned a skill outside the catalog")
            else:
                row["none"] = result.candidate.kind == "none"
                row["clarify"] = result.candidate.kind == "clarify"
        except InputBypass as exc:
            row.update(status="bypass", error=str(exc))
        except TimeoutError:
            row.update(status="timeout", error="selector timeout")
        except (SelectionError, SelectorError) as exc:
            row.update(status="invalid_output", error=str(exc), predicted=[])
        except Exception as exc:
            row.update(status="error", error=f"{type(exc).__name__}: {exc}")
        row.update(getattr(selector, "diagnostics", {}))
        row["latency_ms"] = (time.monotonic() - started) * 1000
        row["metrics"] = set_metrics(row["predicted"], row["expected"])
        rows.append(row)
    grouped = {}
    for field in ("category", "difficulty"):
        groups = defaultdict(list)
        for row in rows:
            groups[str(row[field])].append(row)
        grouped[f"by_{field}"] = {key: summarize(value) for key, value in sorted(groups.items())}
    return {"cases": rows, "summary": summarize(rows), **grouped}


_TYPE_ALIASES = {
    "str": "string",
    "int": "integer",
    "float": "number",
    "bool": "boolean",
    "dict": "object",
    "list": "array",
}


def _normalize_schema(source: Any) -> Any:
    """Translate dataset type aliases only in schema positions, never const data."""
    if not isinstance(source, dict):
        return source
    schema = dict(source)
    typ = schema.get("type")
    if isinstance(typ, str):
        schema["type"] = _TYPE_ALIASES.get(typ.lower(), typ.lower())
    elif isinstance(typ, list):
        schema["type"] = [_TYPE_ALIASES.get(t.lower(), t.lower()) for t in typ]
    for key in ("properties", "patternProperties", "$defs", "definitions", "dependentSchemas"):
        if isinstance(schema.get(key), dict):
            schema[key] = {name: _normalize_schema(value) for name, value in schema[key].items()}
    for key in ("items", "additionalProperties", "contains", "not", "if", "then", "else"):
        if key in schema:
            schema[key] = _normalize_schema(schema[key])
    for key in ("allOf", "anyOf", "oneOf", "prefixItems"):
        if isinstance(schema.get(key), list):
            schema[key] = [_normalize_schema(value) for value in schema[key]]
    return schema


def _parameters(raw: Mapping[str, Any]) -> dict[str, Any]:
    parameters = raw.get("parameters")
    if isinstance(parameters, str):
        parameters = json.loads(parameters)
    if isinstance(parameters, dict) and (
        "properties" in parameters or parameters.get("type") == "object"
    ):
        schema = _normalize_schema(parameters)
        schema.update(type="object", additionalProperties=False)
        if "required" not in schema and isinstance(raw.get("required"), list):
            schema["required"] = list(raw["required"])
        return schema
    properties, required = {}, []
    if isinstance(parameters, dict):
        items = [
            {**(v if isinstance(v, dict) else {"type": v}), "name": k}
            for k, v in parameters.items()
        ]
    elif isinstance(parameters, list):
        items = parameters
    else:
        items = [dict(p, required=True) for p in raw.get("required_parameters", [])]
        items += [dict(p, required=False) for p in raw.get("optional_parameters", [])]
    aliases = {
        "str": "string",
        "int": "integer",
        "float": "number",
        "bool": "boolean",
        "dict": "object",
        "list": "array",
    }
    for p in items:
        typ = str(p.get("type", "string")).lower()
        typ = aliases.get(typ, typ)
        if typ not in ("string", "number", "integer", "boolean", "object", "array", "null"):
            typ = "string"
        prop = {"type": typ}
        if "description" in p:
            prop["description"] = str(p["description"])
        if "enum" in p:
            prop["enum"] = p["enum"]
        if typ == "array":
            prop["items"] = p.get("items", {})
        properties[p["name"]] = prop
        if p.get("required", False) or p["name"] in raw.get("required", []):
            required.append(p["name"])
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def normalize_toolret(row: Mapping[str, Any], row_index: int) -> dict[str, Any]:
    """Normalize both ToolRet tool styles, keeping a recorded collision mapping."""
    tools, expected, mapping, used = [], [], [], set()
    for positive, source in [(True, row["positive"]), (False, row["negative"])]:
        for value in source:
            raw = json.loads(value) if isinstance(value, str) else dict(value)
            if "function" in raw:
                raw = raw["function"]
            original = str(raw.get("name", raw.get("api_name", "tool")))
            base = re.sub("[^a-z0-9_]", "_", original.lower())
            if not base or not base[0].isalpha():
                base = "tool_" + base
            base = base[:64]
            name, counter = base, 0
            while name in used:
                counter += 1
                suffix = f"_{counter}"
                name = base[: 64 - len(suffix)] + suffix
            used.add(name)
            tools.append(
                {
                    "name": name,
                    "description": str(
                        raw.get("description", raw.get("api_description", "")) or original
                    ),
                    "input_schema": _parameters(raw),
                }
            )
            mapping.append({"original": original, "normalized": name, "positive": positive})
            if positive:
                expected.append(name)
    return {
        "id": str(row["id"]),
        "row_index": row_index,
        "request": row["query"],
        "instruction": row.get("instruction", row.get("prompt")),
        "tools": tools,
        "expected": expected,
        "name_mapping": mapping,
    }


def derive_toolret(
    rows: Iterable[Mapping[str, Any]], *, revision: str, seed: int, size: int = 500
) -> tuple[list[dict], dict]:
    """Equal-stratum reservoir sample, deterministic in source shard/row order."""
    if size < 3:
        raise ValueError("sample size must cover all three strata")
    rng = random.Random(seed)
    quotas = [size // 3 + (i < size % 3) for i in range(3)]
    buckets: list[list] = [[], [], []]
    seen = [0, 0, 0]
    for index, row in enumerate(rows):
        n = len(row["positive"])
        if not n:
            raise ValueError("source row has no positives")
        group = 0 if n == 1 else 1 if n <= 3 else 2
        seen[group] += 1
        if len(buckets[group]) < quotas[group]:
            buckets[group].append((index, row))
        else:
            position = rng.randrange(seen[group])
            if position < quotas[group]:
                buckets[group][position] = (index, row)
    if any(len(b) != q for b, q in zip(buckets, quotas, strict=True)):
        raise ValueError("not enough rows in every stratum")
    cases = [normalize_toolret(row, i) for i, row in sorted(sum(buckets, []), key=lambda p: p[0])]
    lock = {
        "schema_version": 1,
        "dataset": TOOLRET_DATASET,
        "revision": revision,
        "seed": seed,
        "size": size,
        "sampling": "equal-stratum-reservoir-v1",
        "strata": ["1", "2-3", "4+"],
        "population": seen,
        "quotas": quotas,
        "license": "not stated; no source rows redistributed",
        "cases": [
            {"id": c["id"], "row_index": c["row_index"], "sha256": canonical_digest(c)}
            for c in cases
        ],
    }
    return cases, lock


def verify_toolret(cases: list[dict], lock: Mapping[str, Any], *, revision: str) -> list[dict]:
    """Verify revision, exact sample order, IDs, and every normalized case digest."""
    if revision != lock["revision"]:
        raise ValueError("ToolRet revision differs from lock")
    actual = [
        {"id": c["id"], "row_index": c["row_index"], "sha256": canonical_digest(c)} for c in cases
    ]
    if actual != lock["cases"]:
        raise ValueError("ToolRet case identity or digest differs from lock")
    return cases


def benchmark_registry(tools: Sequence[Mapping[str, Any]]) -> ToolRegistry:
    """Register declarations for schema validation only; handlers refuse execution."""

    def forbidden(*args, **kwargs):
        raise RuntimeError("benchmark handlers never execute")

    registry = ToolRegistry()
    for tool in tools:
        registry.register(
            ToolDeclaration(
                name=tool["name"],
                description=tool["description"],
                input_schema=tool["input_schema"],
                output_schema={"type": "object"},
                timeout_seconds=1,
                side_effect=SideEffect.READ_ONLY,
                handler=forbidden,
                required_permission="benchmark",
            )
        )
    return registry


def score_proposals(
    case: Mapping[str, Any],
    calls: Sequence[ToolCall],
    registry: ToolRegistry,
    offered: Sequence[str],
) -> dict[str, Any]:
    """Score proposed names independently from argument schema validity (RM09)."""
    rejections = []
    for call in calls:
        try:
            registry.validate(call.tool_name, call.arguments, available=set(offered))
        except ToolRejectedError as exc:
            rejections.append(exc.code)
    predicted = sorted({c.tool_name for c in calls})
    return {
        "predicted": predicted,
        "expected": list(case["expected"]),
        "metrics": set_metrics(predicted, case["expected"]),
        "rejections": rejections,
        "schema_validity": (len(calls) - len(rejections)) / len(calls) if calls else 0.0,
        "proposal_count": len(calls),
    }


async def run_tools(
    cases: Sequence[Mapping[str, Any]],
    selector: Any,
    *,
    adapter: Any = None,
    timeout: float = 30,
    context_limit: int = 4096,
    calls_per_generation: str = "many",
    seed: int = 42,
) -> dict[str, Any]:
    """Narrowing plus full/narrowed first-generation proposals. Never execute."""
    narrowing, full, narrowed = [], [], []
    for case in cases:
        presented = list(case["tools"])
        random.Random(f"{seed}:{case['id']}").shuffle(presented)
        names = [t["name"] for t in presented]
        # Descriptions and schemas travel as data in the synthetic skill description,
        # since the published RM28 catalog carries tool names rather than declarations.
        entry = catalog_entry(
            "benchmark_case", json.dumps(presented, ensure_ascii=False), tools=names
        )
        row = {
            "id": case["id"],
            "expected": case["expected"],
            "predicted": [],
            "status": "ok",
            "instruction": case["instruction"],
            "offered": names,
        }
        started = time.monotonic()
        try:
            if selector.name == "model_router":
                row.update(predicted=names, baseline="model_router keeps all candidates")
            else:
                result = parse_selection_result(
                    await asyncio.wait_for(
                        selector.select(
                            selection_request(case, [entry], timeout), SelectionContext()
                        ),
                        timeout,
                    )
                )
                row["selection"] = result.to_json_object()
                if result.candidate.kind == "skill":
                    if result.candidate.skill_id != entry["id"]:
                        raise SelectionError("unknown synthetic skill")
                    row["predicted"] = (
                        names
                        if result.tools is None
                        else [name for name in names if name in result.tools]
                    )
        except TimeoutError:
            row.update(status="timeout", error="selector timeout")
        except (SelectionError, SelectorError) as exc:
            row.update(status="invalid_output", error=str(exc))
        except Exception as exc:
            row.update(status="error", error=f"{type(exc).__name__}: {exc}")
        row["latency_ms"] = (time.monotonic() - started) * 1000
        row["metrics"] = set_metrics(row["predicted"], row["expected"])
        narrowing.append(row)
        if adapter is None:
            continue
        for label, offered, output in [
            ("full", names, full),
            ("narrowed", row["predicted"], narrowed),
        ]:
            proposal = {
                "id": case["id"],
                "expected": case["expected"],
                "predicted": [],
                "status": "ok",
                "offered": list(offered),
                "schema_validity": 0,
                "proposal_count": 0,
            }
            started = time.monotonic()
            try:
                registry = benchmark_registry(case["tools"])
                skill = synthetic_skill(
                    catalog_entry("benchmark_case", "Choose tools for the request.", tools=names)
                )
                messages = tuple(
                    skill_stage_messages(skill, [Message(Role.USER, case["request"])], offered)
                )
                schemas = registry.tool_schemas(offered)
                if adapter.locality == "local":
                    tokens = await asyncio.wait_for(
                        adapter.count_tokens(messages, schemas), timeout
                    )
                    proposal["input_tokens"] = tokens
                    if tokens > context_limit:
                        raise InputBypass("model_context_limit")
                else:
                    capabilities = await asyncio.wait_for(adapter.capabilities(), timeout)
                    size = await asyncio.wait_for(
                        measure_request(adapter, capabilities, messages, schemas), timeout
                    )
                    proposal["input_size"] = size.to_json_object()
                    if not size.fits:
                        raise InputBypass("model_context_limit")
                request = GenerationRequest(
                    f"benchmark:{case['id']}:{label}",
                    messages,
                    schemas,
                    LOCAL_OPTIONS if adapter.locality == "local" else EXTERNAL_OPTIONS,
                )
                proposal["generation_options"] = asdict(request.options)
                calls, usage = await asyncio.wait_for(collect_generation(adapter, request), timeout)
                proposal.update(score_proposals(case, calls, registry, offered), usage=usage)
                if calls_per_generation == "one" and len(calls) > 1:
                    proposal.update(status="invalid_output", call_budget_exceeded=True)
            except InputBypass as exc:
                proposal.update(status="bypass", error=str(exc))
            except TimeoutError:
                proposal.update(status="timeout", error="generation timeout")
            except SelectorError as exc:
                proposal.update(status="invalid_output", error=str(exc))
            except Exception as exc:
                proposal.update(status="error", error=f"{type(exc).__name__}: {exc}")
            proposal["latency_ms"] = (time.monotonic() - started) * 1000
            proposal["metrics"] = set_metrics(proposal["predicted"], proposal["expected"])
            output.append(proposal)
    return {
        key: {"cases": rows, "summary": summarize(rows)}
        for key, rows in [
            ("narrowing", narrowing),
            ("proposal_full", full),
            ("proposal_narrowed", narrowed),
        ]
    }
