# OpenClaw provider plugin: real-host qualification on macOS (M9; RM33, AC24)

Recorded 2026-10-27 (UTC) on one Apple Silicon Mac. These are operator runs
against the pinned host, following `integrations/openclaw/README.md` and
`docs/TESTING.md` at the commit under test. Only what was observed is
recorded. A step that could not run is `not_run`, and a failing step quotes
its output. Nothing in the plugin, the gateway, or the test plugins was
changed.

## Summary

| Step | Result |
| --- | --- |
| 0. Base commit | done: tip of `devnull.exile/m9-137-round-trips`; the PR #180 "probe follow-up" comment never appeared |
| 1. Package and pytest checks | **passed**: vitest 157/157 in 10 files; pytest 73 passed, 0 skipped |
| 2. Real host load and token setup | **passed**, with two working-form differences from the README (below) |
| 3. Host capture (text, tool) | **passed**; it matches the probe |
| 4a. Harness `host` default scenario | **failed**: the plugin's model is unknown to `agent exec`; with a config-row workaround the host ran, but `checks.passed` is false |
| 4b. `--scenario tool-deny` | **failed** (`checks.passed:false`): the test host tools never registered on the host |
| 4c. `--scenario tool-approve` | **failed** (`checks.passed:false`): same cause |
| 4d. Abort (SIGINT mid-turn) | **passed**: the host exited 130 and the gateway run ended `cancelled` (`cancel_requested`) |
| 5. Request size vs the 8192 local limit | **measured: 0 of 3 host requests fit (0%)**; 10,513 to 10,577 tokens |
| 5 (optional). Live AC24 round trip | **ran** (config-row workaround): route `local`, actor `gateway` only, outcome `failed` / `needs_external` / `context_limit`, 0 model calls |
| 6. Disable and uninstall both plugins | **passed**, with leftovers (below) |

## Environment

| Item | Value |
| --- | --- |
| macOS | 27.0.1 (build 26A434), arm64 |
| Chip, memory | Apple M1, 8 GiB |
| Node, npm | v26.12.1, 11.20.0 |
| OpenClaw | 2026.10.7 (157d8c2), npm prefix install in a temp dir (from the probe) |
| Python | 3.14.9 (venv, `pip install -e ".[dev]"`, pytest 9.1.1) |
| llama-server | pinned build b11785 (`0.7.0-dev (build 11785, commit 8baabd53b)`; `/props` `build_info` `b11785-8baabd53b`, `n_ctx` 8192) |
| Model | `models/Qwen3-0.6B-Q8_0.gguf`, sha256 matches `models/qwen3-0.6b-q8_0.manifest.json` |
| Commit tested | `a52f8bf93f1e7a6a2ff37d21e259529f62221141` ("State the pinned host's wrapper order for the OpenClaw stream guard (#137)") |
| Plugin tarball | `relaymind-openclaw-provider-0.1.0.tgz`, 12 files, sha256 `4ae3b712c188a1fcb16008001db9fe8105adfb849df1c5b2fe3d2a8216187a9b` |

Isolation: every host command ran with `OPENCLAW_HOME`, `OPENCLAW_STATE_DIR`
and `OPENCLAW_CONFIG_PATH` (and `--config` on `agent exec`) pointing at temp
dirs under `<scratch>`. There were two isolated homes: `qoc` for steps 2, 3 and 5
against the real gateway, and `hoc` for the step 4 harness runs, with no stored
token so that `RELAYMIND_APP_TOKEN` from the harness is the only credential.
`~/.openclaw` does not exist on this machine and was never created. In these
notes, `$OC` is `<scratch>/prefix/bin/openclaw`.

## Step 0. Base commit

`gh pr view 180 --comments` was checked 7 times, every 10 minutes, from
16:02 to 17:02 UTC. No comment matched "probe follow-up", and the PR had no
comments at all. The PR head stayed `a52f8bf` throughout. As instructed, the
current tip was used:

```
git fetch origin && git switch --detach origin/devnull.exile/m9-137-round-trips   # a52f8bf
git switch -c devnull.exile/m9-mac-qual
```

## Step 1. Package checks and pytest: passed

```
cd integrations/openclaw && npm ci && npm run typecheck && npm test && npm run pack:check
RELAYMIND_OPENCLAW_NODE=1 pytest tests/test_openclaw_host_transport.py tests/test_openclaw_plugin.py \
  tests/test_openclaw_round_trips.py tests/test_openclaw_qualification_script.py tests/test_openclaw_request_size.py
```

