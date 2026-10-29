# OpenClaw provider plugin: macOS qualification at the M9 tip (#135 to #138, #145; RM33, AC24)

**macOS 27.0.1 arm64, Apple M1 (user's machine), Node v26.12.1.** Recorded
2026-10-29 on the user's Mac, a target platform of spec section 1. Four runs
of the real pinned OpenClaw host at one clean commit (section 12.5). Three
of them ran against the real gateway over scripted model doubles: the
install check of the release artifact, the #137 harness host scenarios, and
the #138 `abort` and `external-failed` scenarios. They are platform
qualification runs over doubles, **not live model evidence**. The fourth ran
against the real `relaymind serve` with the pinned local model and no
external model. It is live evidence for one thing only: at the 8192 local
context an OpenClaw turn through the plugin as packaged ends
`needs_external` / `context_limit` before any model call. It completes no
task. Only what the exports show is recorded here.

## Summary

| Step | Result |
| --- | --- |
| 1. Install check (`--target-platform`) | **passed**: 15 of 15 recorded steps, 83 s; a text and a host `read` round trip; the gateway executed nothing |
| 2a. `text-tool` | **passed** |
| 2b. `tool-deny` | **passed** (0 of 0 sandbox lines) |
| 2c. `tool-approve` | **failed**, correctly: 0 of 1 sandbox lines; headless `agent exec` has no route for a plugin approval |
| 2d. `exec-deny` | **passed** (0 of 0 sandbox lines) |
| 2e. `exec-full` | **passed** (1 of 1 sandbox line) |
| 3a. `abort` | **passed**: host exit 130, the run `cancelled`, no answer text |
| 3b. `external-failed` | **passed**: host exit 1, 8 runs all `failed` / `external_failed`, the host's own error message |
| 4. Live turn, `auto` | ran: host exit 1, `needs_external` / `context_limit`, 0 model calls ("the request needs 9384 (tokenizer); the local limit is 8192") |
| 4. Live turn, `local_only` | ran: host exit 1, `needs_external` / `context_limit`, 0 model calls ("the request needs 9389 (tokenizer); the local limit is 8192") |

## Identity and environment

| Item | Value |
| --- | --- |
| Label | macOS 27.0.1 arm64, Apple M1 (user's machine), Node v26.12.1 |
| Machine | Apple M1, 8 GiB; macOS 27.0.1 (26A434) arm64 (`sw_vers`); every export's `versions.os` is "Darwin 27.0.0", machine "arm64" |
| Host | `OpenClaw 2026.10.7 (157d8c2)`, the package's devDependency, run as `node integrations/openclaw/node_modules/openclaw/openclaw.mjs` |
| Node, npm | v26.12.1 (inside the host's engine range `>=26.12.1`), 11.20.0 |
| Python | 3.14.9 (venv) |
| Commit tested | `6fc09544ec8132062bd998fa2e6c74fd38744efb`, clean: `versions.relaymind_revision.dirty` is false in every export |
| Artifact | `relaymind-openclaw-provider-0.1.0.tgz`, 43,408 bytes, sha256 `16d064d521fa780b8422b32de7292df2e7f36d9ebcb9b582411a6fd2213e87ac`, built at that commit with `npm run pack:release` (`install-check/release.json`, `install-check/SHA256SUMS`); the round trip and cancel drivers checked the same digest before they installed it, and the live export names it |
| Install export | `install-check/install-check.json`, sha256 `c18ae2a165dfddd9776ad4ea78f658b0a135e96705472432aa96aae7bdef0a36` |
| Live runtime (step 4) | llama-server `b11785-8baabd53b` on 127.0.0.1:8033, `models/Qwen3-0.6B-Q8_0.gguf`, `n_ctx` 8192 (`live/llama-props.json`) |
| Date | 29 October 2026 |

The run outputs were kept out of `git status` with `.git/info/exclude` while
the later runs recorded their revision, so all four runs record the same
clean head. Those exclude entries are removed before the commit.

Isolation: every host command ran with `OPENCLAW_HOME`, `OPENCLAW_STATE_DIR`
and `OPENCLAW_CONFIG_PATH` in a fresh temp dir, `--config` on `agent exec`,
`logging.file` inside that dir, and `gateway.port` set to a closed loopback
port, so the host never reached an OpenClaw Gateway of its own. The
operator's own OpenClaw home was never used. In steps 1 and 4 the token
went to the host on standard input (`models auth paste-api-key`); in steps
2 and 3 it came from the harness only. No export holds it.

## How to reproduce

From the repository root, with the venv (`pip install -e ".[dev]"`), Node
on `PATH`, and `npm ci` done in `integrations/openclaw`. Build the artifact
first:

```bash
cd integrations/openclaw && npm run pack:release -- ../../release/openclaw && cd ../..
```

Step 1, the install check, recorded as a target-platform run:

```bash
python scripts/openclaw_install_check.py \
  --tarball release/openclaw/relaymind-openclaw-provider-0.1.0.tgz \
  --out <export.json> \
  --label "macOS 27.0.1 arm64, Apple M1 (user's machine), Node v26.12.1" \
  --target-platform \
  -- node integrations/openclaw/node_modules/openclaw/openclaw.mjs
```

Steps 2 and 3, the harness host scenarios over doubles (no model):

```bash
python docs/evidence/2026-10-29-openclaw-mac-qualification/scaffold/round_trips.py <scratch> \
    [--tarball <release tgz>]
python docs/evidence/2026-10-29-openclaw-mac-qualification/scaffold/cancel_errors.py <scratch> \
    [--tarball <release tgz>]
```

Step 4 needs the pinned llama-server running per `models/README.md`:

```bash
llama-server -m models/Qwen3-0.6B-Q8_0.gguf \
  --host 127.0.0.1 --port 8033 --ctx-size 8192 \
  --jinja --reasoning off --reasoning-format deepseek
python docs/evidence/2026-10-29-openclaw-mac-qualification/scaffold/live_turn.py <scratch> \
    --tarball <release tgz> [--llama-url http://127.0.0.1:8033] [--port 8080]
```

Each driver writes raw outputs under `<scratch>/raw` and a sanitized copy
into this folder.

## Step 1. Install check: passed

`scripts/openclaw_install_check.py --target-platform`. The export's
`evidence` says it is the section 12.5 platform qualification of the
install lifecycle over gateway doubles, not live model evidence, and
`platform_qualification` is true.

| Step | Exit | Seconds |
| --- | --- | --- |
| version | 0 | 0.1 |
| isolate-log | 0 | 3.5 |
| isolate-gateway | 0 | 3.3 |
| install (`plugins install npm-pack:<tgz> --force --accept-capabilities`) | 0 | 21.1 |
| configure | 0 | 3.6 |
| enable | 0 | 3.3 |
| inspect | 0 | 2.8 |
| store-token | 0 | 3.9 |
| discover (`models list --refresh --provider relaymind --json`) | 0 | 5.1 |
| text-exchange | 0 | 10.4 |
| tool-exchange | 0 | 10.0 |
| disable | 0 | 3.5 |
| uninstall (`plugins uninstall relaymind --force`) | 0 | 4.6 |
| remove-config (`config unset plugins.entries.relaymind`) | 0 | 3.2 |
| remove-credential (`models auth logout relaymind:manual --yes`) | 0 | 4.6 |

All 15 steps passed, 83 s in all.

- Text exchange: one run, completed; "Hello from RelayMind." in the host's
  output.
- Tool exchange: the gateway proposed the host's `read` tool
  (`call_read_1`). The host ran it once (1 call, 0 failures) and replayed
  the ID as `callread1`. The result reached the next run, and the host
  printed the gateway's answer. The gateway executed no host tool
  (`host_tool_executions_by_gateway` 0).
- After `plugins uninstall --force`, the config entry, the
  `relaymind:manual` profile, and the token in the host state remained.
  After `config unset` and `models auth logout`, all three were gone.

## Step 2. The #137 harness host scenarios: four passed, `tool-approve` failed correctly

Driver `scaffold/round_trips.py`. It installed the artifact and the test
host tools from `integrations/openclaw/test/host-tool-plugin`, then set
`tools.toolSearch false` and `tools.alsoAllow` naming both test tools.

| Setup step | Result |
| --- | --- |
| install host tools without `--accept-capabilities` | exit 1, as expected |
| install host tools with `--accept-capabilities` | exit 0 |
| `plugins inspect relaymind-test-host-tools --runtime --json` | `status: loaded`, `toolNames` `relaymind_test_echo` and `relaymind_test_write` |
| uninstall host tools without `--force` | exit 1, as expected |
| uninstall host tools with `--force` | exit 0 |

Each scenario is one `python scripts/openclaw_qualification.py host
--scenario <name> -- ... agent exec ...` (the exec scenarios through
`sh -c` with `--cwd "$RELAYMIND_TEST_SANDBOX"`). `checks.passed` decides;
the host's `ok` does not.

| Export | `tools.exec.mode` | Proposed, replayed as | Host (`ok`, final, calls, failures) | Sandbox | Result | Seconds |
| --- | --- | --- | --- | --- | --- | --- |
| `text-tool.json` | unset | `relaymind_test_echo`, `callecho1` | true, "The tool said: echo: lantern.", 1, 0 | none expected | **passed** | 15.6 |
| `tool-deny.json` | `deny` | `relaymind_test_write`, `callwrite1` | true, "The host did not allow the write.", 1, 1 | 0 of 0 | **passed** | 13.0 |
| `tool-approve.json` | `full` | `relaymind_test_write`, `callwrite1` | true, "The note was written.", 1, 1 | **0 of 1** | **failed** (harness exit 1) | 13.1 |
| `exec-deny.json` | `deny` | `exec`, `callexec1` | true, "The host did not allow the command.", 1, 1 | 0 of 0 | **passed** | 11.9 |
| `exec-full.json` | `full` | `exec`, `callexec1` | true, "The note was written.", 1, 0 | 1 of 1 | **passed** | 12.4 |

Each scenario made two gateway runs, both `completed`, and in every one
the result reached the next run. The gateway executed no host tool in any
scenario.

In both write scenarios the host's stderr has
`relaymind_test_write failed: Plugin approval unavailable: non-interactive
CLI runs have no approval-capable initiating surface.` and nothing was
written. So `tool-approve` cannot pass headless: the doubles' closing text
claims a write, the host says `ok: true`, and the sandbox and the checks
show no write. `exec-deny` logged `exec denied: host=gateway security=deny`.
This is the same result as the Linux VPS check at `fd6aca1`
(`docs/evidence/2026-10-28-openclaw-vps-functional-137/`), now on a target
platform, with the Q8 and Q9 fixes that the 2026-10-27 macOS run lacked.

## Step 3. The #138 abort and error mapping: passed

Driver `scaffold/cancel_errors.py`, the same install of the artifact.

| Export | Host | Gateway | `checks` | Seconds |
| --- | --- | --- | --- | --- |
| `abort.json` | status `interrupted`, exit 130, no stdout | 1 run `cancelled` (`run.accepted`, `route.selected`, `model.started`, `model.finished`, `run.cancelled`, reason `cancel_requested`); `model.finished` error `cancelled` | passed: signal sent, `adapter_cancel_requested` true, `answer_deltas` 0 | 12.8 |
| `external-failed.json` | status `failed`, exit 1, `ok` false, final "" | 8 runs, all `failed` / `external_failed` / `invalid_output` | passed: 8 plugin attempt records (503, `external_failed`), each naming a recorded run (`attempt_runs_recorded` true); `host_message` true; `answer_deltas` 0; gateway tool executions 0 | 94.8 |

In `external-failed` the host retried the same model 7 times (7
"transient same-model retry" lines in its stderr, reason `server_error`).
Its stderr names 8 different Idempotency-Key values, one per run. Its final
message was its own: "The AI service is having trouble. Please try again
in a moment." Neither scenario produced answer text or a gateway tool
execution.

## Step 4. Live turn through the packaged plugin

Driver `scaffold/live_turn.py`. This is the live AC24 turn through the
packaged plugin's own resolved model. The 2026-10-27 run could only make it
with a config-row workaround.

Gateway: the real `relaymind serve --config` from a copy of
`relaymind.example.toml` (`live/relaymind.toml`), with the store and sandbox
paths under the scratch dir, port 8080, the local model at the pinned
llama-server on 127.0.0.1:8033, and no `[external_model]`. Warm-up:
"Local warm-up finished: routing 757 ms (first token 720 ms, cached 0),
skill 757 ms (first token 570 ms, cached 0)."

| Step | Result |
| --- | --- |
| `plugins install npm-pack:<tgz> --force --accept-capabilities` | exit 0, 23.3 s |
| `plugins inspect relaymind --runtime --json` | `status: loaded`, enabled, provider `relaymind`, no diagnostics |
| `models auth paste-api-key --provider relaymind` (token on stdin) | exit 0, profile `relaymind:manual` |
| `models list --refresh --provider relaymind --json` | one model, `relaymind/relaymind-client-tools`, "RelayMind client-owned tools (auto)", available, provider outcome `ready`; the plugin logged "mode auto is ready; no context window advertised (no external limit configured for mode auto); no output limit advertised" (the host shows `contextWindow` 128000, its own value) |
| `agent exec --json ... --model relaymind/relaymind-client-tools "Say hello"`, `executionMode` auto | host exit 1, 10.9 s |
| the same, `executionMode` local_only | host exit 1, 11.0 s |
| disable, `uninstall --force`, `config unset`, `models auth logout` | all exit 0 |
| stop the gateway (SIGINT) | exit -2 (uvicorn re-raises the interrupt) |

The two turns:

| Mode | Gateway run | State, error, reason | Detail | Model calls, external calls | Active |
| --- | --- | --- | --- | --- | --- |
| auto | `run_bf7f7f50edd44e51900be11340bef4d2` | failed, `needs_external`, `context_limit` | "the request needs 9384 (tokenizer); the local limit is 8192" | 0, 0 | 42 ms |
| local_only | `run_43403a319bae44e5914fa6301da9010a` | failed, `needs_external`, `context_limit` | "the request needs 9389 (tokenizer); the local limit is 8192" | 0, 0 | 28 ms |

In both turns the requested and effective mode was the plugin's mode. The
events were `run.accepted`, `route.selected` (route `local`, mechanism
`client_owned`), and `run.failed`, all with actor `gateway`. The plugin
wrote one attempt record per turn (`live/diagnostics.jsonl`): outcome
`failed`, status 422, code `needs_external`, the run ID above, attempt 1,
not retried, not replayed. The host's final text was empty, `ok` false, and
its error carried the plugin's message, for auto:

> LLM request rejected: RelayMind needs_external (HTTP 422): The request cannot complete under the effective execution mode without the external model (for example executionMode local_only). RelayMind refused this request under its configuration; this is not an answer. Gateway: "the task cannot complete under the effective execution mode". Gateway run run_bf7f7f50edd44e51900be11340bef4d2, reason context_limit.

(The host escapes the underscores and punctuation in its JSON; the
unescaped form is quoted.)

The request measured 9,384 and 9,389 tokens here, against 10,510 in the
2026-10-27 live turn. The host's prompt differs with its configuration.
Both are over 8192.

The switches on the plugin-supplied model are shown by the gateway's
acceptance, not by a wire capture. The gateway rejects `stream_options` and
the other section 7.1 violations by name, so a completed run in steps 1 to
3, all through the plugin's model, means the host sent none of them. The
Linux VPS #136 check saw the same through its recording proxy. No recording
proxy ran here.

## What this does not show

- A plugin approval answered by an operator (allow-once or deny at a
  running OpenClaw Gateway): `not_run`. Here both write scenarios were
  decided by the missing approval route.
- Windows, native and WSL2: `not_run`. No Windows machine of a user ran the
  host.
- Other error codes on the real host (403, 409, 429, 504, a stream error
  event), aborts under early headers or before sending, and a failover to
  another model: `not_run`.
- The #139 measurement: `not_run` (no external provider credential).
- A completed live task: none. The live turn ended before any model call.
- The wire: no recording proxy ran, so request headers and bodies are not
  recorded. The gateway's trace is.

## Files

| Path | Content |
| --- | --- |
| `install-check/install-check.json` | the install check export: every step, the exchanges, the runs and events, the leftovers, versions |
| `install-check/release.json`, `install-check/SHA256SUMS` | the release record of the artifact, copied from the build |
| `round-trips/steps.json` | every command of step 2 (sanitized argv), exit code, expected exit, seconds, output tails |
| `round-trips/exports/<scenario>.json` | the harness export of each step 2 scenario |
| `cancel-errors/steps.json`, `cancel-errors/exports/<scenario>.json` | the same for step 3 |
| `live/live-turn.json` | step 4: runtime, gateway lines, artifact, every step, and per turn the host output, the attempt records, and the gateway's run view and events |
| `live/steps.json` | the step 4 commands with their output |
| `live/serve.out` | the gateway's output, the token as `<redacted>` |
| `live/relaymind.toml` | the gateway config used |
| `live/diagnostics.jsonl` | the plugin's attempt records |
| `live/llama-props.json` | the runtime's `/props` |
| `scaffold/` | the three drivers |

Sanitizing: the drivers replace the scratch dir with `<scratch>`, the
harness's temp dir with `<tmp>`, the checkout with `<repo>`, the home dir
with `<home>`, the network hostname with `<hostname>`, and loopback ports
with `<port>`. A string field over 2,000 characters (the host's system
prompt) is kept by its length only. The install check writes the host home
as `<host-home>` and the artifact folder as `<artifact-dir>`. The token is
never recorded: every export holds `"token": "<not recorded>"`, and
`serve.out` holds `<redacted>`. The host's runtime line inside each recorded
user message (`host=...`) carries the Mac's computer name, which differs
from the network hostname, so the drivers did not replace it during the
run. It was replaced with `<host>` in every export after the run and before
the record was committed; the install export's digest above is of the file
after that replacement, and `tests/test_openclaw_mac_m9_qualification.py`
checks that every `host=` field reads `<host>`. Nothing else in the exports
changed.
