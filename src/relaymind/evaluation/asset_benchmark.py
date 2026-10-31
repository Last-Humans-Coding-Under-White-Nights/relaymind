"""Production-service retrieval benchmark, disposable storage and exports (RM35/40).

A locked projection provider supplies evaluation metadata only. It does not
fabricate host availability or qualify a source. The production service owns
transport, coordination, result validation, admission and recommendation state.
"""

import asyncio
import copy
import hashlib
import sys
import tempfile
import tomllib
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from relaymind.assets.catalog import CatalogSnapshot, validate_asset_record
from relaymind.canonical import canonical_digest, canonical_json
from relaymind.evaluation.asset_benchmark_io import (
    CONFIG_BYTES,
    REPORT_BYTES,
    atomic_bytes,
    json_bytes,
    read_json_path,
    validate_benchmark_document,
)
from relaymind.evaluation.asset_corpus import assess_asset_release, load_asset_release
from relaymind.evaluation.asset_corpus_io import (
    ORIGINAL_BYTES,
    AssetCorpusError,
    document,
    matches_original,
    read_local,
    relative_path,
)
from relaymind.evaluation.asset_evidence import (
    OfflineResolver,
    SourcePolicyReview,
    VerifiedBenchmarkInputs,
    collect_benchmark_environment,
    effective_asset_config,
    effective_selector_identity,
    file_evidence_id,
    file_inventory,
    semantic_config_documents,
    source_proof,
    validate_definition,
    verify_benchmark_inputs,
)
from relaymind.evaluation.asset_metrics import grade_asset_result, summarize_asset_rows
from relaymind.evaluation.asset_native_runtime import NativeBenchmarkRuntime
from relaymind.evaluation.asset_schedule import plan_asset_slots, validate_asset_rows
from relaymind.evaluation.environment import application_revision
from relaymind.launcher import parse_launcher_config
from relaymind.recommendations.errors import RecommendationError
from relaymind.recommendations.service import CancellationSignal, RecommendationService
from relaymind.selection.asset_contract import SelectorContext
from relaymind.selection.asset_factory import build_asset_selector
from relaymind.selection.asset_lexical import LexicalAssetSelector, OfferCache
from relaymind.selection.asset_preparation import (
    PreparedAssetSelector,
    normalize_preparation,
    preparation_binding,
    prepare_asset_index,
)
from relaymind.store.recommendations import RecommendationClaim
from relaymind.store.run_store import RunStore, TraceClock


def benchmark_repository_root() -> Path:
    """Locate source inventories explicitly in a checkout, never infer compare inputs."""
    candidates = [*Path(__file__).resolve().parents, *Path(sys.argv[0]).absolute().parents]
    for parent in candidates:
        if (parent / "pyproject.toml").is_file() and (
            parent / "scripts/asset_selection_benchmark.py"
        ).is_file():
            return parent
    raise AssetCorpusError("runner_inventory_unavailable")


def benchmark_inventories() -> dict:
    root = benchmark_repository_root()
    inventories = dict(
        runner_inventory=file_inventory(root),
        protocol_inventory=file_inventory(root, protocol=True),
    )
    import relaymind

    imported_root = Path(relaymind.__file__).absolute().parent
    # An editable install imports the very files just hashed; reading them a
    # second time cannot disagree, so only a separate installation is reread.
    same_tree = imported_root.resolve() == (root / "src/relaymind").resolve()
    expected = set()
    for item in inventories["runner_inventory"]["files"]:
        if item["path"].startswith("src/relaymind/"):
            relative = item["path"].removeprefix("src/relaymind/")
            expected.add(relative)
            if same_tree:
                continue
            payload = read_local(imported_root, relative, CONFIG_BYTES)
            if not matches_original(payload, item["sha256"], item["bytes"]):
                raise AssetCorpusError("installed_runner_mismatch")
    actual = {
        p.relative_to(imported_root).as_posix()
        for p in imported_root.rglob("*.py")
        if "__pycache__" not in p.parts
    }
    if expected != actual:
        raise AssetCorpusError("installed_runner_mismatch")
    return inventories


@dataclass(frozen=True)
class AssetBenchmarkConfig:
    path: Path
    data: Mapping
    production: Any
    release: Any
    environment: Mapping
    effective: Mapping
    inventories: Mapping
    environment_lock: Mapping


def _load_production_config(path: Path):
    """Parse one bounded snapshot without relocating its semantic/source roots."""
    path = Path(path).absolute()
    payload = read_local(path.parent, path.name, CONFIG_BYTES)
    try:
        raw = tomllib.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeError):
        raise AssetCorpusError("invalid_production_config") from None
    if raw.get("user_actions"):
        raise AssetCorpusError("evaluation_action_credentials_forbidden")
    production = parse_launcher_config(raw, config_dir=path.parent)
    if production.asset_semantics is not None and production.asset_semantics.adaptation_enabled:
        raise AssetCorpusError("adaptation_unavailable")
    return production


