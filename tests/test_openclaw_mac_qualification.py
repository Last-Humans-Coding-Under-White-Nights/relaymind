"""The measured OpenClaw request size and the live AC24 run on macOS (RM13, RM26, RM33, AC24; #135).

The real-host qualification on macOS
(``docs/evidence/2026-10-27-openclaw-mac-qualification/``, OpenClaw 2026.10.7,
pinned llama-server b11785 with ``qwen3-0.6b-q8_0`` at ``ctx_size`` 8192)
counted the host's requests with the pinned tokenizer and ran one live AC24
turn in auto with no external model. That turn used a config-row workaround
(a ``models.providers`` row, not the plugin's model, because at ``a52f8bf``
``agent exec`` could not resolve the plugin's model; #136), so it is not a
turn through the plugin as packaged. These tests read the stored reports, so
the size the design doc and ``docs/EVIDENCE.md`` state rests on the measured
files, not on the UTF-8 bound:

- 0 of 3 fresh host requests fit the 8192 local limit (10,513 to 10,577
  tokens), and 0 of 7 probe requests (10,500 to 10,548; sanitized, so a
  lower bound, which still proves them over the limit);
- the live turn ended needs_external with reason context_limit before any
  model call, as section 7.5 says a request too large for the local model
  does when no external model can take it.

No model or host runs here; the reports are data.
"""

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
QUAL = ROOT / "docs" / "evidence" / "2026-10-27-openclaw-mac-qualification"
LOCAL_LIMIT = 8192
# The commit the Mac run tested (REPORT.md, Environment).
TESTED = "a52f8bf93f1e7a6a2ff37d21e259529f62221141"


def report(name):
    return json.loads((QUAL / name).read_text("utf-8"))


def test_the_qualification_records_the_host_runtime_and_tested_commit():
    text = (QUAL / "REPORT.md").read_text("utf-8")
    assert "OpenClaw | 2026.10.7 (157d8c2)" in text
    assert "build 11785" in text
    assert "`n_ctx` 8192" in text
    assert TESTED in text


@pytest.mark.parametrize(
    ("name", "measured", "low", "high"),
    [
        ("request-size.json", 3, 10_513, 10_577),
        ("request-size-probe-requests.json", 7, 10_500, 10_548),
    ],
)
def test_no_host_request_fits_the_local_limit_by_the_tokenizer_count(name, measured, low, high):
    """RM13: the tokenizer count decides; every measured request is over 8192."""
    size = report(name)
    assert size["method"] == "tokenizer"
    assert size["local_limit"] == LOCAL_LIMIT
    summary = size["summary"]
    assert summary["measured"] == measured
    assert summary["fits_local"] == 0
    assert summary["local_only"] == {"local": 0, "needs_external": measured}
    assert summary["auto"] == {"local": 0, "external_if_it_fits": measured}
    assert (summary["size_min"], summary["size_max"]) == (low, high)
    rows = [r for r in size["requests"] if "size" in r]
    assert len(rows) == measured
    assert all(r["method"] == "tokenizer" and r["size"] > LOCAL_LIMIT for r in rows)
    assert not any(r["fits_local"] for r in rows)


def test_the_live_ac24_turn_in_auto_without_an_external_model_needs_external():
    """Section 7.5: a request too large for the local model, with no external model,
    ends needs_external / context_limit before any model call."""
    run = report("live/gateway-run.json")
    assert run["requested_execution_mode"] == run["effective_execution_mode"] == "auto"
    assert run["state"] == "failed"
    assert (run["error_code"], run["reason_code"]) == ("needs_external", "context_limit")
    assert run["metrics"]["model_calls"] == 0
    assert run["metrics"]["external_calls"] == 0
    events = (QUAL / "live" / "gateway-run-events.txt").read_text("utf-8")
    assert "the request needs 10510 (tokenizer); the local limit is 8192" in events


@pytest.mark.parametrize(
    "doc",
    ["docs/EVIDENCE.md", "docs/design/2026-10-27-openclaw-integration.md"],
)
def test_the_size_records_cite_the_measured_report_not_only_the_bound(doc):
    """Honest evidence (section 12.5): the measured count replaces "not_run"."""
    text = (ROOT / doc).read_text("utf-8")
    assert "2026-10-27-openclaw-mac-qualification/request-size.json" in text
    assert "Tokenizer count | not_run" not in text
    assert "not_run (needs fresh, unsanitized captures)" not in text
    assert "not_run (needs llama-server with the pinned model" not in text
