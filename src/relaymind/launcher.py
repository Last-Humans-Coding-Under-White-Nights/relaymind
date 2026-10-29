"""The operator launcher: ``relaymind serve --config PATH`` (spec 0.21 section 14.1).

One TOML file wires the models, skill roots, sandbox, run store, API
settings, policy, and UI into the gateway. Startup is all-or-nothing:
every check that can fail runs before the server socket exists, and a
failure is one explicit ``LaunchError`` message, never a traceback.
Restart recovery (section 5) runs exactly once, after the orchestrator
is built and before the server accepts a run. The warm-up of the local
model (``placement.warmup``, section 6) and its detection (``placement.detect``,
RM26) are a separate step, ``warm_up_gateway``, that ``main`` runs after
recovery and before the address and the token are printed; building the app
never sends a generation. The local_model table is optional (RM26): without
it the gateway runs without a local model, allowed_modes defaults to auto and
external_only, and the selector chain to example_match.

The only command line setting is the file. A credential is never in the
file or on the command line: the file names the environment variable
(``api_key_env``) and the external adapter reads it at call time
(RM12). The application token is printed once to standard output and
written nowhere else.
"""

import argparse
import asyncio
import math
import sqlite3
import sys
import tomllib
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any, NoReturn

from fastapi import FastAPI

from relaymind.adapters import ModelAdapter
from relaymind.adapters.chat_completions import ChatCompletionsAdapter, ChatCompletionsConfig
from relaymind.adapters.llamacpp import REQUEST_TIMEOUT_SECONDS, LlamaCppAdapter, LlamaCppConfig
from relaymind.adapters.local_http import is_loopback_url
from relaymind.adapters.manifest import (
    ManifestError,
    launch_adapter_settings,
    load_manifest,
    verify_artifact,
)
from relaymind.api.app import ChatSettings, NativeSettings, create_app
from relaymind.api.chat import ClientToolsChat
from relaymind.api.chat_request import SAMPLING_OPTIONS
from relaymind.api.server import serve, validate_bind_host
from relaymind.assets.service import AssetCatalogService
from relaymind.assets.settings import (
    ADAPTATION_FIELDS,
    ASSET_FIELDS,
    HYBRID_FIELDS,
    RECOMMENDATION_FIELDS,
    REFRESH_FIELDS,
    AssetSettings,
    RecommendationSettings,
    build_asset_selector,
    parse_asset_settings,
    parse_recommendation_settings,
)
from relaymind.orchestrator.budgets import LocalBudgets
from relaymind.orchestrator.local_loop import ConfiguredModel, LocalOrchestrator
from relaymind.orchestrator.local_status import LocalStatus
from relaymind.orchestrator.performance import PerformanceSettings
from relaymind.orchestrator.placement import PlacementSettings
from relaymind.orchestrator.prefetch import PrefetchBudgetError, check_prefetch_budgets
from relaymind.orchestrator.recovery import RecoveryError
from relaymind.orchestrator.route_decision import ExecutionMode
from relaymind.orchestrator.routing_cache import RoutingCache
from relaymind.orchestrator.warmup import WarmupResult, detect_local_model, warm_up
from relaymind.orchestrator.workflows import PARALLEL_WORKFLOWS
from relaymind.selection import EXAMPLE_MATCH, MODEL_ROUTER, SelectionSettings
from relaymind.selection.asset_contract import AssetSelector
from relaymind.skills import SkillConfigError, SkillRegistry, default_skill_roots
from relaymind.skills.predicates import COMPLETION_PREDICATES
from relaymind.store import RunStore
from relaymind.store.asset_catalog import AssetCatalogStore
from relaymind.tools import CallerContext, OperatorPolicy, ToolRegistry
from relaymind.tools.sandbox import (
    DEMO_TOOL_NAMES,
    MUTATE_PERMISSION,
    READ_PERMISSION,
    SandboxDataService,
    SandboxFixtureError,
    register_demo_tools,
)
from relaymind.ui import UiSettings

__all__ = ["LaunchError", "LauncherConfig", "Launched", "build_app", "cli", "load_config", "main"]

