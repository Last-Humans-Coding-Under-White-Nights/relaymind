"""RM40/RM44 AC31/AC36: owned temporary roots do not trust artifact aliases."""

import asyncio
import tempfile

import pytest

from relaymind.evaluation.asset_benchmark import (
    load_asset_benchmark_report,
    run_asset_benchmark,
    write_asset_benchmark_report,
)
from relaymind.evaluation.asset_benchmark_io import atomic_bytes
from relaymind.evaluation.asset_comparison import compare_asset_reports
from relaymind.evaluation.asset_corpus_io import AssetCorpusError
from test_asset_selection_benchmark import fixture_config


def _symlink_to(link, target, *, target_is_directory=False):
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1314:
            pytest.skip("native fixture needs SeCreateSymbolicLinkPrivilege or Developer Mode")
        raise


def test_owned_report_snapshot_accepts_os_temp_ancestor_alias(tmp_path, monkeypatch):
    """A macOS-style /var alias must not block our private report commitment."""
    physical = tmp_path / "physical"
    physical.mkdir()
    alias = tmp_path / "os-temp-alias"
    _symlink_to(alias, physical, target_is_directory=True)
    config = fixture_config(tmp_path, repetitions=1, warmups=0)
    # Use the actual TemporaryDirectory implementation and its default root.
    monkeypatch.setattr(tempfile, "tempdir", str(alias))
    report = asyncio.run(run_asset_benchmark(config))
    assert report.data["status"] == "completed"
    assert report.inputs is not None
    assert len(report.data["results"]) == len(report.data["planned_slots"]) == 1
    assert list(physical.iterdir()) == []
    output = tmp_path / "report.json"
    write_asset_benchmark_report(report, output)
    loaded = load_asset_benchmark_report(
        output, resolver=output.with_name(output.name + ".resolver.json")
    )
    assert loaded.inputs is not None
    assert loaded.data == report.data
    assert compare_asset_reports(report, loaded, quality_only=True).data["status"] == "compatible"
    assert list(physical.iterdir()) == []


@pytest.mark.parametrize("location", ["ancestor", "parent", "leaf"])
def test_requested_output_alias_is_still_refused(tmp_path, location):
    """Canonicalizing an owned root must never resolve caller-selected output."""
    physical = tmp_path / "physical"
    physical.mkdir()
    target = physical / "report.json"
    target.write_bytes(b"original")
    alias = tmp_path / "alias"
    _symlink_to(alias, physical, target_is_directory=True)
    if location == "ancestor":
        output = alias / "nested" / "report.json"
    elif location == "parent":
        output = alias / "report.json"
    else:
        output = tmp_path / "report.json"
        _symlink_to(output, target)
    with pytest.raises(AssetCorpusError, match="^unsafe_file$"):
        atomic_bytes(output, b"replacement")
    assert target.read_bytes() == b"original"
    assert not (physical / "nested").exists()


def test_replaced_owned_temp_root_is_not_followed(tmp_path, monkeypatch):
    """The OS ancestor alias exception must not trust a replaced owned leaf."""
    from contextlib import contextmanager
    from pathlib import Path

    config = fixture_config(tmp_path, repetitions=1, warmups=0)
    outside = tmp_path / "outside"
    outside.mkdir()
    temporary_directory = tempfile.TemporaryDirectory

    @contextmanager
    def replaced_root(*args, **kwargs):
        with temporary_directory(*args, **kwargs) as directory:
            root = Path(directory)
            replace = kwargs.get("prefix") == "asset-report-inputs-"
            if replace:
                root.rmdir()
                _symlink_to(root, outside, target_is_directory=True)
            try:
                yield directory
            finally:
                if replace:
                    root.unlink()
                    root.mkdir()

    monkeypatch.setattr(tempfile, "TemporaryDirectory", replaced_root)
    with pytest.raises(AssetCorpusError, match="^unsafe_file$"):
        asyncio.run(run_asset_benchmark(config))
    assert list(outside.iterdir()) == []
