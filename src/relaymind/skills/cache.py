"""Content-hash cache for skill parsing and template loading (RM07, RM24).

Spec 0.26 section 10: parsed skill files and decoded resource text are
cached by the SHA-256 of the file's bytes. Equal bytes are parsed once;
changed bytes have a new key, so a stale parse is never returned. A parse
that raises is never stored, so an invalid file is refused on every load.
The cache changes cost only: callers still validate every package against
the registered tools on every load.
"""

import hashlib
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

DEFAULT_MAX_ENTRIES = 1024


class ContentCache:
    """A bounded in-memory cache of parse results, keyed by kind and content hash."""

    def __init__(self, max_entries: int = DEFAULT_MAX_ENTRIES) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be at least 1")
        self.max_entries = max_entries
        self.hits = 0
        self.misses = 0
        self._entries: OrderedDict[tuple[str, str], Any] = OrderedDict()

    def __len__(self) -> int:
        return len(self._entries)

    def get_or_parse[T](
        self,
        kind: str,
        data: bytes,
        parse: Callable[[bytes], T],
        *,
        digest: bytes | None = None,
    ) -> T:
        """The parse of these bytes: stored when seen before, else computed and stored.

        kind separates parses that read the same bytes differently (for
        example a SKILL.md and the directory name it must match). digest
        is the SHA-256 digest of data when the caller already has it, so
        the bytes are hashed once.
        """
        if digest is None:
            digest = hashlib.sha256(data).digest()
        key = (kind, digest.hex())
        if key in self._entries:
            self._entries.move_to_end(key)
            self.hits += 1
            return self._entries[key]
        self.misses += 1
        value = parse(data)
        self._entries[key] = value
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)
        return value