- `npm ci`: exit 0. npm 11 listed packages whose install scripts it did not
  run (`npm warn install-scripts`, such as esbuild, fsevents, koffi and
  openclaw). Nothing failed because of it.
- `npm run typecheck`: exit 0.
- `npm test`: 10 files, 157 tests passed.
- `npm run pack:check`: "pack check passed: relaymind-openclaw-provider-0.1.0.tgz (12 files)".
- pytest: 73 passed in 94.72 s, none skipped. Node was required.

Logs: `step1/`.

## Step 2. Real host load and token setup: passed

Gateway: the real `relaymind serve --config` from `relaymind.example.toml`.
The copy run is `configs/relaymind.toml`; only `[store] path` and
`[sandbox] work_dir` were moved into `<scratch>`, because the example's
relative paths would write untracked files into the checkout. The local
model is the pinned llama-server on 127.0.0.1:8033. No external model is
configured. Startup printed "Local warm-up finished: routing 1067 ms ...,
skill 801 ms ..." and "Serving on http://127.0.0.1:8080"
(`live/serve1.out`, token redacted).

| Command | Observed |
| --- | --- |
| `npm pack --pack-destination <scratch>/qual/pack` | 12 files |
| `$OC plugins install npm-pack:<tgz> --force` (README form) | **exit 1**: `Plugin "relaymind" requires capability consent. The plugin was not installed. Re-run the same "openclaw plugins install" or "openclaw plugins update" command with --accept-capabilities, ...` |
| `$OC plugins install npm-pack:<tgz> --force --accept-capabilities` (working form) | exit 0: `Installed plugin "relaymind" without enabling it because it requires configuration first. Configure it, then run \`openclaw plugins enable relaymind\`.` |
| `$OC config set plugins.entries.relaymind.config.baseUrl http://127.0.0.1:8080/v1` | exit 0 |
| `$OC plugins enable relaymind` (not in the README) | `Enabled plugin "relaymind".` |
| `$OC plugins inspect relaymind --runtime --json` | `status: loaded`, `enabled: true`, version 0.1.0, providers `["relaymind"]`, diagnostics `[]` (`inspect.json`) |
| `printf "%s\n" "$TOKEN" \| $OC models auth paste-api-key --provider relaymind` | exit 0, `Auth profile: relaymind:manual (relaymind/api_key)` (with a warning that no host Gateway was running to refresh) |
| `$OC models list --json` (README form) | **no relaymind row**: `Gateway is not running. Showing the local cached model catalog. Use --refresh to discover provider models.` |
| `$OC models list --json --refresh` (working form) | `relaymind/relaymind-client-tools`, name "RelayMind client-owned tools (auto)", `contextWindow` 128000, `available: true` |

Catalog log line on refresh: `RelayMind gateway at http://127.0.0.1:8080/v1
serves relaymind-client-tools; mode auto is ready; no context window
advertised (no external limit configured for mode auto); no output limit
advertised.` The list still shows `contextWindow: 128000`. That value comes
from the host, not from the plugin.

**Stale token.** After the gateway was stopped (SIGINT) and started again,
`$OC models list --json --refresh` listed no relaymind row, and stderr carried
the plugin's reconnect line, verbatim:

> RelayMind rejected the stored application token (HTTP 401 unauthenticated). The gateway generates a new application token every time it starts, so a restart makes the stored one stale. Copy the token the running `relaymind serve` printed and store it with `openclaw models auth paste-api-key --provider relaymind`.

The host added: `Model discovery authentication was rejected for relaymind.
Open Models in the Control UI to check sign-in and catalog access, then retry
with --refresh.` After the new token was pasted the same way, the next
refresh listed `relaymind/relaymind-client-tools` again, with "mode auto is
ready" (`cli/list3-stale.*`, `cli/list4-recovered.*`).

**`executionMode: parallel_demo`.** The host's schema refuses it before the
plugin runs, so the plugin's own wording ("parallel_demo is not a mode of
relaymind-client-tools") never shows on this host:

