"""RM38/AC29: prepare complete compatible lexical offers before metadata commit."""

import asyncio
from dataclasses import replace

import pytest

from helpers import ScriptedAdapter
from launcher_helpers import write_config
from recommendation_helpers import BODY, build_app, client_for, setup
from relaymind import launcher
from relaymind.assets.settings import SourceConfig
from relaymind.assets.sources import CaptureContext, SourceFailure, capture_source
from relaymind.recommendations.errors import RecommendationError
from relaymind.selection import asset_lexical
from relaymind.selection.asset_contract import AttemptFailure, SelectorContext
from relaymind.store import RunStore
from relaymind.store.asset_catalog import AssetCatalogStore
from test_asset_readiness import configured_catalog
from test_asset_sources import capture, skill


def test_source_update_prepares_index_while_old_generation_still_serves(tmp_path, monkeypatch):
    """RM38/AC29: no post-commit unavailable interval exists for a valid source update."""

    async def go():
        config = configured_catalog(tmp_path, warmup=False, detect=False)
        launched = build_app(config, local_adapter=ScriptedAdapter())
        service = launched.app.state.recommendations
        catalog = launched.app.state.asset_catalog
        async with launched.app.router.lifespan_context(launched.app):
            await catalog.rescan("example")
            old = service.provider.pin(["skill"])
            entered, release = asyncio.Event(), asyncio.Event()
            real = asset_lexical.build_lexical_index

            async def held(revision, *args, **kwargs):
                if revision > old.catalog_revision:
                    entered.set()
                    await release.wait()
                return await real(revision, *args, **kwargs)

            monkeypatch.setattr(asset_lexical, "build_lexical_index", held)
            skill(tmp_path / "ordinary", description="Replacement constellation")
            update = asyncio.create_task(catalog.rescan("example"))
            try:
                await asyncio.wait_for(entered.wait(), 3)
                assert catalog.store.revision() == old.catalog_revision
                assert service.provider.pin(["skill"]) is old
                async with await client_for(launched.app) as client:
                    response = await client.post("/relay/v1/recommendations", json=BODY)
                    assert response.status_code == 200, response.text
                    assert response.json()["displayed_candidates"]
            finally:
                release.set()
                await update
            new = service.provider.pin(["skill"])
            assert new.catalog_revision == old.catalog_revision + 1
            assert b"Replacement constellation" in new.catalog_bytes
            assert b"Greeting skill" in old.catalog_bytes
            assert new.index.records[0].description == "Replacement constellation"
            assert new.index.catalog_revision == new.catalog_revision
        launched.orchestrator.store.close()

    asyncio.run(go())


def test_failed_index_preparation_cannot_commit_new_source_content(tmp_path, monkeypatch):
    """RM38/AC29: an unprepared replacement never becomes authoritative metadata."""

    async def go():
        config = configured_catalog(tmp_path, warmup=False, detect=False)
        launched = build_app(config, local_adapter=ScriptedAdapter())
        service = launched.app.state.recommendations
        catalog = launched.app.state.asset_catalog
        async with launched.app.router.lifespan_context(launched.app):
            await catalog.rescan("example")
            old = catalog.store.snapshot()

            async def broken(*args, **kwargs):
                raise RuntimeError("fixture index preparation failure")

            monkeypatch.setattr(asset_lexical, "build_lexical_index", broken)
            skill(tmp_path / "ordinary", description="Unpublished replacement")
            with pytest.raises(SourceFailure):
                await catalog.rescan("example")
            assert catalog.store.snapshot().assets[0].description == old.assets[0].description
            assert service.readiness()["state"] == "unavailable"
        launched.orchestrator.store.close()

    asyncio.run(go())


