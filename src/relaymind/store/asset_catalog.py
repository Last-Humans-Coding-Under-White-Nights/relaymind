"""Atomic independent asset generations over the shared RunStore (RM34, RM17)."""

import json
import sqlite3
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from relaymind.assets.catalog import (
    MAX_OFFER_BYTES,
    MAX_OFFER_RECORDS,
    AssetRecord,
    AssetValidationError,
    CatalogSnapshot,
    CatalogUnavailable,
    InventoryAsset,
    SearchDocument,
    SourceCapture,
    SourceInventory,
    acquisition_budgets,
    failure_exclusions,
    project_asset,
    projection_digest,
    text,
    validate_asset_record,
    validate_catalog_snapshot,
)
from relaymind.canonical import canonical_json
from relaymind.redaction import Redactor
from relaymind.schema_util import freeze_json
from relaymind.store.index_job_event import CANCELLED_REASONS, FAILED_REASONS, validate_job_event
from relaymind.store.run_store import RunStore, append_event, utc_iso

# The snapshot-visible source fields. Observation times (captured_at,
# last_success_at) are not content identity and never bump the revision.
_VISIBLE_SOURCE_FIELDS = (
    "source_version",
    "content_hash",
    "stale",
    "complete",
    "accepted_count",
    "rejected_count",
    "exclusions_json",
)


class CatalogConflict(ValueError):
    """An internal stale catalog or source-owner publication was refused."""


@dataclass(frozen=True)
class Publication:
    revision: int
    changed: bool
    changed_asset_ids: tuple[str, ...]
    removed_asset_ids: tuple[str, ...]


@dataclass(frozen=True)
class PreparedPublication:
    """A capture re-verified for commit; rows are (id, record, search, digest, package)."""

    capture: SourceCapture
    rows: tuple[tuple[str, str, str, str, str | None], ...]
    ids: frozenset[str]
    records: tuple[AssetRecord, ...]


def _effective_stale(row: Mapping[str, Any], source_stale: Mapping[str, bool]) -> bool:
    return bool(row["stale"]) or source_stale[row["source_id"]]


# The same rule as _effective_stale, for rows whose stored record bytes disagree.
_STALE_MISMATCH = (
    "FROM asset_catalog_assets a JOIN asset_catalog_sources s ON s.source_id=a.source_id "
    "WHERE (a.stale OR s.stale) != json_extract(a.record_json,'$.stale')"
)


def _record_json_with_stale(record_json: str, stale: bool) -> str:
    """One validated, canonical rewrite of a stored record's stale flag."""
    value = json.loads(record_json)
    value["stale"] = stale
    return canonical_json(validate_asset_record(value).to_json_object())


def _require_projection(
    documents: tuple[SearchDocument, ...], rows: list[dict], source_stale: Mapping[str, bool]
) -> None:
    """Stored record bytes are authoritative only when they carry effective stale."""
    if any(
        document.record.stale != _effective_stale(row, source_stale)
        for document, row in zip(documents, rows, strict=True)
    ):
        raise CatalogUnavailable("catalog_projection_unavailable")


