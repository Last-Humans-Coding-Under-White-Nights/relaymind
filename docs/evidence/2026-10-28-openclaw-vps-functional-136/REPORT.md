# OpenClaw provider package on the real pinned host: Linux VPS functional check (#136)

**Linux VPS functional check (not a supported-platform qualification).**
This is one run of the real pinned OpenClaw host against the real gateway
over scripted model doubles on a Linux x86-64 VPS. It is not live model
evidence (no llama-server, no live model, no external model), not a
qualification of a supported platform, and not a Mac or Windows result.
macOS and Windows rows for this package stay `not_run`.

## Identity and environment

| Item | Value |
| --- | --- |
| Machine | Linux VPS, `Linux 7.0.0-38-generic x86_64` |
| Host | `OpenClaw 2026.10.7 (157d8c2)`, the pinned `openclaw@2026.10.7` from `integrations/openclaw/node_modules` (`node_modules/.bin/openclaw`) |
| Node, npm | v24.22.1, 11.19.1 |
| Python | 3.14.4 (shared venv) |
| Gateway | the real FastAPI app (`ClientToolsChat`) served by `relaymind.api.server.build_server` on 127.0.0.1, over the model doubles of `tests/gateway_helpers.py` (scripted text turns, no external model) |
| Commit tested | `edf87ccdbe290414db9ece2af301550f167ccac5` (package sources; the working tree also held this scaffold and an uncommitted manifest test, and neither is in the tarball) |
| Plugin tarball | `relaymind-openclaw-provider-0.1.0.tgz`, 10 files, sha256 `87caba0ecc6f13ff3eccbdb7f4c0c2986388ccd7e69b471bc70f4f872b0617cb` |
| Date | 28 October 2026 |

Isolation: every host command ran with `OPENCLAW_HOME`, `OPENCLAW_STATE_DIR`
and `OPENCLAW_CONFIG_PATH` pointing into a fresh temp dir. `agent exec`
also had `--config` set to that file. `RELAYMIND_APP_TOKEN` was removed
from the environment, so the stored credential was the only token path.
A recording loopback proxy sat between the host and the gateway
(`scaffold/record_proxy.mjs`, which uses the package's own
`test/host-transport/recorder.mjs`). The plugin's `baseUrl` was the
proxy's loopback URL. The proxy never records `Authorization`.

Reproduce from the repository root (Node on `PATH`, `npm ci` done in
`integrations/openclaw`):

```bash
PYTHONPATH=src python docs/evidence/2026-10-28-openclaw-vps-functional-136/scaffold/vps_functional.py <scratch dir>
```

## Results

