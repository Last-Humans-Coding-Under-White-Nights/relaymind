# RelayMind provider for OpenClaw

Status: provider package (#136; RM08, RM12, RM19, RM26, RM33, spec section
7.5), on top of the #135 qualification. The plugin registers the `relaymind`
provider through the public plugin SDK of OpenClaw v2026.10.7, publishes the
client-owned alias `relaymind/relaymind-client-tools` when the gateway serves
it, and holds the gateway's application token in the host's credential store.
Round trips with host tools are #137, cancellation, error mapping, and retries
#138, the installable artifact, CI, and the operator guide #145. The real host
has not loaded this package yet: that is an operator step (below), not_run.

What the plugin claims, and nothing more: text input; at most one function
call per response, executed by OpenClaw under its own approvals; the answer is
buffered by the gateway and delivered whole (no live token streaming); no
parallel tool calls, vision, audio, or Responses API; no managed skill
selection, prefetch, or skill stage, and no managed device-aware placement
(client-owned execution has none, section 7.5); usage is unknown. The
gateway's run trace is authoritative for the model calls.

The compatibility table, the SDK seams found at the pin, and the frozen
baseline are in
[`docs/design/2026-10-27-openclaw-integration.md`](../../docs/design/2026-10-27-openclaw-integration.md#compatibility-qualification-135).

## Layout

| Path | Purpose |
| --- | --- |
| `openclaw.plugin.json` | The plugin manifest: provider `relaymind`, the token's env var `RELAYMIND_APP_TOKEN`, the api-key auth choice, the closed config schema |
| `src/index.ts` | The plugin entry (`definePluginEntry`); refuses an invalid configuration by name before registering |
| `src/provider.ts` | The provider: api-key token method, catalog hook, missing-token and reconnect messages; no dynamic models |
| `src/catalog.ts` | Discovery (`GET /v1/models`, `GET /readyz`), the diagnosis, the published entry and its limits, the host outcome |
| `src/config.ts` | The plugin settings: loopback `baseUrl`, `executionMode`, copied `limits` |
| `src/compat.ts` | `relaymindModelCompat()` (the per-model switches) and `relaymindModelEntry()` (#135) |
| `src/diagnostics.ts`, `src/redaction.ts` | The capability statement and operator messages; the logger that scrubs the token |
| `test/*.test.ts` | vitest: manifest, config, catalog, provider, redaction, the switches, the frozen fixtures |
| `test/plugin-transport/drive-plugin.mjs` | plays the host around the plugin: register, catalog, one request through the pinned transport |
| `test/host-transport/` | the #135 transport driver and recording proxy |
| `test/fixtures/v2026.10.7/transport/` | the #135 deterministic transport fixtures, captured by `scripts/openclaw_qualification.py fixtures` |
| `test/fixtures/v2026.10.7/host/` | reserved for captures made with `capture_host_requests.mjs`; the macOS host probe's captures are in `docs/evidence/2026-10-27-openclaw-mac-probe/` |
| `scripts/capture_host_requests.mjs` | the controlled endpoint a real host is pointed at |
| `scripts/pack-check.mjs` | checks the packed tarball: built entry, manifest, README, and public SDK imports only |

## Configuration

The plugin's settings live under `plugins.entries.relaymind.config` in the
host configuration; the schema is closed (an unknown key is refused by name):

```json5
{
  plugins: {
    entries: {
      relaymind: {
        enabled: true,
        config: {
          baseUrl: "http://127.0.0.1:8080/v1",   // required; loopback only
          executionMode: "auto",                   // auto | local_only | external_only
          limits: {                                // optional copies, never estimated
            externalContextLimit: 128000,          // external_model.context_limit
            localContextLimit: 8192,               // ctx_size of the model manifest
            maxCompletionTokens: 1024              // chat.max_completion_tokens
          }
        }
      }
    }
  }
}
```

- `baseUrl` is the gateway's `/v1` root on `localhost`, `127.0.0.0/8`, or
  `[::1]`; another host, credentials in the URL, a query, or another path is
  refused. The gateway is started separately (`relaymind serve --config
  <file>`); the plugin never starts it.
- `executionMode` is sent on every request as `X-RelayMind-Execution-Mode`
  (auto is sent too). `parallel_demo` and unknown values are refused, never
  changed. The gateway's policy keeps its meaning: a mode it does not permit
  is 403 `execution_mode_forbidden`, and the catalog says so. An
  `external_only` setup needs no local model.
- `limits` are copied by hand from the gateway's configuration: no gateway
  endpoint reports them (section 7.5). The host refuses a request over the
  advertised window before sending it, so the advertised window is the
  external limit in `auto` and `external_only` and the local limit in
  `local_only`; a limit not set for the mode is not advertised. The model
  name states the mode, and the catalog log line states which limit the
  window is.
- An operator `parallel_tool_calls: true` model parameter stays refused by
  the gateway (400 `unsupported_parameter`); the plugin does not rewrite it.

## Token handover and reconnect

`relaymind serve` generates a new application token at every start and prints
it once. Store it in the host's credential store (never as a command-line
argument, never in the plugin config):

```bash
openclaw models auth paste-api-key --provider relaymind   # paste at the prompt
# or, in automation:
printf "%s\n" "$TOKEN" | openclaw models auth paste-api-key --provider relaymind
```

`RELAYMIND_APP_TOKEN` in the host's environment also works. After a gateway
restart the stored token is stale: requests and discovery fail with 401
`unauthenticated`, and the catalog logs the reconnect instruction (store the
new token the same way). The plugin never logs, records, or forwards the
token: every message goes through a redacting logger, discovery refuses
redirects, and requests go only to the configured loopback URL.

## Diagnostics

Each catalog run logs one line, and reports the host outcome:

| Diagnosis | Host outcome | Published |
| --- | --- | --- |
| no stored token | none (the catalog returns nothing) | no |
| gateway unreachable at `<baseUrl>` | `unavailable` | no |
| token rejected (401), with the reconnect instruction | `auth-rejected` | no |
| other gateway error or a redirect | `unavailable` | no |
| the gateway does not serve `relaymind-client-tools` | `ready` (empty) | no |
| the configured mode is ready, with the advertised limits | `ready` | yes |
| the configured mode is not ready, with the gateway's reason | `ready` | yes |
| the gateway's policy does not permit the configured mode | `ready` | yes |
| readiness unknown | `ready` | yes |

Only the configured mode is read from `/readyz`: its aggregate 503 (some
permitted mode is not ready) never hides a usable `external_only` mode.

## Checks

Node `>=24.22.1 <25 || >=26.12.1` (the pinned host's range).

```bash
cd integrations/openclaw
npm ci
npm run typecheck
npm test
npm run pack:check     # builds dist/ and checks the packed tarball
```

From the repository root, the Python tier runs the transport and the plugin
against the real gateway over model doubles (it skips without `npm ci`; set
`RELAYMIND_OPENCLAW_NODE=1` to make a missing dependency fail):

```bash
pytest tests/test_openclaw_host_transport.py tests/test_openclaw_plugin.py \
  tests/test_openclaw_qualification_script.py
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

### Real host load and token setup (operator, not run yet)

On a machine with OpenClaw v2026.10.7 and a running `relaymind serve`:

```bash
cd integrations/openclaw && npm ci && npm run build
npm pack --pack-destination /tmp/rm-plugin
openclaw plugins install npm-pack:/tmp/rm-plugin/relaymind-openclaw-provider-0.1.0.tgz --force
openclaw config set plugins.entries.relaymind.config.baseUrl http://127.0.0.1:8080/v1
openclaw plugins inspect relaymind --runtime --json
openclaw models auth paste-api-key --provider relaymind   # the token relaymind serve printed
openclaw models list --json | grep relaymind
# restart relaymind serve: the list reports the 401 reconnect line; paste the new token; it recovers
```

Disable with `openclaw plugins disable relaymind`; remove with
`openclaw plugins uninstall relaymind` (the full operator guide is #145).
The command names follow the pinned host's documentation and are to be
confirmed on the first run.

## Operator runs

These need a real OpenClaw v2026.10.7 install, and the size measurement
with tokenizer counts needs `llama-server` with the pinned model
(`models/README.md`). Their results are evidence only when recorded in
`docs/EVIDENCE.md`; until then they are `not_run`.

Status: a host capture ran on macOS with the probe's own capture server
(`docs/evidence/2026-10-27-openclaw-mac-probe/REPORT.md`, checked by
`tests/test_openclaw_host_captures.py`). The tokenizer size count then ran
on macOS (`docs/evidence/2026-10-27-openclaw-mac-qualification/`, recorded
in `docs/EVIDENCE.md`): 0 of 3 fresh host requests and 0 of 7 probe requests
fit the 8192 local limit (10,500 to 10,577 tokens). The local context stays
8192, so in practice every OpenClaw turn goes external in auto and gets
needs_external in local_only. One live turn in auto without an external
model ended needs_external / context_limit with 0 model calls; it used a
config-row workaround (a `models.providers` row, not the plugin's model,
#136). The real host against the gateway over doubles ran on the same Mac
at `a52f8bf` and failed: the harness host scenarios 4a to 4c ended with
`checks.passed` false (the plugin's model was unknown to `agent exec`, and
the test host tools never registered; REPORT.md step 4). The fixes belong
to #136 and #137; Linux and Windows host runs are not_run.

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

The deciding count is a tokenizer count of requests as the host sent them.
The stored probe captures cannot give it: the probe's sanitizer replaced
local paths and names with shorter placeholders, so they are smaller than
what the host sent (about 26,700 system prompt characters stored against
29,942 received). Capture fresh requests on the Mac with the probe's
capture server (`docs/evidence/2026-10-27-openclaw-mac-probe/REPORT.md`,
section 2; config `configs/config-b-base.json`, the base switches), then
count them before any sanitizing:

```bash
node docs/evidence/2026-10-27-openclaw-mac-probe/probe-src/server/capture-server.mjs \
  <scratch>/captures          # 127.0.0.1:8089; run the hello and read-file turns
python scripts/openclaw_request_size.py \
  --fixtures <scratch>/captures \
  --manifest models/qwen3-0.6b-q8_0.manifest.json \
  --base-url http://127.0.0.1:8033 \
  --out docs/evidence/<date>-openclaw-request-size-<machine>.json
```

Commit only the report (it holds sizes and file names, no request text),
never the raw captures. A run on the stored captures must pass
`--sanitized`: every size is then a lower bound on the host's request, so
a count over the limit proves the request is over it and one under it
decides nothing.

The script reads both the probe's layout (one request per file, a folder
per run) and the fixture layout of `capture_host_requests.mjs`. Without
`--base-url` every number is a UTF-8 byte upper bound, labeled so: a
request over the bound is undecided, never needs_external. With
`--external-context-limit` the report also counts the requests that
provably fit the external limit. The offline run on the stored captures is
recorded in
`docs/evidence/2026-10-27-openclaw-request-size-mac-probe-utf8.json`
(`--sanitized`; 0 of 7 proved to fit, 7 undecided); the tokenizer count on
macOS superseded it (`docs/evidence/2026-10-27-openclaw-mac-qualification/request-size.json`).

### Real host against the gateway over doubles

```bash
python scripts/openclaw_qualification.py host \
  --out docs/evidence/<date>-openclaw-host-qualification-<machine>.json \
  -- openclaw agent exec --config <a config whose relaymind provider reads
     RELAYMIND_BASE_URL and RELAYMIND_APP_TOKEN> --json \
     --model relaymind/relaymind-client-tools "Say hello"
```

Result so far: the one run, on macOS at `a52f8bf`
(`docs/evidence/2026-10-27-openclaw-mac-qualification/REPORT.md`, step 4),
failed: the default, tool-deny, and tool-approve scenarios ended with
`checks.passed` false, and only the abort scenario passed. The fixes belong
to #136 and #137 (`docs/EVIDENCE.md`).

The harness passes the gateway URL and token in the command's environment
only and never writes the token. A command that cannot start is `not_run`;
one that runs past `--timeout` (900 s by default) is `timed_out`, and the
export still holds the runs the gateway recorded.