- `$OC config set ... executionMode parallel_demo`: exit 1, `Config change declined. No settings were saved.` and `Config validation failed: plugins.entries.relaymind.config.executionMode: invalid config: must be equal to one of the allowed values (allowed: "auto", "local_only", "external_only")`.
- Written straight into the config file instead: `models list --refresh` logs `relaymind invalid config: executionMode: must be equal to one of the allowed values (allowed: "auto", "local_only", "external_only")`, and the plugin is left out (10 models, no relaymind). `plugins inspect` exits 1 with `OpenClaw config is invalid`.

**Non-loopback `baseUrl`.** `config set ... baseUrl http://192.168.1.10:8080/v1`
is accepted (exit 0, because the manifest schema types it only as a
string). The plugin then refuses it at register:

> RelayMind plugin configuration: baseUrl host 192.168.1.10 is not a loopback address; the gateway serves only localhost, 127.0.0.0/8, or [::1]

The host logs `relaymind failed during register ...: RelayMindConfigError: ...`
and inspect reports `status: error` with that message as its diagnostic
(`inspect-nonloopback.json`). The model list has no relaymind row. The
working config was restored afterwards.

## Step 3. Host capture: passed, matches the probe

`node scripts/capture_host_requests.mjs --port 8089 --out <scratch>/qual/host/{text,tool}.json`
(the tool run added `--tool-call read --tool-arguments '{"path":"notes.txt"}'`),
with the README's capture provider config (`configs/capture.json`), then
`$OC agent exec --config <capture.json> --json [--cwd <dir with notes.txt>] --model relaymind-capture/relaymind-client-tools "<prompt>"`.
The fixtures were written to the evidence folder, not into the repo.

- Text: one request, 40,458 bytes, an 11-tool catalog, a 29,986-char system message, and the fields `model, messages, stream, tools, tool_choice, max_completion_tokens`. Exec `ok:true`.
- Tool: two requests (40,528 and 40,796 bytes). The host ran `read`, and its result went back as `role: tool` with `tool_call_id: "callcapture1"`: the host rewrote the endpoint's id, as in the probe. Exec `ok:true`, 2 assistant turns.

Same shape and sizes as the probe's compat-switch runs. Files: `host/`.

## Step 4. Harness against the real host

### 4a. Default scenario: failed

```
python scripts/openclaw_qualification.py host --timeout 300 --out <export> -- \
  $OC agent exec --config <hoc config> --json --cwd <dir> --model relaymind/relaymind-client-tools "Say hello"
```

The config reads the gateway URL as `baseUrl: "${RELAYMIND_BASE_URL}"` (the
host substitutes `${VAR}` in config), and the token comes from
`RELAYMIND_APP_TOKEN`.

**As documented: `failed`, exit 1, 0 runs** (`exports/host-default.json`):

> Unknown model: relaymind/relaymind-client-tools. Run `openclaw models list --refresh --provider relaymind` to inspect this provider's model choices, then retry with a model supported by your account.

The same error happened against the real gateway right after a successful
`models list --refresh` that listed the model (`live/exec-plugin-catalog-only.*`).
The plugin registers `catalog` and `wrapStreamFn` but no
`resolveDynamicModel`. The probe found that `agent exec` resolves plugin models only
through that hook, and here exec does not use the refreshed catalog. **On
this host, `agent exec` cannot use the plugin's model as packaged.** This
blocks every host run through the plugin.

**Workaround: config only, no code change, recorded as such.** A
`models.providers.relaymind` row in the host config, with the README's
compat switches and the same provider id, so the plugin's auth and
`wrapStreamFn` still apply (`configs/harness-config-row.json`). With it, the
host ran:

| Export | Command | status / exit | runs | `checks.passed` | Why |
| --- | --- | --- | --- | --- | --- |
| `host-default-row.json` | one exec, "Say hello" | completed / 0 | 1 | **false** | Exec `ok:true`, final "Hello from RelayMind.". The scenario scripts text, then an echo call, then text; one exec uses only the first turn, so `proposed: []` |
| `host-default-row-2exec.json` | `sh -c` running two execs ("Say hello", then "Echo lantern with the test echo tool") | completed / 0 | 2 | **false** | The doubles proposed `relaymind_test_echo`, but the host's 11 offered tools were `apply_patch, edit, exec, ls, process, read, sessions_yield, tool_call, tool_describe, tool_search, write`. The gateway ran a repair generation, which used the next scripted text: the host got "The tool said: echo: lantern." with `ok:true`, but there was no `tool.proposed` |
| `host-default-direct-2exec.json` | same, plus `tools.toolSearch: false` (direct schemas) | completed / 0 | 2 | **false** | 41 tools offered, still no `relaymind_test_*` (see 4b) |

