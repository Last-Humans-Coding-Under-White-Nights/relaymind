"""RM34/AC25 + RM17: durable catalog generations publish atomically."""

import json
import sqlite3

import pytest

from helpers import ACCEPTED_PAYLOAD
from relaymind.store import RunStore
from relaymind.store.trace_event import EventType
from test_asset_sources import capture, imported, skill


def facade(store):
    from relaymind.store.asset_catalog import AssetCatalogStore

    return AssetCatalogStore(store)


def publish_local(catalog, path):
    got = capture(path, previous=catalog.inventory("example"))
    return catalog.publish(got, expected_revision=catalog.snapshot().catalog_revision)


def test_atomic_publish_noop_changes_restart_and_immutable_snapshot(tmp_path):
    source = tmp_path / "skills"
    skill(source)
    db = tmp_path / "catalog.sqlite"
    with RunStore(db) as run_store:
        catalog = facade(run_store)
        assert catalog.snapshot().catalog_revision == 0
        first = publish_local(catalog, source)
        assert first.changed and first.revision == 1
        frozen = catalog.snapshot()
        second = publish_local(catalog, source)
        assert not second.changed and second.revision == 1
        (source / "skill.manifest.json").write_text('{"version":"new"}')
        third = publish_local(catalog, source)
        assert third.revision == 2
        assert third.changed_asset_ids == (frozen.assets[0].asset_id,)
        assert frozen.assets[0].identity["package_version"] is None
        assert catalog.snapshot().assets[0].identity["package_version"] == "new"
    with RunStore(db) as reopened:
        assert facade(reopened).snapshot().catalog_revision == 2
        assert facade(reopened).search_documents()[0].body == "Say hello."


def test_conflicting_facades_and_sql_rollback_do_not_partially_publish(tmp_path):
    from relaymind.store.asset_catalog import CatalogConflict

    source = tmp_path / "skills"
    skill(source / "first")
    skill(source / "second")
    with RunStore(tmp_path / "db") as shared:
        one, two = facade(shared), facade(shared)
        assert publish_local(one, source).revision == 1
        staged = capture(source, previous=two.inventory("example"))
        with pytest.raises(CatalogConflict):
            two.publish(staged, expected_revision=0)
        previous = one.snapshot().to_json_object()
        # Database failure after an upsert and before the removal must roll back both.
        (source / "second" / "SKILL.md").unlink()
        skill(source / "first", description="Changed")
        staged = capture(source, previous=one.inventory("example"))
        with shared.sql_transaction() as connection:
            connection.execute(
                "CREATE TRIGGER fail_removal BEFORE DELETE ON asset_catalog_assets "
                "BEGIN SELECT RAISE(ABORT,'injected failure'); END"
            )
        with pytest.raises(sqlite3.IntegrityError):
            one.publish(staged, expected_revision=1)
        assert one.snapshot().to_json_object() == previous
        with shared.sql_transaction() as connection:
            connection.execute("DROP TRIGGER fail_removal")
        result = two.publish(staged, expected_revision=1)
        assert result.revision == 2
        assert len(result.removed_asset_ids) == 1
        assert len(one.snapshot().assets) == 1


def test_partial_import_retained_omissions_stale_complete_needs_tombstones(tmp_path):
    from relaymind.assets.sources import SourceFailure

    manifest, data = imported(tmp_path / "import")
    other = json.loads(json.dumps(data["records"][0]))
    other["record"]["asset_id"] = "example:second"
    data["records"].append(other)
    data["complete"] = True
    manifest.write_text(json.dumps(data))
    with RunStore() as run_store:
        catalog = facade(run_store)
        catalog.publish(capture(manifest, "metadata_import"), expected_revision=0)
        data["complete"] = False
        data["records"].pop()
        manifest.write_text(json.dumps(data))
        catalog.publish(
            capture(manifest, "metadata_import", previous=catalog.inventory("example")),
            expected_revision=1,
        )
        snapshot = catalog.snapshot()
        assert not snapshot.sources[0]["complete"]
        assert {a.asset_id: a.stale for a in snapshot.assets} == {
            "example:hello": False,
            "example:second": True,
        }
        data["complete"] = True
        manifest.write_text(json.dumps(data))
        with pytest.raises(SourceFailure):
            capture(manifest, "metadata_import", previous=catalog.inventory("example"))
        data["removed_asset_ids"] = ["example:second"]
        manifest.write_text(json.dumps(data))
        staged = capture(manifest, "metadata_import", previous=catalog.inventory("example"))
        result = catalog.publish(staged, expected_revision=2)
        assert result.removed_asset_ids == ("example:second",)
        replay = capture(manifest, "metadata_import", previous=catalog.inventory("example"))
        assert not catalog.publish(replay, expected_revision=3).changed
        assert catalog.snapshot().sources[0]["complete"]