DEFAULT_SANDBOX_SEED = "tests/fixtures/sandbox/baseline.json"
DEFAULT_SANDBOX_DIR = ".relaymind/sandbox"
DEFAULT_STORE_PATH = "relaymind.sqlite3"
DEFAULT_LOCAL_URL = "http://127.0.0.1:8033"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8080
DEFAULT_WARMUP_SECONDS = 30.0
DEFAULT_PERMISSIONS = (READ_PERMISSION, MUTATE_PERMISSION)
# Section 14.1: the modes a gateway without a local_model table may permit (RM26).
NO_LOCAL_MODES = (ExecutionMode.AUTO, ExecutionMode.EXTERNAL_ONLY)
KNOWN_PERMISSIONS = frozenset(DEFAULT_PERMISSIONS)

_TABLES: dict[str, frozenset[str]] = {
    "recommendations": RECOMMENDATION_FIELDS,
    "assets": ASSET_FIELDS,
    "asset_refresh": REFRESH_FIELDS,
    "asset_hybrid": HYBRID_FIELDS,
    "asset_adaptation": ADAPTATION_FIELDS,
    "server": frozenset({"host", "port"}),
    "store": frozenset({"path"}),
    "skills": frozenset({"roots"}),
    "sandbox": frozenset({"seed", "work_dir"}),
    "local_model": frozenset({"manifest", "artifact", "base_url"}),
    "external_model": frozenset(
        {
            "base_url",
            "model",
            "context_limit",
            "api_key_env",
            "supported_options",
            "request_options",
        }
    ),
    "policy": frozenset({"degraded_mode", "enabled_workflows", "allowed_modes", "permissions"}),
    "cache": frozenset({"routing"}),
    "chat": frozenset({"max_completion_tokens", "supported_options"}),
    "native": frozenset(
        {"poll_seconds", "heartbeat_seconds", "sweep_seconds", "cancel_wait_seconds"}
    ),
    "ui": frozenset({"mode", "arm_wait_seconds", "poll_seconds"}),
    # Spec 0.31 section 14.1: the keys are the section 11 limit names, so a new limit
    # added to LocalBudgets is configurable without a second list (RM03).
    "budgets": frozenset(f.name for f in fields(LocalBudgets)),
    # Spec 0.32 section 14.1: the keys are the PerformanceSettings fields (RM31, RM32).
    "performance": frozenset(f.name for f in fields(PerformanceSettings)),
    # Spec 0.31 section 14.1: the warm-up keys and the local model check (RM26). The
    # RM27: complete placement table; defaults retain baseline behavior.
    "placement": frozenset(
        {
            "warmup",
            "warmup_seconds",
            "detect",
            "detect_stages",
            "policy",
            "margin",
            "probe_after_seconds",
        }
    ),
    # Spec 0.32 section 14.1: the keys are the SelectionSettings fields (RM28).
    "selection": frozenset(f.name for f in fields(SelectionSettings)),
}


class LaunchError(Exception):
    """The gateway cannot start; the message names the cause (section 14.1)."""


@dataclass(frozen=True)
class LauncherConfig:
    """A validated configuration file. It holds no credential, only a variable name.

    manifest and artifact are None when the file has no local_model table (RM26).
    """

    manifest: Path | None = None
    artifact: Path | None = None
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    store_path: Path = Path(DEFAULT_STORE_PATH)
    skill_roots: tuple[str, ...] = field(default_factory=default_skill_roots)
    sandbox_seed: Path = Path(DEFAULT_SANDBOX_SEED)
    sandbox_dir: Path = Path(DEFAULT_SANDBOX_DIR)
    local_url: str = DEFAULT_LOCAL_URL
    external: ChatCompletionsConfig | None = None
    degraded_mode: bool = False
    enabled_workflows: frozenset[str] = frozenset()
    # Spec 0.26 section 10: the routing-result cache; off by default and in the benchmark.
    routing_cache: bool = False
    permissions: frozenset[str] = frozenset(DEFAULT_PERMISSIONS)
    chat: ChatSettings = field(default_factory=ChatSettings)
    native: NativeSettings = field(default_factory=NativeSettings)
    ui: UiSettings | None = None
    # Spec 0.31 section 14.1: the section 11 limits; an omitted key keeps its default (RM03).
    budgets: LocalBudgets = field(default_factory=LocalBudgets)
    # Spec 0.32 section 14.1: the performance options; every default is the baseline.
    performance: PerformanceSettings = field(default_factory=PerformanceSettings)
    # Spec 0.39 section 14.1: the placement policy, margin and probe age (RM27, section 14.2).
    placement: PlacementSettings = field(default_factory=PlacementSettings)
    # Spec 0.31 section 14.1: the startup warm-up of the local model (section 6).
    warmup: bool = True
    warmup_seconds: float = DEFAULT_WARMUP_SECONDS
    # Spec 0.31 section 14.1: classify the local model at startup and in readiness (RM26).
    detect: bool = True
    # Spec 0.48 section 14.4.7: the stage rule and the skill-shaped startup check (RM26).
    detect_stages: bool = False
    # Spec 0.31 section 14.1: the selector chain; the default is model_router alone (RM28).
    selection: SelectionSettings = field(default_factory=SelectionSettings)
    assets: AssetSettings = field(default_factory=AssetSettings)
    recommendations: RecommendationSettings = field(default_factory=RecommendationSettings)


