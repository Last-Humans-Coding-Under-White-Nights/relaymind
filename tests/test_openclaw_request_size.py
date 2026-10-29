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
    assert second["fits_local"] is None
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


def probe_capture(path, request_body, *, method="POST", route="/v1/chat/completions"):
    """One request as the Mac host probe stored it (one file per request)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"method": method, "path": route, "headers": {}, "body": request_body}
    path.write_text(json.dumps(data), "utf-8")


def test_the_host_probe_captures_are_read_one_request_per_file_in_run_folders(tmp_path):
    """#135: the real-host capture (docs/evidence/2026-10-27-openclaw-mac-probe/requests/)
    keeps one request per file under a folder per run; only chat requests are measured."""
    probe_capture(tmp_path / "b1-hello" / "001-POST-_v1_chat_completions.json", body("hi"))
    probe_capture(tmp_path / "c2-read" / "001-POST-_v1_chat_completions.json", body("a"))
    probe_capture(tmp_path / "c2-read" / "002-POST-_v1_chat_completions.json", body("b"))
    probe_capture(
        tmp_path / "p0-refresh" / "001-GET-_v1_models.json", None, method="GET", route="/v1/models"
    )
    (tmp_path / "c2-read" / "events.jsonl").write_text("{}\n", "utf-8")
    requests = size.load_requests(tmp_path)
    assert [(source, index) for source, index, _ in requests] == [
        ("b1-hello/001-POST-_v1_chat_completions.json", 0),
        ("c2-read/001-POST-_v1_chat_completions.json", 0),
        ("c2-read/002-POST-_v1_chat_completions.json", 0),
    ]
    assert requests[2][2]["messages"][1]["content"] == "b"


def test_a_request_over_the_byte_bound_is_undecided_not_refused(tmp_path):
    """RM13: bytes bound tokens from above, so a request over the bound may still fit;
    only the tokenizer count decides it, and the report never counts it as needs_external."""
    fixture(tmp_path, "a", body("x" * 10), body("y" * 9000), body("z" * 9000))
    report = asyncio.run(size.measure_fixtures(size.load_requests(tmp_path), size.ByteBound(8192)))
    rows = report["requests"]
    assert [r["fits_local"] for r in rows] == [True, None, None]
    summary = report["summary"]
    assert summary["fits_local"] == 1
    assert summary["undecided"] == 2
    assert summary["share_fits_local"] == pytest.approx(1 / 3)
    assert summary["share_fits_local_is"] == "lower_bound"
    assert summary["local_only"] == {"local": 1, "needs_external": 0, "undecided": 2}
    assert summary["auto"] == {"local": 1, "external_if_it_fits": 0, "undecided": 2}


def test_a_tokenizer_count_decides_every_request(tmp_path):
    fixture(tmp_path, "a", body("x" * 10), body("x" * 200))
    adapter = ScriptedAdapter(capabilities=make_capabilities(context_limit=100))
    report = asyncio.run(size.measure_fixtures(size.load_requests(tmp_path), adapter))
    assert report["summary"]["undecided"] == 0
    assert report["summary"]["share_fits_local_is"] == "measured"


def test_on_sanitized_captures_a_tokenizer_count_proves_only_that_a_request_is_over(tmp_path):
    """RM16, section 12: the probe's stored captures are smaller than what the host
    sent (shorter placeholders), so their size is a lower bound on the host's: a
    count over the limit proves the host's request is over, one under proves nothing."""
    fixture(tmp_path, "a", body("x" * 10), body("x" * 200))
    adapter = ScriptedAdapter(capabilities=make_capabilities(context_limit=100))
    report = asyncio.run(
        size.measure_fixtures(size.load_requests(tmp_path), adapter, sanitized=True)
    )
    assert report["captures"] == "sanitized"
    assert "lower bound" in report["captures_note"]
    assert [r["fits_local"] for r in report["requests"]] == [None, False]
    summary = report["summary"]
    assert summary["fits_local"] == 0
    assert summary["undecided"] == 1
    assert summary["share_fits_local_is"] == "lower_bound"
    assert summary["local_only"] == {"local": 0, "needs_external": 1, "undecided": 1}
    assert summary["auto"] == {"local": 0, "external_if_it_fits": 1, "undecided": 1}


def test_on_sanitized_captures_the_byte_bound_decides_nothing(tmp_path):
    """Neither bound holds: bytes bound the stored request's tokens, not the host's."""
    fixture(tmp_path, "a", body("x" * 10), body("y" * 9000))
    report = asyncio.run(
        size.measure_fixtures(size.load_requests(tmp_path), size.ByteBound(8192), sanitized=True)
    )
    assert [r["fits_local"] for r in report["requests"]] == [None, None]
    assert report["summary"]["share_fits_local_is"] == "lower_bound"


def test_unsanitized_captures_are_labeled_as_the_host_sent_them(tmp_path):
    fixture(tmp_path, "a", body("x" * 10))
    adapter = ScriptedAdapter(capabilities=make_capabilities(context_limit=100))
    report = asyncio.run(size.measure_fixtures(size.load_requests(tmp_path), adapter))
    assert report["captures"] == "as_sent"
    assert report["summary"]["share_fits_local_is"] == "measured"


def test_undecided_requests_under_the_external_limit_by_the_byte_bound_fit_externally(tmp_path):
    """A byte bound under the external limit proves an external fit, even when the
    local fit is undecided; the report keeps that count and the external limit result."""
    fixture(tmp_path, "a", body("x" * 10), body("y" * 9000), body("z" * 200_000))
    report = asyncio.run(
        size.measure_fixtures(
            size.load_requests(tmp_path), size.ByteBound(8192), external_context_limit=128_000
        )
    )
    summary = report["summary"]
    assert summary["auto"] == {
        "local": 1,
        "external": 0,
        "context_limit_exceeded": 0,
        "undecided": 2,
    }
    assert summary["undecided_within_external_limit"] == 1
    assert [r.get("fits_external") for r in report["requests"]] == [None, True, None]


def test_the_main_entry_labels_sanitized_captures(tmp_path):
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    fixture(fixtures, "a", body("hello"))
    out = tmp_path / "report.json"
    from pathlib import Path

    manifest = Path(__file__).resolve().parent.parent / "models/qwen3-0.6b-q8_0.manifest.json"
    args = ["--fixtures", str(fixtures), "--manifest", str(manifest), "--out", str(out)]
    assert size.main([*args, "--sanitized"]) == 0
    report = json.loads(out.read_text("utf-8"))
    assert report["captures"] == "sanitized"
    assert report["requests"][0]["fits_local"] is None
