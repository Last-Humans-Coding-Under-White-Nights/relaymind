"""One canonical JSON form (RM07, RM10, RM11).

Tool schema hashes, operation IDs, and approval argument binding must
agree on one text form, so the tool registry and the run store share
relaymind.canonical.canonical_json. The store stays independent of the
tools package.
"""

import subprocess
import sys

import pytest

from relaymind import canonical
from relaymind.store import run_store
from relaymind.tools import registry


def test_canonical_json_is_stable_and_compact():
    assert canonical.canonical_json({"b": 1, "a": "日志"}) == '{"a":"日志","b":1}'


def test_the_tool_registry_and_the_run_store_share_one_function():
    assert registry.canonical_json is canonical.canonical_json
    assert run_store.canonical_json is canonical.canonical_json


def test_the_store_does_not_import_the_tools_package():
    code = "import sys, relaymind.store; print('relaymind.tools' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"


@pytest.mark.parametrize("number", [float("nan"), float("inf"), float("-inf")])
def test_canonical_json_refuses_numbers_that_json_cannot_hold(number):
    """RM07, RM10, RM11: NaN and infinities are not RFC 8259 JSON, so the
    canonical form refuses them with a clear error instead of writing
    NaN or Infinity into a hash or an operation ID."""
    with pytest.raises(ValueError, match="NaN or infinite"):
        canonical.canonical_json({"value": [1, number]})
