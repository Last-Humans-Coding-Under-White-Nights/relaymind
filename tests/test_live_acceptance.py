"""The live acceptance harness against the pinned model (#88, #104; spec section 13).

Runs scripts/live_acceptance.py end to end: llama-server from the
manifest, then ``relaymind serve --config`` for each launch plan, the
scenarios over HTTP, and the demo UI in Chromium. Model-quality results
are findings and do not fail this test (RM16, section 12.3); every
gateway-owned check must hold, because those are invariants the code
keeps whatever the model does (RM05, RM08 to RM12, RM14, RM17, RM19 to
RM24).

Run explicitly with: pytest -m live tests/test_live_acceptance.py
RELAYMIND_LIVE_MANIFEST selects another manifest (AC08). The recorded
export for docs/EVIDENCE.md comes from running the script itself.
"""

import os
import shutil
from types import SimpleNamespace

import pytest

from helpers import live_manifest_path, load_script_module

pytestmark = pytest.mark.live


def test_gateway_checks_hold_on_the_live_model(tmp_path):
    if shutil.which("llama-server") is None:
        pytest.skip("llama-server not on PATH")
    harness = load_script_module("live_acceptance")
    args = SimpleNamespace(
        out=str(tmp_path / "live.json"),
        manifest=str(live_manifest_path(os.environ)),
        external_base_url=None,
        external_model=None,
        external_context_limit=None,
        second_external_model=None,
    )
    document = harness.run_live(args)
    statuses = {s["status"] for s in document["scenarios"]}
    assert statuses <= {"passed", "failed", "not_run", "error"}
    broken = [
        f"{s['launch']}/{s['name']}: {c['name']}: {c['detail']}"
        for s in document["scenarios"]
        for c in s["checks"]
        if c["owner"] == "gateway" and not c["ok"]
    ]
    assert broken == []
    assert all(launch["readyz"]["status"] == 200 for launch in document["launches"])