def _load_benchmark_release(lock_path: Path):
    """Resolve declared links only within the explicitly selected release root.

    The benchmark config supplies one local root. Custodians stage related locks
    there at their exact declared bounded paths; no alias causes an outside read
    or a directory/digest search. The shared corpus loader validates the closure.
    """
    lock_path = Path(lock_path).absolute()
    root = lock_path.parent
    pending = [lock_path.name]
    visited = set()
    scheduled = {lock_path.name}
    committed_documents = set()
    aliases = {}
    while pending:
        relative = pending.pop()
        if relative in visited:
            continue
        if len(visited) >= 10000:
            raise AssetCorpusError("closure_limit")
        visited.add(relative)
        lock = document(root, relative, "lock")
        committed_documents.add(("lock", lock["digest"]))
        committed_documents.update(
            (kind, lock[kind]["digest"]) for kind in ("catalog", "cases", "labels")
        )
        if len(committed_documents) > 10000:
            raise AssetCorpusError("closure_limit")
        for reference in lock["related_locks"]:
            relative_path(reference["path"])
            aliases[reference["id"]] = root
            if reference["path"] not in scheduled:
                if len(scheduled) >= 10000:
                    raise AssetCorpusError("closure_limit")
                scheduled.add(reference["path"])
                pending.append(reference["path"])
    return load_asset_release(lock_path, evidence_root=root, related_roots=aliases)


def included_cases(release, split: str) -> tuple[dict, dict]:
    """Included labels by case ID, and the labeled cases of one split."""
    labels = {
        label["case_id"]: label
        for label in release.labels["adjudications"]
        if label["disposition"] == "include"
    }
    cases = {
        c["id"]: c for c in release.cases["cases"] if c["id"] in labels and c["split"] == split
    }
    return labels, cases


def _benchmark_requested_kinds(release, split: str) -> tuple[str, ...]:
    _, cases = included_cases(release, split)
    envelopes = {tuple(sorted(case["requested_kinds"])) for case in cases.values()}
    if len(envelopes) > 1:
        raise AssetCorpusError("heterogeneous_requested_kinds")
    return next(iter(envelopes), ("mcp_tool", "skill"))


def _effective_config(production, release, inventories: Mapping, split: str) -> dict:
    return effective_asset_config(
        production,
        release,
        runner_digest=canonical_digest(inventories["runner_inventory"]),
        protocol_digest=canonical_digest(inventories["protocol_inventory"]),
        requested_kinds=_benchmark_requested_kinds(release, split),
    )


def load_benchmark_config(path: Path) -> AssetBenchmarkConfig:
    """Validate bounded TOML and the existing production launcher configuration."""
    path = Path(path).absolute()
    payload = read_local(path.parent, path.name, CONFIG_BYTES)
    try:
        raw = tomllib.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeError):
        raise AssetCorpusError("invalid_benchmark_config") from None
    validate_benchmark_document("config", raw)
    for key in ("lock", "production_config", "environment_lock"):
        relative_path(raw[key])
        read_local(path.parent, raw[key], CONFIG_BYTES)
    production = _load_production_config(path.parent / raw["production_config"])
    normalize_preparation(
        raw.get("preparation"),
        http_primary=production.recommendations.primary == "http",
        local_primary=production.recommendations.primary == "hybrid",
    )
    if raw["track"] == "primary_only" and production.recommendations.fallback:
        raise AssetCorpusError("primary_policy")
    release = _load_benchmark_release(path.parent / raw["lock"])
    if release.lock["digest"] != raw["lock_digest"]:
        raise AssetCorpusError("lock_digest")
    environment, _ = read_json_path(path.parent / raw["environment_lock"])
    validate_definition("Environment", environment)
    if canonical_digest(environment) != raw["environment_lock_digest"]:
        raise AssetCorpusError("environment_digest")
    observed = collect_benchmark_environment(production=production)
    for key, value in environment.items():
        if value is not None and value != observed[key]:
            raise AssetCorpusError("environment_lock_mismatch")
    inventories = benchmark_inventories()
    effective = _effective_config(production, release, inventories, raw["split"])
    if canonical_digest(effective) != raw["production_config_digest"]:
        raise AssetCorpusError("production_config_digest")
    # No benchmark-only authorization or arbitrary subset is introduced.
    return AssetBenchmarkConfig(
        path, raw, production, release, observed, effective, inventories, environment
    )