def test_future_offer_preparation_does_not_replace_current_publication(tmp_path):
    """RM38/AC29: cached preparation can precede its metadata CAS without becoming visible."""

    async def go():
        with RunStore() as runs:
            store = AssetCatalogStore(runs)
            skill(tmp_path / "source")
            store.publish(capture(tmp_path / "source"), expected_revision=0)
            snapshot = store.snapshot()
            cache = asset_lexical.OfferCache(current_revision=store.revision)
            context = SelectorContext(runs.clock.monotonic, lambda: None)
            options = dict(scope={}, permissions=(), policy=lambda record: True, context=context)
            await cache.publish(snapshot, **options)
            old = cache.get("discovery", ["skill"], catalog_revision=1)
            future = replace(snapshot, catalog_revision=2)
            prepared = await cache.prepare(future, **options)
            assert cache.get("discovery", ["skill"], catalog_revision=1) is old
            with pytest.raises(AttemptFailure):
                cache.install(prepared)
            assert cache.get("discovery", ["skill"], catalog_revision=1) is old

    asyncio.run(go())


@pytest.mark.parametrize("change", ["unchanged", "content", "removed", "stale-recovery"])
def test_source_preview_matches_commit_without_mutating_rows(tmp_path, change):
    """RM38/AC29: pure proposed records preserve no-op bytes and match actual CAS publication."""
    source = tmp_path / "source"
    skill(source)
    with RunStore() as runs:
        catalog = AssetCatalogStore(runs)
        catalog.publish(capture(source), expected_revision=0)
        if change == "content":
            skill(source, description="Changed description")
        elif change == "removed":
            (source / "SKILL.md").unlink()
        elif change == "stale-recovery":
            catalog.record_failure("example", rejected_count=1, exclusions=())
        old = catalog.snapshot()
        configured = SourceConfig("example", "local_directory", source)
        staged = capture_source(
            configured,
            previous=catalog.inventory("example"),
            context=CaptureContext(runs.clock.utc_now()),
        )
        prepared = catalog.prepare(staged)
        preview = catalog.preview(prepared, expected_revision=old.catalog_revision, base=old)
        assert catalog.snapshot() == old
        result = catalog.commit(prepared, expected_revision=old.catalog_revision)
        actual = catalog.snapshot()
        assert preview.catalog_revision == result.revision
        assert preview.assets == actual.assets
        assert preview.record_bytes == actual.record_bytes


def test_policy_eligibility_publishes_a_revision_and_restores_without_source_io(tmp_path):
    """RM38/AC29: eligibility changes cannot create two different offers under one revision."""
    from relaymind.recommendations.service import CatalogProvider

    async def go():
        _, service, runs, catalog = setup(tmp_path)
        await service.refresh_catalog()
        original = service.provider.pin(["skill"])
        source_bytes = catalog.snapshot().record_bytes
        service.provider.policy = lambda record: False
        await service.refresh_catalog()
        excluded_revision = catalog.revision()
        assert excluded_revision == original.catalog_revision + 1
        assert service.readiness()["state"] == "empty"
        assert not service.provider.pin(["skill"]).catalog
        assert catalog.snapshot().record_bytes == source_bytes
        settings, scope = service.provider.settings, service.scope
        await service.stop()
        runs.close()
        (tmp_path / "source" / "SKILL.md").unlink()
        with RunStore(tmp_path / "db") as reopened:
            restored = CatalogProvider(
                AssetCatalogStore(reopened),
                settings,
                scope=scope,
                permissions=(),
                policy=lambda record: False,
            )
            await restored.refresh()
            assert restored.readiness()["state"] == "empty"
            assert restored.store.revision() == excluded_revision
            restored.policy = lambda record: True
            await restored.refresh()
            assert restored.readiness()["state"] == "ready"
            assert restored.store.revision() == excluded_revision + 1
            assert restored.pin(["skill"]).catalog_bytes == original.catalog_bytes

    asyncio.run(go())


def test_failed_eligibility_preparation_keeps_old_published_revision(tmp_path, monkeypatch):
    """RM38/AC29: a policy generation is prepared completely before the revision commit."""

    async def go():
        _, service, runs, catalog = setup(tmp_path)
        await service.refresh_catalog()
        original = service.provider.pin(["skill"])
        service.provider.policy = lambda record: False

        async def broken(*args, **kwargs):
            raise RuntimeError("fixture preparation failure")

        monkeypatch.setattr(asset_lexical, "build_lexical_index", broken)
        await service.refresh_catalog()
        assert catalog.revision() == original.catalog_revision
        assert service.readiness()["state"] == "unavailable"
        with pytest.raises(RecommendationError):
            service.provider.pin(["skill"])
        await service.stop()
        runs.close()

    asyncio.run(go())


