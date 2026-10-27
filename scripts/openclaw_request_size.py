"""Captured OpenClaw request sizes against the local input limit (RM13, RM26, RM33; #135).

Spec section 7.5: the host's system prompt and tool catalog are large, so
#135 reports the size of the captured host requests against the configured
local input limit. That size, not the routing policy, bounds how much of the
host's work can run locally (the local-value hypothesis of #139).

The script reads fixture files (``exchanges[].request.body``: the transport
fixtures of ``scripts/openclaw_qualification.py`` or the host captures of
``integrations/openclaw/scripts/capture_host_requests.mjs``), validates each
body as the gateway does (``parse_chat_request``; a request the gateway
rejects is counted as rejected, never measured as if it ran), and measures it
with the gateway's own ``measure_request``:

- with ``--base-url`` of a running llama-server (models/README.md), through
  the pinned runtime's template and tokenizer: method ``tokenizer``;
- without it, the UTF-8 length of the chat body's prompt fields: method
  ``utf8_bytes``, an upper bound on the token count, labeled as such: a
  request under the limit by this bound fits; one over it is undecided.

The local limit is the manifest's ``launch_settings.ctx_size``, read the way
the launcher reads it. Fitting the limit is necessary for local work, not
sufficient: the model must still produce an admissible answer or call.

Usage:
    python scripts/openclaw_request_size.py \\
        --fixtures integrations/openclaw/test/fixtures/v2026.10.7/host \\
        --manifest models/qwen3-0.6b-q8_0.manifest.json \\
        [--base-url http://127.0.0.1:8033] [--external-context-limit N] \\
        --out docs/evidence/<date>-openclaw-request-size-<machine>.json
"""

import argparse
import asyncio
import json
import statistics
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from relaymind.adapters import AdapterCapabilities, ToolSchema  # noqa: E402
from relaymind.adapters.manifest import launch_adapter_settings, load_manifest  # noqa: E402
from relaymind.api.chat_request import CLIENT_TOOLS, parse_chat_request  # noqa: E402
from relaymind.api.errors import ApiError  # noqa: E402
from relaymind.orchestrator.client_loop import ToolOffer  # noqa: E402
from relaymind.orchestrator.handoff import measure_request  # noqa: E402

NOTE = (
    "Fitting the local limit is necessary for local work, not sufficient: the local "
    "model must still produce an admissible answer or call (sections 7.2 and 11)."
)
METHOD_NOTES = {
    "tokenizer": "token counts through the pinned runtime's chat template and tokenizer",
    "utf8_bytes": (
        "UTF-8 bytes of the chat body's prompt fields: an upper bound on the token "
        "count, not a token count; a request that fits by this bound fits, and one "
        "over it is undecided (fits_local null) until a tokenizer count"
    ),
}


class ByteBound:
    """No tokenizer: measure_request then measures UTF-8 bytes against the limit."""

    def __init__(self, context_limit: int) -> None:
        self._capabilities = AdapterCapabilities(
            text_streaming=True,
            tool_calling=True,
            structured_routing_output=True,
            context_limit=context_limit,
            reasoning_controls=(),
            usage_reporting=False,
            tokenizer_available=False,
        )

    async def capabilities(self) -> AdapterCapabilities:
        return self._capabilities


def local_limit_of(manifest: Path) -> int:
    """The local context limit, as the launcher derives it (section 6)."""
    context, _ = launch_adapter_settings(load_manifest(manifest).launch_settings)
    return context


def _chat_body(data: dict) -> dict | None:
    """The body of one stored request, when it is a chat request."""
    if data.get("method", "POST") != "POST":
        return None
    if not str(data.get("path", "/v1/chat/completions")).endswith("/chat/completions"):
        return None
    body = data.get("body")
    return body if isinstance(body, dict) else None


def load_requests(directory: Path) -> list[tuple[str, int, dict]]:
    """Every captured chat request body: (file path under directory, exchange index, body).

    Two layouts are read: a fixture file with ``exchanges[].request`` (the
    transport fixtures and ``capture_host_requests.mjs``), and one stored
    request per file under a folder per run (the real-host probe in
    ``docs/evidence/2026-10-27-openclaw-mac-probe/requests/``). Other requests,
    such as ``GET /v1/models``, are not measured.
    """
    requests = []
    for path in sorted(directory.rglob("*.json")):
        data = json.loads(path.read_text("utf-8"))
        if not isinstance(data, dict):
            continue
        source = path.relative_to(directory).as_posix()
        if "exchanges" in data:
            for index, exchange in enumerate(data["exchanges"]):
                body = _chat_body(exchange.get("request") or {})
                if body is not None:
                    requests.append((source, index, body))
        elif "body" in data:
            body = _chat_body(data)
            if body is not None:
                requests.append((source, 0, body))
    return requests