class _LockedProjectionProvider:
    """Explicit evaluation-only metadata injection, never qualified host evidence."""

    def __init__(self, release, effective):
        self.scope = {k: effective["recommendations"][k] for k in ("user_id", "workspace_id")}
        self.permissions = tuple(effective["permissions"])
        self.eligible = {e["asset_id"] for e in effective["eligibility"] if e["eligible"]}
        records = tuple(validate_asset_record(a["record"]) for a in release.catalog["assets"])
        # Revision zero is the real initial empty publication. A nonempty
        # locked evaluation publication has its own fixed revision one.
        revision = 1 if any(r.asset_id in self.eligible for r in records) else 0
        self.snapshot = CatalogSnapshot(1, revision, "2026-01-01T00:00:00Z", (), records)
        self.cache = OfferCache()

    def policy(self, record):
        return record.asset_id in self.eligible

    def current(self):
        return self.snapshot

    def pin(self, kinds):
        return self.cache.get("discovery", kinds, catalog_revision=self.snapshot.catalog_revision)

    def stop(self):
        self.cache.invalidate()

    async def publish(self):
        clock = TraceClock()
        await self.cache.publish(
            self.snapshot,
            scope=self.scope,
            permissions=self.permissions,
            policy=self.policy,
            context=SelectorContext(clock.monotonic, lambda: None),
        )


async def execute_asset_slot(service, provider, normal, case, slot, label):
    """Run one planned slot through the production offer and service.

    Returns ``(row, status, recommendation_id)``: ``status`` is ``None`` or the
    run status the slot forces (``interrupted`` or ``blocked``), and
    ``recommendation_id`` names the stored recommendation when one exists.
    Shared by the retrieval benchmark and the section 12.6.5 episode driver.
    """
    status = None
    cancel = None
    recommendation_id = None
    request = dict(
        schema_version=1,
        task_text=case["task_text"],
        conversation=list(case["conversation"]),
        requested_kinds=normal["requested_kinds"],
        selection_scope=normal["selection_scope"],
        host_snapshot_revision=None,
        language_tags=list(case["language_tags"]),
        code_switched=case["code_switched"],
    )
    budget = service.new_budget()
    row = _not_run(slot, label)
    try:
        offer = service.precheck(request)
        # Same production offer.request envelope as service.create;
        # volatile ID/deadline fields alone are removed from digest.
        envelope = dict(
            request,
            schema_version=2,
            recommendation_id=str(uuid.uuid4()),
            task_id=canonical_digest(
                dict(
                    task_text=request["task_text"],
                    conversation=request["conversation"],
                )
            ),
            scope=provider.scope,
            permissions=list(provider.permissions),
            catalog_revision=offer.catalog_revision,
            deadline_ms=budget.remaining_ms(),
            candidate_limit=normal["budgets"]["candidate_limit"],
        )
        selection = offer.request(
            envelope,
            context=SelectorContext(budget.clock.monotonic, budget.check),
        )
        row.update(
            request_digest=stable_request_digest(selection.to_json_object()),
            offered_projection_digest=canonical_digest(list(offer.catalog)),
            catalog_revision=offer.catalog_revision,
        )
        claim = RecommendationClaim(
            provider.scope,
            "evaluation",
            "POST",
            "/relay/v1/recommendations",
            None,
            str(slot["ordinal"]),
            canonical_digest(request),
        )
        cancel = CancellationSignal()
        response = await service.create(request, claim, budget, cancel)
        body = (
            dict(service.get(provider.scope, cancel.recommendation_id))
            if cancel.recommendation_id is not None
            else dict(response.body)
        )
        recommendation_id = body.get("recommendation_id")
        if body.get("state") == "ready":
            result = dict(body["result"])
            row.update(
                status="ready",
                reason=None,
                result=result,
                attempts=list(result["attempts"]),
                embeddings=list(result["embeddings"]),
            )
        else:
            terminal = terminal_payload(
                service,
                provider.scope,
                body["recommendation_id"],
                ("failed", "cancelled"),
            )
            row.update(
                status="cancelled" if body.get("state") == "cancelled" else "failed",
                reason=(body.get("error") or {}).get("code", "recommendation_unavailable"),
                attempts=list(terminal["attempts"]),
                embeddings=list(terminal.get("embeddings", [])),
            )
        row["observed_ms"] = budget.elapsed_ms()
    except RecommendationError as failure:
        row.update(
            status="failed",
            reason=failure.code,
            observed_ms=budget.elapsed_ms(),
        )
    except asyncio.CancelledError:
        row.update(
            status="cancelled",
            reason="cancelled",
            observed_ms=budget.elapsed_ms(),
        )
        if cancel.recommendation_id is not None:
            terminal = terminal_payload(
                service,
                provider.scope,
                cancel.recommendation_id,
                ("cancelled",),
            )
            row.update(
                attempts=list(terminal["attempts"]),
                embeddings=list(terminal.get("embeddings", [])),
            )
        status = "interrupted"
    except Exception:
        row.update(
            status="failed",
            reason="internal_error",
            observed_ms=budget.elapsed_ms(),
        )
        status = "blocked"
    except KeyboardInterrupt:
        row.update(
            status="cancelled",
            reason="cancelled",
            observed_ms=budget.elapsed_ms(),
        )
        status = "interrupted"
    # Actual observed no-dispatch is zero inference only for the
    # admitted initial empty catalog, never an unavailable catalog.
    if row["result"] is not None and row["result"]["decision"] == "empty_catalog":
        row["observed_costs"] = [
            dict(
                provenance="observed",
                value=dict(LexicalAssetSelector.observed_cost),
            )
        ]
    row["grade"] = grade_asset_result(label, row["result"])
    if recommendation_id is None and cancel is not None:
        recommendation_id = cancel.recommendation_id
    return row, status, recommendation_id