def test_failure_retains_generation_and_records_only_failure_observation(tmp_path):
    from relaymind.assets.sources import SourceFailure

    skill(tmp_path / "source")
    with RunStore() as run_store:
        catalog = facade(run_store)
        publish_local(catalog, tmp_path / "source")
        old = catalog.snapshot()
        catalog.record_failure("example", SourceFailure("source_unavailable"))
        now = catalog.snapshot()
        assert now.catalog_revision == old.catalog_revision
        assert now.published_at == old.published_at
        assert now.assets[0].content_hash == old.assets[0].content_hash
        assert now.assets[0].stale
        assert now.sources[0]["content_hash"] == old.sources[0]["content_hash"]
        assert now.sources[0]["last_success_at"] == old.sources[0]["last_success_at"]
        assert now.sources[0]["accepted_count"] == 1
        assert now.sources[0]["rejected_count"] == 1
        assert not now.sources[0]["complete"]
        assert len(catalog.search_documents()) == 1


def test_ownership_rechecked_at_commit_and_tombstone_owner_preserved(tmp_path):
    from dataclasses import replace

    from relaymind.assets.catalog import validate_asset_record
    from relaymind.assets.sources import SourceFailure
    from relaymind.store.asset_catalog import CatalogConflict

    skill(tmp_path / "source")
    with RunStore() as run_store:
        catalog = facade(run_store)
        staged = capture(tmp_path / "source")
        catalog.publish(staged, expected_revision=0)
        stolen = validate_asset_record(
            staged.documents[0].record.to_json_object() | {"source_id": "other"}
        )
        forged = replace(
            staged, source_id="other", documents=(replace(staged.documents[0], record=stolen),)
        )
        with pytest.raises((CatalogConflict, SourceFailure, ValueError)):
            catalog.publish(forged, expected_revision=1)
        (tmp_path / "source" / "SKILL.md").unlink()
        removed = publish_local(catalog, tmp_path / "source")
        assert removed.removed_asset_ids
        assert catalog.inventory("other").owners[stolen.asset_id] == "example"
        with pytest.raises((CatalogConflict, SourceFailure, ValueError)):
            catalog.publish(forged, expected_revision=2)


def test_upgrade_keeps_existing_runs_and_events(tmp_path):
    from relaymind.store.run_store import MIGRATIONS, split_sql_statements

    db = tmp_path / "old.sqlite"
    with sqlite3.connect(db) as raw:
        for index, migration in enumerate(MIGRATIONS[:-1]):
            for statement in split_sql_statements(migration):
                raw.execute(statement)
            raw.execute(f"PRAGMA user_version={index + 1}")
        # A valid pre-M10 run row survives the appended migration unchanged.
        raw.execute(
            "INSERT INTO runs(run_id,state,version,created_at,updated_at) "
            "VALUES('run_existing','completed',1,'2026-10-29T00:00:00Z','2026-10-29T00:00:00Z')"
        )
    with RunStore(db) as store:
        assert store.get_run("run_existing").state == "completed"
        assert facade(store).snapshot().catalog_revision == 0
        new = store.create_run("accepted")
        store.append_event(new.run_id, EventType.RUN_ACCEPTED, "gateway", ACCEPTED_PAYLOAD)
        assert len(store.events(new.run_id)) == 1


def test_interrupted_jobs_recover_without_losing_catalog(tmp_path):
    with RunStore(tmp_path / "db") as store:
        catalog = facade(store)
        skill(tmp_path / "source")
        publish_local(catalog, tmp_path / "source")
        job = catalog.begin_job("example", "0" * 64)
        assert job
    with RunStore(tmp_path / "db") as store:
        catalog = facade(store)
        assert catalog.recover_interrupted_jobs() == 1
        assert catalog.recover_interrupted_jobs() == 0
        assert catalog.snapshot().catalog_revision == 1
        assert catalog.snapshot().assets[0].stale
        assert catalog.job_outcomes()[0]["state"] == "interrupted"