class AssetCatalogStore:
    def __init__(self, store: RunStore) -> None:
        self.store = store
        self._normalize_stale_bytes()

    def initialize_evaluation_offer(
        self,
        documents: tuple[SearchDocument, ...],
        *,
        config_digests: Mapping[str, str],
        check: Callable[[], None] = lambda: None,
    ) -> CatalogSnapshot:
        """Install a locked evaluation offer once in an unused private store.

        This trusted catalog-input seam grants no source acquisition/qualification
        or host authority. Preserve every record byte/state; invent neither a
        successful source capture nor an acquisition job. Ordinary stores cannot
        be replaced by this fresh-store-only initialization.
        """
        check()
        if len(documents) > MAX_OFFER_RECORDS:
            raise AssetValidationError("offer_size_limit")
        rows, source_counts, seen = [], {}, set()
        byte_count = 2
        for document in documents:
            check()
            record = validate_asset_record(document.record.to_json_object())
            verified = project_asset(record, body=None, redactor=Redactor(), secrets=())
            if (
                document.body is not None
                or verified.projection_digest != document.projection_digest
                or record.asset_id in seen
                or record.source_id not in config_digests
            ):
                raise AssetValidationError("invalid_evaluation_offer")
            config_digest = config_digests[record.source_id]
            if (
                not isinstance(config_digest, str)
                or len(config_digest) != 64
                or any(char not in "0123456789abcdef" for char in config_digest)
            ):
                raise AssetValidationError("invalid_evaluation_offer")
            seen.add(record.asset_id)
            encoded = canonical_json(record.to_json_object())
            byte_count += len(encoded.encode("utf-8")) + int(bool(rows))
            if byte_count > MAX_OFFER_BYTES:
                raise AssetValidationError("offer_size_limit")
            source_counts[record.source_id] = source_counts.get(record.source_id, 0) + 1
            rows.append(
                (
                    record.asset_id,
                    record.source_id,
                    encoded,
                    canonical_json({"body": None, "projection_digest": document.projection_digest}),
                    document.projection_digest,
                    None,
                    int(record.stale),
                )
            )
        with self.store.sql_transaction() as db:
            check()
            meta = db.execute("SELECT * FROM asset_catalog_meta").fetchone()
            if meta["revision"] != 0 or meta["published_at"] != "1970-01-01T00:00:00.000000Z":
                raise CatalogConflict("evaluation_store_not_fresh")
            for table in (
                "runs",
                "recommendations",
                "asset_catalog_sources",
                "asset_catalog_assets",
                "asset_catalog_jobs",
                "asset_catalog_tombstones",
            ):
                if db.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone():
                    raise CatalogConflict("evaluation_store_not_fresh")
            for source_id, count in source_counts.items():
                self._ensure_source(db, source_id)
                # No captured_at/content_hash/last_success_at is fabricated.
                # The evaluation provider separately labels its locked input.
                db.execute(
                    "UPDATE asset_catalog_sources SET stale=0,accepted_count=?,config_digest=? "
                    "WHERE source_id=?",
                    (count, config_digests[source_id], source_id),
                )
            db.executemany("INSERT INTO asset_catalog_assets VALUES(?,?,?,?,?,?,?)", rows)
            db.execute(
                "UPDATE asset_catalog_meta SET revision=?,published_at=?",
                (int(bool(rows)), utc_iso(self.store.clock.utc_now())),
            )
            check()
        return self.snapshot()

    def _normalize_stale_bytes(self) -> None:
        """Rewrite rows stored before record bytes carried the effective stale flag.

        An older writer set only the stale column. A repaired projection is a new
        usable generation, so the revision advances once. A row that cannot be
        validated is left alone: readers then report the catalog unavailable.
        """
        with self.store.sql_transaction() as connection:
            rows = connection.execute(
                f"SELECT a.asset_id, a.record_json, (a.stale OR s.stale) AS effective "
                f"{_STALE_MISMATCH}"
            ).fetchall()
            if not rows:
                return
            try:
                prepared = [
                    (
                        _record_json_with_stale(row["record_json"], bool(row["effective"])),
                        row["asset_id"],
                    )
                    for row in rows
                ]
            except (ValueError, TypeError, KeyError, RecursionError):
                return
            connection.executemany(
                "UPDATE asset_catalog_assets SET record_json=? WHERE asset_id=?", prepared
            )
            connection.execute(
                "UPDATE asset_catalog_meta SET revision=revision+1,published_at=?",
                (utc_iso(self.store.clock.utc_now()),),
            )

    def _state(self) -> tuple[dict, list[dict], list[dict]]:
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
        return meta, sources, assets

    def _documents(self, rows: list[dict]) -> tuple[SearchDocument, ...]:
        """Committed rows, validated once; integrity is the stored projection digest."""
        documents = []
        try:
            for row in rows:
                record = validate_asset_record(json.loads(row["record_json"]))
                search = json.loads(row["search_json"])
                if set(search) != {"body", "projection_digest"}:
                    raise ValueError
                body = search["body"]
                if body is not None:
                    text(body, 262144)
                digest = projection_digest(record, body)
                if (
                    row["asset_id"] != record.asset_id
                    or row["source_id"] != record.source_id
                    or digest != row["projection_digest"]
                    or digest != search["projection_digest"]
                ):
                    raise ValueError
                documents.append(SearchDocument(record, body, digest))
        except (ValueError, KeyError, TypeError, RecursionError):
            raise CatalogUnavailable("catalog_corrupt") from None
        return tuple(documents)

    def snapshot(self) -> CatalogSnapshot:
        meta, source_rows, asset_rows = self._state()
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
            _require_projection(
                documents, asset_rows, {s["source_id"]: s["stale"] for s in sources}
            )
            snapshot = validate_catalog_snapshot(
                {
                    "schema_version": 1,
                    "catalog_revision": meta["revision"],
                    "published_at": meta["published_at"],
                    "sources": sources,
                    "assets": [document.record.to_json_object() for document in documents],
                }
            )
            return replace(
                snapshot,
                record_bytes=tuple(row["record_json"].encode("utf-8") for row in asset_rows),
            )
        except (ValueError, KeyError, TypeError, RecursionError):
            raise CatalogUnavailable("catalog_corrupt") from None

    def revision(self) -> int:
        """Cheap current revision pin; a snapshot is validated at publication."""
        with self.store.sql_read() as connection:
            try:
                incompatible = connection.execute(f"SELECT 1 {_STALE_MISMATCH} LIMIT 1").fetchone()
            except sqlite3.Error:
                raise CatalogUnavailable("catalog_corrupt") from None
            if incompatible:
                raise CatalogUnavailable("catalog_projection_unavailable")
            return connection.execute("SELECT revision FROM asset_catalog_meta").fetchone()[0]

    def search_documents(self) -> tuple[SearchDocument, ...]:
        _, sources, rows = self._state()
        documents = self._documents(rows)
        _require_projection(documents, rows, {s["source_id"]: bool(s["stale"]) for s in sources})
        return documents

    def inventory(self, source_id: str) -> SourceInventory:
        """One source's committed records, plus every asset ID's owning source."""
        with self.store.sql_transaction() as connection:
            revision = connection.execute("SELECT revision FROM asset_catalog_meta").fetchone()[0]
            rows = [
                dict(r)
                for r in connection.execute(
                    "SELECT * FROM asset_catalog_assets WHERE source_id=? ORDER BY asset_id",
                    (source_id,),
                )
            ]
            tombstones = frozenset(
                r[0]
                for r in connection.execute(
                    "SELECT asset_id FROM asset_catalog_tombstones WHERE source_id=?",
                    (source_id,),
                )
            )
            owners = {
                r[0]: r[1]
                for r in connection.execute(
                    "SELECT asset_id,source_id FROM asset_catalog_assets UNION ALL "
                    "SELECT asset_id,source_id FROM asset_catalog_tombstones"
                )
            }
        return SourceInventory(
            source_id,
            revision,
            tuple(
                InventoryAsset(doc.record, row["relative_package_path"])
                for doc, row in zip(self._documents(rows), rows, strict=True)
            ),
            tombstones,
            freeze_json(owners),
        )

    def prepare(
        self, capture: SourceCapture, *, check: Callable[[], None] = lambda: None
    ) -> PreparedPublication:
        """Re-verify every captured projection; CPU work only, safe off the event loop."""
        check()
        rows, records = [], []
        ids: set[str] = set()
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
            records.append(record)
            rows.append(
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
        # Spec 0.66 section 4.6.1: excluded candidates make a partial generation.
        if (
            ids & capture.removed_asset_ids
            or (not capture.complete and capture.removed_asset_ids)
            or (capture.complete and capture.exclusions)
        ):
            raise AssetValidationError("invalid_capture")
        return PreparedPublication(capture, tuple(rows), frozenset(ids), tuple(records))

    def preview(
        self,
        prepared: PreparedPublication,
        *,
        expected_revision: int,
        base: CatalogSnapshot,
        check: Callable[[], None] = lambda: None,
    ) -> CatalogSnapshot:
        """Prepare effective offered records before a source CAS; no rows are mutated.

        The source commit still owns final identifier/ownership/deadline checks.
        Existing published bytes survive a capture-only observation unchanged.
        """
        check()
        if base.catalog_revision != expected_revision:
            raise CatalogConflict("catalog_revision_conflict")
        with self.store.sql_read() as db:
            revision = db.execute("SELECT revision FROM asset_catalog_meta").fetchone()[0]
            source = db.execute(
                "SELECT * FROM asset_catalog_sources WHERE source_id=?",
                (prepared.capture.source_id,),
            ).fetchone()
            old_rows = {
                r["asset_id"]: dict(r)
                for r in db.execute(
                    "SELECT asset_id,projection_digest,relative_package_path,stale "
                    "FROM asset_catalog_assets WHERE source_id=?",
                    (prepared.capture.source_id,),
                )
            }
        if revision != expected_revision:
            raise CatalogConflict("catalog_revision_conflict")
        capture = prepared.capture
        previous = (
            dict(source)
            if source is not None
            else dict(
                source_version=None,
                content_hash=None,
                stale=1,
                complete=0,
                accepted_count=0,
                rejected_count=0,
                exclusions_json="[]",
            )
        )
        records = {r.asset_id: r for r in base.assets}
        encoded = dict(zip((r.asset_id for r in base.assets), base.record_bytes, strict=True))
        changed = False
        for row, record in zip(prepared.rows, prepared.records, strict=True):
            check()
            asset_id, record_json, _, digest, package = row
            old = old_rows.get(asset_id)
            if (
                old is None
                or old["projection_digest"] != digest
                or old["relative_package_path"] != package
                or old["stale"]
                or previous["stale"]
            ):
                changed = True
                records[asset_id] = record
                encoded[asset_id] = record_json.encode("utf-8")
        if not capture.complete:
            for asset_id in old_rows.keys() - prepared.ids:
                check()
                record = records[asset_id]
                if not record.stale:
                    changed = True
                    records[asset_id] = replace(record, stale=True)
                    encoded[asset_id] = canonical_json(records[asset_id].to_json_object()).encode()
        for asset_id in capture.removed_asset_ids:
            if asset_id in records:
                changed = True
                records.pop(asset_id)
                encoded.pop(asset_id)
        accepted = sum(r.source_id == capture.source_id for r in records.values())
        exclusions = [dict(e) for e in capture.exclusions]
        after = dict(
            source_version=capture.source_version,
            content_hash=capture.source_digest,
            stale=0,
            complete=int(capture.complete),
            accepted_count=accepted,
            rejected_count=capture.rejected_count,
            exclusions_json=canonical_json(exclusions),
        )
        changed = changed or any(previous[k] != v for k, v in after.items())
        source_view = dict(
            source_id=capture.source_id,
            source_version=capture.source_version,
            content_hash=capture.source_digest,
            captured_at=capture.captured_at,
            last_success_at=utc_iso(self.store.clock.utc_now()),
            accepted_count=accepted,
            rejected_count=capture.rejected_count,
            stale=False,
            complete=capture.complete,
            exclusions=exclusions,
        )
        sources = {s["source_id"]: s for s in base.sources}
        sources[capture.source_id] = freeze_json(source_view)
        ordered = tuple(records[k] for k in sorted(records))
        check()
        return CatalogSnapshot(
            1,
            expected_revision + int(changed),
            utc_iso(self.store.clock.utc_now()) if changed else base.published_at,
            tuple(sources[k] for k in sorted(sources)),
            ordered,
            tuple(encoded[r.asset_id] for r in ordered),
        )

    def publish(
        self,
        capture: SourceCapture,
        *,
        expected_revision: int,
        check: Callable[[], None] = lambda: None,
    ) -> Publication:
        return self.commit(
            self.prepare(capture, check=check), expected_revision=expected_revision, check=check
        )

    def commit(
        self,
        prepared: PreparedPublication,
        *,
        expected_revision: int,
        check: Callable[[], None] = lambda: None,
    ) -> Publication:
        # All parsing/projection hashing is finished before taking the SQL write lock.
        if type(expected_revision) is not int or expected_revision < 0:
            raise CatalogConflict("catalog_revision_conflict")
        capture, ids = prepared.capture, prepared.ids
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
            touched = ids | capture.removed_asset_ids
            owned_elsewhere, known = set(), set()
            for row in connection.execute(
                "SELECT asset_id,source_id FROM asset_catalog_assets UNION ALL "
                "SELECT asset_id,source_id FROM asset_catalog_tombstones"
            ):
                if row["asset_id"] in touched:
                    known.add(row["asset_id"])
                    if row["source_id"] != capture.source_id:
                        owned_elsewhere.add(row["asset_id"])
            if owned_elsewhere:
                raise CatalogConflict("source_owner_conflict")
            if capture.removed_asset_ids - known:
                raise CatalogConflict("unknown_tombstone")
            if capture.complete and set(current) - ids - capture.removed_asset_ids:
                raise CatalogConflict("incomplete_inventory")
            previous_source = connection.execute(
                "SELECT * FROM asset_catalog_sources WHERE source_id=?", (capture.source_id,)
            ).fetchone()
            # Content and effective stale changes both publish new bytes;
            # observation-only capture leaves the committed representation intact.
            stale_changed = False
            for asset_id, record_json, search_json, digest, package in prepared.rows:
                check()
                old = current.get(asset_id)
                content_changed = (
                    old is None
                    or old["projection_digest"] != digest
                    or old["relative_package_path"] != package
                )
                effective_stale_changed = old is not None and bool(
                    old["stale"] or previous_source["stale"]
                )
                if content_changed:
                    changed_ids.append(asset_id)
                stale_changed = stale_changed or effective_stale_changed
                if not content_changed and not effective_stale_changed:
                    continue
                connection.execute(
                    "INSERT INTO asset_catalog_assets(asset_id,source_id,record_json,search_json,"
                    "projection_digest,relative_package_path,stale) VALUES(?,?,?,?,?,?,0) "
                    "ON CONFLICT(asset_id) DO UPDATE SET record_json=excluded.record_json,"
                    "search_json=excluded.search_json,projection_digest=excluded.projection_digest,"
                    "relative_package_path=excluded.relative_package_path,stale=0",
                    (asset_id, capture.source_id, record_json, search_json, digest, package),
                )
            connection.executemany(
                "DELETE FROM asset_catalog_tombstones WHERE asset_id=?", [(i,) for i in ids]
            )
            if not capture.complete:
                for asset_id, old in current.items():
                    if asset_id in ids:
                        continue
                    check()
                    stale_changed = stale_changed or not bool(
                        old["stale"] or previous_source["stale"]
                    )
                    if not old["stale"] or not json.loads(old["record_json"])["stale"]:
                        connection.execute(
                            "UPDATE asset_catalog_assets SET stale=1,record_json=? "
                            "WHERE asset_id=?",
                            (_record_json_with_stale(old["record_json"], True), asset_id),
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
            exclusions_json = canonical_json([dict(e) for e in capture.exclusions])
            source_after = {
                "source_version": capture.source_version,
                "content_hash": capture.source_digest,
                "stale": 0,
                "complete": int(capture.complete),
                "accepted_count": accepted,
                "rejected_count": capture.rejected_count,
                "exclusions_json": exclusions_json,
            }
            connection.execute(
                "UPDATE asset_catalog_sources SET source_version=?,content_hash=?,captured_at=?,"
                "last_success_at=?,stale=0,complete=?,accepted_count=?,rejected_count=?,"
                "exclusions_json=?,config_digest=?,consecutive_failures=0,next_attempt_at=NULL "
                "WHERE source_id=?",
                (
                    capture.source_version,
                    capture.source_digest,
                    capture.captured_at,
                    now,
                    int(capture.complete),
                    accepted,
                    capture.rejected_count,
                    exclusions_json,
                    capture.config_digest,
                    capture.source_id,
                ),
            )
            # Spec 4.6: one increment for each committed generation whose
            # snapshot-visible state changed, including stale/complete/exclusions.
            changed = bool(
                changed_ids
                or removed_ids
                or stale_changed
                or any(previous_source[k] != v for k, v in source_after.items())
            )
            revision = expected_revision + int(changed)
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

    def record_failure(
        self,
        source_id: str,
        *,
        rejected_count: int,
        exclusions: tuple[Mapping[str, Any], ...],
    ) -> None:
        with self.store.sql_transaction() as connection:
            self._record_failure(connection, source_id, rejected_count, exclusions)

    def _record_failure(
        self,
        connection: sqlite3.Connection,
        source_id: str,
        rejected_count: int,
        exclusions: tuple[Mapping[str, Any], ...],
    ) -> None:
        self._ensure_source(connection, source_id)
        before = connection.execute(
            "SELECT stale,complete,rejected_count,exclusions_json FROM asset_catalog_sources "
            "WHERE source_id=?",
            (source_id,),
        ).fetchone()
        rows = connection.execute(
            "SELECT asset_id,record_json FROM asset_catalog_assets WHERE source_id=?",
            (source_id,),
        ).fetchall()
        prepared = []
        try:
            for row in rows:
                if not json.loads(row["record_json"])["stale"]:
                    prepared.append(
                        (_record_json_with_stale(row["record_json"], True), row["asset_id"])
                    )
        except (ValueError, TypeError, KeyError, RecursionError):
            # Keep the source failure visible even when its matching projection
            # could not be prepared. Readers reject the mismatched old bytes.
            prepared = None
        if prepared:
            connection.executemany(
                "UPDATE asset_catalog_assets SET record_json=?,stale=1 WHERE asset_id=?", prepared
            )
        exclusions_json = canonical_json([dict(e) for e in exclusions])
        connection.execute(
            "UPDATE asset_catalog_sources SET stale=1,complete=0,rejected_count=?,"
            "exclusions_json=?,consecutive_failures=consecutive_failures+1 WHERE source_id=?",
            (rejected_count, exclusions_json, source_id),
        )
        # Preserve the parent's broader source-visible revision rule. A failed
        # byte preparation can advance metadata but is never a usable published
        # offer. Repairing those bytes later advances the revision once again.
        if prepared or tuple(before) != (1, 0, rejected_count, exclusions_json):
            connection.execute(
                "UPDATE asset_catalog_meta SET revision=revision+1,published_at=?",
                (utc_iso(self.store.clock.utc_now()),),
            )

    def _job_event(self, connection, job_id, event_type, payload, *, timestamp=None):
        stamp = timestamp or utc_iso(self.store.clock.utc_now())
        append_event(
            connection,
            "asset_index_job_events",
            "job_id",
            job_id,
            lambda seq: validate_job_event(
                dict(
                    schema_version=1,
                    job_id=job_id,
                    seq=seq,
                    timestamp=stamp,
                    event_type=event_type,
                    payload=payload,
                )
            ),
        )

    def job_events(self, job_id: str) -> tuple[Mapping, ...]:
        """Actual evidence only; legacy jobs may have unavailable initial history."""
        with self.store.sql_read() as connection:
            return tuple(
                validate_job_event(json.loads(row[0]))
                for row in connection.execute(
                    "SELECT event_json FROM asset_index_job_events WHERE job_id=? ORDER BY seq",
                    (job_id,),
                )
            )

    def job_event_stream(self, *, limit: int = 100000) -> tuple[Mapping, ...]:
        """Bounded actual insertion order across jobs, never UUID/time sorting."""
        if type(limit) is not int or not 0 <= limit <= 100000:
            raise ValueError("job_evidence_limit")
        events, size = [], 2
        with self.store.sql_read() as db:
            for row in db.execute(
                "SELECT event_json FROM asset_index_job_events ORDER BY rowid LIMIT ?",
                (limit + 1,),
            ):
                if len(events) == limit:
                    raise ValueError("job_evidence_limit")
                size += len(row[0].encode("utf-8")) + int(bool(events))
                if size > 256 * 1024 * 1024:
                    raise ValueError("job_evidence_limit")
                events.append(validate_job_event(json.loads(row[0])))
        return tuple(events)

    def begin_job(
        self, source_id: str | None, config_digest: str, *, job_kind: str = "refresh"
    ) -> str:
        if job_kind not in ("refresh", "lexical", "embedding"):
            raise ValueError("invalid_job_kind")
        job_id = "assetjob_" + uuid.uuid4().hex
        with self.store.sql_transaction() as connection:
            if source_id is not None:
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
            revision = connection.execute("SELECT revision FROM asset_catalog_meta").fetchone()[0]
            self._job_event(
                connection,
                job_id,
                "queued",
                dict(
                    source_id=source_id,
                    catalog_revision=revision,
                    config_digest=config_digest,
                    job_kind=job_kind,
                    budgets=acquisition_budgets(),
                ),
            )
            self._job_event(
                connection, job_id, "started", dict(catalog_revision=revision, index_revision=None)
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
        batches: tuple[Mapping, ...] = (),
        accepted_count: int = 0,
        rejected_count: int = 0,
        index_revision: str | None = None,
    ) -> None:
        if state not in ("succeeded", "failed", "cancelled"):
            raise ValueError("invalid_job_state")
        with self.store.sql_transaction() as connection:
            row = connection.execute(
                "SELECT state FROM asset_catalog_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            if row is None or row["state"] != "running":
                return
            for index, batch in enumerate(batches, 1):
                self._job_event(
                    connection,
                    job_id,
                    "batch.finished",
                    dict(
                        batch=index,
                        accepted_count=batch["accepted_count"],
                        rejected_count=batch["rejected_count"],
                        received_bytes=batch["received_bytes"],
                        duration_ms=batch["duration_ms"],
                        embeddings=batch.get("embeddings", []),
                    ),
                    timestamp=batch["timestamp"],
                )
            revision = connection.execute("SELECT revision FROM asset_catalog_meta").fetchone()[0]
            if state == "succeeded":
                self._job_event(
                    connection,
                    job_id,
                    "published",
                    dict(
                        catalog_revision=revision,
                        index_revision=index_revision,
                        accepted_count=accepted_count,
                        rejected_count=rejected_count,
                        duration_ms=duration_ms,
                    ),
                )
            elif state == "cancelled":
                self._job_event(
                    connection,
                    job_id,
                    "cancelled",
                    dict(
                        reason=reason if reason in CANCELLED_REASONS else "operator",
                        duration_ms=duration_ms,
                        retained_catalog_revision=revision,
                    ),
                )
            else:
                safe_reason = reason if reason in FAILED_REASONS else "index_error"
                self._job_event(
                    connection,
                    job_id,
                    "failed",
                    dict(
                        reason=safe_reason,
                        duration_ms=duration_ms,
                        retained_catalog_revision=revision,
                        received_bytes=received_bytes,
                        accepted_count=accepted_count,
                        rejected_count=rejected_count,
                    ),
                )
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

    def job_counts(self) -> dict[str, int]:
        """How many jobs are in each state, for readiness, without loading their history."""
        with self.store.sql_read() as connection:
            return {
                row[0]: row[1]
                for row in connection.execute(
                    "SELECT state, COUNT(*) FROM asset_catalog_jobs GROUP BY state"
                )
            }

    def lexical_generation(self, *, snapshot: CatalogSnapshot | None = None) -> Mapping | None:
        """Derived compatibility only; original records and feedback retain their owners."""
        with self.store.sql_read() as db:
            row = db.execute("SELECT * FROM asset_lexical_generation").fetchone()
        if row is None:
            return None
        try:
            value = dict(row)
            ids = json.loads(value.pop("eligible_ids_json"))
            assets = json.loads(value.pop("asset_ids_json"))
            if (
                not isinstance(ids, list)
                or not isinstance(assets, list)
                or any(not isinstance(k, str) or not k for k in ids)
                or any(not isinstance(k, str) or not k for k in assets)
                or ids != sorted(set(ids))
                or assets != sorted(set(assets))
                or not set(ids) <= set(assets)
                or type(value["catalog_revision"]) is not int
                or value["catalog_revision"] < 0
                or (
                    snapshot is not None
                    and (
                        value["catalog_revision"] > snapshot.catalog_revision
                        or (
                            value["catalog_revision"] == snapshot.catalog_revision
                            and assets != [r.asset_id for r in snapshot.assets]
                        )
                    )
                )
                or not value["representation"]
                or len(value["config_digest"]) != 64
            ):
                raise ValueError
            return freeze_json(value | dict(eligible_ids=ids, asset_ids=assets))
        except (ValueError, TypeError, KeyError):
            raise CatalogUnavailable("catalog_corrupt") from None

    def commit_lexical_generation(
        self,
        *,
        expected_revision: int,
        eligible_ids: tuple[str, ...],
        asset_ids: tuple[str, ...],
        representation: str,
        config_digest: str,
        advance: bool = False,
        check: Callable[[], None] = lambda: None,
    ) -> int:
        with self.store.sql_transaction() as db:
            check()
            revision = db.execute("SELECT revision FROM asset_catalog_meta").fetchone()[0]
            if revision != expected_revision:
                raise CatalogConflict("catalog_revision_conflict")
            revision += int(advance)
            if advance:
                db.execute(
                    "UPDATE asset_catalog_meta SET revision=?,published_at=?",
                    (revision, utc_iso(self.store.clock.utc_now())),
                )
            db.execute(
                "INSERT INTO asset_lexical_generation VALUES(1,?,?,?,?,?) "
                "ON CONFLICT(singleton) DO UPDATE SET catalog_revision=excluded.catalog_revision,"
                "asset_ids_json=excluded.asset_ids_json,"
                "eligible_ids_json=excluded.eligible_ids_json,representation=excluded.representation,"
                "config_digest=excluded.config_digest",
                (
                    revision,
                    canonical_json(sorted(asset_ids)),
                    canonical_json(sorted(eligible_ids)),
                    representation,
                    config_digest,
                ),
            )
            check()
        return revision

    def snapshot_with_provenance(self) -> tuple[CatalogSnapshot, dict[str, Mapping]]:
        """One consistent read: the snapshot and each source's provenance row."""
        with self.store.sql_transaction():
            snapshot = self.snapshot()
            rows = self.source_provenance()
        return snapshot, rows

    def source_provenance(self) -> dict[str, Mapping]:
        with self.store.sql_read() as connection:
            return {
                row["source_id"]: dict(row)
                for row in connection.execute(
                    "SELECT source_id,config_digest,last_success_at,captured_at,content_hash "
                    "FROM asset_catalog_sources"
                )
            }

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
                "SELECT job_id,source_id,duration_ms,received_bytes FROM asset_catalog_jobs "
                "WHERE state='running'"
            ).fetchall()
            for row in rows:
                # A derived-only rebuild (null source) has no source state to mark.
                if row["source_id"] is not None:
                    self._record_failure(
                        connection, row["source_id"], 0, failure_exclusions("interrupted")
                    )
                revision = connection.execute("SELECT revision FROM asset_catalog_meta").fetchone()[
                    0
                ]
                self._job_event(
                    connection,
                    row["job_id"],
                    "failed",
                    dict(
                        reason="interrupted",
                        duration_ms=row["duration_ms"] or 0,
                        retained_catalog_revision=revision,
                        received_bytes=row["received_bytes"],
                        accepted_count=0,
                        rejected_count=0,
                    ),
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