@dataclass
class AssetBenchmarkReport:
    data: dict
    config: AssetBenchmarkConfig | None = field(default=None, repr=False)
    inputs: VerifiedBenchmarkInputs | None = field(default=None, repr=False)
    input_error: str | None = field(default=None, repr=False)

    def to_json_object(self) -> dict:
        return copy.deepcopy(self.data)


def terminal_payload(service, scope, recommendation_id, event_types) -> dict:
    """The last terminal event payload of one recommendation, or no evidence."""
    events = service.events(scope, recommendation_id, after_seq=0, limit=1000)["events"]
    return next(
        (e["payload"] for e in reversed(events) if e["event_type"] in event_types),
        dict(attempts=[], embeddings=[]),
    )


def _not_run(slot, label, reason="interrupted") -> dict:
    return dict(
        slot=dict(slot),
        status="not_run",
        reason=reason,
        request_digest=None,
        offered_projection_digest=None,
        catalog_revision=None,
        result=None,
        attempts=[],
        embeddings=[],
        observed_ms=None,
        observed_costs=[],
        resources=dict(cpu_ms=None, peak_bytes=None, coverage="unknown"),
        grade=grade_asset_result(label, None),
    )


def _normal(config: AssetBenchmarkConfig) -> dict:
    effective = config.effective
    s = config.production.recommendations
    selector = effective_selector_identity(effective)
    return dict(
        **{k: config.data[k] for k in ("split", "track", "seed", "repetitions", "warmups")},
        scope={k: getattr(s, k) for k in ("user_id", "workspace_id")},
        permissions=effective["permissions"],
        selection_scope=effective["selection_scope"],
        requested_kinds=effective["requested_kinds"],
        selector=selector,
        budgets=effective["budgets"],
        fallback=s.fallback,
        adaptation_enabled=False,
        adaptation_config_digest=None,
        production_config_digest=canonical_digest(effective),
        preparation=normalize_preparation(
            config.data.get("preparation"),
            http_primary=s.primary == "http",
            local_primary=s.primary == "hybrid",
        ),
    )


def _offer_binding(provider: _LockedProjectionProvider, normal: Mapping) -> tuple[dict, list]:
    """The preparation binding of the pinned production offer, and its catalog."""
    offer = provider.pin(normal["requested_kinds"])
    catalog = list(offer.catalog)
    binding = preparation_binding(
        selector={k: normal["selector"][k] for k in ("name", "version")},
        catalog_revision=offer.catalog_revision,
        catalog=catalog,
        scope=normal["scope"],
        permissions=normal["permissions"],
        selection_scope=normal["selection_scope"],
        requested_kinds=normal["requested_kinds"],
    )
    return binding, catalog


def stable_request_digest(request: Mapping) -> str:
    """Digest of a v2 request without its volatile ID and deadline fields."""
    return canonical_digest(
        {
            k: v
            for k, v in request.items()
            if k not in ("recommendation_id", "task_id", "deadline_ms")
        }
    )


