"""Fsynced per-request evidence and conservative cell resume (RM16/RM25)."""

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def atomic_json(path: Path, value: object) -> None:
    """Publish one complete checkpoint, syncing bytes before atomic replacement."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    if os.name != "nt":
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class Checkpoint:
    """Restart only finalized cells. Partial sequences retain all available evidence."""

    def __init__(self, path: Path, identity: dict[str, Any]) -> None:
        self.path = path
        if path.exists():
            self.document = json.loads(path.read_text("utf-8"))
            if self.document["identity"] != identity:
                raise ValueError("checkpoint identity differs")
            if not self.document["completed"]:
                raise ValueError(
                    "interrupted cell: preserve this attempt; explicitly declare a new attempt"
                )
        else:
            self.document = dict(
                schema_version=1,
                identity=identity,
                started_at_utc=utc_now(),
                completed=False,
                steps=[],
            )
            self._save()

    def _save(self) -> None:
        atomic_json(self.path, self.document)

    def start(self, identity: str) -> None:
        if self.document["completed"] or any(
            s["identity"] == identity for s in self.document["steps"]
        ):
            raise ValueError("checkpoint cannot repeat a measured request")
        self.document["steps"].append(
            dict(identity=identity, started_at_utc=utc_now(), result=None)
        )
        self._save()

    def finish(self, identity: str, result: dict) -> None:
        step = self.document["steps"][-1]
        if step["identity"] != identity or step["result"] is not None:
            raise ValueError("checkpoint completion does not match pending request")
        step.update(result=result, finished_at_utc=utc_now())
        self._save()

    def complete(self, result: dict) -> None:
        if any(s["result"] is None for s in self.document["steps"]):
            raise ValueError("checkpoint has an unfinished request")
        self.document.update(completed=True, result=result, finished_at_utc=utc_now())
        self._save()
