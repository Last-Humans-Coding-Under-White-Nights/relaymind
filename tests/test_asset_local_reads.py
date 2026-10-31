"""Batched original-byte reads keep every per-file fence (RM40, spec 14.5)."""

import os

import pytest

from relaymind.evaluation import asset_corpus_io, asset_evidence
from relaymind.evaluation.asset_corpus_io import AssetCorpusError, read_local, read_local_files


def tree(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "one.txt").write_bytes(b"one")
    (tmp_path / "two.txt").write_bytes(b"two-two")
    return tmp_path


def counting(monkeypatch):
    opened = []
    original = asset_corpus_io.open_source_tree

    def open_tree(root, **kwargs):
        opened.append(root)
        return original(root, **kwargs)

    monkeypatch.setattr(asset_corpus_io, "open_source_tree", open_tree)
    return opened


def test_rm40_batch_returns_the_same_bytes_as_single_reads_with_one_tree(tmp_path, monkeypatch):
    root = tree(tmp_path)
    names = ["a/one.txt", "two.txt"]
    expected = [read_local(root, name, 100) for name in names]
    opened = counting(monkeypatch)
    assert read_local_files(root, names, 100) == expected
    assert len(opened) == 1


def test_rm40_batch_keeps_single_read_failures(tmp_path):
    root = tree(tmp_path)
    for names, limit, reason in (
        (["two.txt", "missing.txt"], 100, "missing_file"),
        (["a/one.txt", "two.txt"], 4, "size_limit"),
        (["a/one.txt", "../two.txt"], 100, "unsafe_path"),
    ):
        with pytest.raises(AssetCorpusError) as failure:
            read_local_files(root, names, limit)
        assert str(failure.value) == reason
        if reason != "unsafe_path":
            with pytest.raises(AssetCorpusError) as single:
                read_local(root, names[-1], limit)
            assert str(single.value) == reason


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privileges on Windows")
def test_rm40_batch_refuses_a_linked_member(tmp_path):
    root = tree(tmp_path)
    (root / "link.txt").symlink_to(root / "two.txt")
    with pytest.raises(AssetCorpusError) as failure:
        read_local_files(root, ["two.txt", "link.txt"], 100)
    assert str(failure.value) == "unsafe_file"


def test_rm40_file_inventory_opens_the_tree_once(tmp_path, monkeypatch):
    root = tmp_path
    for relative in ("pyproject.toml", "scripts/asset_selection_benchmark.py"):
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        (root / relative).write_bytes(b"x")
    package = root / "src/relaymind"
    (package / "sub").mkdir(parents=True)
    (package / "__init__.py").write_bytes(b"")
    (package / "sub" / "mod.py").write_bytes(b"value = 1\n")
    opened = counting(monkeypatch)
    inventory = asset_evidence.file_inventory(root)
    assert [item["path"] for item in inventory["files"]] == [
        "pyproject.toml",
        "scripts/asset_selection_benchmark.py",
        "src/relaymind/__init__.py",
        "src/relaymind/sub/mod.py",
    ]
    assert len(opened) == 1
