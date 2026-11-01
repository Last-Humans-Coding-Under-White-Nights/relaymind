"""Crash-safe progress journal for the section 12.6.5 run drivers (#236, #237).

Spec 0.65 section 12.6.5 (RM16, RM42, RM44; AC33, AC36). A journal is a private
working file, never an export or evidence: one canonical JSON record per line,
appended and flushed to disk before the driver moves on. Its first record is
a header binding the preregistered input, freeze and plan digests, so a journal
cannot be resumed against changed inputs. ``started`` is written before any
work begins and ``finished`` holds the conserved outcome, so a crash leaves a
started entry that the driver must account for, never silently rerun.

Only an unterminated trailing record (a torn final write) is dropped; any other
malformed record rejects the journal. One runner at a time holds the journal
through an exclusive lock file, which a crashed runner leaves behind for the
operator to remove after checking no runner is still alive.
"""

import os
from collections.abc import Mapping
from pathlib import Path

from relaymind.canonical import canonical_json
from relaymind.evaluation.asset_corpus_io import AssetCorpusError, parse_json

RECORD_BYTES = 256 * 1024 * 1024


class JournalError(ValueError):
    """A fixed safe journal failure reason."""


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":  # Windows has no directory descriptor to flush.
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class RunJournal:
    """Append-only started/finished records under one bound header."""

    def __init__(self, path: Path, header: Mapping):
        self.path = Path(path).absolute()
        self.header = dict(header)
        self.started: set[str] = set()
        self.finished: dict[str, dict] = {}
        self._lock = self.path.parent / (self.path.name + ".lock")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self._lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise JournalError("journal_locked") from None
        with os.fdopen(fd, "w", encoding="utf-8") as lock:
            lock.write(str(os.getpid()))
        try:
            self._open()
        except BaseException:
            self._lock.unlink(missing_ok=True)
            raise

    def _open(self) -> None:
        if self.path.is_symlink() or self.path.exists() and not self.path.is_file():
            raise JournalError("journal_unsafe")
        if not self.path.exists():
            self._file = open(self.path, "xb")  # noqa: SIM115 - held until close()
            self._append(dict(self.header, type="header"))
            _fsync_directory(self.path.parent)
            return
        payload = self.path.read_bytes()
        complete, _, torn = payload.rpartition(b"\n")
        lines = complete.split(b"\n") if complete else []
        records = []
        for line in lines:
            try:
                records.append(parse_json(line))
            except AssetCorpusError:
                raise JournalError("journal_corrupt") from None
        if not records or records[0] != dict(self.header, type="header"):
            raise JournalError("journal_binding")
        for record in records[1:]:
            kind, key = record.get("type"), record.get("key")
            if not isinstance(key, str) or set(record) - {"type", "key", "value"}:
                raise JournalError("journal_corrupt")
            if kind == "started" and key not in self.started and set(record) == {"type", "key"}:
                self.started.add(key)
            elif kind == "finished" and key in self.started and key not in self.finished:
                if not isinstance(record.get("value"), dict):
                    raise JournalError("journal_corrupt")
                self.finished[key] = record["value"]
            else:
                raise JournalError("journal_corrupt")
        self.started -= set(self.finished)
        self._file = open(self.path, "r+b")  # noqa: SIM115 - held until close()
        if torn:
            # A torn final write never completed; drop it before appending.
            self._file.truncate(len(complete) + 1 if complete else 0)
            self._sync()
        self._file.seek(0, os.SEEK_END)

    def _sync(self) -> None:
        self._file.flush()
        os.fsync(self._file.fileno())

    def _append(self, record: Mapping) -> None:
        line = (canonical_json(record) + "\n").encode("utf-8")
        if len(line) > RECORD_BYTES:
            raise JournalError("journal_record_size")
        self._file.write(line)
        self._sync()

    def start(self, key: str) -> None:
        if key in self.started or key in self.finished:
            raise JournalError("journal_order")
        self._append(dict(type="started", key=key))
        self.started.add(key)

    def finish(self, key: str, value: Mapping) -> None:
        if key not in self.started:
            raise JournalError("journal_order")
        self._append(dict(type="finished", key=key, value=dict(value)))
        self.started.discard(key)
        self.finished[key] = dict(value)

    def close(self) -> None:
        file = getattr(self, "_file", None)
        if file is not None and not file.closed:
            file.close()
        self._lock.unlink(missing_ok=True)

    def __enter__(self) -> "RunJournal":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
