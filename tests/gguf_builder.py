"""Synthetic GGUF file builder, shared by manifest and reader tests."""

import struct

# GGUF value type ids (v3).
T_UINT32 = 4
T_FLOAT32 = 6
T_BOOL = 7
T_STRING = 8
T_ARRAY = 9

TEMPLATE = "{% for m in messages %}{{ m.content }}{% endfor %}"


def _string(value: str) -> bytes:
    raw = value.encode()
    return struct.pack("<Q", len(raw)) + raw


def _kv(key: str, type_id: int, payload: bytes) -> bytes:
    return _string(key) + struct.pack("<I", type_id) + payload


def write_gguf(path, kvs: list[bytes], magic: bytes = b"GGUF", version: int = 3):
    header = magic + struct.pack("<I", version) + struct.pack("<Q", 0)
    header += struct.pack("<Q", len(kvs))
    path.write_bytes(header + b"".join(kvs))
    return path


def sample_kvs() -> list[bytes]:
    tokens = _string("<pad>") + _string("hello")
    return [
        _kv("general.architecture", T_STRING, _string("qwen3")),
        _kv("qwen3.context_length", T_UINT32, struct.pack("<I", 40960)),
        _kv("general.some_scale", T_FLOAT32, struct.pack("<f", 0.5)),
        _kv("tokenizer.ggml.add_bos_token", T_BOOL, b"\x00"),
        _kv(
            "tokenizer.ggml.tokens",
            T_ARRAY,
            struct.pack("<I", T_STRING) + struct.pack("<Q", 2) + tokens,
        ),
        _kv("tokenizer.chat_template", T_STRING, _string(TEMPLATE)),
    ]