`"ok":true` was not success in any of these runs. The checks were right to
fail them.

### 4b, 4c. tool-deny and tool-approve: failed

```
$OC plugins install integrations/openclaw/test/host-tool-plugin --force                        # README form: exit 1, capability consent
$OC plugins install integrations/openclaw/test/host-tool-plugin --force --accept-capabilities  # working form
python scripts/openclaw_qualification.py host --scenario tool-deny|tool-approve --timeout 300 --out <export> -- \
  $OC agent exec --config <cfg> --json --cwd <dir> --model relaymind/relaymind-client-tools "Write a note with the test write tool"
```

Modes: `tools.exec.mode: "deny"` for the deny run and `"full"` for the
approve run (`configs/harness-config-tool-*.json`). The probe found that
headless `ask` fails closed. Both runs also used the config-row workaround
and `tools.toolSearch: false`.

The test host tools plugin loads (`status: loaded`, its
`before_tool_call` hook registered), but **its tools are rejected at
register**. `plugins inspect relaymind-test-host-tools --runtime --json`
reports `tools: []` and two diagnostics:

> plugin must declare contracts.tools before registering agent tools

So `relaymind_test_write` is never offered to the model, and the host's
approval policy for it was never exercised:

| Export | status / exit | Host output (`ok`, final) | checks |
| --- | --- | --- | --- |
| `tool-deny.json` | completed / 0 | `true`, "The host did not allow the write." | `passed:false`; `proposed: []`, `sandbox_lines` 0 of 0, `gateway_tool_executions` 0, `result_reached_next_run:false` |
| `tool-approve.json` | completed / 0 | `true`, "The note was written." | `passed:false`; `proposed: []`, `sandbox_lines` **0 of 1**, `gateway_tool_executions` 0, `result_reached_next_run:false` |

In both runs the doubles' `relaymind_test_write` call was not among the
offered tools. The gateway repaired, and the scripted closing text went back
as the answer. In the approve run, that text claims a write that never
happened. The sandbox and the checks show it did not.

The README's fallback, the host's own `exec` under an allowlist, cannot be
used with these harness scenarios: they script `relaymind_test_write` and
check `note.txt` in the sandbox. It was not run.

### 4d. Abort: passed (scratch scaffold)

The harness's `host` mode has no abort scenario. `scaffold/abort_run.py`
serves the harness's own `abort-buffered` gateway (from
`openclaw_qualification._gateway`, with a gated local generation that never
finishes). It runs the real host
(`$OC agent exec --config <harness-config-row.json> --json ... "Say hello"`)
and sends SIGINT 2 s after the gateway has a run in flight
(`exports/abort.json`):

- State at SIGINT: `local_running`, 13.26 s after launch, which includes host startup.
- Host: exit **130**, 0.14 s after SIGINT, with empty stdout.
- Gateway run: **`cancelled`** (settled by the time the host had exited). `model.finished` error `{"code":"cancelled","message":"model generation interrupted"}`, then `run.cancelled` with `reason: cancel_requested`, 0 external calls.

## Step 5. Request size against the local limit

```
python scripts/openclaw_request_size.py --fixtures <step 3 host fixtures> \
  --manifest models/qwen3-0.6b-q8_0.manifest.json --base-url http://127.0.0.1:8033 --out request-size.json
```

Method: `tokenizer`, using the pinned runtime's chat template and tokenizer.
The local limit is 8192.

| Source | Requests | Fit 8192 | Tokens (min / median / max) |
| --- | --- | --- | --- |
| Step 3 host fixtures | 3 | **0 (0%)** | 10,513 / 10,535 / 10,577 |
| Probe captures, converted to fixtures (`request-size-probe-requests.json`) | 26 captured; 19 rejected, 7 measured | **0 of 7** | 10,500 / 10,508 / 10,548 |

The script refuses the 19 rejected probe requests the way the gateway would.
18 of them come from probe runs that sent `stream_options`:
`unsupported_parameter: stream_options is not supported by this gateway`.
The 19th, `b2-readfile-compat` turn 2, failed with `invalid_request:
messages[2].content must be a string or an array of text parts` (the
probe's b2 run, which used the brief's full compat set).
In both sets, `local_only` gives `needs_external` for every request, and
`auto` would need the external model for every one. The smallest request
is about 2,300 tokens over the limit, before any completion budget.

