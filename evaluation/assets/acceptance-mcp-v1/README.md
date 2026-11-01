# Acceptance MCP tools, capture v1 (spec 0.66 section 12.6.6)

Issue #261 (#251); RM40, AC31. These are the **100 genuine MCP tools** of the
300-asset acceptance catalog. Each comes from a public open-source MCP server,
installed locally at a pinned release and started over stdio on the owner's
Ubuntu VPS (`linux_host`) on 1 November 2026. Its original listing is that
server's own `tools/list` response.

| Server (`id`) | Package and version | License | Tools captured / selected |
| --- | --- | --- | --- |
| `modelcontextprotocol-filesystem` | npm `@modelcontextprotocol/server-filesystem` 2.1.0 | MIT | 14 / 13 |
| `modelcontextprotocol-memory` | npm `@modelcontextprotocol/server-memory` 2.0.1 | MIT | 9 / 7 |
| `modelcontextprotocol-everything` | npm `@modelcontextprotocol/server-everything` 2.1.0 | MIT | 16 / 11 |
| `modelcontextprotocol-sequential-thinking` | npm `@modelcontextprotocol/server-sequential-thinking` 2.0.0 | MIT | 1 / 1 |
| `modelcontextprotocol-git` | PyPI `mcp-server-git` 2026.10.28 | MIT | 12 / 12 |
| `modelcontextprotocol-fetch` | PyPI `mcp-server-fetch` 2026.10.28 | MIT | 1 / 1 |
| `modelcontextprotocol-time` | PyPI `mcp-server-time` 2026.10.28 | MIT | 2 / 2 |
| `microsoft-playwright-mcp` | npm `@playwright/mcp` 0.0.84 | Apache-2.0 | 25 / 24 |
| `chromedevtools-chrome-devtools-mcp` | npm `chrome-devtools-mcp` 1.12.0 | Apache-2.0 | 32 / 29 |

The first seven are reference servers of the `modelcontextprotocol/servers`
repository.

## Files

- `originals/<id>.initialize.json` and `originals/<id>.tools-list.0.json`: the
  exact response line bytes of `initialize` and `tools/list`. No server
  returned a `nextCursor`.
- `licenses/`: the license evidence shipped in each installed package. The four
  npm reference packages ship no LICENSE file (`"license": "SEE LICENSE IN
  LICENSE"`), so their `README.md`, which states the MIT License, is kept
  instead. `mcp-server-time` ships no license file either, so its `METADATA`
  (`License: MIT`) is kept. These are recorded assertions for review, not a
  legal check.
- `capture.v1.json`: every server's pinned package, launch arguments (local
  paths replaced by placeholders), `serverInfo`, license evidence and original
  digests. For every one of the 112 captured tools it gives the original's
  SHA-256, the canonical `schema_hash` (`canonical_digest` of its
  `inputSchema`), origin `genuine_mcp`, and whether the product's tool-schema
  check (`check_parameters_schema`) admits it. All 112 are admitted.
- `selection` in `capture.v1.json`: exactly 100 tools, chosen by a fixed rule:
  SHA-256 of the UTF-8 text `<id>/<tool name>`, ascending, first 100. The other
  12 are listed as excluded with reason `beyond_quota`. No tool was edited,
  translated or generated.
- `lock.v1.json`: the canonical digest of `capture.v1.json`
  (`470c5747e461aae2a65ee91103c99e1468adf1dd5d2e3cf72dbcdf5a271d586b`) and the
  SHA-256 and length of every original, license and script.
- `capture_mcp_tools.py` (acquisition: start each server, send `initialize`,
  `notifications/initialized` and `tools/list`, never call a tool) and
  `freeze_mcp_capture.py` (writes the capture document, the selection and the
  lock). Both need a RelayMind checkout on the import path.

## Reproduce

Both scripts are kept exactly as they ran: the lock binds their bytes, and
`capture.v1.json` binds `capture_mcp_tools.py`. Two things are needed that the
scripts do not say:

- `servers.json` is not committed, because it held local install paths. It is
  a JSON list with one object per server: `id`, `package` (`ecosystem`,
  `name`, `version`), `source_url`, `license_spdx`, `license_file` (the
  installed file kept in `licenses/`, named in `license.evidence`), `argv`, and
  optional `cwd` and `env`. `servers[].launch` in `capture.v1.json` is that
  `argv` with local paths replaced by `<node>`, `<npm>`, `<python>`,
  `<fsroot>` and `<gitrepo>`. The capture does not record `cwd` or `env`.
- `freeze_mcp_capture.py` hashes and locks the scripts it finds in `OUT_DIR`,
  so copy both scripts into OUT_DIR before the freeze. Without them it stops
  with `FileNotFoundError` after the quota check.

```bash
export PYTHONPATH=<relaymind checkout>/src
python capture_mcp_tools.py --servers servers.json --out CAPTURE_DIR
mkdir OUT_DIR && cp capture_mcp_tools.py freeze_mcp_capture.py OUT_DIR/
python freeze_mcp_capture.py CAPTURE_DIR servers.json OUT_DIR CAPTURED_AT
```

Given a capture directory with these originals, the freeze writes these
`capture.v1.json` and `lock.v1.json` byte for byte
(`tests/test_acceptance_mcp_capture.py` replays it from the committed
originals). A new capture is not a replay: servers can answer with other
bytes, and any change needs a new capture version and review, never a
replacement of these files (spec section 12.6.6).

## What this is not

This is not yet an `AssetCatalogSetV1` acceptance release. Still missing for
the 300-asset catalog:

- exactly 100 original Chinese Matrix skills and 100 original English skills
  (the development source has 87 Matrix skills);
- at least 25 reviewed equivalent-function pairs;
- rights review (acquisition, evaluation, retention, redistribution) with
  evidence, and language review;
- the `AssetRecordV1` records of these tools (MCP server/tool namespace,
  capture time, source version) inside a locked release, with cases and labels.

The platform runs of #261 used the generated 300-skill catalog for that reason.
