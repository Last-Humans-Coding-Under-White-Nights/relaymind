"""Minimal GGUF metadata reader (RM18, spec section 6).

Reads only the metadata key-value section of a GGUF file: enough to
pin tokenizer and chat-template hashes in the artifact manifest. It is
not a general GGUF library and never loads tensor data.

Format reference: https://github.com/ggml-org/ggml/blob/master/docs/gguf.md
"""

import hashlib
import json
import struct
from io import BufferedReader
from pathlib import Path

MAGIC = b"GGUF"
SUPPORTED_VERSIONS = (2, 3)

CHAT_TEMPLATE_KEY = "tokenizer.chat_template"
TOKENIZER_PREFIX = "tokenizer."


class GGUFError(Exception):
    """The file is not a GGUF file this reader can handle."""


class _Reader:
    """A file handle that refuses reads beyond the actual file size.

    A corrupt file can declare a multi-gigabyte string or array length;
    checking against the real size fails cleanly before any allocation.
    """

    def __init__(self, handle: BufferedReader, file_size: int):
        self._handle = handle
        self._file_size = file_size

    def read_exact(self, size: int) -> bytes:
        if self._handle.tell() + size > self._file_size:
            raise GGUFError("declared size exceeds file size in GGUF metadata")
        data = self._handle.read(size)
        if len(data) != size:
            raise GGUFError("unexpected end of file in GGUF metadata")
        return data


def _read_string(reader: _Reader) -> str:
    (length,) = struct.unpack("<Q", reader.read_exact(8))
    try:
        return reader.read_exact(length).decode()
    except UnicodeDecodeError as exc:
        raise GGUFError(f"invalid UTF-8 in GGUF metadata: {exc}") from exc


_SCALAR_FORMATS = {
    0: "<B",  # uint8
    1: "<b",  # int8
    2: "<H",  # uint16
    3: "<h",  # int16
    4: "<I",  # uint32
    5: "<i",  # int32
    6: "<f",  # float32
    10: "<Q",  # uint64
    11: "<q",  # int64
    12: "<d",  # float64
}
_TYPE_BOOL = 7
_TYPE_STRING = 8
_TYPE_ARRAY = 9


def _read_value(reader: _Reader, type_id: int) -> object:
    if type_id in _SCALAR_FORMATS:
        fmt = _SCALAR_FORMATS[type_id]
        (value,) = struct.unpack(fmt, reader.read_exact(struct.calcsize(fmt)))
        return value
    if type_id == _TYPE_BOOL:
        return reader.read_exact(1) != b"\x00"
    if type_id == _TYPE_STRING:
        return _read_string(reader)
    if type_id == _TYPE_ARRAY:
        (element_type,) = struct.unpack("<I", reader.read_exact(4))
        (count,) = struct.unpack("<Q", reader.read_exact(8))
        return [_read_value(reader, element_type) for _ in range(count)]
    raise GGUFError(f"unsupported GGUF value type {type_id}")


def read_metadata(path: Path) -> dict[str, object]:
    """Return the metadata key-value section of a GGUF file."""
    file_size = Path(path).stat().st_size
    with open(path, "rb") as handle:
        reader = _Reader(handle, file_size)
        if reader.read_exact(4) != MAGIC:
            raise GGUFError(f"not a GGUF file: {path}")
        (version,) = struct.unpack("<I", reader.read_exact(4))
        if version not in SUPPORTED_VERSIONS:
            raise GGUFError(f"unsupported GGUF version {version}")
        _tensor_count, kv_count = struct.unpack("<QQ", reader.read_exact(16))
        metadata: dict[str, object] = {}
        for _ in range(kv_count):
            key = _read_string(reader)
            (type_id,) = struct.unpack("<I", reader.read_exact(4))
            metadata[key] = _read_value(reader, type_id)
        return metadata


def artifact_hashes(path: Path) -> tuple[str, str]:
    """Return (tokenizer_hash, chat_template_hash) for a GGUF artifact.

    The chat-template hash is the SHA-256 of the template string. The
    tokenizer hash is the SHA-256 of the canonical JSON (sorted keys)
    of every ``tokenizer.*`` metadata entry except the chat template.
    """
    metadata = read_metadata(path)
    template = metadata.get(CHAT_TEMPLATE_KEY)
    if not isinstance(template, str):
        raise GGUFError(f"{CHAT_TEMPLATE_KEY} missing from {path}")
    tokenizer_meta = {
        key: value
        for key, value in metadata.items()
        if key.startswith(TOKENIZER_PREFIX) and key != CHAT_TEMPLATE_KEY
    }
    if not tokenizer_meta:
        raise GGUFError(f"no tokenizer metadata in {path}")
    # Canonical JSON: sorted keys, compact separators (RFC 8785 style).
    canonical = json.dumps(
        tokenizer_meta, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    tokenizer_hash = hashlib.sha256(canonical.encode()).hexdigest()
    template_hash = hashlib.sha256(template.encode()).hexdigest()
    return tokenizer_hash, template_hash
