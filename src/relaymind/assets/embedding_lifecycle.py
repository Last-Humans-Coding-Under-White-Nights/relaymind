"""Public vector generations on the existing asset worker (RM38/RM41)."""

from relaymind.assets.embedding_backends import EmbeddingFailure
from relaymind.assets.embeddings import represent_record
from relaymind.assets.sources import SourceFailure
from relaymind.store.asset_catalog import Publication
from relaymind.store.run_store import utc_iso


class EmbeddingIndexLifecycle:
    """No independent worker, downloads, task vectors, or hidden candidate subset."""

    def __init__(self, provider, selector, vectors, loader):
        self.provider, self.selector, self.vectors, self.loader = (
            provider,
            selector,
            vectors,
            loader,
        )

    def generation_config(self):
        return self.selector.config.digest

    @property
    def revision(self):
        return self.selector.index.revision

    async def stop(self):
        if self.selector.engine is not None:
            await self.selector.engine.drain()

    async def rebuild(self, *, context, run_cpu):
        selector, provider = self.selector, self.provider
        offer = provider.pin(("skill", "mcp_tool"))
        records = tuple(offer.catalog)
        revision = offer.catalog_revision

        def check():
            context.check_limits()
            current = provider.pin(("skill", "mcp_tool"))
            if current.catalog_revision != revision or tuple(current.catalog) != records:
                raise SourceFailure("index_error", rejected_count=0)

        try:
            restored = await run_cpu(
                self.vectors.restore, selector.config, selector.index.scope, records, check=check
            )
            await run_cpu(
                self.vectors.prune, selector.config, selector.index.scope, records, check=check
            )
            check()
            selector.index._publication = restored.pin()
            if not records:
                return Publication(revision, False, (), ())
            if selector.engine is None:
                selector.engine = await run_cpu(self.loader.load, selector.config, control=context)
            engine = selector.engine
            # Another foreground owner can still be draining a cancelled call.
            # Waiting consumes the original job deadline and dispatches no retries.
            context.end_cpu_window()
            await engine.drain()
            await context.async_checkpoint()
            missing = [record for record in records if selector.index.compatible(record) is None]
            batch_size = selector.config.data["index"]["batch_size"]
            for start in range(0, len(missing), batch_size):
                await context.async_checkpoint()
                check()
                batch = missing[start : start + batch_size]
                representations = [
                    represent_record(
                        record, selector.config.data["representation"]["document_prefix"]
                    )
                    for record in batch
                ]
                started = context.monotonic()
                observations = ()
                accepted = 0
                try:
                    result = await engine.embed(
                        representations,
                        max_tokens=selector.config.data["representation"]["max_tokens"],
                        deadline=context.deadline,
                        clock=context.monotonic,
                        cancelled=context.cancelled,
                        work_control=context,
                    )
                    observations = (result.observation,)
                    await context.async_checkpoint()
                    check()
                    await run_cpu(
                        self.vectors.save_batch, selector.index, batch, result.vectors, check=check
                    )
                    check()
                    selector.index.publish_batch(batch, result.vectors, check=check)
                    accepted = len(batch)
                except EmbeddingFailure as failure:
                    observations = failure.observations
                    raise
                finally:
                    # Background inference belongs exclusively to job evidence.
                    context.batch_observations.append(
                        dict(
                            timestamp=utc_iso(context.utc_now()),
                            accepted_count=accepted,
                            rejected_count=0,
                            received_bytes=0,
                            duration_ms=max(0, (context.monotonic() - started) * 1000),
                            embeddings=list(observations),
                        )
                    )
            check()
            return Publication(revision, False, (), ())
        except EmbeddingFailure as failure:
            raise SourceFailure(
                "cancelled"
                if str(failure) == "embedding_cancelled"
                else "deadline"
                if str(failure) == "embedding_deadline"
                else "index_error",
                rejected_count=0,
            ) from None
        finally:
            # Native ownership outlives cancellation until it actually drains.
            # No late vector publication is possible after this job returns.
            if selector.engine is not None:
                context.end_cpu_window()
                await selector.engine.drain()