def test_source_eligibility_change_has_its_own_commit_after_unpublished_metadata(tmp_path):
    """RM38/AC29: a separate policy commit cannot reuse an earlier metadata revision."""
    from relaymind.assets.catalog import SourceCapture
    from relaymind.assets.sources import source_config_digest

    async def go():
        _, service, runs, catalog = setup(tmp_path)
        await service.refresh_catalog()
        second = SourceConfig("second", "local_directory", tmp_path / "second")
        catalog.publish(
            SourceCapture(
                "second",
                source_config_digest(second),
                "c" * 64,
                None,
                "2026-10-29T00:00:00Z",
                (),
                (),
                False,
                frozenset(),
                {},
                0,
            ),
            expected_revision=catalog.revision(),
        )
        before_policy = catalog.revision()
        service.provider.settings = replace(service.provider.settings, sources=(second,))
        await service.refresh_catalog()
        assert catalog.revision() == before_policy + 1
        assert service.readiness()["state"] == "empty"
        assert catalog.snapshot().assets
        assert not service.provider.policy(catalog.snapshot().assets[0])
        await service.stop()
        runs.close()

    asyncio.run(go())


def test_policy_changed_during_preparation_cannot_commit_stale_eligibility(tmp_path, monkeypatch):
    """RM38/AC29: the final CAS rechecks effective policy after yielding index preparation."""

    async def go():
        _, service, runs, catalog = setup(tmp_path)
        await service.refresh_catalog()
        revision = catalog.revision()
        service.provider.policy = lambda record: False
        entered, release = asyncio.Event(), asyncio.Event()
        real = asset_lexical.build_lexical_index

        async def held(*args, **kwargs):
            entered.set()
            await release.wait()
            return await real(*args, **kwargs)

        monkeypatch.setattr(asset_lexical, "build_lexical_index", held)
        refresh = asyncio.create_task(service.refresh_catalog())
        await asyncio.wait_for(entered.wait(), 1)
        service.provider.policy = lambda record: True
        release.set()
        await refresh
        assert catalog.revision() == revision
        await service.refresh_catalog()
        assert service.readiness()["state"] == "ready"
        assert catalog.revision() == revision
        await service.stop()
        runs.close()

    asyncio.run(go())


def test_noop_source_capture_can_publish_changed_policy_without_source_failure(tmp_path):
    """RM38/AC29: successful unchanged capture and changed eligibility share one safe commit."""

    async def go():
        launched = build_app(
            configured_catalog(tmp_path, warmup=False, detect=False),
            local_adapter=ScriptedAdapter(),
        )
        service, catalog = launched.app.state.recommendations, launched.app.state.asset_catalog
        async with launched.app.router.lifespan_context(launched.app):
            await catalog.rescan("example")
            before = catalog.store.revision()
            service.provider.policy = lambda record: False
            await catalog.rescan("example")
            assert catalog.store.revision() == before + 1
            assert service.readiness()["state"] == "empty"
            assert not catalog.store.snapshot().sources[0]["stale"]
            assert catalog.store.job_outcomes()[0]["state"] == "succeeded"
        launched.orchestrator.store.close()

    asyncio.run(go())


def test_derived_rebuild_failure_retains_last_compatible_generation(tmp_path, monkeypatch):
    """RM38/AC29: a derived failure cannot remove a still-compatible serving generation."""

    async def go():
        _, service, runs, catalog = setup(tmp_path)
        await service.refresh_catalog()
        old = service.provider.pin(["skill"])

        async def broken(*args, **kwargs):
            raise RuntimeError("fixture derived build failure")

        monkeypatch.setattr(asset_lexical, "build_lexical_index", broken)
        with pytest.raises(SourceFailure):
            await service.provider.lifecycle.rebuild(check=lambda: None)
        assert service.readiness()["state"] == "ready"
        assert service.provider.pin(["skill"]) is old
        assert catalog.revision() == old.catalog_revision
        await service.stop()
        runs.close()

    asyncio.run(go())


