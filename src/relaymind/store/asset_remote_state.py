"""Private installed-profile cooldown; source aliases cannot reset it (RM43/RM17)."""

from datetime import datetime, timedelta

from relaymind.assets.sources import SourceFailure
from relaymind.store.run_store import RunStore, utc_iso


class RemoteSourceStateStore:
    def __init__(self, store: RunStore) -> None:
        self.store = store
        with store.sql_transaction() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS asset_remote_state "
                "(profile_id TEXT PRIMARY KEY,not_before_at TEXT NOT NULL)"
            )

    def not_before(self, profile_id: str) -> str | None:
        with self.store.sql_read() as db:
            row = db.execute(
                "SELECT not_before_at FROM asset_remote_state WHERE profile_id=?", (profile_id,)
            ).fetchone()
        return None if row is None else row[0]

    def defer_until(self, profile_id: str, timestamp: str) -> None:
        canonical = utc_iso(datetime.fromisoformat(timestamp))
        with self.store.sql_transaction() as db:
            db.execute(
                "INSERT INTO asset_remote_state VALUES(?,?) ON CONFLICT(profile_id) "
                "DO UPDATE SET not_before_at=MAX(not_before_at,excluded.not_before_at)",
                (profile_id, canonical),
            )

    def reserve_poll(self, profile_id: str, now: datetime, minimum_seconds: int) -> None:
        """Atomically fence a second process/owner before its first HTTP dispatch."""
        instant = utc_iso(now)
        with self.store.sql_transaction() as db:
            row = db.execute(
                "SELECT not_before_at FROM asset_remote_state WHERE profile_id=?", (profile_id,)
            ).fetchone()
            if row is not None and row[0] > instant:
                raise SourceFailure("source_unavailable")
            self.defer_until(profile_id, utc_iso(now + timedelta(seconds=minimum_seconds)))