async def prepare_benchmark_index(
    config: AssetBenchmarkConfig, *, interrupted=lambda: False
) -> dict:
    """Explicit standalone preparation, using the same pinned production offer."""
    normal = _normal(config)
    if normal["preparation"]["mode"] == "none":
        return await prepare_asset_index(normal["preparation"], None, [])
    if normal["preparation"]["mode"] == "local":
        runtime = await NativeBenchmarkRuntime.create(
            config.production,
            config.release,
            config.effective,
            requested_kinds=normal["requested_kinds"],
        )
        try:
            return await runtime.prepare_local(
                normal["preparation"],
                requested_kinds=normal["requested_kinds"],
                cancelled=interrupted,
            )
        finally:
            await runtime.close()
    provider = _LockedProjectionProvider(config.release, config.effective)
    try:
        await provider.publish()
        binding, catalog = _offer_binding(provider, normal)
        return await prepare_asset_index(
            normal["preparation"], binding, catalog, cancelled=interrupted
        )
    finally:
        provider.stop()


async def run_asset_benchmark(
    config: AssetBenchmarkConfig, *, interrupted: Callable[[], bool] = lambda: False
) -> AssetBenchmarkReport:
    """Attempt each preregistered slot once, conserving interrupted work."""
    normal = _normal(config)
    labels, cases = included_cases(config.release, normal["split"])
    slots = plan_asset_slots(
        cases, seed=normal["seed"], repetitions=normal["repetitions"], warmups=normal["warmups"]
    )
    qualification = assess_asset_release(config.release)
    observed = collect_benchmark_environment(production=config.production)
    if any(v is not None and observed[k] != v for k, v in config.environment.items()):
        raise AssetCorpusError("environment_lock_mismatch")
    if benchmark_inventories() != config.inventories:
        raise AssetCorpusError("runner_changed")
    provider = _LockedProjectionProvider(config.release, config.effective)
    rows = []
    status = "completed"
    preparation = await prepare_asset_index(normalize_preparation(None), None, [])
    primary = None
    runtime = None
    try:
        try:
            if config.production.asset_semantics is not None:
                runtime = await NativeBenchmarkRuntime.create(
                    config.production,
                    config.release,
                    config.effective,
                    requested_kinds=normal["requested_kinds"],
                )
                provider = runtime.provider
                if normal["preparation"]["mode"] == "local":
                    preparation = await runtime.prepare_local(
                        normal["preparation"],
                        requested_kinds=normal["requested_kinds"],
                        cancelled=interrupted,
                    )
                    if preparation["status"] not in ("ready", "not_applicable"):
                        status = (
                            "interrupted" if preparation["status"] == "cancelled" else "blocked"
                        )
                    elif preparation["status"] == "ready":
                        primary = PreparedAssetSelector(runtime.service.primary, preparation)
                else:
                    # Native inference/index construction requires explicit local
                    # preparation. The ordinary lexical floor still has its cache.
                    await runtime.worker.rebuild()
            else:
                await provider.publish()
            if normal["preparation"]["mode"] == "http":
                offer = provider.pin(normal["requested_kinds"])
                catalog = list(offer.catalog)
                binding = preparation_binding(
                    selector={k: normal["selector"][k] for k in ("name", "version")},
                    catalog_revision=offer.catalog_revision,
                    catalog=catalog,
                    scope=normal["scope"],
                    permissions=normal["permissions"],
                    selection_scope=normal["selection_scope"],
                    requested_kinds=normal["requested_kinds"],
                )
                preparation = await prepare_asset_index(
                    normal["preparation"], binding, catalog, cancelled=interrupted
                )
                if preparation["status"] != "ready":
                    status = "interrupted" if preparation["status"] == "cancelled" else "blocked"
                else:
                    primary = PreparedAssetSelector(
                        build_asset_selector(config.production.recommendations), preparation
                    )
        except Exception:
            status = "blocked"
        try:
            with tempfile.TemporaryDirectory(prefix="asset-benchmark-") as directory:
                runs = (
                    runtime.runs
                    if runtime is not None
                    else RunStore(Path(directory) / "evaluation.sqlite3")
                )
                service = (
                    runtime.service
                    if runtime is not None
                    else RecommendationService.for_provider(
                        runs,
                        provider,
                        config.production.recommendations,
                    )
                )
                if primary is not None:
                    service.primary = primary
                try:
                    for slot in slots:
                        label = labels[slot["case_id"]]
                        if status != "completed" or interrupted():
                            if status == "completed":
                                status = "interrupted"
                            rows.append(
                                _not_run(
                                    slot, label, "blocked" if status == "blocked" else "interrupted"
                                )
                            )
                            continue
                        case = cases[slot["case_id"]]
                        row, forced, _ = await execute_asset_slot(
                            service, provider, normal, case, slot, label
                        )
                        if forced is not None:
                            status = forced
                        rows.append(row)
                finally:
                    if runtime is None:
                        await service.stop()
                        runs.close()
        except Exception:
            status = "blocked"
            for slot in slots[len(rows) :]:
                rows.append(_not_run(slot, labels[slot["case_id"]], "blocked"))
    finally:
        if runtime is not None:
            await runtime.close()
    validate_asset_rows(slots, rows)
    root = benchmark_repository_root()
    revision = application_revision(root)
    limits = {
        "projection_fixture_injection",
        "retrieval_costs_only",
        "resource_coverage_unknown",
        "source_policy_review_unverified",
        "cost_coverage_unknown",
    }
    if normal["preparation"]["mode"] == "local":
        limits.add("native_static_validation_outside_timing")
    if any(row["request_digest"] is None for row in rows):
        limits.add("offer_observation_incomplete")
    if qualification.status != "qualified":
        limits.add("qualification_incomplete")
    if config.environment["gpu"] is None:
        limits.add("environment_coverage_unknown")
    data = dict(
        schema_version=1,
        benchmark="asset_selection",
        created_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        status=status,
        qualification=qualification.status,
        qualification_evidence_ids=list(config.release.lock["qualification_evidence_ids"]),
        lock_digest=config.release.lock["digest"],
        input_manifest=dict(path=None, digest="0" * 64, bytes=0),
        config=normal,
        config_digest=canonical_digest(normal),
        preparation=preparation,
        environment=config.environment,
        environment_digest=canonical_digest(config.environment),
        implementation=dict(
            checkout=revision["head"] or "unknown",
            dirty=revision["dirty"],
            runner_digest=canonical_digest(config.inventories["runner_inventory"]),
            protocol_digest=canonical_digest(config.inventories["protocol_inventory"]),
        ),
        artifacts=copy.deepcopy(config.effective["artifacts"]),
        planned_slots=slots,
        results=rows,
        summaries=summarize_asset_rows(
            config.release,
            normal,
            rows,
            qualification=qualification.status,
            effective=config.effective,
        ),
        limits=sorted(limits),
    )
    validate_benchmark_document("report", data)
    report = AssetBenchmarkReport(data, config=config)
    # The library result itself has a real companion commitment and verified
    # snapshot; a disk writer later binds the same inputs to its chosen locator.
    with tempfile.TemporaryDirectory(prefix="asset-report-inputs-") as directory:
        # This root was just created and is owned by this call. Canonicalize
        # OS temp aliases (for example macOS /var), never caller artifact paths.
        temporary_root = Path(directory)
        root = temporary_root.parent.resolve(strict=True) / temporary_root.name
        if root.is_symlink():
            raise AssetCorpusError("unsafe_file")
        write_asset_benchmark_report(report, root / "capture.json")
    report.data["input_manifest"]["path"] = None
    return report


