"""Bounded selected-text ZIP inspection for benchmark acquisition (RM34/RM40)."""

import binascii
import hashlib
import io
import stat
import struct
import unicodedata
import zipfile
import zlib
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from relaymind.evaluation.asset_corpus_io import MIB, AssetCorpusError, relative_path

_SELECTED = frozenset(
    {
        "SKILL.md",
        "LICENSE",
        "LICENSE.md",
        "LICENSE.txt",
        "NOTICE",
        "NOTICE.md",
        "NOTICE.txt",
        "COPYING",
        "COPYRIGHT",
        "package.json",
        "skill.manifest.json",
    }
)


@dataclass(frozen=True)
class ArchiveInspection:
    original_sha256: str
    entry_count: int
    declared_bytes: int
    selected: Mapping[str, bytes]


def _directory(payload: bytes) -> int:
    """Bound central-directory enumeration; return where the directory starts."""
    at = payload.rfind(b"PK\x05\x06", max(0, len(payload) - 65557))
    if at < 0 or at + 22 > len(payload):
        raise AssetCorpusError("malformed_archive")
    _, disk, directory_disk, disk_count, count, size, offset, comment = struct.unpack_from(
        "<4s4H2LH", payload, at
    )
    if disk or directory_disk or disk_count != count or at + 22 + comment != len(payload):
        raise AssetCorpusError("malformed_archive")
    if count > 1000:
        raise AssetCorpusError("archive_limit")
    if offset + size != at:
        raise AssetCorpusError("malformed_archive")
    seen, position = 0, offset
    while position < at:
        if position + 46 > at or payload[position : position + 4] != b"PK\x01\x02":
            raise AssetCorpusError("malformed_archive")
        lengths = struct.unpack_from("<3H", payload, position + 28)
        position += 46 + sum(lengths)
        seen += 1
        if seen > 1000:
            raise AssetCorpusError("archive_limit")
    if position != at or seen != count:
        raise AssetCorpusError("malformed_archive")
    return offset


def _name(info: zipfile.ZipInfo) -> str:
    name = info.orig_filename
    try:
        relative_path(name[:-1] if name.endswith("/") else name)
    except AssetCorpusError:
        raise AssetCorpusError("unsafe_archive_path") from None
    mode = info.external_attr >> 16
    kind = stat.S_IFMT(mode)
    if kind not in (0, stat.S_IFREG, stat.S_IFDIR) or (kind == stat.S_IFDIR and not info.is_dir()):
        raise AssetCorpusError("unsafe_archive_entry")
    if info.flag_bits & 1:
        raise AssetCorpusError("encrypted_archive")
    return name


def _entry_bounds(infos: list[zipfile.ZipInfo], central_start: int) -> dict[zipfile.ZipInfo, int]:
    """The exclusive end of each entry's data window, as ZipFile itself bounds it.

    One entry's region ends where the next local header starts, and the last
    entry's ends where the central directory starts. Python 3.13 records this
    in the private ``ZipInfo._end_offset``; computing it here keeps the same
    bound on Python 3.12, where that attribute does not exist.
    """
    bounds: dict[zipfile.ZipInfo, int] = {}
    end = central_start
    for info in sorted(infos, key=lambda item: item.header_offset, reverse=True):
        bounds[info] = end
        end = info.header_offset
    return bounds