def test_corrupt_committed_projection_is_unavailable_not_empty(tmp_path):
    from relaymind.store.asset_catalog import CatalogUnavailable

    skill(tmp_path / "source")
    with RunStore() as store:
        catalog = facade(store)
        publish_local(catalog, tmp_path / "source")
        with store.sql_transaction() as connection:
            connection.execute("UPDATE asset_catalog_assets SET search_json='{}'")
        with pytest.raises(CatalogUnavailable):
            catalog.search_documents()
        with pytest.raises(CatalogUnavailable):
            catalog.snapshot()


def test_two_connections_share_cas_and_current_search_removals(tmp_path):
    from relaymind.store.asset_catalog import CatalogConflict

    source = tmp_path / "source"
    skill(source / "one")
    skill(source / "two")
    db = tmp_path / "db"
    with RunStore(db) as first, RunStore(db) as second:
        a, b = facade(first), facade(second)
        stale = capture(source, previous=b.inventory("example"))
        a.publish(stale, expected_revision=0)
        with pytest.raises(CatalogConflict):
            b.publish(stale, expected_revision=0)
        removed_id = next(
            item.record.asset_id
            for item in b.inventory("example").records
            if item.relative_package_path == "two"
        )
        (source / "two" / "SKILL.md").unlink()
        result = publish_local(b, source)
        assert result.removed_asset_ids == (removed_id,)
        assert removed_id not in {d.record.asset_id for d in a.search_documents()}


def test_invalid_replacement_and_missing_root_retain_all_prior_assets(tmp_path):
    from relaymind.assets.sources import SourceFailure

    source = tmp_path / "source"
    skill(source / "one")
    with RunStore() as run_store:
        catalog = facade(run_store)
        publish_local(catalog, source)
        first = catalog.snapshot()
        (source / "one" / "SKILL.md").write_text("bad replacement")
        with pytest.raises(SourceFailure):
            capture(source, previous=catalog.inventory("example"))
        source.rename(tmp_path / "missing")
        with pytest.raises(SourceFailure):
            capture(source, previous=catalog.inventory("example"))
        assert catalog.snapshot().to_json_object() == first.to_json_object()


def test_source_secrets_are_absent_from_database_and_originals_unchanged(tmp_path):
    import hashlib

    skill(
        tmp_path / "source",
        description="Use credential-987",
        metadata="password: credential-987\n",
        body="Read credential-987 safely.",
    )
    source_file = tmp_path / "source" / "SKILL.md"
    original = source_file.read_bytes()
    with RunStore(tmp_path / "db") as run_store:
        catalog = facade(run_store)
        publish_local(catalog, tmp_path / "source")
        assert catalog.snapshot().assets[0].content_hash == hashlib.sha256(original).hexdigest()
        with run_store.sql_transaction() as connection:
            for table in (
                "asset_catalog_assets",
                "asset_catalog_sources",
                "asset_catalog_jobs",
                "asset_catalog_provenance",
            ):
                assert "credential-987" not in repr(
                    connection.execute(f"SELECT * FROM {table}").fetchall()
                )
                for row in connection.execute(f"SELECT * FROM {table}"):
                    assert "credential-987" not in repr(tuple(row))
        assert catalog.search_documents()[0].body == "Read [redacted] safely."
        assert source_file.read_bytes() == original


@pytest.mark.parametrize("body_secret", ["credential-e\u0301-987", "credential-é-987"])
def test_known_secret_is_scrubbed_before_and_after_nfc_in_sqlite(tmp_path, body_secret):
    """RM12/RM34/AC25: canonically equivalent secrets must never enter search storage."""
    secret = "credential-e\u0301-987"
    skill(
        tmp_path / "source",
        metadata=f"password: {secret}\n",
        description=f"Read {body_secret}",
        body=f"Read {body_secret} safely.",
    )
    original = (tmp_path / "source" / "SKILL.md").read_bytes()
    with RunStore() as store:
        catalog = facade(store)
        publish_local(catalog, tmp_path / "source")
        assert catalog.search_documents()[0].body == "Read [redacted] safely."
        assert catalog.snapshot().assets[0].description == "Read [redacted]"
        with store.sql_transaction() as connection:
            persisted = repr(
                tuple(
                    connection.execute(
                        "SELECT record_json,search_json FROM asset_catalog_assets"
                    ).fetchone()
                )
            )
        assert secret not in persisted
        assert "credential-é-987" not in persisted
        assert (tmp_path / "source" / "SKILL.md").read_bytes() == original
