"""Atomic independent asset generations over the shared RunStore (RM34, RM17)."""

import json
import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from relaymind.assets.catalog import (
    AssetValidationError,
    CatalogSnapshot,
    SearchDocument,
    project_asset,
    validate_asset_record,
    validate_catalog_snapshot,
)
from relaymind.assets.sources import InventoryAsset, SourceCapture, SourceFailure, SourceInventory
from relaymind.canonical import canonical_json
from relaymind.redaction import Redactor
from relaymind.schema_util import freeze_json
from relaymind.store.run_store import RunStore, utc_iso


class CatalogConflict(ValueError):
    """An internal stale catalog or source-owner publication was refused."""


class CatalogUnavailable(RuntimeError):
    """Committed catalog data is corrupt; never translate this to healthy empty."""


@dataclass(frozen=True)
class Publication:
    revision: int
    changed: bool
    changed_asset_ids: tuple[str, ...]
    removed_asset_ids: tuple[str, ...]


class AssetCatalogStore:
    def __init__(self, store: RunStore) -> None:
        self.store = store

    def _state(self) -> tuple[dict, list[dict], list[dict], list[dict]]:
        with self.store.sql_transaction() as connection:
            meta = dict(connection.execute("SELECT * FROM asset_catalog_meta").fetchone())
            sources = [
                dict(r)
                for r in connection.execute(
                    "SELECT * FROM asset_catalog_sources ORDER BY source_id"
                )
            ]
            assets = [
                dict(r)
                for r in connection.execute("SELECT * FROM asset_catalog_assets ORDER BY asset_id")
            ]
            tombstones = [
                dict(r)
                for r in connection.execute(
                    "SELECT * FROM asset_catalog_tombstones ORDER BY asset_id"
                )
            ]
        return meta, sources, assets, tombstones

    def _documents(self, rows: list[dict]) -> tuple[SearchDocument, ...]:
        documents = []
        try:
            for row in rows:
                record = validate_asset_record(json.loads(row["record_json"]))
                search = json.loads(row["search_json"])
                if set(search) != {"body", "projection_digest"}:
                    raise ValueError
                document = project_asset(
                    record, body=search["body"], redactor=Redactor(), secrets=()
                )
                if (
                    row["asset_id"] != record.asset_id
                    or row["source_id"] != record.source_id
                    or document.projection_digest != row["projection_digest"]
                    or document.projection_digest != search["projection_digest"]
                ):
                    raise ValueError
                documents.append(document)
        except (ValueError, KeyError, TypeError, RecursionError):
            raise CatalogUnavailable("catalog_corrupt") from None
        return tuple(documents)

    def snapshot(self) -> CatalogSnapshot:
        meta, source_rows, asset_rows, _ = self._state()
        documents = self._documents(asset_rows)
        try:
            sources = [
                {
                    key: row[key]
                    for key in (
                        "source_id",
                        "source_version",
                        "content_hash",
                        "captured_at",
                        "last_success_at",
                        "accepted_count",
                        "rejected_count",
                    )
                }
                | {
                    "stale": bool(row["stale"]),
                    "complete": bool(row["complete"]),
                    "exclusions": json.loads(row["exclusions_json"]),
                }
                for row in source_rows
            ]
            source_stale = {s["source_id"]: s["stale"] for s in sources}
            assets = [
                replace(
                    document.record,
                    stale=bool(row["stale"]) or source_stale[document.record.source_id],
                ).to_json_object()
                for document, row in zip(documents, asset_rows, strict=True)
            ]
            return validate_catalog_snapshot(
                {
                    "schema_version": 1,
                    "catalog_revision": meta["revision"],
                    "published_at": meta["published_at"],
                    "sources": sources,
                    "assets": assets,
                }
            )
        except (ValueError, KeyError, TypeError, RecursionError):
            raise CatalogUnavailable("catalog_corrupt") from None

    def search_documents(self) -> tuple[SearchDocument, ...]:
        _, sources, rows, _ = self._state()
        stale = {s["source_id"]: bool(s["stale"]) for s in sources}
        return tuple(
            replace(
                doc,
                record=replace(doc.record, stale=bool(row["stale"]) or stale[doc.record.source_id]),
            )
            for doc, row in zip(self._documents(rows), rows, strict=True)
        )

    def inventory(self, source_id: str) -> SourceInventory:
        meta, _, rows, tombstones = self._state()
        documents = self._documents(rows)
        owners = {r["asset_id"]: r["source_id"] for r in [*rows, *tombstones]}
        return SourceInventory(
            source_id,
            meta["revision"],
            tuple(
                InventoryAsset(doc.record, row["relative_package_path"])
                for doc, row in zip(documents, rows, strict=True)
                if doc.record.source_id == source_id
            ),
            frozenset(r["asset_id"] for r in tombstones if r["source_id"] == source_id),
            freeze_json(owners),
        )

    def publish(
        self,
        capture: SourceCapture,
        *,
        expected_revision: int,
        check: Callable[[], None] = lambda: None,
    ) -> Publication:
        # All parsing/projection hashing is finished before taking the SQL write lock.
        if type(expected_revision) is not int or expected_revision < 0:
            raise CatalogConflict("catalog_revision_conflict")
        check()
        prepared = []
        ids = set()
        for document in capture.documents:
            check()
            record = validate_asset_record(document.record.to_json_object())
            verified = project_asset(record, body=document.body, redactor=Redactor(), secrets=())
            if (
                record.source_id != capture.source_id
                or record.availability != "unknown"
                or record.stale
                or record.asset_id in ids
                or verified.projection_digest != document.projection_digest
            ):
                raise AssetValidationError("invalid_capture")
            ids.add(record.asset_id)
            prepared.append(
                (
                    record.asset_id,
                    canonical_json(record.to_json_object()),
                    canonical_json(
                        {"body": document.body, "projection_digest": document.projection_digest}
                    ),
                    document.projection_digest,
                    capture.package_paths.get(record.asset_id),
                )
            )
        check()
        if ids & capture.removed_asset_ids or (not capture.complete and capture.removed_asset_ids):
            raise AssetValidationError("invalid_capture")
        now = utc_iso(self.store.clock.utc_now())
        changed_ids, removed_ids = [], []
        check()
        with self.store.sql_transaction() as connection:
            check()
            meta = connection.execute("SELECT * FROM asset_catalog_meta").fetchone()
            if meta["revision"] != expected_revision:
                raise CatalogConflict("catalog_revision_conflict")
            self._ensure_source(connection, capture.source_id)
            current = {
                r["asset_id"]: dict(r)
                for r in connection.execute(
                    "SELECT * FROM asset_catalog_assets WHERE source_id=?", (capture.source_id,)
                )
            }
            for asset_id in ids | capture.removed_asset_ids:
                owners = connection.execute(
                    "SELECT source_id FROM asset_catalog_assets WHERE asset_id=? UNION "
                    "SELECT source_id FROM asset_catalog_tombstones WHERE asset_id=?",
                    (asset_id, asset_id),
                ).fetchall()
                if any(row["source_id"] != capture.source_id for row in owners):
                    raise CatalogConflict("source_owner_conflict")
                if asset_id in capture.removed_asset_ids and not owners:
                    raise CatalogConflict("unknown_tombstone")
            if capture.complete and set(current) - ids - capture.removed_asset_ids:
                raise CatalogConflict("incomplete_inventory")
            previous_source = connection.execute(
                "SELECT * FROM asset_catalog_sources WHERE source_id=?", (capture.source_id,)
            ).fetchone()
            for asset_id, record_json, search_json, digest, package in prepared:
                check()
                old = current.get(asset_id)
                if (
                    old is None
                    or old["projection_digest"] != digest
                    or old["relative_package_path"] != package
                ):
                    changed_ids.append(asset_id)
                connection.execute(
                    "INSERT INTO asset_catalog_assets(asset_id,source_id,record_json,search_json,"
                    "projection_digest,relative_package_path,stale) VALUES(?,?,?,?,?,?,0) "
                    "ON CONFLICT(asset_id) DO UPDATE SET record_json=excluded.record_json,"
                    "search_json=excluded.search_json,projection_digest=excluded.projection_digest,"
                    "relative_package_path=excluded.relative_package_path,stale=0",
                    (asset_id, capture.source_id, record_json, search_json, digest, package),
                )
                connection.execute(
                    "DELETE FROM asset_catalog_tombstones WHERE asset_id=?", (asset_id,)
                )
            if not capture.complete:
                for asset_id in set(current) - ids:
                    connection.execute(
                        "UPDATE asset_catalog_assets SET stale=1 WHERE asset_id=?", (asset_id,)
                    )
            for asset_id in sorted(capture.removed_asset_ids):
                check()
                if asset_id in current:
                    removed_ids.append(asset_id)
                connection.execute("DELETE FROM asset_catalog_assets WHERE asset_id=?", (asset_id,))
                connection.execute(
                    "INSERT INTO asset_catalog_tombstones VALUES(?,?,?) "
                    "ON CONFLICT(asset_id) DO NOTHING",
                    (asset_id, capture.source_id, now),
                )
            changed = bool(
                changed_ids
                or removed_ids
                or previous_source["content_hash"] != capture.source_digest
            )
            revision = expected_revision + int(changed)
            connection.execute(
                "DELETE FROM asset_catalog_provenance WHERE source_id=?", (capture.source_id,)
            )
            connection.executemany(
                "INSERT INTO asset_catalog_provenance VALUES(?,?,?,?)",
                [(capture.source_id, f.relative_path, f.sha256, f.size) for f in capture.files],
            )
            accepted = connection.execute(
                "SELECT count(*) FROM asset_catalog_assets WHERE source_id=?", (capture.source_id,)
            ).fetchone()[0]
            connection.execute(
                "UPDATE asset_catalog_sources SET source_version=?,content_hash=?,captured_at=?,"
                "last_success_at=?,stale=0,complete=?,accepted_count=?,rejected_count=0,"
                "exclusions_json='[]',config_digest=?,consecutive_failures=0,next_attempt_at=NULL "
                "WHERE source_id=?",
                (
                    capture.source_version,
                    capture.source_digest,
                    capture.captured_at,
                    now,
                    int(capture.complete),
                    accepted,
                    capture.config_digest,
                    capture.source_id,
                ),
            )
            if changed:
                connection.execute(
                    "UPDATE asset_catalog_meta SET revision=?,published_at=?", (revision, now)
                )
            # Last guard is inside the transaction so exhaustion rolls back all
            # upserts/removals and metadata, using the original job deadline.
            check()
        return Publication(
            revision, changed, tuple(sorted(changed_ids)), tuple(sorted(removed_ids))
        )

    @staticmethod
    def _ensure_source(connection: sqlite3.Connection, source_id: str) -> None:
        connection.execute(
            "INSERT INTO asset_catalog_sources(source_id) VALUES(?) "
            "ON CONFLICT(source_id) DO NOTHING",
            (source_id,),
        )

    def record_failure(self, source_id: str, failure: SourceFailure) -> None:
        with self.store.sql_transaction() as connection:
            self._ensure_source(connection, source_id)
            connection.execute(
                "UPDATE asset_catalog_sources SET stale=1,complete=0,rejected_count=?,"
                "exclusions_json=?,consecutive_failures=consecutive_failures+1 WHERE source_id=?",
                (failure.rejected_count, canonical_json(list(failure.exclusions)), source_id),
            )

    def begin_job(self, source_id: str, config_digest: str) -> str:
        job_id = "assetjob_" + uuid.uuid4().hex
        with self.store.sql_transaction() as connection:
            self._ensure_source(connection, source_id)
            connection.execute(
                "INSERT INTO asset_catalog_jobs(job_id,source_id,config_digest,state,"
                "started_at) VALUES(?,?,?,'running',?)",
                (job_id, source_id, config_digest, utc_iso(self.store.clock.utc_now())),
            )
            # This schedule belongs to the attempt starting now. A crash of
            # this attempt must derive a new backoff from its new failure count.
            connection.execute(
                "UPDATE asset_catalog_sources SET next_attempt_at=NULL WHERE source_id=?",
                (source_id,),
            )
        return job_id

    def finish_job(
        self,
        job_id: str,
        *,
        state: str,
        reason: str | None,
        duration_ms: float,
        received_bytes: int,
        batch_ms: tuple[float, ...] = (),
        overlap_observed: bool = False,
        drain_ms: float | None = None,
    ) -> None:
        if state not in ("succeeded", "failed", "cancelled"):
            raise ValueError("invalid_job_state")
        with self.store.sql_transaction() as connection:
            connection.execute(
                "UPDATE asset_catalog_jobs SET state=?,reason=?,duration_ms=?,"
                "received_bytes=?,finished_at=?,batch_ms_json=?,overlap_observed=?,drain_ms=? "
                "WHERE job_id=? AND state='running'",
                (
                    state,
                    reason,
                    duration_ms,
                    received_bytes,
                    utc_iso(self.store.clock.utc_now()),
                    canonical_json(list(batch_ms)),
                    int(overlap_observed),
                    drain_ms,
                    job_id,
                ),
            )
            connection.execute(
                "DELETE FROM asset_catalog_jobs WHERE state!='running' AND job_id "
                "NOT IN (SELECT job_id FROM asset_catalog_jobs WHERE state!='running' "
                "ORDER BY started_at DESC,job_id DESC LIMIT 1000)"
            )

    def job_outcomes(self) -> tuple[dict[str, Any], ...]:
        with self.store.sql_transaction() as connection:
            outcomes = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM asset_catalog_jobs ORDER BY started_at DESC,job_id DESC"
                )
            ]
        for outcome in outcomes:
            outcome["batch_ms"] = json.loads(outcome.pop("batch_ms_json"))
            outcome["overlap_observed"] = bool(outcome["overlap_observed"])
        return tuple(outcomes)

    def recover_interrupted_jobs(self) -> int:
        with self.store.sql_transaction() as connection:
            rows = connection.execute(
                "SELECT job_id,source_id FROM asset_catalog_jobs WHERE state='running'"
            ).fetchall()
            for row in rows:
                self.record_failure(
                    row["source_id"], SourceFailure("interrupted", rejected_count=0)
                )
                connection.execute(
                    "UPDATE asset_catalog_jobs SET state='interrupted',reason='interrupted',"
                    "finished_at=? WHERE job_id=?",
                    (utc_iso(self.store.clock.utc_now()), row["job_id"]),
                )
            return len(rows)

    def schedules(self) -> tuple[tuple[str, int, str | None], ...]:
        with self.store.sql_transaction() as connection:
            return tuple(
                (r["source_id"], r["consecutive_failures"], r["next_attempt_at"])
                for r in connection.execute("SELECT * FROM asset_catalog_sources")
            )

    def schedule_retry(self, source_id: str, next_attempt_at: str) -> None:
        with self.store.sql_transaction() as connection:
            connection.execute(
                "UPDATE asset_catalog_sources SET next_attempt_at=? WHERE source_id=?",
                (next_attempt_at, source_id),
            )
