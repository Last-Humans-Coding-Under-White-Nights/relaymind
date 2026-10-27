# RelayMind provider for OpenClaw (qualification stage)

Status: qualification only (#135; RM33, AC24, spec section 7.5). This
package holds the declared host compatibility switches, the host transport
driver, and the frozen wire fixtures for OpenClaw v2026.10.7. The provider
plugin itself (registration, catalog, configuration, token setup) is #136;
the operator guide is #145. Nothing here is a working plugin yet.

The compatibility table, the SDK seams found at the pin, and the frozen
baseline for #136 to #138 are in
[`docs/design/2026-10-27-openclaw-integration.md`](../../docs/design/2026-10-27-openclaw-integration.md#compatibility-qualification-135).

## Layout

| Path | Purpose |
| --- | --- |
| `src/compat.ts` | `relaymindModelCompat()` (the per-model switches) and `relaymindModelEntry()` (the client-owned alias entry; limits copied, never estimated) |
| `test/compat.test.ts`, `test/fixtures.test.ts` | vitest: the switches, and the frozen fixtures (pins, no credential, 7.1 fields only) |
| `test/host-transport/drive.mjs` | runs the pinned `openai-completions` transport against a gateway and plays the host |
| `test/host-transport/recorder.mjs` | the recording loopback proxy and scripted endpoint |
| `test/fixtures/v2026.10.7/transport/` | deterministic transport fixtures, captured by `scripts/openclaw_qualification.py fixtures` |
| `test/fixtures/v2026.10.7/host/` | reserved for captures made with `capture_host_requests.mjs`; the macOS host probe's captures are in `docs/evidence/2026-10-27-openclaw-mac-probe/` |
| `scripts/capture_host_requests.mjs` | the controlled endpoint a real host is pointed at |

## Checks

Node `>=24.22.1 <25 || >=26.12.1` (the pinned host's range).

```bash
cd integrations/openclaw
npm ci
npm run typecheck
npm test
```

From the repository root, the Python tier runs the transport against the
real gateway over model doubles (it skips without `npm ci`; set
`RELAYMIND_OPENCLAW_NODE=1` to make a missing dependency fail):

```bash
pytest tests/test_openclaw_host_transport.py tests/test_openclaw_qualification_script.py
```

To recapture the transport fixtures (they must not change on the same pins):

```bash
python scripts/openclaw_qualification.py fixtures \
  --out-dir integrations/openclaw/test/fixtures/v2026.10.7/transport
```

Capture from a clean checkout of a commit that already holds the harness, the
driver, and the fixtures, then commit `VERSIONS.json` on its own: it records
that commit as its provenance, and
`tests/test_openclaw_qualification_script.py` fails when the tree was dirty or
when a capture input changed after the recorded commit.

## Operator runs

These need a real OpenClaw v2026.10.7 install, and the size measurement
with tokenizer counts needs `llama-server` with the pinned model
(`models/README.md`). Their results are evidence only when recorded in
`docs/EVIDENCE.md`; until then they are `not_run`.

Status: a host capture ran on macOS with the probe's own capture server
(`docs/evidence/2026-10-27-openclaw-mac-probe/REPORT.md`, checked by
`tests/test_openclaw_host_captures.py`). The tokenizer size count and the
real host against the gateway are not run.

Host facts from that probe that the steps below rely on: a model in the
host config needs `name`; `strictMessageKeys` must stay false (true strips
the tool linkage); isolate the host with `OPENCLAW_HOME`,
`OPENCLAW_STATE_DIR`, `OPENCLAW_CONFIG_PATH`, and `--config` (the host
still writes its log to `/tmp/openclaw/`); the host removes underscores
from a replayed tool call ID, so compare IDs with that in mind.

### Host capture (operator)

Point a real host at the controlled endpoint and run one text task and one
tool task. The endpoint answers with scripted text, or with one call to a
named host tool first.

```bash
cd integrations/openclaw && npm ci
node scripts/capture_host_requests.mjs --port 8089 \
  --out test/fixtures/v2026.10.7/host/text.json
```

In the host configuration, add a custom provider for the capture (the
switches are `relaymindModelCompat()`; the field names are the host's
`models.providers` schema at the pin, confirmed by the macOS probe, where
`name` is required):

```json5
{
  models: {
    providers: {
      "relaymind-capture": {
        baseUrl: "http://127.0.0.1:8089/v1",
        api: "openai-completions",
        apiKey: "capture",
        models: [{
          id: "relaymind-client-tools", name: "RelayMind capture",
          reasoning: false, input: ["text"],
          compat: {
            supportsUsageInStreaming: false, supportsStrictMode: false,
            supportsReasoningEffort: false, supportsDeveloperRole: false,
            maxTokensField: "max_completion_tokens",
            supportsJsonSchemaResponseFormat: false, supportsPromptCacheKey: false,
            supportsStore: false, strictMessageKeys: false,
            requiresToolResultName: false, requiresAssistantAfterToolResult: false,
            requiresStringContent: false, supportsTools: true
          }
        }]
      }
    }
  }
}
```

```bash
openclaw agent exec --config <that file> --json \
  --model relaymind-capture/relaymind-client-tools "Say hello"
# restart the endpoint with --out .../host/tool.json --tool-call <a host tool name>
openclaw agent exec --config <that file> --json --cwd /tmp/rm-capture \
  --model relaymind-capture/relaymind-client-tools \
  "Read the file notes.txt in this folder and tell me its first line"
```

Review each host fixture before committing it: the host's system prompt
can name local paths (the script replaces the home directory with `<home>`).

### Request size against the local limit

```bash
python scripts/openclaw_request_size.py \
  --fixtures docs/evidence/2026-10-27-openclaw-mac-probe/requests \
  --manifest models/qwen3-0.6b-q8_0.manifest.json \
  --base-url http://127.0.0.1:8033 \
  --out docs/evidence/<date>-openclaw-request-size-<machine>.json
```

The script reads both the probe's layout (one request per file, a folder
per run) and the fixture layout of `capture_host_requests.mjs`. Without
`--base-url` every number is a UTF-8 byte upper bound, labeled so: a
request over the bound is undecided, never needs_external. That offline
run is recorded in
`docs/evidence/2026-10-27-openclaw-request-size-mac-probe-utf8.json`
(0 of 7 proved to fit, 7 undecided).

### Real host against the gateway over doubles

```bash
python scripts/openclaw_qualification.py host \
  --out docs/evidence/<date>-openclaw-host-qualification-<machine>.json \
  -- openclaw agent exec --config <a config whose relaymind provider reads
     RELAYMIND_BASE_URL and RELAYMIND_APP_TOKEN> --json \
     --model relaymind/relaymind-client-tools "Say hello"
```

The harness passes the gateway URL and token in the command's environment
only and never writes the token. A command that cannot start is `not_run`;
one that runs past `--timeout` (900 s by default) is `timed_out`, and the
export still holds the runs the gateway recorded.