async def measure_fixtures(
    requests: Sequence[tuple[str, int, dict]],
    adapter: Any,
    *,
    external_context_limit: int | None = None,
) -> dict[str, Any]:
    """Measure each request; summarize what auto and local_only would do by size."""
    capabilities = await adapter.capabilities()
    method = "tokenizer" if capabilities.tokenizer_available else "utf8_bytes"
    rows = []
    for source, index, body in requests:
        row: dict[str, Any] = {"source": source, "exchange": index}
        try:
            request = parse_chat_request(body, served=(CLIENT_TOOLS,))
        except ApiError as error:
            row["rejected"] = error.code
            row["message"] = str(error)
            rows.append(row)
            continue
        choice = request.tool_choice
        offer = ToolOffer(
            tuple(ToolSchema(t.name, t.description or "", t.parameters) for t in request.tools),
            choice="auto" if choice is None else choice.mode,
            function=None if choice is None else choice.name,
        )
        measured = await measure_request(adapter, capabilities, request.messages, offer.offered)
        # A byte count bounds the token count from above: under the limit it
        # proves a fit; over it, only the tokenizer count decides (None).
        fits: bool | None = measured.fits
        if not measured.fits and measured.method == "utf8_bytes":
            fits = None
        row.update(
            size=measured.size,
            method=measured.method,
            messages=len(request.messages),
            tools=len(offer.offered),
            fits_local=fits,
        )
        rows.append(row)
    measured_rows = [r for r in rows if "size" in r]
    fits = sum(1 for r in measured_rows if r["fits_local"] is True)
    undecided = sum(1 for r in measured_rows if r["fits_local"] is None)
    over = [r for r in measured_rows if r["fits_local"] is False]
    if undecided:
        auto: dict[str, int] = {"local": fits, "undecided": undecided}
        local_only = {"local": fits, "undecided": undecided}
    elif external_context_limit is None:
        auto = {"local": fits, "external_if_it_fits": len(over)}
        local_only = {"local": fits, "needs_external": len(over)}
    else:
        # The same measurement against the external limit; a tokenizer count
        # is local-model tokens, so this is indicative for another tokenizer.
        external = sum(1 for r in over if r["size"] <= external_context_limit)
        auto = {
            "local": fits,
            "external": external,
            "context_limit_exceeded": len(over) - external,
        }
        local_only = {"local": fits, "needs_external": len(over)}
    sizes = [r["size"] for r in measured_rows]
    summary = {
        "captured": len(rows),
        "rejected": len(rows) - len(measured_rows),
        "measured": len(measured_rows),
        "fits_local": fits,
        "undecided": undecided,
        "share_fits_local": fits / len(measured_rows) if measured_rows else None,
        "share_fits_local_is": "lower_bound" if method == "utf8_bytes" else "measured",
        "auto": auto,
        "local_only": local_only,
        "size_min": min(sizes) if sizes else None,
        "size_median": statistics.median(sizes) if sizes else None,
        "size_max": max(sizes) if sizes else None,
    }
    return {
        "kind": "openclaw-request-size",
        "method": method,
        "method_note": METHOD_NOTES[method],
        "local_limit": capabilities.context_limit,
        "external_context_limit": external_context_limit,
        "note": NOTE,
        "summary": summary,
        "requests": rows,
    }


def _adapter(base_url: str | None, limit: int):
    if base_url is None:
        return ByteBound(limit)
    from relaymind.adapters.llamacpp import LlamaCppAdapter, LlamaCppConfig

    return LlamaCppAdapter(LlamaCppConfig(base_url=base_url, context_limit=limit))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--fixtures", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--base-url", help="a running llama-server for tokenizer counts")
    parser.add_argument("--external-context-limit", type=int)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    limit = local_limit_of(args.manifest)
    requests = load_requests(args.fixtures)
    report = asyncio.run(
        measure_fixtures(
            requests,
            _adapter(args.base_url, limit),
            external_context_limit=args.external_context_limit,
        )
    )
    report["manifest"] = str(args.manifest)
    report["sources"] = sorted({source for source, _, _ in requests})
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", "utf-8")
    print(json.dumps(report["summary"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
