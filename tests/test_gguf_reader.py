"""Minimal GGUF metadata reader used to pin tokenizer and template hashes.

Covers RM18 / spec section 6: the manifest records tokenizer and
chat-template hashes taken from the actual artifact. Tests build a
synthetic GGUF file, so no model download is needed. Issue #2.
"""

import hashlib
import json
import struct

import pytest

from relaymind.adapters.gguf import GGUFError, artifact_hashes, read_metadata

# GGUF value type ids (v3).
T_UINT32 = 4
T_FLOAT32 = 6
T_BOOL = 7
T_STRING = 8
T_ARRAY = 9


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


TEMPLATE = "{% for m in messages %}{{ m.content }}{% endfor %}"


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


def test_read_metadata_decodes_all_value_types(tmp_path):
    path = write_gguf(tmp_path / "model.gguf", sample_kvs())
    meta = read_metadata(path)
    assert meta["general.architecture"] == "qwen3"
    assert meta["qwen3.context_length"] == 40960
    assert meta["general.some_scale"] == 0.5
    assert meta["tokenizer.ggml.add_bos_token"] is False
    assert meta["tokenizer.ggml.tokens"] == ["<pad>", "hello"]
    assert meta["tokenizer.chat_template"] == TEMPLATE


def test_read_metadata_rejects_wrong_magic(tmp_path):
    path = write_gguf(tmp_path / "model.gguf", [], magic=b"NOPE")
    with pytest.raises(GGUFError):
        read_metadata(path)


def test_artifact_hashes_are_deterministic_and_documented(tmp_path):
    path = write_gguf(tmp_path / "model.gguf", sample_kvs())
    tokenizer_hash, template_hash = artifact_hashes(path)

    assert template_hash == hashlib.sha256(TEMPLATE.encode()).hexdigest()

    tokenizer_meta = {
        "tokenizer.ggml.add_bos_token": False,
        "tokenizer.ggml.tokens": ["<pad>", "hello"],
    }
    canonical = json.dumps(tokenizer_meta, sort_keys=True, ensure_ascii=False)
    assert tokenizer_hash == hashlib.sha256(canonical.encode()).hexdigest()


def test_artifact_hashes_require_a_chat_template(tmp_path):
    kvs = [kv for kv in sample_kvs() if b"chat_template" not in kv]
    path = write_gguf(tmp_path / "model.gguf", kvs)
    with pytest.raises(GGUFError):
        artifact_hashes(path)
