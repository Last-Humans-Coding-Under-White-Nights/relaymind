"""Minimal GGUF metadata reader used to pin tokenizer and template hashes.

Covers RM18 / spec section 6: the manifest records tokenizer and
chat-template hashes taken from the actual artifact. Tests build a
synthetic GGUF file, so no model download is needed. Issue #2.
"""

import hashlib
import json
import struct

import pytest

from gguf_builder import TEMPLATE, _kv, _string, sample_kvs, write_gguf
from relaymind.adapters.gguf import GGUFError, artifact_hashes, read_metadata


def test_read_metadata_decodes_all_value_types(tmp_path):
    path = write_gguf(tmp_path / "model.gguf", sample_kvs())
    meta = read_metadata(path)
    assert meta["general.architecture"] == "qwen3"
    assert meta["qwen3.context_length"] == 40960
    assert meta["general.some_scale"] == 0.5
    assert meta["tokenizer.ggml.add_bos_token"] is False
    assert meta["tokenizer.ggml.tokens"] == ["<pad>", "hello"]
    assert meta["tokenizer.chat_template"] == TEMPLATE


def test_read_metadata_decodes_every_scalar_type(tmp_path):
    scalars = [
        ("u8", 0, "<B", 255),
        ("i8", 1, "<b", -7),
        ("u16", 2, "<H", 65535),
        ("i16", 3, "<h", -1000),
        ("i32", 5, "<i", -100000),
        ("u64", 10, "<Q", 1 << 40),
        ("i64", 11, "<q", -(1 << 40)),
        ("f64", 12, "<d", 0.25),
    ]
    kvs = [_kv(name, type_id, struct.pack(fmt, value)) for name, type_id, fmt, value in scalars]
    path = write_gguf(tmp_path / "model.gguf", kvs)
    meta = read_metadata(path)
    for name, _type_id, _fmt, value in scalars:
        assert meta[name] == value


def test_read_metadata_rejects_wrong_magic(tmp_path):
    path = write_gguf(tmp_path / "model.gguf", [], magic=b"NOPE")
    with pytest.raises(GGUFError):
        read_metadata(path)


def test_read_metadata_accepts_v2_and_rejects_other_versions(tmp_path):
    v2 = write_gguf(tmp_path / "v2.gguf", sample_kvs(), version=2)
    assert read_metadata(v2)["general.architecture"] == "qwen3"
    for version in (1, 4):
        path = write_gguf(tmp_path / f"v{version}.gguf", [], version=version)
        with pytest.raises(GGUFError):
            read_metadata(path)


def test_read_metadata_rejects_truncated_file(tmp_path):
    path = write_gguf(tmp_path / "model.gguf", sample_kvs())
    truncated = tmp_path / "truncated.gguf"
    truncated.write_bytes(path.read_bytes()[:-20])
    with pytest.raises(GGUFError):
        read_metadata(truncated)


def test_read_metadata_rejects_oversized_declared_length(tmp_path):
    # A corrupt file declaring a multi-GB string must fail cleanly, not
    # attempt the allocation.
    bogus = struct.pack("<Q", 1 << 40) + b"x"
    path = write_gguf(tmp_path / "model.gguf", [bogus])
    with pytest.raises(GGUFError):
        read_metadata(path)


def test_read_metadata_rejects_invalid_utf8(tmp_path):
    bad_key = struct.pack("<Q", 2) + b"\xff\xfe" + struct.pack("<I", 8) + _string("v")
    path = write_gguf(tmp_path / "model.gguf", [bad_key])
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
    # Canonical JSON: sorted keys, compact separators (RFC 8785 style).
    canonical = json.dumps(
        tokenizer_meta, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    assert tokenizer_hash == hashlib.sha256(canonical.encode()).hexdigest()


def test_artifact_hashes_require_a_chat_template(tmp_path):
    kvs = [kv for kv in sample_kvs() if b"chat_template" not in kv]
    path = write_gguf(tmp_path / "model.gguf", kvs)
    with pytest.raises(GGUFError):
        artifact_hashes(path)


def test_artifact_hashes_require_tokenizer_metadata(tmp_path):
    kvs = [kv for kv in sample_kvs() if not kv.startswith(_string("tokenizer.ggml.add_bos_token"))]
    kvs = [kv for kv in kvs if not kv.startswith(_string("tokenizer.ggml.tokens"))]
    path = write_gguf(tmp_path / "model.gguf", kvs)
    with pytest.raises(GGUFError):
        artifact_hashes(path)