def _write_bundle(report: AssetBenchmarkReport, path: Path) -> dict:
    config = report.config
    if config is None:
        raise AssetCorpusError("input_unresolved")
    path = Path(path).absolute()
    base = path.parent
    bundle = path.name + ".assets"
    private = base / (path.name + ".private")
    if private.is_symlink():
        raise AssetCorpusError("unsafe_file")
    private.mkdir(mode=0o700, parents=True, exist_ok=True)
    private.chmod(0o700)
    documents = []
    evidence = []
    resolver_entries = []

    def save_document(id, kind, document, *, public=True):
        payload = (
            canonical_json(document).encode("utf-8")
            if kind == "referenced_config"
            else json_bytes(document)
        )
        digest = canonical_digest(document)
        relative = (
            f"{bundle}/documents/{kind}-{digest}.json"
            if public
            else f"{private.name}/documents/{kind}-{digest}.json"
        )
        atomic_bytes(
            base / relative,
            payload,
            limit=65536 if kind == "referenced_config" else 64 * 1024 * 1024,
        )
        documents.append(
            dict(
                id=id,
                kind=kind,
                path=relative if public else None,
                digest=digest,
                bytes=len(payload),
                format="json",
            )
        )
        if not public:
            resolver_entries.append(
                dict(id=id, hash_kind="canonical", hash=digest, bytes=len(payload), path=relative)
            )

    normalized = dict(
        config.data,
        lock="lock.json",
        production_config="production.toml",
        environment_lock="environment.json",
    )
    save_document("benchmark_config", "benchmark_config", normalized)
    save_document("effective_config", "effective_config", config.effective)
    save_document("environment_lock", "environment_lock", config.environment_lock)
    for id, value in sorted(semantic_config_documents(config.production).items()):
        # Embedded config locators/provenance may be private. Preserve exact
        # preimages through the explicit resolver, never a public launcher dump.
        save_document(id, "referenced_config", value, public=False)
    for kind, value in config.inventories.items():
        save_document(kind, kind, value)
    all_releases = [config.release, *config.release.related_releases]
    seen_documents = set()
    seen_evidence = {}
    for release in all_releases:
        for kind in ("lock", "catalog", "cases", "labels"):
            document = getattr(release, kind)
            digest = canonical_digest(document)
            id = kind + ":" + digest
            if id in seen_documents:
                continue
            seen_documents.add(id)
            # Private labels/prompts never enter a public bundle. Fixture cases
            # are synthetic and may be public; genuine inputs are resolver-only.
            save_document(id, kind, document, public=config.release.lock["purpose"] == "fixture")
        for item in release.catalog["evidence"]:
            if item["id"] in seen_evidence:
                if seen_evidence[item["id"]] != item:
                    raise AssetCorpusError("conflicting_evidence")
                continue
            seen_evidence[item["id"]] = item
            payload = None
            if item["path"] is not None:
                try:
                    payload = read_local(
                        (config.path.parent / config.data["lock"]).parent,
                        item["path"],
                        ORIGINAL_BYTES,
                    )
                except AssetCorpusError as failure:
                    if item["access"] != "restricted" or str(failure) != "missing_file":
                        raise
            if payload is None:
                evidence.append(dict(item, path=None))
                continue
            if not matches_original(payload, item["sha256"], item["bytes"]):
                raise AssetCorpusError("input_digest")
            entry = dict(item)
            if item["access"] == "restricted":
                relative = (
                    f"{private.name}/evidence/{hashlib.sha256(item['id'].encode()).hexdigest()}"
                )
                entry["path"] = None
            else:
                # The corpus locator is input data, never an output filename.
                # An explicit resolver relocates its exact committed bytes.
                relative = f"{bundle}/evidence/{item['sha256']}"
            resolver_entries.append(
                dict(
                    id=item["id"],
                    hash_kind="original",
                    hash=item["sha256"],
                    bytes=len(payload),
                    path=relative,
                )
            )
            atomic_bytes(base / relative, payload, limit=ORIGINAL_BYTES)
            evidence.append(entry)
    proof = source_proof(config.production, config.effective)
    policy = json_bytes(
        dict(
            schema_version=1,
            purpose="fixture_protocol"
            if config.release.lock["purpose"] == "fixture"
            else "unreviewed_projection",
            effective_config_digest=canonical_digest(config.effective),
            generator=config.effective["generator"],
            sources=config.effective["sources"],
            eligibility=config.effective["eligibility"],
            budgets=config.effective["budgets"],
            job_budgets=config.effective["job_budgets"],
            refresh=config.effective["refresh"],
            reviewed=False,
        )
    )
    for id, payload in [
        ("benchmark-source-proof", json_bytes(proof)),
        ("benchmark-policy", policy),
    ]:
        relative = f"{private.name}/evidence/{id}.json"
        atomic_bytes(base / relative, payload, limit=CONFIG_BYTES)
        digest = hashlib.sha256(payload).hexdigest()
        evidence.append(
            dict(
                id=id,
                kind="source_provenance",
                access="restricted",
                path=None,
                sha256=digest,
                bytes=len(payload),
            )
        )
        resolver_entries.append(
            dict(id=id, hash_kind="original", hash=digest, bytes=len(payload), path=relative)
        )
    # Code files are explicit original-byte resolver commitments. No compare
    # command reads the current checkout or trusts a saved implementation grade.
    root = benchmark_repository_root()
    copied = set()
    for inventory in config.inventories.values():
        for item in inventory["files"]:
            id = file_evidence_id(item["path"])
            key = (id, item["sha256"])
            if key in copied:
                continue
            copied.add(key)
            payload = read_local(root, item["path"], CONFIG_BYTES)
            if not matches_original(payload, item["sha256"], item["bytes"]):
                raise AssetCorpusError("runner_changed")
            relative = f"{private.name}/files/{item['sha256']}"
            atomic_bytes(base / relative, payload, limit=CONFIG_BYTES)
            resolver_entries.append(
                dict(
                    id=id,
                    hash_kind="original",
                    hash=item["sha256"],
                    bytes=len(payload),
                    path=relative,
                )
            )
    resolver = dict(
        schema_version=1,
        entries=sorted(resolver_entries, key=lambda e: (e["id"], e["hash_kind"], e["hash"])),
    )
    validate_definition("OfflineResolverV1", resolver)
    atomic_bytes(
        path.with_name(path.name + ".resolver.json"), json_bytes(resolver), limit=CONFIG_BYTES
    )
    manifest = dict(
        schema_version=1,
        documents=sorted(documents, key=lambda d: d["id"]),
        evidence=sorted(evidence, key=lambda e: e["id"]),
        source_proof_evidence_ids=["benchmark-policy", "benchmark-source-proof"],
    )
    validate_definition("InputManifestV1", manifest)
    return manifest


