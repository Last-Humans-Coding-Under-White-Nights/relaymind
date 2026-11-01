"""Crash-safe progress journal for the section 12.6.5 run drivers (#236, #237).

Spec 0.65 section 12.6.5 (RM16, RM42, RM44; AC33, AC36). A journal is a private
working file, never an export or evidence: one canonical JSON record per line,
appended and flushed to disk before the driver moves on. Its first record is
a header binding the preregistered input, freeze and plan digests and the
digest of the run's execution identity (environment, artifacts, input manifest
and qualification label), so a journal cannot be resumed against changed
inputs or under another identity: earlier rows are never attributed to it.
The journal holds row and reset evidence, so it is created owner-only (0600,
in a new 0700 directory) and restored to 0600 on resume. ``started`` is written before any
work begins and ``finished`` holds the conserved outcome, so a crash leaves a
started entry that the driver must account for, never silently rerun.

Only an unterminated trailing record (a torn final write) is dropped; any other
malformed record rejects the journal. A file with no complete record (a crash
while the header was written) cannot hold a started row and is initialized
again. One runner at a time holds the journal
through an exclusive lock file, which a crashed runner leaves behind for the
operator to remove after checking no runner is still alive.
"""

import os
from collections.abc import Mapping
from pathlib import Path

from relaymind.canonical import canonical_digest, canonical_json
from relaymind.evaluation.asset_corpus_io import AssetCorpusError, parse_json

RECORD_BYTES = 256 * 1024 * 1024
JOURNAL_FORMAT = "relaymind.s3_run_journal.v1"
_BOUND = ("input_digest", "freeze_digest", "plan_digest")


class JournalError(ValueError):
    """A fixed safe journal failure reason."""


def bound_header(kind: str, record: Mapping, identity: Mapping) -> dict:
    """The journal header binding a run to its preregistered digests and identity.

    ``identity`` holds every run-wide fact the report asserts besides its
    rows (environment, artifacts, input manifest, qualification label).
    """
    return dict(
        format=JOURNAL_FORMAT,
        kind=kind,
        **{k: record[k] for k in _BOUND},
        identity_digest=canonical_digest(dict(identity)),
    )


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
        if not self.path.parent.exists():
            # Another runner may create it between the check and here.
            self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        try:
            fd = os.open(self._lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise JournalError("journal_locked") from None
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as lock:
                lock.write(str(os.getpid()))
            self._open()
        except BaseException:
            self.close()  # Closes a file already opened, then removes the lock.
            raise

    def _open(self) -> None:
        if self.path.is_symlink() or self.path.exists() and not self.path.is_file():
            raise JournalError("journal_unsafe")
        if not self.path.exists():
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
            self._file = os.fdopen(os.open(self.path, flags, 0o600), "wb")
            self._append(dict(self.header, type="header"))
            _fsync_directory(self.path.parent)
            return
        if os.name != "nt":
            os.chmod(self.path, 0o600)
        payload = self.path.read_bytes()
        if b"\n" not in payload:
            # A crash while the header was written: no record completed, so no
            # row can have started. Write the header again.
            self._file = open(self.path, "r+b")  # noqa: SIM115 - held until close()
            self._file.truncate(0)
            self._append(dict(self.header, type="header"))
            return
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
        if not isinstance(key, str):
            raise JournalError("journal_corrupt")  # It could never be read back.
        if key in self.started or key in self.finished:
            raise JournalError("journal_order")
        self._append(dict(type="started", key=key))
        self.started.add(key)

    def finish(self, key: str, value: Mapping) -> None:
        if not isinstance(key, str):
            raise JournalError("journal_corrupt")  # It could never be read back.
        if key not in self.started:
            raise JournalError("journal_order")
        value = dict(value)
        self._append(dict(type="finished", key=key, value=value))
        self.started.discard(key)
        self.finished[key] = value

    def close(self) -> None:
        file = getattr(self, "_file", None)
        if file is not None and not file.closed:
            file.close()
        self._lock.unlink(missing_ok=True)

    def __enter__(self) -> "RunJournal":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