| # | Step (the command form that worked) | Result |
| --- | --- | --- |
| 1 | `npm run build`, `npm pack --pack-destination <dir>` | exit 0 |
| 2 | `plugins install npm-pack:<tgz> --force` | exit 1, as expected: `Plugin "relaymind" requires capability consent ... Re-run ... with --accept-capabilities` |
| 3 | `plugins install npm-pack:<tgz> --force --accept-capabilities` | exit 0: `Installed plugin "relaymind" without enabling it because it requires configuration first. Configure it, then run openclaw plugins enable relaymind.` |
| 4 | `config set plugins.entries.relaymind.config.baseUrl http://127.0.0.1:<port>/v1` | exit 0 (warning: the plugin is disabled but has config) |
| 5 | `plugins inspect relaymind --runtime --json` before enable | `status: disabled`, `activationReason: disabled in config` |
| 6 | `plugins enable relaymind` | exit 0: `Enabled plugin "relaymind".` |
| 7 | `plugins inspect relaymind --runtime --json` | `status: loaded`, `enabled: true`, version 0.1.0, providers `["relaymind"]`, `diagnostics: []` |
| 8 | `config set plugins.entries.relaymind.config.executionMode parallel_demo` | exit 1: `Config change declined. No settings were saved.` and `Config validation failed: plugins.entries.relaymind.config.executionMode: invalid config: must be equal to one of the allowed values (allowed: "auto", "local_only", "external_only")`. The host's own validation of the plugin's `configSchema` refused the value before any plugin code ran, so the plugin's own parallel_demo message never showed (Q5) |
| 9 | `models list --refresh --json` with no stored token | no relaymind row, no request to the gateway, and no plugin diagnosis line (only the capability statement). The host did not run the catalog hook without a credential, so the plugin's "no stored token" line did not appear |
| 10 | `printf "%s\n" "$TOKEN" \| models auth paste-api-key --provider relaymind` | exit 0, `Auth profile: relaymind:manual (relaymind/api_key)`, with a warning that no local Gateway could refresh |
| 11 | `models list --json` (no `--refresh`) | `count: 0`: `Gateway is not running. Showing the local cached model catalog. Use --refresh to discover provider models.` |
| 12 | `agent exec --config <cfg> --json --cwd <dir> --model relaymind/relaymind-client-tools "Say hello"`, **before any catalog refresh** | exit 0, `ok: true`, `final: "Hello from RelayMind."`, `provider: relaymind`, `model: relaymind-client-tools`. On the wire: one `POST /v1/chat/completions` and **no** `GET /v1/models` (exchange 0). The cached catalog was empty and the host config had no `models.providers` row, so the host found the model through the plugin's `resolveDynamicModel`. This is the "Unknown model" fix of Q7, now seen on a real host (Linux) |
| 13 | `models list --refresh --json` | relaymind row `relaymind/relaymind-client-tools`, name `RelayMind client-owned tools (auto)`; plugin line `... serves relaymind-client-tools; mode auto is ready; no context window advertised ...; no output limit advertised.` The host shows `contextWindow: 128000` for the row even though the plugin advertised none. That is the host's own default, not a RelayMind limit |
| 14 | `config set plugins.entries.relaymind.config.limits '{"externalContextLimit":128000,"maxCompletionTokens":512}' --strict-json`, then `models list --refresh --json` | exit 0; plugin line `context window 128000 (external limit, mode auto); output limit 512 (chat.max_completion_tokens).` |
| 15 | `agent exec` as in 12 | exit 0, `final: "Hello from RelayMind."`; the request carried `max_completion_tokens: 512` (exchange 5) |
| 16 | Gateway restart (a new gateway with a new token on the same port), then `models list --refresh --json` | no relaymind row; `GET /v1/models` was 401; the plugin logged `RelayMind rejected the stored application token (HTTP 401 unauthenticated). The gateway generates a new application token every time it starts, so a restart makes the stored one stale. Copy the token the running relaymind serve printed and store it with openclaw models auth paste-api-key --provider relaymind.`; the host added `Model discovery authentication was rejected for relaymind.` |
| 17 | `agent exec` with the stale token | exit 1, `ok: false`, host message `Couldn't sign in to the AI service ...`; host log `rawError=401 a valid application token is required`, `reason=auth`, no fallback. `agent exec` shows the host's own message, not the plugin's reconnect line (error mapping is #138) |
| 18 | `paste-api-key` with the new token, then `models list --refresh --json` and `agent exec` | recovered: the row is back, `mode auto is ready`, `final: "Hello again after the restart."` |
| 19 | `plugins disable relaymind` | exit 0 |
| 20 | `plugins uninstall relaymind` (no TTY, stdin closed) | exit 1: `plugins uninstall requires confirmation input. Re-run in an interactive TTY or pass --force.` |
| 21 | `plugins uninstall relaymind --force` | exit 0: `Removed: plugin settings, install record, directory.` |
| 22 | After uninstall (Q12) | the config still had `plugins.entries.relaymind: {"enabled": false}` and `auth.profiles["relaymind:manual"]`; `models auth list` still listed `relaymind:manual [relaymind/api_key]`; the host state DB `state/state/openclaw.sqlite` still held the gateway token bytes (checked by comparison, never printed) |
| 23 | `models auth logout relaymind:manual --yes`, then `config unset plugins.entries.relaymind` | both exit 0: `Removed auth profile: relaymind:manual`, `Removed plugins.entries.relaymind.` Afterwards the config had no `relaymind` profile or entry, and **no file** under the isolated home held the token bytes (config, backups, state DB) |

Gateway trace: three runs completed, each with `run.accepted`,
`route.selected`, `model.started`, `model.finished`, `answer.delta`, and
`run.completed` (`gateway-runs.json`; a payload field over 2,000 JSON
characters, such as the host's prompt, is kept by size only). The
stale-token request never became a run.

## Plugin-supplied model switches on the wire (spec 0.49, section 7.5)

The proxy recorded all four host chat requests (`wire.json`: exchanges 0,
5, and 10 were answered, and exchange 7 was refused with 401 by the stale
token; message text is kept only as role, keys, and length). Each body has
exactly the keys `model`, `messages`, `stream: true`, `tools` (11), and
`tool_choice: "auto"`, plus `max_completion_tokens` when the limit was
configured (exchanges 5, 7, and 10). The probe saw `stream_options` with
`include_usage` on every streamed loopback request from a model that
declared no switches. Here, with this package's model, no request carried
any of these:

- `stream_options` (so the host honored `supportsUsageInStreaming: false`);
- `max_tokens` (it used `max_completion_tokens`, per `maxTokensField`);
- `reasoning_effort`, `store`, `prompt_cache_key`, `parallel_tool_calls`,
  or `response_format`;
- `strict` in any tool function (each function has only `name`,
  `description`, and `parameters`);
- a `developer` message (the roles were `system` and `user`).

Exchange 0 came from the entry that `resolveDynamicModel` returned.
Exchanges 5, 7, and 10 came after a catalog refresh. So the host honored the
switches on both paths of the plugin-supplied model. The gateway accepted
every request under section 7.1 with no gateway change. Every chat
request had the header `X-RelayMind-Execution-Mode: auto`. No
`Idempotency-Key` header was sent (that is #138).

## What this does not show

- Any macOS or Windows result for this package: `not_run`. The macOS
  facts for Q4, Q5, and Q12 come from the earlier Mac run at `a52f8bf`
  (`docs/evidence/2026-10-27-openclaw-mac-qualification/`). This run
  repeats them on Linux only.
- Host tool round trips, approvals, abort (#137, #138), and the harness
  host scenarios: not run here.
- Live model behavior and request size against the local limit: the
  doubles do not count tokens. The measured size result is the macOS
  tokenizer count (0 of 3 requests fit 8192).
- `models auth login` at a prompt, the `RELAYMIND_APP_TOKEN` env var
  path, and the host's own bound on a slow catalog hook: `not_run`.
- Python's `sqlite3` (SQLite 3.46.1) could not open the host state DB after
  uninstall (`database disk image is malformed`; not investigated). The
  token check after uninstall therefore uses a byte comparison over every
  file under the isolated home (`state-checks.json`, `files_holding_token`).

## Files

| File | Content |
| --- | --- |
| `steps.json` | every command (sanitized argv), its exit code and expected exit, seconds, stdout and stderr tails, and the range of proxy exchanges it caused |
| `wire.json` | every request the host sent through the proxy, with the gateway's response; chat bodies are summarized (no prompt text) |
| `gateway-runs.json` | the runs each gateway recorded, with their events |
| `state-checks.json` | the config entry, the auth profiles, and where the token bytes were found after paste, after uninstall, and after logout and unset |
| `scaffold/vps_functional.py`, `scaffold/record_proxy.mjs` | the driver and the proxy that produced these files |

Sanitizing: the gateway tokens became `<token>` (the run fails if one
remains). The scratch dir became `<scratch>`, the checkout `<repo>`, the
home dir `<home>`, the hostname `<hostname>`, and the ports `<port>` and
`<gateway-port>`.