def write_asset_benchmark_report(report: AssetBenchmarkReport, path: Path) -> None:
    """Persist companion inputs first, then atomically publish the closed report."""
    path = Path(path).absolute()
    manifest = _write_bundle(report, path)
    payload = json_bytes(manifest)
    manifest_path = path.with_name(path.name + ".inputs.json")
    atomic_bytes(manifest_path, payload, limit=CONFIG_BYTES)
    report.data["input_manifest"] = dict(
        path=manifest_path.name, digest=canonical_digest(manifest), bytes=len(payload)
    )
    validate_benchmark_document("report", report.data)
    atomic_bytes(path, json_bytes(report.data), limit=REPORT_BYTES)
    verified = load_asset_benchmark_report(
        path, resolver=path.with_name(path.name + ".resolver.json")
    )
    report.inputs, report.input_error = verified.inputs, verified.input_error


def load_asset_benchmark_report(
    path: Path,
    *,
    inputs: Path | None = None,
    resolver: Path | OfflineResolver | None = None,
    source_policy_review: SourcePolicyReview | None = None,
) -> AssetBenchmarkReport:
    """Parse a report; retain safe closure failure for an incompatible export."""
    path = Path(path).absolute()
    data, _ = read_json_path(path, REPORT_BYTES)
    validate_benchmark_document("report", data)
    report = AssetBenchmarkReport(data)
    try:
        if resolver is not None and not isinstance(resolver, OfflineResolver):
            resolver = OfflineResolver(resolver)
        if inputs is None:
            relative = data["input_manifest"]["path"]
            if relative is None:
                raise AssetCorpusError("input_unresolved")
            relative_path(relative)
            inputs = path.parent / relative
        report.inputs = verify_benchmark_inputs(
            data, inputs, resolver=resolver, source_policy_review=source_policy_review
        )
    except ValueError as failure:
        report.input_error = (
            "input_unresolved"
            if str(failure) in ("input_unresolved", "missing_file")
            else "evidence_unverified"
        )
    return report