def test_new_metadata_during_derived_rebuild_fences_the_old_build(tmp_path, monkeypatch):
    """RM38/AC29: a stale derived build never silently rebases onto newly committed records."""

    async def go():
        _, service, runs, catalog = setup(tmp_path)
        await service.refresh_catalog()
        entered, release = asyncio.Event(), asyncio.Event()
        real = asset_lexical.build_lexical_index

        async def held(*args, **kwargs):
            entered.set()
            await release.wait()
            return await real(*args, **kwargs)

        monkeypatch.setattr(asset_lexical, "build_lexical_index", held)
        rebuild = asyncio.create_task(service.provider.lifecycle.rebuild(check=lambda: None))
        await asyncio.wait_for(entered.wait(), 1)
        skill(tmp_path / "source", description="New source generation")
        catalog.publish(capture(tmp_path / "source"), expected_revision=catalog.revision())
        release.set()
        with pytest.raises(SourceFailure):
            await rebuild
        assert service.readiness()["state"] == "unavailable"
        await service.refresh_catalog()
        assert service.readiness()["state"] == "ready"
        assert b"New source generation" in service.provider.pin(["skill"]).catalog_bytes
        await service.stop()
        runs.close()

    asyncio.run(go())


def test_restart_rebuilds_corrupt_derived_manifest_from_safe_metadata(tmp_path):
    """RM38/AC29: derived corruption repairs from metadata; source originals are unnecessary."""
    from relaymind.recommendations.service import CatalogProvider

    async def go():
        _, service, runs, catalog = setup(tmp_path)
        await service.refresh_catalog()
        old = service.provider.pin(["skill"])
        settings, scope = service.provider.settings, service.scope
        with runs.sql_transaction() as db:
            db.execute("UPDATE asset_lexical_generation SET eligible_ids_json='broken'")
        await service.stop()
        runs.close()
        (tmp_path / "source" / "SKILL.md").unlink()
        with RunStore(tmp_path / "db") as reopened:
            restored = CatalogProvider(
                AssetCatalogStore(reopened), settings, scope=scope, permissions=()
            )
            await restored.refresh()
            assert restored.readiness()["state"] == "ready"
            new = restored.pin(["skill"])
            assert new.catalog_revision == old.catalog_revision + 1
            assert new.catalog_bytes == old.catalog_bytes
            assert new.index.offer_identity == new.identity

    asyncio.run(go())


