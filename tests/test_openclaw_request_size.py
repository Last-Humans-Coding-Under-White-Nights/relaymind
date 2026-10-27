"""The captured OpenClaw request sizes against the local input limit (RM13, RM26, RM33; #135).

``scripts/openclaw_request_size.py`` reads captured host requests, measures
each with the gateway's own request measurement (``measure_request``: the
local tokenizer, or the UTF-8 byte upper bound without one) and reports the
share that fits the local limit, with what auto and local_only do with the
rest. Every number carries its method. Deterministic: a model double counts.
"""

import asyncio
import json

import pytest

from helpers import ScriptedAdapter, load_script_module, make_capabilities

size = load_script_module("openclaw_request_size")

TOOL = {
    "type": "function",
    "function": {
        "name": "relaymind_test_echo",
        "description": "echo",
        "parameters": {"type": "object", "properties": {"text": {"type": "string"}}},
    },
}


def body(prompt, **fields):
    return {
        "model": "relaymind-client-tools",
        "messages": [{"role": "system", "content": "sys"}, {"role": "user", "content": prompt}],
        "stream": True,
        "tools": [TOOL],
        **fields,
    }


def fixture(tmp_path, name, *bodies):
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps({"exchanges": [{"request": {"body": b}} for b in bodies]}), "utf-8")
    return path


def test_the_share_that_fits_the_local_limit_is_reported_with_its_method(tmp_path):
    """One of three requests fits; auto sends the rest external, local_only refuses them."""
    fixture(tmp_path, "a", body("x" * 10), body("x" * 200))
    fixture(tmp_path, "b", body("x" * 300))
    adapter = ScriptedAdapter(capabilities=make_capabilities(context_limit=100))
    report = asyncio.run(
        size.measure_fixtures(size.load_requests(tmp_path), adapter, external_context_limit=250)
    )
    assert report["method"] == "tokenizer"
    assert report["local_limit"] == 100
    summary = report["summary"]
    assert summary["captured"] == 3
    assert summary["measured"] == 3
    assert summary["fits_local"] == 1
    assert summary["share_fits_local"] == pytest.approx(1 / 3)
    assert summary["auto"] == {"local": 1, "external": 1, "context_limit_exceeded": 1}
    assert summary["local_only"] == {"local": 1, "needs_external": 2}
    sizes = [r["size"] for r in report["requests"]]
    # The double counts one unit per content character and per tool (sys + prompt + 1).
    assert sizes == [14, 204, 304]
    assert all(r["method"] == "tokenizer" for r in report["requests"])
    assert "necessary" in report["note"]


def test_without_an_external_limit_auto_reports_the_rest_as_external_unmeasured(tmp_path):
    fixture(tmp_path, "a", body("x" * 10), body("x" * 200))
    adapter = ScriptedAdapter(capabilities=make_capabilities(context_limit=100))
    report = asyncio.run(size.measure_fixtures(size.load_requests(tmp_path), adapter))
    assert report["summary"]["auto"] == {"local": 1, "external_if_it_fits": 1}


def test_without_a_tokenizer_every_number_is_a_utf8_upper_bound(tmp_path):
    """RM13: the byte length bounds the token count from above; it is labeled so."""
    fixture(tmp_path, "a", body("x" * 10), body("y" * 9000))
    report = asyncio.run(size.measure_fixtures(size.load_requests(tmp_path), size.ByteBound(8192)))
    assert report["method"] == "utf8_bytes"
    assert "upper bound" in report["method_note"]
    assert all(r["method"] == "utf8_bytes" for r in report["requests"])
    first, second = report["requests"]
    assert first["fits_local"] is True
    assert second["fits_local"] is False
    assert report["summary"]["fits_local"] == 1


def test_a_request_the_gateway_rejects_is_counted_separately_with_its_code(tmp_path):
    """Section 7.1: a host default the gateway rejects is not measured as if it ran."""
    fixture(tmp_path, "a", body("hi", stream_options={"include_usage": True}), body("hi"))
    adapter = ScriptedAdapter(capabilities=make_capabilities(context_limit=100))
    report = asyncio.run(size.measure_fixtures(size.load_requests(tmp_path), adapter))
    rejected, measured = report["requests"]
    assert rejected["rejected"] == "unsupported_parameter"
    assert "size" not in rejected
    assert measured["fits_local"] is True
    assert report["summary"]["captured"] == 2
    assert report["summary"]["rejected"] == 1
    assert report["summary"]["measured"] == 1


def test_the_local_limit_comes_from_the_manifest_launch_settings():
    """Section 7.5: the local limit is the manifest's ctx_size, never an estimate."""
    from pathlib import Path

    manifest = Path(__file__).resolve().parent.parent / "models/qwen3-0.6b-q8_0.manifest.json"
    assert size.local_limit_of(manifest) == 8192


def test_the_main_entry_writes_a_labeled_offline_report(tmp_path):
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    fixture(fixtures, "a", body("hello"))
    out = tmp_path / "report.json"
    from pathlib import Path

    manifest = Path(__file__).resolve().parent.parent / "models/qwen3-0.6b-q8_0.manifest.json"
    assert (
        size.main(["--fixtures", str(fixtures), "--manifest", str(manifest), "--out", str(out)])
        == 0
    )
    report = json.loads(out.read_text("utf-8"))
    assert report["method"] == "utf8_bytes"
    assert report["local_limit"] == 8192
    assert report["summary"]["captured"] == 1
    assert report["sources"] == ["a.json"]