**Optional live AC24 round trip: ran.** Real `relaymind serve`, the pinned
local model, `auto` mode, and `$OC agent exec ... "Say hello"`, which needed
the config-row workaround from 4a (`live/exec-config-row.*`):

- Host: exit 1, `LLM request rejected: the task cannot complete under the effective execution mode` (raw `422`).
- `/relay/v1/runs/run_88306e1afed84dd09ea317bb29d611c7` (`live/gateway-run.json`, `live/gateway-run-events.txt`): requested and effective mode `auto`; `route.selected` route `local`, mechanism `client_owned`; every event's actor is `gateway`; state `failed`, `error_code: needs_external`, `reason_code: context_limit`. Detail: **"the request needs 10510 (tokenizer); the local limit is 8192"**. `model_calls: 0`, `external_calls: 0`, active 45 ms.

The gateway's own count (10,510) agrees with the offline measure. With no
external model configured, a real OpenClaw turn cannot run on this setup.

## Step 6. Disable and uninstall: passed, with leftovers

```
$OC plugins disable relaymind-test-host-tools && $OC plugins disable relaymind
$OC plugins uninstall <id>            # README form: exit 1, "plugins uninstall requires confirmation input. Re-run in an interactive TTY or pass --force."
$OC plugins uninstall <id> --force    # working form: "Uninstalled plugin ... Removed: plugin settings, install record, directory."
```

This was done in both homes. `plugins list` then shows no relaymind or
test-tools row, and no install dir remains on disk. **Left behind:**

- `plugins.entries.relaymind: {"enabled": false}` and `plugins.entries.relaymind-test-host-tools: {"enabled": false}` in the config file.
- `auth.profiles["relaymind:manual"]` (provider and mode) in the config.
- In the host state DB (`state/openclaw.sqlite`, table `config_machine_state`, key `authProfiles.store`), the profile with its key. The stored key is the current gateway token, 43 chars (checked by comparison, never printed).
- `config.json.bak*` files that the host wrote on every config change.

This matches the probe, except that this time no older install-generation
dirs were left (only one install per home).

## Notes on the README and TESTING.md commands

| README / TESTING.md | What the host accepted |
| --- | --- |
| `plugins install npm-pack:<tgz> --force` | needs `--accept-capabilities` (same for the test host tools) |
| (no enable step) | the install leaves the plugin disabled until it is configured; `plugins enable relaymind` is needed |
| `models list --json \| grep relaymind` | needs `--refresh` when no host Gateway is running |
| `agent exec ... --model relaymind/relaymind-client-tools` | `Unknown model` unless the host config also declares a `models.providers.relaymind` row |
| test host tools: allow-once / deny at the host prompt | the tools never register (`contracts.tools` missing), so no prompt can happen |
| `plugins uninstall relaymind` | needs `--force` when there is no TTY |
| config "whose relaymind provider reads RELAYMIND_BASE_URL" | `baseUrl: "${RELAYMIND_BASE_URL}"`; the host substitutes it |

## Files

| Path | Content |
| --- | --- |
| `step1/` | npm and pytest logs |
| `inspect.json`, `inspect-nonloopback.json`, `inspect-hosttools.json` | `plugins inspect --runtime --json` output |
| `cli/` | every host CLI transcript from steps 2, 4 and 6 |
| `configs/` | the host configs used (temp homes), the capture config, and the gateway TOML |
| `host/` | step 3 fixtures (`text.json`, `tool.json`) with the exec envelopes |
| `exports/` | the harness exports (`host-default*.json`, `tool-deny.json`, `tool-approve.json`), their console output, and `abort.json` |
| `request-size.json`, `request-size-probe-requests.json` | the size reports |
| `live/` | the live AC24 run: exec output, the gateway run view and events, and the serve stdout |
| `scaffold/` | `abort_run.py` (scratch only, not repo code) and the sanitizer |

Sanitized: gateway tokens and Authorization values are `<redacted>`, the
home path is `<home>`, temp paths are `<scratch>` and `<os-tmpdir>`, host names
(including JSON-escaped forms) are `<host>`, and the user name is `<user>`.
The leak scan, over all three tokens, the host names, the user name, the
email, and the home and temp paths, found 0 hits.