def _local_header(payload: bytes, info: zipfile.ZipInfo, end_bound: int) -> int:
    """Check local/central metadata and deferred descriptors without reading bodies."""
    start = info.header_offset
    if start < 0 or start + 30 > len(payload) or payload[start : start + 4] != b"PK\x03\x04":
        raise AssetCorpusError("malformed_archive")
    flags, method = struct.unpack_from("<2H", payload, start + 6)
    crc, compressed, expanded = struct.unpack_from("<3I", payload, start + 14)
    if flags & (1 | 0x40):
        raise AssetCorpusError("encrypted_archive")
    if flags != info.flag_bits or method != info.compress_type:
        raise AssetCorpusError("malformed_archive")
    name_size, extra_size = struct.unpack_from("<2H", payload, start + 26)
    start += 30 + name_size + extra_size
    end = start + info.compress_size
    if end > len(payload) or end > end_bound:
        raise AssetCorpusError("malformed_archive")
    expected = (info.CRC, info.compress_size, info.file_size)
    if flags & 8:
        if any(
            local not in (0, actual)
            for local, actual in zip((crc, compressed, expanded), expected, strict=True)
        ):
            raise AssetCorpusError("malformed_archive")
        descriptor = end + (4 if payload[end : end + 4] == b"PK\x07\x08" else 0)
        if (
            descriptor + 12 > end_bound
            or struct.unpack_from("<3I", payload, descriptor) != expected
        ):
            raise AssetCorpusError("malformed_archive")
    elif (crc, compressed, expanded) != expected:
        raise AssetCorpusError("malformed_archive")
    return start


def _selected_bytes(payload: bytes, info: zipfile.ZipInfo, limit: int, end_bound: int) -> bytes:
    """Expand the whole selected stream, checking actual output, EOF, size and CRC."""
    start = _local_header(payload, info, end_bound)
    end = start + info.compress_size
    decoder = zlib.decompressobj(-15) if info.compress_type == zipfile.ZIP_DEFLATED else None
    chunks, count, crc = [], 0, 0
    for offset in range(start, end, 65536):
        compressed = payload[offset : min(offset + 65536, end)]
        chunk = decoder.decompress(compressed, limit + 1 - count) if decoder else compressed
        count += len(chunk)
        if count > limit or (decoder and decoder.unconsumed_tail):
            raise AssetCorpusError("archive_limit")
        crc = binascii.crc32(chunk, crc)
        chunks.append(chunk)
    if decoder and (not decoder.eof or decoder.unused_data):
        raise AssetCorpusError("malformed_archive")
    if count != info.file_size or crc != info.CRC:
        raise AssetCorpusError("malformed_archive")
    return b"".join(chunks)


def inspect_skill_archive(payload: bytes) -> ArchiveInspection:
    """Read root SKILL.md and fixed root metadata; ordinary other entries stay opaque."""
    if len(payload) > 10 * MIB:
        raise AssetCorpusError("archive_limit")
    try:
        central_start = _directory(payload)
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            infos = archive.infolist()
            bounds = _entry_bounds(infos, central_start)
            names, total = set(), 0
            # Validate every entry, even one whose content is never selected.
            for info in infos:
                name = _name(info)
                key = unicodedata.normalize("NFC", name.rstrip("/")).casefold()
                if key in names:
                    raise AssetCorpusError("duplicate_archive_path")
                names.add(key)
                total += info.file_size
                if total > 20 * MIB or (
                    info.file_size
                    and (not info.compress_size or info.file_size > 100 * info.compress_size)
                ):
                    raise AssetCorpusError("archive_limit")
                if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                    raise AssetCorpusError("unsupported_compression")
                _local_header(payload, info, bounds[info])
                # Opening only checks the local header/overlap; it reads no body.
                with archive.open(info):
                    pass
            if "SKILL.md" not in {info.filename for info in infos if not info.is_dir()}:
                raise AssetCorpusError("missing_skill")
            selected = {}
            for info in infos:
                if info.filename not in _SELECTED or info.is_dir():
                    continue
                limit = 256 * 1024 if info.filename == "SKILL.md" else 4 * MIB
                if info.file_size > limit:
                    raise AssetCorpusError("archive_limit")
                selected[info.filename] = _selected_bytes(payload, info, limit, bounds[info])
            return ArchiveInspection(
                hashlib.sha256(payload).hexdigest(), len(infos), total, MappingProxyType(selected)
            )
    except (
        zipfile.BadZipFile,
        NotImplementedError,
        RuntimeError,
        struct.error,
        zlib.error,
        UnicodeError,
    ):
        raise AssetCorpusError("malformed_archive") from None
