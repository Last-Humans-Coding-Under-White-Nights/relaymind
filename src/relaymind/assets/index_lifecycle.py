"""Prepare lexical offers beside a pinned catalog, then publish one compatible pair (RM38)."""

from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import TYPE_CHECKING

from relaymind.assets.catalog import CatalogUnavailable
from relaymind.assets.sources import CaptureContext, SourceFailure, source_config_digest
from relaymind.selection.asset_contract import SelectorContext
from relaymind.selection.asset_lexical import IMPLEMENTATION
from relaymind.store.asset_catalog import CatalogConflict, PreparedPublication, Publication

if TYPE_CHECKING:
    from relaymind.recommendations.service import CatalogProvider


class IndexLifecycle:
    """Publication work for the existing provider/cache and sole asset worker."""

    def __init__(self, provider: "CatalogProvider"):
        self.provider = provider

    async def rebuild(
        self,
        *,
        check: Callable[[], None],
        yield_control: Callable[[], Awaitable[None]] | None = None,
    ) -> Publication:
        """Derived-only work; the asset service supplies scheduling/job ownership."""
        async with self.provider._refresh_lock:
            before = self.provider.store.revision()
            if not await self.provider._refresh_owned(
                force=True, check=check, yield_control=yield_control
            ):
                raise SourceFailure("index_error", rejected_count=0)
            after = self.provider.store.revision()
            return Publication(after, before != after, (), ())

    async def publish(
        self,
        prepared: PreparedPublication,
        *,
        expected_revision: int,
        context: CaptureContext,
        run_cpu: Callable,
    ) -> Publication:
        provider, store = self.provider, self.provider.store
        check = context.check_limits
        async with provider._refresh_lock:
            check()
            config_digest = provider.generation_config()
            configured = {s.source_id: source_config_digest(s) for s in provider.settings.sources}
            if configured.get(prepared.capture.source_id) != prepared.capture.config_digest:
                raise CatalogConflict("catalog_configuration_changed")
            provenance = store.source_provenance()
            eligible_sources = frozenset(
                source_id
                for source_id, digest in configured.items()
                if source_id in provenance and provenance[source_id]["config_digest"] == digest
            ) | {prepared.capture.source_id}

            def policy(record):
                return record.source_id in eligible_sources and provider._policy(record)

            base = provider.snapshot
            if base is None or base.catalog_revision != expected_revision:
                base = store.snapshot()
            snapshot = await run_cpu(
                store.preview,
                prepared,
                expected_revision=expected_revision,
                base=base,
                check=context.check,
            )
            source_revision = snapshot.catalog_revision
            try:
                manifest = store.lexical_generation(snapshot=base)
            except CatalogUnavailable:
                manifest = None
            eligible_ids = tuple(r.asset_id for r in snapshot.assets if policy(r))
            old_ids = () if manifest is None else tuple(manifest["eligible_ids"])
            # Changed source bytes already get a fresh identity. A no-op must
            # still repair unknown prior offer identity with a new revision.
            advance = source_revision == expected_revision and (
                manifest is None or eligible_ids != old_ids
            )
            if advance:
                snapshot = replace(snapshot, catalog_revision=source_revision + 1)
            # Capture-only observations leave published record bytes and the
            # matching lexical objects intact. A missing cache still restores.
            offers = None
            if snapshot.catalog_revision != expected_revision or not provider._cache_valid(
                expected_revision
            ):
                offers = await provider.cache.prepare(
                    snapshot,
                    scope=provider.scope,
                    permissions=provider.permissions,
                    policy=policy,
                    context=SelectorContext(
                        store.store.clock.monotonic, check, context.async_checkpoint
                    ),
                )
            await context.async_checkpoint()
            check()
            if provider._stopping:
                raise CatalogConflict("catalog_stopping")
            # The writer rechecks policy after the yielding preparation. This
            # cannot admit different eligibility under the prepared revision.
            if provider.generation_config() != config_digest or (
                offers is not None
                and tuple(r for r in snapshot.assets if policy(r)) != offers.records
            ):
                raise CatalogConflict("catalog_policy_changed")
            with store.store._lock:
                with store.store.sql_transaction():
                    result = store.commit(
                        prepared, expected_revision=expected_revision, check=check
                    )
                    if result.revision != source_revision:
                        raise CatalogConflict("catalog_preparation_changed")
                    final_revision = store.commit_lexical_generation(
                        expected_revision=result.revision,
                        eligible_ids=eligible_ids,
                        asset_ids=tuple(r.asset_id for r in snapshot.assets),
                        representation=IMPLEMENTATION,
                        config_digest=config_digest,
                        advance=advance,
                        check=check,
                    )
                    result = replace(
                        result, revision=final_revision, changed=result.changed or advance
                    )
                # No await or new pin can interleave with commit and install.
                # A failed install leaves the revision check fail closed until
                # the observer/restart rebuilds from the committed safe bytes.
                try:
                    provider._eligible_sources = frozenset(eligible_sources)
                    if offers is not None:
                        provider.cache.install(offers)
                    provider.snapshot = snapshot
                    provider.known_revision = result.revision
                    provider.size = len(snapshot.assets)
                    provider.reason = None
                except Exception:
                    provider.cache.invalidate()
                    provider.snapshot = None
                    provider.reason = "no_lexical_generation"
            return result