def refresh_fixture_benchmark_config(path: Path, *, reauthor_fixture_unicode: bool = False) -> None:
    """Explicitly author fresh fixture settings/environment on the current device.

    This authoring helper is never called by run or compare. It refuses genuine
    corpus purposes and never changes source data, labels, rights or policy.
    """
    import json

    path = Path(path).absolute()
    raw = tomllib.loads(read_local(path.parent, path.name, CONFIG_BYTES).decode("utf-8"))
    validate_benchmark_document("config", raw)
    lock_path = path.parent / raw["lock"]
    if type(reauthor_fixture_unicode) is not bool:
        raise AssetCorpusError("invalid_fixture_authoring")
    if reauthor_fixture_unicode:
        import unicodedata

        lock = document(lock_path.parent, lock_path.name, "lock")
        committed = {key: value for key, value in lock.items() if key != "digest"}
        if canonical_digest(committed) != lock["digest"]:
            raise AssetCorpusError("lock_digest")
        if lock["purpose"] != "fixture" or lock["state"] != "provisional" or lock["related_locks"]:
            raise AssetCorpusError("fixture_authoring_only")
        if lock["unicode_version"] != unicodedata.unidata_version:
            committed["unicode_version"] = unicodedata.unidata_version
            authored = committed | dict(digest=canonical_digest(committed))
            atomic_bytes(lock_path, json_bytes(authored), limit=CONFIG_BYTES)
    release = _load_benchmark_release(lock_path)
    if release.lock["purpose"] != "fixture":
        raise AssetCorpusError("fixture_authoring_only")
    read_local(path.parent, raw["production_config"], CONFIG_BYTES)
    production = _load_production_config(path.parent / raw["production_config"])
    inventories = benchmark_inventories()
    effective = _effective_config(production, release, inventories, raw["split"])
    environment = collect_benchmark_environment()
    raw.update(
        lock_digest=release.lock["digest"],
        production_config_digest=canonical_digest(effective),
        environment_lock_digest=canonical_digest(environment),
    )
    atomic_bytes(path.parent / raw["environment_lock"], json_bytes(environment), limit=CONFIG_BYTES)
    lines = [
        key + " = " + json.dumps(value, ensure_ascii=False)
        for key, value in raw.items()
        if key != "preparation"
    ]
    if "preparation" in raw:
        lines.append("[preparation]")
        lines.extend(
            key + " = " + json.dumps(value, ensure_ascii=False)
            for key, value in raw["preparation"].items()
            if value is not None
        )
    payload = ("\n".join(lines) + "\n").encode("utf-8")
    atomic_bytes(path, payload, limit=CONFIG_BYTES)
