# RelayMind provider for OpenClaw

Status: provider package (#136; RM08, RM12, RM19, RM26, RM33, spec section
7.5) with text and host-owned tool round trips (#137), on top of the #135
qualification. The plugin registers the `relaymind`
provider through the public plugin SDK of OpenClaw v2026.10.7, publishes the
client-owned alias `relaymind/relaymind-client-tools` when the gateway serves
it, and holds the gateway's application token in the host's credential store.
Cancellation, error mapping, and retries are #138, the installable artifact,
CI, and the operator guide #145. The real
pinned host loaded this package once, in a Linux VPS functional check (not a
supported-platform qualification) against the gateway over model doubles
([`docs/evidence/2026-10-28-openclaw-vps-functional-136/`](../../docs/evidence/2026-10-28-openclaw-vps-functional-136/REPORT.md)):
install, enable, inspect, token setup, `models list --refresh`, a text turn
through `agent exec`, the stale-token 401 line and recovery, and removal
all worked with the command forms below. This package on macOS and Windows
is `not_run`. The real host's approval prompt on a host tool has not been
exercised yet: those are operator steps (below), not_run.

What the plugin claims, and nothing more: text input; at most one function
call per response, executed by OpenClaw under its own approvals; the answer is
buffered by the gateway and delivered whole (no live token streaming); no
parallel tool calls, vision, audio, or Responses API; no managed skill
selection, prefetch, or skill stage, and no managed device-aware placement
(client-owned execution has none, section 7.5); usage is unknown. The
gateway's run trace is authoritative for the model calls.

Declared compatibility is the qualified release only: `openclaw.compat.pluginApi`
and the `openclaw` peer dependency are both exactly `2026.10.7`, so the host's
own range check refuses to load the plugin on any other release (a numeric
correction such as `2026.10.7-1` is the same release). A later release is
declared only after it is qualified again: the #135 capture rerun against it,
its fixtures recorded under `test/fixtures/v<release>/`, and the pins moved.

The compatibility table, the SDK seams found at the pin, and the frozen
baseline are in
[`docs/design/2026-10-27-openclaw-integration.md`](../../docs/design/2026-10-27-openclaw-integration.md#compatibility-qualification-135).

## Layout

| Path | Purpose |
| --- | --- |
| `openclaw.plugin.json` | The plugin manifest: provider `relaymind`, the token's env var `RELAYMIND_APP_TOKEN`, the api-key auth choice, the closed config schema |
| `src/index.ts` | The plugin entry (`definePluginEntry`); refuses an invalid configuration by name before registering |
| `src/provider.ts` | The provider: api-key token method, catalog hook, `resolveDynamicModel` for the one alias (the headless `agent exec` path), missing-token and reconnect messages |
| `src/catalog.ts` | Discovery (`GET /v1/models`, `GET /readyz`), the diagnosis, the published entry and its limits, the host outcome |
| `src/config.ts` | The plugin settings: loopback `baseUrl`, `executionMode`, copied `limits` |
| `src/compat.ts` | `relaymindModelCompat()` (the per-model switches) and `relaymindModelEntry()` (#135) |
| `src/stream.ts` | The provider's `wrapStreamFn`: the input guard before sending, and an empty answer or more than one call made a failure (#137) |
| `src/input-guard.ts` | Unsupported input refused by field, never removed (#137) |
| `src/diagnostics.ts`, `src/redaction.ts` | The capability statement and operator messages; the logger that scrubs the token |
| `test/*.test.ts` | vitest: manifest, config, catalog, provider, redaction, the switches, the frozen fixtures |
| `test/plugin-transport/drive-plugin.mjs` | plays the host around the plugin: register, catalog (or `resolveDynamicModel`), the conversation through the pinned transport wrapped by the plugin, and the host's sandbox tools and approval decision |
| `test/host-tool-plugin/` | test only, never packed: the `relaymind_test_echo` and `relaymind_test_write` host tools, every write behind the host's approval |
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
  changed. The host refuses them first: it validates the entry against the
  manifest's `configSchema`, whose enum is `auto`, `local_only`,
  `external_only`, before any plugin code runs. `openclaw config set
  ... executionMode parallel_demo` exits 1 with `Config change declined`
  and `must be equal to one of the allowed values`, so the plugin's own
  parallel_demo message does not show (macOS run at `a52f8bf`, and the
  Linux VPS functional check). A value written straight into the config
  file makes the host leave the plugin out (macOS run). The gateway's
  policy keeps its meaning: a mode it does not permit is 403
  `execution_mode_forbidden`, and the catalog says so. An `external_only`
  setup needs no local model.
- `limits` are copied by hand from the gateway's configuration: no gateway
  endpoint reports them (section 7.5). The host refuses a request over the
  advertised window before sending it, so the advertised window is the
  external limit in `auto` and `external_only` and the local limit in
  `local_only`; a limit not set for the mode is not advertised. The model
  name states the mode, and the catalog log line states which limit the
  window is.
- An operator `parallel_tool_calls: true` model parameter stays refused: the
  plugin refuses the request before sending it, by name and with the
  gateway's code (`unsupported_parameter`), and never rewrites the value
  (see "What the plugin refuses and why").

## Round trips and host tools (#137)

The host sends the whole conversation and its tool catalog on every request;
the plugin forwards them unchanged (the pinned host orders the catalog by
name). A response is a text answer or exactly one function call. OpenClaw
executes the call under its own permissions and approvals, and sends the
result in the next request, which is a new RelayMind run: RelayMind records
`tool.proposed` and never starts, finishes, approves, or certifies a host
tool. A tool result reaches the model as data, unchanged; it grants nothing.
The gateway's answer is buffered: a streamed response is a replay of a
finished run (keep-alive comments under early headers are never content),
so there is no token-by-token generation.

### What the plugin refuses and why

The pinned host adapts input a model cannot take instead of failing; each
of these would change the question, so the plugin refuses the request
before sending it and names the field (it never trims, summarizes, or
drops a message or a tool):

| Input | Field named | Why |
| --- | --- | --- |
| an image part in a user message or a tool result | `messages[i].content[j].type` | the host would replace it with "(image omitted: ...)" text; the alias is text only |
| a structured response format | `response_format` | section 7.1 refuses it (`unsupported_parameter`); the switches turn structured output off |
| `parallel_tool_calls: true` (an operator model parameter) | `parallel_tool_calls` | the gateway refuses it (`unsupported_parameter`): one call per response |
| a strict tool | `tools[i].function.strict` | section 7.1 has no strict schemas |
| `n` other than 1 | `n` | one choice per response |
| a payload that lost a host tool | `tools` | the plugin never drops a tool |

After the response, an answer with no text and no call, and a response with
more than one call, are failures, never an empty success. The pinned
transport already fails a stream without a finish chunk, an `{"error": ...}`
data event, malformed JSON, a malformed call, and an HTTP error.

## Token handover and reconnect

`relaymind serve` generates a new application token at every start and prints
it once. Store it in the host's credential store (never as a command-line
argument, never in the plugin config):

```bash
openclaw models auth paste-api-key --provider relaymind   # paste at the prompt
# or, in automation:
printf "%s\n" "$TOKEN" | openclaw models auth paste-api-key --provider relaymind
```

On the macOS host probe (OpenClaw 2026.10.7), `paste-api-key` took a
43-character token on standard input with no format check, and the stored
key reached the catalog hook; the Linux VPS functional check saw the same
with this package (`Auth profile: relaymind:manual (relaymind/api_key)`).
It warns that the local Gateway could not refresh; a headless `agent exec`
still reads the stored key. The host keeps the key in its state database
(`$OPENCLAW_STATE_DIR/state/openclaw.sqlite`), not in the config file.
`openclaw models auth login --provider relaymind` asks for the token too,
but only in an interactive terminal: without one it refuses, and typing the
token at its prompt was not checked (`not_run`).
`RELAYMIND_APP_TOKEN` in the host's environment is the provider's declared
env var; reading it was not checked on a real host (`not_run`). After a gateway
restart the stored token is stale: requests and discovery fail with 401
`unauthenticated`, and `models list --refresh` logs the reconnect instruction
(store the new token the same way). `agent exec` with the stale token exits 1
with the host's own message (`Couldn't sign in to the AI service`, logged as
`401 a valid application token is required`), not the plugin's line. After
`paste-api-key` with the new token, both recover (Linux VPS functional check
and macOS run). The plugin never logs, records, or forwards the
token: every message goes through a redacting logger, discovery refuses
redirects, and requests go only to the configured loopback URL.

## How the host finds the model

The provider publishes the alias two ways:

- the catalog hook (`openclaw models list --refresh`) discovers it on the
  gateway and logs the diagnosis below;
- `resolveDynamicModel` resolves the same entry for the one ID
  `relaymind-client-tools`, synchronously and without a request. The pinned
  host needs it: on the macOS probe, headless `openclaw agent exec` never ran
  the catalog hook and failed with `Unknown model` until the plugin had
  `resolveDynamicModel` (a static catalog did not help either). Any other ID
  resolves to nothing. When the gateway does not serve the alias, a request
  fails with the gateway's own error, never an empty answer. With this
  package, `agent exec` answered through the alias before any catalog
  refresh, sending no discovery request (Linux VPS functional check).

`openclaw models list` without `--refresh` shows only the cached catalog
when no host Gateway is running, so it has no relaymind row; use
`--refresh` to run the catalog hook. The host shows a 128000 context window
for the row when the plugin advertises none: that is the host's default,
not a RelayMind limit.

On the wire, the host honored the plugin-supplied model's switches on both
paths (Linux VPS functional check, spec 0.49): no `stream_options`, no
`max_tokens` (it sent `max_completion_tokens` when the limit was copied), no
`reasoning_effort`, `store`, `prompt_cache_key`, `parallel_tool_calls`, or
tool `strict`, and no developer message.

The pinned host calls the catalog hook with no abort signal, so every
discovery request has the plugin's own five-second timeout; a hung gateway
is reported as unreachable. Whether the host also bounds the hook was not
checked (`not_run`).

## Diagnostics

Each catalog run logs one line, and reports the host outcome:

| Diagnosis | Host outcome | Published |
| --- | --- | --- |
| no stored token (the pinned host did not call the hook without a stored credential, so no line showed) | none (the catalog returns nothing) | no |
| gateway unreachable at `<baseUrl>` | `unavailable` | no |
| token rejected (401 from `/v1/models` or `/readyz`), with the reconnect instruction | `auth-rejected` | no |
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
  tests/test_openclaw_round_trips.py tests/test_openclaw_qualification_script.py
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

### Real host load and token setup

On a machine with OpenClaw v2026.10.7 and a running `relaymind serve`.
These forms worked with this package in the
Linux VPS functional check (not a supported-platform qualification)
([`docs/evidence/2026-10-28-openclaw-vps-functional-136/`](../../docs/evidence/2026-10-28-openclaw-vps-functional-136/REPORT.md)),
and match the macOS run at `a52f8bf`. Isolate the host's state with
`OPENCLAW_HOME`, `OPENCLAW_STATE_DIR`, `OPENCLAW_CONFIG_PATH`, and
`--config`.

```bash
cd integrations/openclaw && npm ci && npm run build
npm pack --pack-destination /tmp/rm-plugin
# without --accept-capabilities the pinned host refuses a local package (exit 1)
openclaw plugins install npm-pack:/tmp/rm-plugin/relaymind-openclaw-provider-0.1.0.tgz \
  --force --accept-capabilities
openclaw config set plugins.entries.relaymind.config.baseUrl http://127.0.0.1:8080/v1
# the install leaves the plugin disabled until it is configured
openclaw plugins enable relaymind
openclaw plugins inspect relaymind --runtime --json         # status: loaded
printf "%s\n" "$TOKEN" | openclaw models auth paste-api-key --provider relaymind
openclaw models list --refresh --json                       # runs the catalog hook
openclaw agent exec --config "$OPENCLAW_CONFIG_PATH" --json \
  --model relaymind/relaymind-client-tools "Say hello"
# restart relaymind serve: models list --refresh logs the 401 reconnect line;
# paste the new token the same way, and both list and exec recover
```

### Disable and remove

```bash
openclaw plugins disable relaymind
# without a terminal, uninstall asks for confirmation and exits 1; --force skips it
openclaw plugins uninstall relaymind --force
```

Uninstall removes the plugin's install record, directory, and settings, but
the host keeps three things (macOS run, and the Linux VPS functional check):
`plugins.entries.relaymind: {"enabled": false}` in the config, the auth
profile `relaymind:manual`, and the gateway token in the host state database.
Remove the credential and the entry explicitly:

```bash
openclaw models auth list                                  # shows relaymind:manual
openclaw models auth logout relaymind:manual --yes          # removes the profile and the stored token
openclaw config unset plugins.entries.relaymind
```

After these two commands no file under the isolated host home held the
token (Linux VPS functional check). The host also leaves `config.json.bak*`
files from each config change; they did not hold the token. The full
operator guide is #145.

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
to #136 and #137. The Linux VPS functional check (not a supported-platform
qualification) then ran this package's load, token setup, and text turn on
the real pinned host against the gateway over doubles, with the fix of the
`Unknown model` failure seen on the wire; the harness host scenarios, and
any Windows run, are not_run.

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

### Host tool approval (#137, operator, not run yet)

The `tool-deny` and `tool-approve` scenarios script one call to the mutating
sandbox tool `relaymind_test_write`. Install the test host tools next to the
provider (test only; never on a real workspace), then answer the host's
approval prompt with deny in one run and allow-once in the other:

```bash
openclaw plugins install integrations/openclaw/test/host-tool-plugin --force
python scripts/openclaw_qualification.py host --scenario tool-deny \
  --out docs/evidence/<date>-openclaw-tool-deny-<machine>.json \
  -- openclaw agent exec --config <the relaymind config above> --json \
     --model relaymind/relaymind-client-tools "Write a note with the test write tool"
python scripts/openclaw_qualification.py host --scenario tool-approve \
  --out docs/evidence/<date>-openclaw-tool-approve-<machine>.json \
  -- openclaw agent exec --config <the relaymind config above> --json \
     --model relaymind/relaymind-client-tools "Write a note with the test write tool"
openclaw plugins uninstall relaymind-test-host-tools
```

As above, the host command names follow the pinned documentation and are to
be confirmed on the first run.

The tool writes into `RELAYMIND_TEST_SANDBOX`, an empty directory the
harness creates and passes in the command's environment. The export's
`checks` read the sandbox and the trace: a denial left no line, an approval
exactly one, the gateway executed nothing and proposed the call, and the
host's result reached the next run; `passed` is false for a scenario that
did not complete. How the pinned host answers an approval headlessly
(`agent exec` with a permission mode, or an interactive prompt) is to be
confirmed on the first run; if a plugin tool cannot be gated, the fallback
is the host's own `exec` tool under an allowlist, recorded with the result.