@pytest.mark.parametrize("damage", ["missing", "keyset"])
def test_restart_and_derived_work_preserve_feedback_key_and_one_lifetime_owner(tmp_path, damage):
    """RM38/AC29 + RM37/AC28: restore never resets private feedback or its protected key."""
    from relaymind.api.user_actions import _claim
    from relaymind.assets.settings import AssetSettings, RecommendationSettings
    from test_asset_feedback_privacy import insert_feedback, privacy_store
    from test_asset_user_actions import ACTION_TOKEN

    runs, recommendations, registrations, feedback = privacy_store(tmp_path)
    now = runs.clock.utc_now()
    source = tmp_path / "ordinary"
    skill(source)
    feedback.catalog.publish(capture(source), expected_revision=0)
    insert_feedback(recommendations, registrations, feedback, workspace="a", key="erased", now=now)
    scope = {"user_id": "u", "workspace_id": "a"}
    action = registrations.authenticate(ACTION_TOKEN + "a", scope)
    body = dict(schema_version=1, expected_feedback_revision=1)
    receipt = _claim(action, "POST", "/relay/v1/feedback/reset", None, "reset", body)
    feedback.reset(action, 1, receipt, now=now)
    insert_feedback(
        recommendations, registrations, feedback, workspace="a", key="retained", now=now
    )
    interrupted = feedback.catalog.begin_job(None, "a" * 64, job_kind="lexical")
    original_bytes = feedback.catalog.snapshot().record_bytes
    if damage == "keyset":
        feedback.catalog.commit_lexical_generation(
            expected_revision=feedback.catalog.revision(),
            eligible_ids=(),
            asset_ids=(),
            representation=asset_lexical.IMPLEMENTATION,
            config_digest="b" * 64,
        )
    private_tables = (
        "feedback_records",
        "feedback_bindings",
        "feedback_event_ids",
        "action_receipts",
        "feedback_audits",
        "erasure_key_binding",
        "recommendation_scope",
    )

    def private_rows(store):
        with store.sql_read() as db:
            return {
                table: tuple(
                    tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid")
                )
                for table in private_tables
            }

    expected = private_rows(runs)
    key_path = tmp_path / "privacy-db.erasure-key"
    key, key_identity = key_path.read_bytes(), key_path.stat().st_ino
    runs.close()
    (source / "SKILL.md").unlink()
    config = replace(
        launcher.load_config(write_config(tmp_path)),
        store_path=tmp_path / "privacy-db",
        assets=AssetSettings((SourceConfig("example", "local_directory", source),)),
        recommendations=RecommendationSettings(enabled=True, user_id="u", workspace_id="a"),
        warmup=False,
        detect=False,
    )
    launched = build_app(config, local_adapter=ScriptedAdapter())
    worker, service = launched.app.state.asset_catalog, launched.app.state.recommendations
    # This fixture isolates restoration/derived work from the independently
    # tested periodic source scan; the source is deliberately offline.
    worker._next["example"] = float("inf")
    owner = launched.app.state.feedback_store
    assert service.store.store._asset_feedback_owner is owner
    assert len(service.store._purge_hooks) == 1

    async def go():
        async with launched.app.router.lifespan_context(launched.app):
            assert service.readiness()["state"] == "ready"
            assert service.readiness()["failed_jobs"] == 1
            assert private_rows(service.store.store) == expected
            await worker.rebuild()
            assert private_rows(service.store.store) == expected
            assert worker.store.snapshot().record_bytes == original_bytes
            assert worker.store.recover_interrupted_jobs() == 0
            events = worker.store.job_events(interrupted)
            assert [e["event_type"] for e in events] == ["queued", "started", "failed"]
            assert events[-1]["payload"]["reason"] == "interrupted"
            assert key_path.read_bytes() == key and key_path.stat().st_ino == key_identity
        assert len(service.store._purge_hooks) == 1
        launched.orchestrator.store.close()

    asyncio.run(go())


def test_source_cas_loser_cannot_overwrite_another_committed_generation(tmp_path, monkeypatch):
    """RM38/AC29: yielding source preparation does not rebase across another writer's CAS."""
    from test_chat_disconnect_cancel import until

    async def go():
        launched = build_app(
            configured_catalog(tmp_path, warmup=False, detect=False),
            local_adapter=ScriptedAdapter(),
        )
        worker, service = launched.app.state.asset_catalog, launched.app.state.recommendations
        async with launched.app.router.lifespan_context(launched.app):
            await worker.rescan("example")
            before = worker.store.snapshot()
            entered, release = asyncio.Event(), asyncio.Event()
            real = asset_lexical.build_lexical_index

            async def held(revision, *args, **kwargs):
                if revision > before.catalog_revision:
                    entered.set()
                    await release.wait()
                return await real(revision, *args, **kwargs)

            monkeypatch.setattr(asset_lexical, "build_lexical_index", held)
            skill(tmp_path / "ordinary", description="Losing replacement")
            change = asyncio.create_task(worker.rescan("example"))
            await asyncio.wait_for(entered.wait(), 1)
            skill(tmp_path / "outside", name="Outside", description="Committed separately")
            worker.store.publish(
                capture(tmp_path / "outside", source_id="outside"),
                expected_revision=before.catalog_revision,
            )
            release.set()
            with pytest.raises(SourceFailure) as error:
                await change
            assert error.value.reason == "index_error"
            await until(lambda: service.readiness()["state"] == "ready", timeout=2)
            assets = worker.store.snapshot().assets
            assert any(a.description == "Committed separately" for a in assets)
            assert any(a.description == "Greeting skill" and a.stale for a in assets)
            assert not any(a.description == "Losing replacement" for a in assets)
        launched.orchestrator.store.close()

    asyncio.run(go())


