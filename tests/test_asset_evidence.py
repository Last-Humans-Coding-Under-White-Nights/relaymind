"""RM40/AC31: protocol inventories admit only reviewed specification bytes."""

import copy
from pathlib import Path

import pytest

from relaymind.evaluation.asset_corpus_io import AssetCorpusError
from relaymind.evaluation.asset_evidence import file_inventory, verify_compiled_asset_inventory


def test_current_protocol_inventory_is_a_supported_generator():
    """RM40/AC31: a spec revision must not strand newly generated reports."""
    root = Path(__file__).resolve().parents[1]
    inventory = file_inventory(root, protocol=True)

    verify_compiled_asset_inventory("protocol_inventory", inventory)


def test_unreviewed_spec_bytes_cannot_certify_a_protocol_inventory():
    """RM40/AC31: matching package schemas cannot authorize arbitrary spec bytes."""
    root = Path(__file__).resolve().parents[1]
    inventory = copy.deepcopy(file_inventory(root, protocol=True))
    spec = next(item for item in inventory["files"] if item["path"] == "docs/SPECIFICATION.md")
    spec["sha256"] = "f" * 64

    with pytest.raises(AssetCorpusError, match="unsupported_generator"):
        verify_compiled_asset_inventory("protocol_inventory", inventory)


def test_unreviewed_spec_length_cannot_certify_a_protocol_inventory():
    """RM40/AC31: a valid hash cannot authorize an unreviewed byte length."""
    root = Path(__file__).resolve().parents[1]
    inventory = copy.deepcopy(file_inventory(root, protocol=True))
    spec = next(item for item in inventory["files"] if item["path"] == "docs/SPECIFICATION.md")
    spec["bytes"] += 1

    with pytest.raises(AssetCorpusError, match="unsupported_generator"):
        verify_compiled_asset_inventory("protocol_inventory", inventory)