@dataclass(frozen=True)
class Launched:
    """A gateway ready to serve: recovery has already run.

    new_local_adapter makes a fresh adapter for the local runtime, or is None
    when the caller passed a model double. The warm-up uses it so that no
    httpx client serves two event loops. warmup holds the timings once
    ``warm_up_gateway`` has run, and is None before that and with warmup false.
    """

    app: FastAPI
    orchestrator: LocalOrchestrator
    recovered: int
    new_local_adapter: Callable[[], ModelAdapter] | None = None
    warmup: WarmupResult | None = None
    asset_selector: AssetSelector | None = None


# -- reading the file ---------------------------------------------------------------


def _table(raw: Mapping[str, Any], name: str) -> dict[str, Any]:
    table = raw.get(name, {})
    if not isinstance(table, dict):
        raise LaunchError(f"{name} must be a table")
    unknown = sorted(set(table) - _TABLES[name])
    if unknown:
        hint = (
            " (the credential comes from the environment: set api_key_env)"
            if ("api_key" in unknown)
            else ""
        )
        raise LaunchError(f"unknown key in [{name}]: {', '.join(unknown)}{hint}")
    return table


def _value(table: Mapping[str, Any], name: str, key: str, kind: type | tuple, default: Any = None):
    value = table.get(key, default)
    if value is None:
        return None
    kinds = kind if isinstance(kind, tuple) else (kind,)
    # bool is an int subclass; a flag is never a number.
    if (isinstance(value, bool) and bool not in kinds) or not isinstance(value, kinds):
        raise LaunchError(f"[{name}] {key} has the wrong type")
    return value


def _required(table: Mapping[str, Any], name: str, key: str, kind: type | tuple):
    value = _value(table, name, key, kind)
    if value is None:
        raise LaunchError(f"[{name}] {key} is required")
    return value


def _positive(table: Mapping[str, Any], name: str, key: str, kind: type | tuple, default=None):
    value = _value(table, name, key, kind, default)
    if value is not None and not value > 0:
        raise LaunchError(f"[{name}] {key} must be positive")
    return value


def _strings(table: Mapping[str, Any], name: str, key: str, default: list[str]) -> list[str]:
    value = _value(table, name, key, list, default)
    if not all(isinstance(item, str) and item.strip() for item in value):
        raise LaunchError(f"[{name}] {key} must be a list of non-blank strings")
    return value


def _seconds(table: Mapping[str, Any], name: str, keys: Iterable[str]) -> dict[str, float]:
    """The duration keys present in the table, as positive floats."""
    return {key: float(_positive(table, name, key, (int, float))) for key in keys if key in table}


def _settings(name: str, build: Callable[[], Any]) -> Any:
    try:
        return build()
    except (ValueError, TypeError) as exc:
        raise LaunchError(f"[{name}] {exc}") from exc