def test_durable_commit_before_failed_memory_swap_recovers_without_source_download(
    tmp_path, monkeypatch
):
    """RM38/AC29: failed installation is unavailable before replay, then restart restores bytes."""

    async def go():
        config = configured_catalog(tmp_path, warmup=False, detect=False)
        launched = build_app(config, local_adapter=ScriptedAdapter())
        worker, service = launched.app.state.asset_catalog, launched.app.state.recommendations
        async with launched.app.router.lifespan_context(launched.app):
            await worker.rescan("example")
            async with await client_for(launched.app) as client:
                first = await client.post("/relay/v1/recommendations", json=BODY)
                assert first.status_code == 200

                def broken_install(*args):
                    raise RuntimeError("fixture failed memory swap")

                monkeypatch.setattr(service.provider.cache, "install", broken_install)
                skill(tmp_path / "ordinary", description="Committed before swap failure")
                publication = await worker.rescan("example")
                assert publication.revision == 3
                assert (
                    worker.store.snapshot().assets[0].description == "Committed before swap failure"
                )
                assert service.readiness()["state"] == "unavailable"
                retry = await client.post("/relay/v1/recommendations", json=BODY)
                assert retry.status_code == 503, retry.text
                with service.store.store.sql_read() as db:
                    assert db.execute("SELECT count(*) FROM recommendations").fetchone()[0] == 1
        launched.orchestrator.store.close()
        (tmp_path / "ordinary" / "SKILL.md").unlink()
        restored = build_app(config, local_adapter=ScriptedAdapter())
        restored.app.state.asset_catalog._next["example"] = float("inf")
        try:
            async with restored.app.router.lifespan_context(restored.app):
                current = restored.app.state.recommendations
                assert current.readiness()["state"] == "ready"
                assert current.provider.pin(["skill"]).catalog_revision == 3
                assert (
                    b"Committed before swap failure"
                    in current.provider.pin(["skill"]).catalog_bytes
                )
                async with await client_for(restored.app) as client:
                    replay = await client.post("/relay/v1/recommendations", json=BODY)
                    assert replay.status_code == 200 and replay.json() == first.json()
        finally:
            restored.orchestrator.store.close()

    asyncio.run(go())


@pytest.mark.parametrize("damage", ["missing", "keyset", "future_revision"])
def test_unknown_derived_binding_gets_fresh_revision_on_metadata_only_restart(
    tmp_path, monkeypatch, damage
):
    """RM38/AC29: unknown prior eligibility cannot re-enable assets under an old offer revision."""
    from relaymind.assets.settings import AssetSettings
    from relaymind.recommendations.service import CatalogProvider

    async def go():
        _, service, runs, catalog = setup(tmp_path)
        await service.refresh_catalog()
        original_bytes = catalog.snapshot().record_bytes
        second_path = tmp_path / "second"
        second_path.mkdir()
        second = SourceConfig("second", "local_directory", second_path)
        catalog.publish(
            capture(second_path, source_id="second"), expected_revision=catalog.revision()
        )
        service.provider.settings = AssetSettings((second,))
        await service.refresh_catalog()
        old_revision = catalog.revision()
        assert service.readiness()["state"] == "empty"
        with runs.sql_transaction() as db:
            if damage == "missing":
                db.execute("DELETE FROM asset_lexical_generation")
            elif damage == "keyset":
                db.execute("UPDATE asset_lexical_generation SET asset_ids_json='[]'")
            else:
                db.execute(
                    "UPDATE asset_lexical_generation SET catalog_revision=catalog_revision+10"
                )
        settings = AssetSettings(
            (SourceConfig("example", "local_directory", tmp_path / "source"), second)
        )
        await service.stop()
        runs.close()
        (tmp_path / "source" / "SKILL.md").unlink()
        with RunStore(tmp_path / "db") as reopened:
            restored = CatalogProvider(
                AssetCatalogStore(reopened), settings, scope={}, permissions=()
            )
            real = asset_lexical.build_lexical_index

            async def broken(*args, **kwargs):
                raise RuntimeError("fixture repair failure")

            monkeypatch.setattr(asset_lexical, "build_lexical_index", broken)
            await restored.refresh()
            assert restored.readiness()["state"] == "unavailable"
            assert restored.store.revision() == old_revision
            with pytest.raises(RecommendationError):
                restored.pin(["skill"])
            monkeypatch.setattr(asset_lexical, "build_lexical_index", real)
            await restored.refresh()
            assert restored.readiness()["state"] == "ready"
            assert restored.store.revision() == old_revision + 1
            assert restored.store.snapshot().record_bytes == original_bytes
            assert restored.pin(["skill"]).catalog

    asyncio.run(go())


