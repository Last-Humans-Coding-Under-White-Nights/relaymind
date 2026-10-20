"""Minimal GGUF metadata reader used to pin tokenizer and template hashes.

Covers RM18 / spec section 6: the manifest records tokenizer and
chat-template hashes taken from the actual artifact. Tests build a
synthetic GGUF file, so no model download is needed. Issue #2.
"""

import hashlib
import json

import pytest

from gguf_builder import TEMPLATE, sample_kvs, write_gguf
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