def load_config(path: Path | str) -> LauncherConfig:
    """Read and validate a configuration file (spec 0.21 section 14.1)."""
    path = Path(path)
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        reason = getattr(exc, "strerror", None) or exc
        raise LaunchError(f"cannot read {path}: {reason}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise LaunchError(f"{path} is not valid TOML: {exc}") from exc
    unknown = sorted(set(raw) - set(_TABLES))
    if unknown:
        raise LaunchError(f"unknown table: {', '.join(unknown)}")
    tables = {name: _table(raw, name) for name in _TABLES}
    assets = _settings(
        "assets",
        lambda: parse_asset_settings(
            tables["assets"], tables["asset_refresh"], config_dir=path.absolute().parent
        ),
    )
    recommendations = _settings(
        "recommendations",
        lambda: parse_recommendation_settings(
            tables["recommendations"],
            source_count=len(assets.sources),
            hybrid=tables["asset_hybrid"],
            adaptation=tables["asset_adaptation"],
        ),
    )
    server, local = tables["server"], tables["local_model"]
    policy, chat, native = tables["policy"], tables["chat"], tables["native"]
    # Section 14.1 (RM26): the local_model table is optional, but one model table is needed.
    has_local = "local_model" in raw
    if not has_local and "external_model" not in raw:
        raise LaunchError(
            "the file has neither a [local_model] nor an [external_model] table;"
            " configure at least one model"
        )

    default_modes = ExecutionMode if has_local else NO_LOCAL_MODES
    modes = _strings(policy, "policy", "allowed_modes", [m.value for m in default_modes])
    try:
        allowed = frozenset(ExecutionMode(m) for m in modes)
    except ValueError as exc:
        raise LaunchError(f"[policy] allowed_modes: {exc}") from exc
    if not has_local and (needing := sorted(m.value for m in allowed - set(NO_LOCAL_MODES))):
        raise LaunchError(
            f"[policy] allowed_modes names {', '.join(needing)}, which need a [local_model] table"
        )
    options = _strings(chat, "chat", "supported_options", sorted(SAMPLING_OPTIONS))
    chat_args: dict[str, Any] = {"allowed_modes": allowed, "supported_options": frozenset(options)}
    if (limit := _positive(chat, "chat", "max_completion_tokens", int)) is not None:
        chat_args["max_completion_tokens"] = limit
    native_args = _seconds(native, "native", _TABLES["native"])

    ui = None
    if "ui" in raw:
        ui_table = tables["ui"]
        ui_args = _seconds(ui_table, "ui", ("arm_wait_seconds", "poll_seconds"))
        mode = _required(ui_table, "ui", "mode", str)
        ui = _settings("ui", lambda: UiSettings(mode=mode, **ui_args))

    port = _value(server, "server", "port", int, DEFAULT_PORT)
    if not 1 <= port <= 65535:
        raise LaunchError("[server] port must be between 1 and 65535")
    permissions = frozenset(_strings(policy, "policy", "permissions", list(DEFAULT_PERMISSIONS)))
    if unknown_permissions := sorted(permissions - KNOWN_PERMISSIONS):
        raise LaunchError(f"[policy] permissions names unknown: {', '.join(unknown_permissions)}")

    budgets = _settings("budgets", lambda: LocalBudgets(**tables["budgets"]))
    performance = _settings("performance", lambda: PerformanceSettings(**tables["performance"]))
    placement = tables["placement"]
    warmup = _value(placement, "placement", "warmup", bool, True)
    warmup_seconds = _value(
        placement, "placement", "warmup_seconds", (int, float), DEFAULT_WARMUP_SECONDS
    )
    if not math.isfinite(warmup_seconds) or not warmup_seconds > 0:
        raise LaunchError("[placement] warmup_seconds must be a positive number")
    detect = _value(placement, "placement", "detect", bool, True)
    # Spec 0.48 section 14.4.7: off by default, the spec 0.47 check (RM26).
    detect_stages = _value(placement, "placement", "detect_stages", bool, False)
    placement_settings = _settings(
        "placement",
        lambda: PlacementSettings(
            **{k: v for k, v in placement.items() if k not in ("warmup", "detect", "detect_stages")}
        ),
    )
    selection = _selection(tables["selection"], has_local=has_local)

    return LauncherConfig(
        manifest=Path(_required(local, "local_model", "manifest", str)) if has_local else None,
        artifact=Path(_required(local, "local_model", "artifact", str)) if has_local else None,
        local_url=_local_url(local),
        host=_value(server, "server", "host", str, DEFAULT_HOST),
        port=port,
        store_path=Path(_value(tables["store"], "store", "path", str, DEFAULT_STORE_PATH)),
        skill_roots=tuple(
            _strings(tables["skills"], "skills", "roots", list(default_skill_roots()))
        ),
        sandbox_seed=Path(_value(tables["sandbox"], "sandbox", "seed", str, DEFAULT_SANDBOX_SEED)),
        sandbox_dir=Path(
            _value(tables["sandbox"], "sandbox", "work_dir", str, DEFAULT_SANDBOX_DIR)
        ),
        external=_external(tables["external_model"]) if "external_model" in raw else None,
        degraded_mode=_value(policy, "policy", "degraded_mode", bool, False),
        routing_cache=_value(tables["cache"], "cache", "routing", bool, False),
        enabled_workflows=frozenset(_strings(policy, "policy", "enabled_workflows", [])),
        permissions=permissions,
        chat=_settings("chat", lambda: ChatSettings(**chat_args)),
        native=_settings("native", lambda: NativeSettings(**native_args)),
        ui=ui,
        budgets=budgets,
        performance=performance,
        placement=placement_settings,
        assets=assets,
        recommendations=recommendations,
        warmup=warmup,
        warmup_seconds=float(warmup_seconds),
        detect=detect,
        detect_stages=detect_stages,
        selection=selection,
    )


def _selection(table: Mapping[str, Any], *, has_local: bool) -> SelectionSettings:
    """The [selection] table (RM28), without contacting configured HTTP endpoints.

    Without a local model (RM26) the default chain is example_match, and
    model_router, which needs the local model, is a startup error.
    """
    args = dict(table)
    if not has_local:
        args.setdefault("chain", [EXAMPLE_MATCH])
    if "chain" in table:
        chain = _value(table, "selection", "chain", list)
        if not has_local and MODEL_ROUTER in chain:
            raise LaunchError(
                "[selection] chain names model_router, which needs a [local_model] table;"
                " use example_match"
            )
        args["chain"] = chain
    return _settings("selection", lambda: SelectionSettings(**args))


def _local_url(table: Mapping[str, Any]) -> str:
    """The llama-server URL; a remote one would break the local-first evidence (RM01)."""
    url = _value(table, "local_model", "base_url", str, DEFAULT_LOCAL_URL)
    try:
        loopback = is_loopback_url(url)
    except ValueError:  # httpx.InvalidURL is a ValueError
        loopback = False
    if not loopback:
        raise LaunchError("[local_model] base_url must be a URL on localhost or a loopback address")
    return url


def _external(table: Mapping[str, Any]) -> ChatCompletionsConfig:
    args: dict[str, Any] = {
        "base_url": _required(table, "external_model", "base_url", str),
        "model": _required(table, "external_model", "model", str),
        "context_limit": _required(table, "external_model", "context_limit", int),
    }
    _positive(table, "external_model", "context_limit", int)
    if (env := _value(table, "external_model", "api_key_env", str)) is not None:
        args["api_key_env"] = env
    if "supported_options" in table:
        args["supported_options"] = tuple(
            _strings(table, "external_model", "supported_options", [])
        )
    if (options := _value(table, "external_model", "request_options", dict)) is not None:
        # Checked by the config: no credential, header, or gateway-owned field, and JSON only.
        args["request_options"] = options
    return _settings("external_model", lambda: ChatCompletionsConfig(**args))


# -- building the gateway -----------------------------------------------------------


def _local_model(
    config: LauncherConfig, adapter: ModelAdapter | None
) -> tuple[ConfiguredModel | None, Callable[[], ModelAdapter] | None]:
    """The local model, and a maker of fresh adapters for it (None for an injected double).

    Both are None when no local model is configured (RM26).
    """
    if config.manifest is None or config.artifact is None:
        return None, None
    try:
        manifest = load_manifest(config.manifest)
        verify_artifact(manifest, config.artifact)
        context, kwargs = launch_adapter_settings(manifest.launch_settings)
    except ManifestError as exc:
        raise LaunchError(f"local model: {exc}") from exc
    maker: Callable[[], ModelAdapter] | None = None
    if adapter is None:
        adapter_config = LlamaCppConfig(
            base_url=config.local_url,
            context_limit=context,
            chat_template_kwargs=kwargs,
            # llama-server sends nothing until prefill ends, so a raised
            # active_seconds needs a read timeout at least as long (RM03).
            request_timeout_seconds=max(REQUEST_TIMEOUT_SECONDS, config.budgets.active_seconds),
        )

        def maker() -> ModelAdapter:
            return LlamaCppAdapter(adapter_config)

        adapter = maker()
    return ConfiguredModel(adapter=adapter, model_id=manifest.model_id), maker


def build_app(
    config: LauncherConfig,
    *,
    local_adapter: ModelAdapter | None = None,
    external_adapter: ModelAdapter | None = None,
) -> Launched:
    """Check everything, wire the gateway, and run restart recovery once.

    The adapter arguments replace the real adapters with model doubles;
    the launcher never accepts them from the file.
    """
    try:
        validate_bind_host(config.host)
    except ValueError as exc:
        raise LaunchError(str(exc)) from exc
    unknown = sorted(config.enabled_workflows - PARALLEL_WORKFLOWS)
    if unknown:
        raise LaunchError(
            f"[policy] enabled_workflows names unknown workflow: {', '.join(unknown)}"
        )
    local, new_local_adapter = _local_model(config, local_adapter)
    external = None
    if config.external is not None:
        external = ConfiguredModel(
            adapter=external_adapter
            or ChatCompletionsAdapter(
                # A raised external_seconds needs a read timeout at least as long (RM03).
                replace(
                    config.external,
                    request_timeout_seconds=max(
                        config.external.request_timeout_seconds,
                        config.budgets.external_seconds,
                    ),
                )
            ),
            model_id=config.external.model,
        )
    # The skills name tools, so the sandbox and its tools come first.
    tools = ToolRegistry()
    try:
        sandbox = SandboxDataService(config.sandbox_seed, work_dir=config.sandbox_dir)
    except (OSError, ValueError, SandboxFixtureError) as exc:  # JSONDecodeError is a ValueError
        raise LaunchError(f"sandbox: {exc}") from exc
    register_demo_tools(tools, sandbox)
    try:
        skills = SkillRegistry.load(
            config.skill_roots,
            tools=tools,
            predicates=COMPLETION_PREDICATES.keys(),
            workflows=PARALLEL_WORKFLOWS,
        )
    except SkillConfigError as exc:
        raise LaunchError(f"skills: {exc}") from exc
    if config.performance.prefetch:
        # RM29: declared calls that cannot fit the configured limits stop startup, before
        # the run store is opened. The orchestrator checks again for other callers.
        try:
            check_prefetch_budgets(skills, config.budgets)
        except PrefetchBudgetError as exc:
            raise LaunchError(f"[performance] prefetch: {exc}") from exc
    try:
        config.store_path.parent.mkdir(parents=True, exist_ok=True)
        store = RunStore(config.store_path)
    except (OSError, sqlite3.Error) as exc:
        raise LaunchError(f"run store {config.store_path}: {exc}") from exc
    try:
        orchestrator = LocalOrchestrator(
            local,
            skills=skills,
            tools=tools,
            store=store,
            policy=OperatorPolicy(enabled_tools=DEMO_TOOL_NAMES),
            caller=CallerContext(permissions=config.permissions),
            enabled_workflows=config.enabled_workflows,
            external=external,
            degraded_mode=config.degraded_mode,
            budgets=config.budgets,
            performance=config.performance,
            routing_cache=RoutingCache() if config.routing_cache else None,
            selection=config.selection,
            # RM26: readiness repeats the availability check when detection is on.
            detect=config.detect,
            detect_stages=config.detect_stages,
            placement=config.placement,
        )
        report = orchestrator.recover_after_restart()
        catalog = None
        if config.recommendations.enabled:
            catalog = AssetCatalogService(
                config.assets,
                AssetCatalogStore(store),
                busy=lambda: (
                    store.active_run_id() is not None or store.probe_reservation() is not None
                ),
            )
        ui = None if config.ui is None else replace(config.ui, fixtures=sandbox)
        app = create_app(
            orchestrator=orchestrator,
            client_tools=ClientToolsChat(orchestrator),
            settings=config.chat,
            native=config.native,
            ui=ui,
            asset_catalog=catalog,
        )
        return Launched(
            app=app,
            orchestrator=orchestrator,
            recovered=len(report.runs),
            new_local_adapter=new_local_adapter,
            asset_selector=build_asset_selector(config.recommendations)
            if config.recommendations.enabled
            else None,
        )
    except RecoveryError as exc:
        store.close()
        raise LaunchError(f"restart recovery failed: {exc}") from exc
    except BaseException:
        store.close()
        raise


# -- the warm-up --------------------------------------------------------------------


def warm_up_gateway(
    config: LauncherConfig,
    launched: Launched,
    *,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> Launched:
    """Classify and warm up the local model, once (RM26, section 6).

    Called by ``main`` after restart recovery and before the address and the
    token are printed. It creates no run. With placement.detect true the
    local model is checked first and its class is set on the orchestrator
    (unavailable or unsuitable when it cannot run or cannot help); with
    detect false only the warm-up runs, as before spec 0.31. The timings are
    kept in ``Launched.warmup``. With a model double the double (the
    orchestrator's own adapter) is used; otherwise a short-lived adapter of
    its own does the work in its own event loop and is closed there, so the
    client that serves the app's loop is never used from two loops. A
    runtime that does not answer is a result, never an exception. Without
    a local model, or with warmup and detect false, nothing is sent.
    sleep is the wait between health checks, a test seam.
    """
    model = launched.orchestrator.model
    if model is None or not (config.warmup or config.detect):
        return launched
    if launched.new_local_adapter is not None:
        adapter, owned = launched.new_local_adapter(), True
    else:
        adapter, owned = model.adapter, False

    async def work() -> tuple[LocalStatus | None, WarmupResult | None]:
        try:
            if config.detect:
                return await detect_local_model(
                    launched.orchestrator,
                    adapter,
                    warmup=config.warmup,
                    seconds=config.warmup_seconds,
                    sleep=sleep,
                )
            return None, await warm_up(
                launched.orchestrator, adapter, seconds=config.warmup_seconds
            )
        finally:
            if owned:
                await adapter.aclose()

    # The catalog worker starts only with the served app's lifespan, after this.
    status, result = asyncio.run(work())
    if status is not None:
        launched.orchestrator.set_local_status(status)
    return replace(launched, warmup=result)


# -- the command --------------------------------------------------------------------


class _ArgumentParser(argparse.ArgumentParser):
    """Keep argparse diagnostics from echoing untrusted argument values (RM12)."""

    def error(self, message: str) -> NoReturn:
        # Both this parser and its subparsers use this handler, including
        # errors argparse raises before returning any parsed arguments.
        super().error("invalid arguments: use relaymind serve --config PATH")


def _parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(
        prog="relaymind", description="The RelayMind gateway.", allow_abbrev=False
    )
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser(
        "serve", help="start the gateway on a loopback address", allow_abbrev=False
    )
    start.add_argument("--config", required=True, type=Path, help="path of the TOML config file")
    return parser


def _parse(argv: list[str] | None) -> argparse.Namespace:
    """Parse the command line without echoing a refused argument (it may be a credential)."""
    parser = _parser()
    return parser.parse_args(argv)


def _out(line: str) -> None:
    print(line, flush=True)


def _err(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


def main(
    argv: list[str] | None = None,
    *,
    serve_fn: Callable[..., None] = serve,
    stdout: Callable[[str], None] = _out,
    stderr: Callable[[str], None] = _err,
    local_adapter: ModelAdapter | None = None,
    external_adapter: ModelAdapter | None = None,
) -> int:
    """Run the command; return the exit status (0 after a normal stop, 2 on a refused start).

    The keyword arguments are test seams: a server function and output
    sinks, and model doubles for the adapters.
    """
    args = _parse(argv)
    try:
        config = load_config(args.config)
        launched = build_app(config, local_adapter=local_adapter, external_adapter=external_adapter)
    except LaunchError as exc:
        stderr(f"relaymind: cannot start: {exc}")
        return 2
    if launched.recovered:
        stdout(
            f"Ended {launched.recovered} unfinished run(s) from an earlier process as interrupted."
        )
    # Section 14.1: after recovery, before the socket. A runtime that is not ready in time
    # is one printed line, not a startup error (section 6).
    launched = warm_up_gateway(config, launched)
    if launched.warmup is not None:
        if launched.warmup.ready:
            stdout(launched.warmup.summary())
        else:
            stderr(f"relaymind: local warm-up did not finish: {launched.warmup.detail}")
    # RM26: one line names the class and the cause when the local model cannot run or help.
    local = launched.orchestrator.local_status
    if not local.usable:
        stdout(local.line())
    host = validate_bind_host(config.host)
    url_host = f"[{host}]" if ":" in host else host
    stdout(f"Serving on http://{url_host}:{config.port}")
    # The one place the token is written (RM12, section 14.1).
    stdout(f"Application token: {launched.app.state.application_token}")
    serve_fn(launched.app, host=config.host, port=config.port)
    return 0


def cli() -> None:
    """The ``relaymind`` console script."""
    raise SystemExit(main())