def test_expired_rebuild_before_preparation_preserves_compatible_offer(tmp_path):
    """RM38/AC29: an early original deadline is a job failure, not catalog corruption."""

    async def go():
        _, service, runs, catalog = setup(tmp_path)
        await service.refresh_catalog()
        old = service.provider.pin(["skill"])

        def expired():
            raise SourceFailure("deadline")

        with pytest.raises(SourceFailure, match="deadline"):
            await service.provider.lifecycle.rebuild(check=expired)
        assert service.readiness()["state"] == "ready"
        assert service.provider.pin(["skill"]) is old
        assert catalog.revision() == old.catalog_revision
        await service.stop()
        runs.close()

    asyncio.run(go())


def test_cancel_while_waiting_for_refresh_owner_preserves_compatible_offer(tmp_path):
    """RM38/AC29: queued rebuild cancellation records no source failure or serving outage."""
    from test_chat_disconnect_cancel import until

    async def go():
        launched = build_app(
            configured_catalog(tmp_path, warmup=False, detect=False),
            local_adapter=ScriptedAdapter(),
        )
        worker, service = launched.app.state.asset_catalog, launched.app.state.recommendations
        async with launched.app.router.lifespan_context(launched.app):
            await worker.rescan("example")
            old = service.provider.pin(["skill"])
            before = worker.store.snapshot()
            async with service.provider._refresh_lock:
                rebuild = asyncio.create_task(worker.rebuild())
                await until(lambda: bool(worker.store.job_counts().get("running")))
                rebuild.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await rebuild
            await until(lambda: not worker.store.job_counts().get("running"))
            assert service.readiness()["state"] == "ready"
            assert service.provider.pin(["skill"]) is old
            assert worker.store.snapshot() == before
            job = next(j for j in worker.store.job_outcomes() if j["source_id"] is None)
            assert (job["state"], job["reason"]) == ("cancelled", "operator")
        launched.orchestrator.store.close()

    asyncio.run(go())


@pytest.mark.parametrize("damage", ["missing", "keyset"])
def test_noop_source_capture_repairs_unknown_offer_binding_with_new_revision(tmp_path, damage):
    """RM38/AC29: source no-op must not reuse a revision whose derived offer proof was lost."""

    async def go():
        launched = build_app(
            configured_catalog(tmp_path, warmup=False, detect=False),
            local_adapter=ScriptedAdapter(),
        )
        worker, service = launched.app.state.asset_catalog, launched.app.state.recommendations
        async with launched.app.router.lifespan_context(launched.app):
            await worker.rescan("example")
            before = service.provider.pin(["skill"])
            with worker.store.store.sql_transaction() as db:
                if damage == "missing":
                    db.execute("DELETE FROM asset_lexical_generation")
                else:
                    db.execute(
                        "UPDATE asset_lexical_generation SET "
                        "eligible_ids_json='[]',asset_ids_json='[]'"
                    )
            await worker.rescan("example")
            after = service.provider.pin(["skill"])
            assert after.catalog_revision == before.catalog_revision + 1
            assert after.catalog_bytes == before.catalog_bytes
        launched.orchestrator.store.close()

    asyncio.run(go())
