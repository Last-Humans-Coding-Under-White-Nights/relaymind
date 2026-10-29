# OpenClaw host probe on macOS (M9 qualification)

Date: 2026-10-27. Scope: M9 (RM33, AC24; issues #135–#139, #145).
This is evidence from a real OpenClaw host, collected for the plugin work
on a Linux VPS that has no OpenClaw host. It does not implement the plugin.

Rules used: each answer below is `observed` (with the run that shows it),
`docs` (read from the installed package docs, not exercised), or `not_run`
(with the reason). Paths are sanitized: `<scratch>` is a disposable session
temp dir, `<os-tmpdir>` is the macOS per-user temp dir, `<host>` is the
machine name, and every token or `Authorization` value is `<redacted>`.

## 1. Environment

| Item | Value |
|------|-------|
| `sw_vers` | macOS 27.0.1, build 26A434 |
| `uname -m` | arm64 |
| `node --version` | v26.12.1 (Homebrew; meets `>=26.12.1`, no nvm needed) |
| `npm --version` | 11.20.0 |
| `openclaw --version` | `OpenClaw 2026.10.7 (157d8c2)` |

Install: `npm install -g --prefix <scratch>/prefix openclaw@2026.10.7`
(temp prefix, not the global one). npm 11 did **not** run the package
install scripts, including openclaw's
`postinstall: node scripts/postinstall-bundled-plugins.mjs`:

```
npm warn install-scripts   openclaw@2026.10.7 (preinstall: node scripts/preinstall-package-manager-warning.mjs; postinstall: node scripts/postinstall-bundled-plugins.mjs)
```

Nothing below failed because of this, but a plugin test on a host where
that script ran may differ.

### Isolation

From `docs/help/environment.md` in the installed package:

- `OPENCLAW_STATE_DIR` "Override the state directory (default `~/.openclaw`)."
- `OPENCLAW_CONFIG_PATH` "Override the config file path (default `~/.openclaw/openclaw.json`)."
- `OPENCLAW_HOME` sets the home used for derived paths.

Every command ran with all three set to `<scratch>/oc/...`, and every
`agent exec` also had `--config <scratch>/oc/<file>.json`. No `~/.openclaw`
existed before or after. Two side effects landed outside these dirs:

- `agent exec` without `--state-dir` creates its own run state in
  `<os-tmpdir>/openclaw-agent-exec-XXXXXX/` (documented, "isolated temporary state").
- The host wrote its file log to `/tmp/openclaw/openclaw-2026-10-27.log`
  (the documented default, `docs/logging.md`). `OPENCLAW_STATE_DIR` does
  not move it. The file was created by the first probe command and removed
  at the end.

## 2. Commands actually run

```bash
export OPENCLAW_HOME=<scratch>/oc/home OPENCLAW_STATE_DIR=<scratch>/oc/state
export OPENCLAW_CONFIG_PATH=<scratch>/oc/<config>.json
node probe-src/server/capture-server.mjs <scratch>/captures        # 127.0.0.1:8089

openclaw config validate
openclaw agent exec "Say hello" --model relaymind-capture/relaymind-client-tools --json --config <cfg> --cwd <dir>
openclaw agent exec "Read notes.txt and report its first line." --model relaymind-capture/relaymind-client-tools --json --config <cfg> --cwd <dir>

openclaw plugins init probe --type provider --directory <scratch>/plugin-init/probe --name Probe   # scaffold, read only
npm pack                                                             # in probe-src/plugin
openclaw plugins install npm-pack:<scratch>/openclaw-plugin-probe-0.1.0.tgz --force
openclaw plugins install npm-pack:<scratch>/openclaw-plugin-probe-0.1.0.tgz --force --accept-capabilities
openclaw plugins inspect probe --runtime --json
openclaw models auth login --provider probe --method api-key         # needs a TTY, see B5
openclaw models auth paste-api-key --provider probe                  # token on stdin
openclaw models auth list
openclaw models list --refresh --provider probe
openclaw agent exec "Say hello" --model probe/relaymind-client-tools --json --config <cfg> --cwd <dir>
openclaw plugins disable probe
openclaw plugins list
openclaw plugins uninstall probe --force
```

The subcommand at this version is `openclaw agent exec [message]`
("Run one isolated headless embedded agent turn"). It has `--json`,
`--model`, `--config`, `--cwd`, `--state-dir`, `--timeout`, `--isolated`,
`--auth-env-only`. `openclaw agent exec --help` was checked first.

Config for Probe A (`configs/config-a.json`):

```json
{ "models": { "providers": { "relaymind-capture": {
  "baseUrl": "http://127.0.0.1:8089/v1", "api": "openai-completions", "apiKey": "<redacted>",
  "models": [ { "id": "relaymind-client-tools", "name": "relaymind-client-tools", "reasoning": false, "input": ["text"] } ] } } } }
```

**Observed:** `name` is required. The model object from the brief, without
`name`, failed validation:

```
Config needs correction: <scratch>/oc/config-a.json
  - models.providers.relaymind-capture.models[0].name: Invalid input: expected string, received undefined
```

## 3. Probe A: what the host sends

### Runs

| Run dir (`requests/`) | Config | Scenario | Result |
|---|---|---|---|
| `a1-hello-nocompat` | no compat | hello, SSE | ok, final `hello from probe` |
| `a2-readfile-nocompat` | no compat | read tool, SSE | ok, 1 `read` call, final has `probe line one` |
| `b1-hello-compat` | brief compat, all 10 keys | hello, SSE | ok |
| `b2-readfile-compat` | brief compat, all 10 keys | read tool, SSE | ok, **but tool linkage stripped** (see A.3) |
| `c2-readfile-compat-base7` | the 7 base compat keys only | read tool, SSE | ok, linkage intact |
| `d1-hello-nostream` | base 7 + `params.streaming: false` | hello, JSON | ok |
| `d2-readfile-nostream` | base 7 + `params.streaming: false` | read tool, JSON | ok |

Every run exited 0 with `"ok": true, "status": "ok"`. The exec JSON
envelopes are in `runs/out-*.json`, and stderr is in `runs/err-*.txt`.

**Compat schema:** all ten keys in the brief are accepted by
`openclaw config validate`, including `supportsJsonSchemaResponseFormat`,
`requiresAssistantAfterToolResult`, `requiresToolResultName`, and
`strictMessageKeys`. The schema does reject unknown keys. Adding a bogus
key gives:

```
models.providers.relaymind-capture.models[0].compat: Unrecognized key: "bogusProbeKey"
```

### A.1 Top-level body keys

The same set appears in every chat request, apart from `stream_options`:

| Key | Type | Value seen |
|-----|------|-----------|
| `model` | string | `relaymind-client-tools` |
| `messages` | array | `system`, `user`, then `assistant`/`tool` on turn 2 |
| `stream` | boolean | `true` by default; `false` with `params.streaming: false` |
| `stream_options` | object | `{"include_usage": true}`: **present with no compat** (a1, a2) and with the plugin provider (p1); **absent** with `supportsUsageInStreaming: false` (b*, c2-readfile, d*) |
| `tools` | array | 11 function tools |
| `tool_choice` | string | `auto` |
| `max_completion_tokens` | number | `8192` (config model, no `maxTokens` set); `4096` for the plugin model (`maxTokens: 4096`). Sent as `max_completion_tokens` **even with no compat** |

The docs explain the `stream_options` default
(`docs/gateway/config-tools/custom-providers.md`): "Unknown remote
`openai-completions` endpoints default to `supportsUsageInStreaming: false`
... Loopback endpoints and the bundled llama.cpp, LM Studio, Ollama, and
vLLM providers already request or collect usage." So a loopback baseUrl
gets `stream_options.include_usage: true` unless compat turns it off.

Absent in **every** captured request, with or without compat: `strict`
(top level and per-tool `function.strict`), `reasoning_effort`, `store`,
`prompt_cache_key`, `parallel_tool_calls`, `response_format`, `temperature`.
No `developer` role was ever sent. There is exactly one `system` message.

### A.2 Messages

- **Tool message `name`:** never present (`requiresToolResultName: false`
  and no compat both give the same result).
- **Inserted assistant after tool results:** none. The turn-2 order is
  always `system > user > assistant(tool_calls) > tool`. Nothing comes after
  the tool message.
- **Assistant replay of the tool call:** `{"role":"assistant","content":null,"tool_calls":[...]}`.
  This is the host's replay of the model's own turn, not an inserted message.
- **Extra message keys:** none beyond `role`, `content`, `tool_calls`,
  `tool_call_id` in any run.
- **Tool call id rewritten (observed):** the server sent
  `"id": "call_probe_1"`, and the host replayed it as `"id": "callprobe1"`
  in both the assistant `tool_calls` and the tool message `tool_call_id`
  (a2, c2-readfile, d2). The host strips `_` from tool call ids. A server
  that matches tool results to its own issued ids must not depend on the
  exact id string it emitted.
- **User message:** a plain string. The host prefixes a timestamp and
  appends a runtime line:
  `"[Tue 2026-10-27 23:14 GMT+8] Say hello\n\nRuntime: agent=main | session=agent:main:explicit:<uuid> | host=<host> | os=macOS 27.0.1 (arm64) | node=v26.12.1 | ... | model=relaymind-capture/relaymind-client-tools | default_model=relaymind-capture/relaymind-client-tools | shell=zsh"`.
  The user text RelayMind receives is not the raw prompt.

### A.3 `strictMessageKeys: true` breaks tool calling (observed)

With the full compat set from the brief (b2), turn 2 was:

```json
{ "role": "assistant", "content": null },
{ "role": "tool", "content": "probe line one\nprobe line two\n" }
```

`tool_calls` was removed from the assistant message and `tool_call_id`
from the tool message. To isolate the cause, c2-readfile ran with the 7
base keys only, without the three optional ones, and the linkage was intact
(`tool_calls[0].id == tool_call_id == "callprobe1"`). The capture server
answered anyway because it keys on `role: "tool"`. A real server that
validates tool linkage would get an orphan tool message.
**Recommendation: do not set `strictMessageKeys: true`.**
`requiresAssistantAfterToolResult: false` and `requiresToolResultName: false`
produced the same output as leaving them out.

### A.4 Responses without `usage`

- **SSE without `usage` and without honoring `stream_options`** (a1, a2,
  b1, b2, c2-readfile): accepted silently. No warning on stderr and none
  in the file log. The envelope `usage` is
  `{"cacheRead":0,"cacheWrite":0,"cost":{"total":0}}`.
- **JSON (non-stream) without `usage`** (d1, d2): accepted silently. Same
  envelope `usage`. The host only sends `stream: false` with model
  `params: { "streaming": false }` (documented in
  `docs/providers/ollama/advanced.md` for `openai-completions`). Without
  it, every request had `stream: true`.
- The documented once-per-model warning `<provider>/<model> returned no token usage`
  never appeared. The docs scope it to "custom remote endpoints with
  streaming usage disabled", and this baseUrl is loopback.

The only warning in all runs is unrelated to the provider. It appears on
every exec run, including successful ones:

```
[trajectory] retention deferred until the next append: WorkerTaskError: Session history database read was revoked
```

### A.5 Request size

| Run | Body bytes (turn 1 / turn 2) | System prompt chars | Tools |
|-----|------|------|------|
| a1 | 40,462 | 29,942 | 11 |
| a2 | 40,494 / 40,738 | 29,942 | 11 |
| b1 | 40,422 | 29,942 | 11 |
| b2 | 40,454 / 40,551 | 29,942 | 11 |
| c2-readfile-compat-base7 | 40,454 / 40,698 | 29,942 | 11 |
| d1 | 40,423 | 29,942 | 11 |
| d2 | 40,455 / 40,699 | 29,942 | 11 |
| p1 (plugin provider) | 33,672 | 23,192 | 11 |

Tools offered (same 11 in all Probe A runs):
`apply_patch, edit, exec, ls, process, read, sessions_yield, tool_call, tool_describe, tool_search, write`.
stderr says `tool-search: cataloged 40 tools behind compact prompt surface`:
40 tools are cataloged behind `tool_search` / `tool_describe` / `tool_call`.
The built-in read tool is named `read` with arguments `{"path": "..."}`.

The plugin run's system prompt is ~6.7k chars shorter. The one config
difference is that the plugin model declares `contextWindow: 32768,
maxTokens: 4096`, while the config model leaves both unset. The cause was
not investigated.

### A.6 Loopback baseUrl / SSRF

**Observed: never refused.** Every request reached `127.0.0.1:8089`, for
both the config provider and the plugin provider (`/v1/models` and
`/v1/chat/completions`). No SSRF, allowlist, or private-network error was
seen in stderr or in the file log. The docs agree: "Configuring a
custom/local provider `baseUrl` is also the narrow network trust decision
... OpenClaw allows that exact `scheme://host:port` origin through the
guarded fetch path" (`custom-providers.md`).

### A.7 Headers on the config provider

`Authorization: Bearer <redacted>` (length 18, the configured key),
`user-agent: OpenAI/JS 7.27.0`, `x-stainless-*` (OpenAI JS SDK),
`accept: application/json`, `traceparent`, `sec-fetch-mode: cors`. Every
chat request in every run had the same header set. The plugin run added
only the three headers from B2/B3.

## 4. Probe B: plugin seams

Plugin source: `probe-src/plugin/` (plain ESM, no build step; it imports
`openclaw/plugin-sdk/plugin-entry` and `openclaw/plugin-sdk/provider-auth`).
It registers provider `probe` with an api-key auth method (env var
`PROBE_TOKEN`), `catalog`, `staticCatalog`, `resolveDynamicModel`,
`wrapStreamFn`, and `resolveTransportTurnState`. Each hook logs what it
receives to `plugin-hooks.jsonl`. The SDK subpaths came from
`package.json` `exports` (347 entries); the types came from
`dist/config-contracts-*.d.ts`.

### B1. Pack, install, inspect: observed

- The first install, without `--accept-capabilities`, failed with exit 1:

  ```
  Plugin "probe" requires capability consent. The plugin was not installed. Re-run the same "openclaw plugins install" or "openclaw plugins update" command with --accept-capabilities, keeping its source and other options.
  ```

- With `--accept-capabilities` it succeeded. Output:

  ```
  WARNING - Installing plugin from local npm-pack archive: npm-pack:<scratch>/openclaw-plugin-probe-0.1.0.tgz
  This source is outside ClawHub review and trust metadata. ...
  Linked peerDependency "openclaw" -> <scratch>/prefix/lib/node_modules/openclaw
  Plugin manifest id "probe" differs from npm package name "openclaw-plugin-probe"; using manifest id as the config key.
  Installed plugin: probe
  ```

- `openclaw plugins inspect probe --runtime --json`: exit 0, with
  `"status": "loaded"`, `"providerIds": ["probe"]`,
  `"capabilities": [{"kind": "text-inference", "ids": ["probe"]}]`,
  `"diagnostics": []`. Saved as `inspect-0.1.0.json` (first build) and
  `inspect.json` (final 0.1.2 build).

### Making the plugin model resolvable in `agent exec`: observed

A plugin model is **not** found by `agent exec` from the `catalog` hook
alone:

```
Unknown model: probe/relaymind-client-tools. Run `openclaw models list --refresh --provider probe` to inspect this provider's model choices, then retry with a model supported by your account.
```

What was tried, in order:

1. `catalog.run` only: Unknown model. `catalog.run` was never called by exec.
2. `openclaw models list --refresh --provider probe`, then exec: still
   Unknown model. The refresh ran `catalog.run` and listed
   `probe/relaymind-client-tools`.
3. The same with `--state-dir $OPENCLAW_STATE_DIR`: still Unknown model.
4. Adding `staticCatalog`: still Unknown model. `staticCatalog.run` was not called.
5. Adding `resolveDynamicModel`: **works**. Exec called
   `resolveDynamicModel` (twice) with ctx keys
   `agentDir, authProfileId, authProfileMode, config, modelId, modelRegistry, provider, providerConfig, workspaceDir`.

So the RelayMind plugin should implement `resolveDynamicModel`, or the
manifest `modelCatalog`, which was not tried. A `catalog` hook alone is not
enough for headless `agent exec`.

### B2. `wrapStreamFn` headers: observed, they reach the wire

`wrapStreamFn` was called once in the one-turn plugin run (factory once,
stream call once). ctx keys:
`agentDir, agentId, auth, config, extraParams, model, modelId, nativeWebSearchAllowedByToolPolicy, provider, streamFn, thinkingLevel, workspaceDir`
(`sourceApi` was undefined). The wrapper spreads
`model.headers` (`{...m.headers, "X-RelayMind-Execution-Mode": "auto", "Idempotency-Key": "test-123"}`),
the same pattern the bundled clawrouter plugin uses. It returns the
wrapper with `preservesGenericCompatibility: true`. Captured request
(`requests/p1-plugin-hello-storedkey/001-*.json`):

```
"idempotency-key": "test-123",
"x-relaymind-execution-mode": "auto",
"x-probe-turn-state": "8ca64aeb-2e21-4fe1-99d4-3609a188b93d:1",
```

The stream call's `options` carried `signal` (an AbortSignal),
`requestId: "<runId>:model:1"`, `sessionId`, and
`headers: {"traceparent": ...}`. `model.headers` was `null` on entry.

### B3. `resolveTransportTurnState`: observed, called

It was called for the single model request of the plugin run, on the
openai-completions transport, with:

```json
{"provider":"probe","modelId":"relaymind-client-tools","sessionId":"<runId>","turnId":"8ca64aeb-...","attempt":1,"transport":"stream","modelApi":"openai-completions"}
```

The returned `headers` reached the HTTP request (`x-probe-turn-state`
above). Only one attempt was observed, so whether `turnId` stays stable
across a retry was not tested (`not_run`: no retry was triggered).

### B4. `catalog` fetch with the stored credential, abort signal, timeout

- **Observed:** during `openclaw models list --refresh --provider probe`,
  `catalog.run` got ctx keys
  `agentDir, config, env, providerIds, resolveProviderApiKey, resolveProviderAuth, workspaceDir`.
  It had **no `signal`** (`hasSignal: false`).
  `resolveProviderApiKey("probe")` returned the stored key (length 43,
  `profileId: "probe:manual"`, `mode: "api_key"`). The hook's own `fetch`
  of `GET /v1/models` with `Authorization: Bearer <stored key>` returned
  200 in 16 ms. The capture confirms that the Bearer token is the stored
  43-char token (`requests/p0-models-refresh/`).
- **Docs:** `docs/plugins/sdk-provider-plugins/model-catalogs.md` gives
  `signal` to `staticCatalog.run` and `prepareSyntheticAuth` "during
  model-runtime preparation". The type `ProviderCatalogContext.signal?`
  is optional. The SDK's shared live-catalog helper has "one 5-second
  timeout budget across pagination, a 4 MiB response limit per page, and
  a 50-page limit", and its failures fall back to the static seed.
- **not_run:** whether the host puts a timeout on a hand-written `fetch`
  inside `catalog.run`. No slow `/v1/models` was tested. In the observed
  path there was no signal to pass, so the plugin must set its own timeout
  (for example `AbortSignal.timeout`).
- The catalog hook (`catalog.run`) was **not** called during
  `agent exec` (see "Making the plugin model resolvable" above).

### B5. A 43-char random URL-safe token as the api key: observed, accepted

Token: 32 random bytes, base64url, 43 chars.

- `openclaw models auth login --provider probe --method api-key` without
  a TTY:
  `models auth login requires an interactive TTY. In automation, use openclaw models auth paste-token --provider <provider> when token auth is available.`
  Under `expect` the masked prompt `Enter the probe token` appeared, but
  the scripted keystrokes were not accepted, and the attempt was abandoned.
  Login through the TTY prompt counts as `not_run`.
- `openclaw models auth paste-api-key --provider probe` with the token on
  stdin: exit 0, no shape validation.

  ```
  Warning: Model auth changes were saved, but the local Gateway could not refresh them. Run `openclaw gateway restart` to apply the saved changes.
  API key saved, but the Gateway has not confirmed applying the provider settings. Run `openclaw gateway restart` to apply them.
  Updated config: <scratch>/oc/config-p.json
  Auth profile: probe:manual (probe/api_key)
  ```

  The key is stored in `<state>/state/openclaw.sqlite`. The config only
  gets `auth.profiles["probe:manual"] = {provider, mode}`.
- The stored token was used end to end with `PROBE_TOKEN` unset: it was
  the Bearer on `/v1/models` and on `/v1/chat/completions`
  (`authorization__token_length: 43`, matching the stored token).
- `not_run`: the `PROBE_TOKEN` env-var path (only the stored profile was tested).

### B6. Disable and uninstall: observed

```
openclaw plugins disable probe        -> Disabled plugin "probe". Saved for the next Gateway start.
openclaw plugins list                 -> probe ... disabled
agent exec --model probe/...          -> Unknown model: probe/relaymind-client-tools ...   (exit 1)
openclaw plugins uninstall probe --force
  -> Will remove: plugin settings, install record, directory: <scratch>/oc/state/npm/projects/openclaw-plugin-probe__openclaw-generation__g-96471dd94c6116df
  -> Uninstalled plugin "probe". Removed: plugin settings, install record, directory.
openclaw plugins list | grep -c probe -> 0
```

Left behind after uninstall (observed):

- `plugins.entries.probe: {"enabled": false}` was still in the config file.
- `auth.profiles["probe:manual"]` was still in the config, with its key
  still in the state DB.
- Two install dirs from the earlier `--force` reinstalls,
  `npm/projects/openclaw-plugin-probe` and `...__g-ac8d916522629bf8`, were
  still on disk. Only the current generation was removed.

## 5. Probe C: host policy facts

### C1. Approval for a plugin-registered tool: docs

`docs/plugins/plugin-permission-requests.md`: a plugin's
`api.on("before_tool_call", ...)` hook can return
`{ requireApproval: { title, description, severity, allowedDecisions, timeoutMs, onResolution } }`
for **any** tool name, including its own tools. Outcomes table: `deny`,
timeout, cancellation, and "No approval route" all block the call ("The
call is blocked because no connected approval surface can resolve it.").
`timeoutMs` defaults to 120000 and is capped at 600000. Not exercised
with a plugin tool (`not_run`: time).

### C2. Headless `agent exec` with deny / ask / allow: observed

`agent exec` defaults (docs/cli/agent.md): "the agent sandbox is off, the
`coding` tool profile is selected, filesystem tools are restricted to
`--cwd`, and exec runs under the full execution policy a headless turn
needs." Config values override these defaults.

| Run | Policy | Tool call | Result |
|-----|--------|-----------|--------|
| `c1-write-allow` | default | `write {path: probe-out.txt}` | **allowed**: file created, tool result `Successfully wrote 17 bytes to <scratch>/work/c1-write-allow/probe-out.txt` |
| `c2-write-deny` | `tools.deny: ["write"]` | n/a | **denied by omission**: `write` is not in the offered `tools` (10 tools); no file |
| `c3-exec-deny` | `tools.exec.mode: "deny"` | `exec echo probe > probe-exec.txt` | **denied**: `{"status":"error","tool":"exec","error":"exec denied: host=gateway security=deny"}`; no file |
| `c4-exec-ask` | `tools.exec.mode: "ask"` | `exec echo probe > probe-exec.txt` | **denied**: `Exec denied (SYSTEM_RUN_DENIED: approval cannot safely bind this command): echo probe > probe-exec.txt` |
| `c6-exec-ask-touch` | `tools.exec.mode: "ask"` | `exec touch probe-exec.txt` | **denied (fails closed)**: `Exec approval registration failed: GatewayTransportError: Gateway not reachable at ws://127.0.0.1:18789 (ECONNREFUSED).` No hang; the run took ~10.7 s, like the others |
| `c5-exec-full`, `c7-exec-full-touch` | `tools.exec.mode: "full"` | both commands | **allowed**: file created, result `(no output)` |

In every denied case the denial goes back to the model as a tool result.
The run still ends with `exit 0`, `"ok": true, "status": "ok"`. Only
`toolSummary.failures: 1` and `unresolvedError: {"toolName":"exec"}` show
it. Headless `ask` needs a Gateway to register the approval. Without one
it fails closed rather than prompting.

### C3. SIGINT on a running `agent exec`: observed, the HTTP request is aborted

Run `s1-sigint`: the server answered with an SSE role chunk, then held the
stream open with keep-alives. `kill -INT <agent exec pid>` at 15:33:05.879Z:

```
15:33:05.809Z request / slow_hold_start
15:33:06.031Z client_closed_connection (writableEnded: false)
15:33:06.031Z socket_closed
```

The connection closed ~150 ms after SIGINT. Exit code 130, empty stdout
(no JSON envelope), and the child process exited too. The signal went to
the top process only; the child was gone afterwards.

### C4. Windows: docs only

`docs/platforms/windows.md` (summary: "Windows support: Windows Hub, native
CLI and Gateway, WSL2 gateway setup, node mode, and troubleshooting"):
"OpenClaw ships a native **Windows Hub** companion app plus Windows CLI
support. ... Use the PowerShell installer for the CLI/Gateway directly. Use
WSL2 for the most Linux-compatible Gateway runtime." The section "Native
Windows CLI and Gateway" installs with
`iwr -useb https://openclaw.ai/install.ps1 | iex`. So 2026.10.7 documents
native Windows, with WSL2 as an option. `not_run` on Windows: no Windows
host was available.

## 6. Files

| Path | Content |
|------|---------|
| `requests/<run>/NNN-*.json` | every captured request: method, path, headers (Authorization redacted, token length kept), body bytes, full JSON body |
| `requests/<run>/events.jsonl` | server-side events: reply plan, client close |
| `captures-summary.json` | per-request analysis (`probe-src/server/analyze.mjs`) |
| `runs/out-*.json`, `runs/err-*.txt` | `agent exec --json` envelope and stderr per run |
| `cli/` | install / auth / refresh / disable / uninstall transcripts |
| `configs/` | every temp host config used (apiKey redacted) |
| `inspect.json`, `inspect-0.1.0.json` | `plugins inspect probe --runtime --json` |
| `plugin-hooks.jsonl` | hook log of the final plugin run (p1). The `catalog.run` lines from the refresh were overwritten before the final run; they are quoted in B4 from the console |
| `probe-src/` | capture server, analyzer, probe plugin, sanitizer |

Run names: `c2-readfile-compat-base7` (Probe A) and `c2-write-deny`
(Probe C) are different runs.
