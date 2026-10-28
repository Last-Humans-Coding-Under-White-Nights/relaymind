# OpenClaw abort and error mapping on the real pinned host: Linux VPS functional check (#138)

**Linux VPS functional check (not a supported-platform qualification).**
This is one run of the real pinned OpenClaw host, through the `abort` and
`external-failed` host scenarios of `scripts/openclaw_qualification.py`,
against the real gateway over scripted model doubles on a Linux x86-64 VPS.
It is not live model evidence (no llama-server, no live model, no external
model), not a qualification of a supported platform, and not a Mac or
Windows result. macOS and Windows rows for these scenarios stay `not_run`
(the macOS abort result Q11 at `a52f8bf` is the earlier evidence, below).

## Identity and environment

| Item | Value |
| --- | --- |
| Machine | Linux VPS, `Linux 7.0.0-38-generic x86_64` |
| Host | `OpenClaw 2026.10.7 (157d8c2)`, the pinned `openclaw@2026.10.7` from `integrations/openclaw/node_modules` (`node_modules/.bin/openclaw`) |
| Node, npm | v24.22.1, 11.19.1 |
| Python | 3.14.4 (shared venv) |
| Gateway | the harness's host mode: the real FastAPI app (`ClientToolsChat`) on 127.0.0.1 over the model doubles of `tests/`, one fresh gateway and token per scenario |
| Commit tested | `aabcece75b173a0239b262b98ebe4dbc9ec66a6a`, clean (both exports' `versions.relaymind_revision` say `dirty: false`) |
| Plugin tarball | `relaymind-openclaw-provider-0.1.0.tgz`, 15 files, npm shasum `b83a1e5e3d3437e4d79b503c537e1c4894d3a0d2` |
| Date | 28 October 2026 |

Isolation: every host command ran with `OPENCLAW_HOME`, `OPENCLAW_STATE_DIR`
and `OPENCLAW_CONFIG_PATH` pointing into a fresh temp dir, and `agent exec`
had `--config` set to that file. The plugin's `baseUrl` was the literal
`${RELAYMIND_BASE_URL}`, filled by the host from the environment the
harness gives the command. No token was pasted: the only token path was
`RELAYMIND_APP_TOKEN` in that environment. The plugin ran with its defaults
(`maxRetries: 0`, `executionMode: auto`); the gateway had early headers off.

Reproduce from the repository root (Node on `PATH`, `npm ci` done in
`integrations/openclaw`), about three minutes, no model:

```bash
python docs/evidence/2026-10-28-openclaw-vps-functional-138/scaffold/vps_functional.py <scratch dir>
```

## Setup steps

| # | Step | Result |
| --- | --- | --- |
| 1 | `openclaw --version` | `OpenClaw 2026.10.7 (157d8c2)` |
| 2 | `npm run build`, `npm pack --pack-destination <dir>` | exit 0 |
| 3 | `plugins install npm-pack:<tgz> --force --accept-capabilities` | exit 0; installed disabled until configured |
| 4 | `config set plugins.entries.relaymind.config.baseUrl '${RELAYMIND_BASE_URL}'`, `plugins enable relaymind` | exit 0 |

Each scenario is then one `python scripts/openclaw_qualification.py host
--scenario <name> --timeout 240 --out <export> -- openclaw agent exec
--config <cfg> --json --model relaymind/relaymind-client-tools "Say hello"`.
`checks.passed` decides; the host's exit code and its own report do not.

## Abort: SIGINT mid-turn (`exports/abort.json`)

The harness holds the local generation open over a gated double and sends
SIGINT to `agent exec` 0.5 s after the generation started.

| Item | Result |
| --- | --- |
| Export status, host exit | `interrupted`, exit **130**, empty stdout (no JSON report) |
| Gateway run | one run, `cancelled`: `run.accepted`, `route.selected`, `model.started` (local, purpose `skill`), `model.finished` with error `cancelled` / "model generation interrupted", `run.cancelled` with reason `cancel_requested`; no handoff |
| `checks` | `signal_sent` true, `adapter_cancel_requested` true, `answer_deltas` 0, **passed** |
| Plugin attempt record | none in the host's output: the host process ended on the signal before the plugin logged one |
| Harness step | 26.85 s, mostly host start-up; the time from SIGINT to exit was not measured here |

This matches Q11, the macOS run at `a52f8bf`
(`docs/evidence/2026-10-27-openclaw-mac-qualification/REPORT.md`, step 4d,
on branch `devnull.exile/m9-mac-qual`): there a scratch script sent SIGINT
during `local_running`, the host exited 130 0.14 s later with empty stdout,
and the run ended `cancelled` with `cancel_requested`. That run used a
config-file provider row, not the plugin's resolved model; this one used the
plugin.

**Fixed on this branch before the recorded run.** The first try of the
`abort` scenario on this host did not reach the generation: the gated double
counted the host's request by character (its system prompt alone is 45,442
characters here), so the request fit neither the local nor the external
double's limit and the run failed at once with 400
`context_limit_exceeded` (the host exited 1 with
`RelayMind context_limit_exceeded (HTTP 400)` and kind `context_overflow`).
The stub host of the deterministic tests sends a few bytes, so they passed.
The harness now fixes the double's count at 800, as the default local double
does, and a harness test in `tests/test_openclaw_qualification_script.py`
now sends a 40,000-character system prompt through the abort scenario. Why the macOS request reached the
generation at `a52f8bf` was not checked. That first try was not kept.

## Error mapping: 503 external_failed (`exports/external-failed.json`)

Both doubles answer every generation with a call to
`relaymind_test_not_offered`, a tool no host offers, so each run makes a
local generation and its repair, hands off, makes two external generations,
and fails with `external_failed` / `invalid_output` (HTTP 503).

| Item | Result |
| --- | --- |
| Export status, host exit | `failed`, exit 1, JSON report `ok: false`, `status: error` |
| Gateway runs | **8**, every one `failed` with `external_failed` / `invalid_output`, 0 answer deltas, 0 gateway tool executions; 16 local and 16 external generations in all |
| Plugin attempt records | 8, one per run: `status` 503, `code` `external_failed`, `outcome` `failed`, `runIdSource` `error_body`, `retried` false, `usage` `unknown`; every `runId` is a recorded run (`checks.attempt_runs_recorded`) |
| Plugin message | `RelayMind external_failed (HTTP 503): The gateway's external model could not finish the run. This is a failure of the RelayMind provider, not an answer. Gateway: "the external model could not finish the run". Gateway run run_..., reason invalid_output.`, in the host's log lines (`rawError=`, and the `model-fallback/decision` line's `detail=`) |
| Host's final message | its own: `⚠️ The AI service is having trouble. Please try again in a moment.` (JSON `error.message`, kind `exception`, and the last stderr line) |
| `checks` | `host_message` true, `named_runs` the last run, **passed** |
| Harness step | 104.69 s |

**The host retried the same model 7 times.** The plugin classed the 503 as
a failure of this provider (`server_error`), and the pinned host answered
that with `transient same-model retry 1/8 ... reason=server_error` through
`7/8` (delays from 0.6 s to 26.5 s), then `transient retry window elapsed
... after 7/8 retries; stopping same-model retries`, and the fallback
decision `next=none`. It did not fail over to another model (none was
configured). Each retry rewrote the conversation: the host added text such
as `[Earlier unanswered user message. ...]`, `Continue the current task from
the existing transcript, ...`, and `[Queued user message from a previous
active turn; ...]`, so the 8 requests had 8 different message lists. So the
plugin computed 8 different turns and 8 different `Idempotency-Key` values,
and each retry was a new gateway run. That follows the key rule of section
7.5 (a changed payload is not the same inference payload); the host never
sent one payload twice, and no run was replayed. With a real external
model, one host turn that ends in `external_failed` can make up to 8
gateway runs, each with its own external calls (here 2 per run, 16 in all,
all on doubles). The plugin's own `maxRetries` stayed 0.

**Harness fix found by this run.** The first recorded try of this scenario
scripted a fixed number of turns per double; the host's retries used them
up, and the later runs failed with `internal_error` on an unscripted
double. The doubles now answer every generation the same way, and a
harness test in `tests/test_openclaw_qualification_script.py` plays a host
that retries 7 times. That try was not kept.

## What this does not show

- Any macOS or Windows result for these two scenarios on this commit:
  `not_run`. The macOS abort (Q11) ran at `a52f8bf` through a config-file
  provider row.
- An abort under early headers, while the replay is read, or before the
  request is sent, on the real host: `not_run` (covered over doubles by
  `tests/test_openclaw_cancel_errors.py`).
- Any other error code on the real host here (403, 409, 422, 429, 504, the
  stream error event): `not_run`. The 401 line was in the #136 check. A 429
  needs a second request while one holds the slot, which one headless
  `agent exec` does not make.
- The host's own turn ID across its retries (the plugin derives its turn
  from the conversation), whether a host config can bound its same-model
  retries, the time from SIGINT to exit, the wire (no recording proxy ran),
  live model behavior, and request size against the local limit (the
  doubles' count is fixed).

## Files

| File | Content |
| --- | --- |
| `steps.json` | every command (sanitized argv), its exit code and expected exit, seconds, stdout and stderr tails |
| `exports/abort.json`, `exports/external-failed.json` | the harness export of each scenario: status, exit code, host stdout and stderr tails, the gateway's runs and events, versions, and `checks` |
| `scaffold/vps_functional.py` | the driver that produced these files |

Sanitizing: the scratch dir became `<scratch>` (the harness's temp dir
`<tmp>`), the checkout `<repo>`, the home dir `<home>`, the hostname
`<hostname>`, and loopback ports `<port>`. A string field in an event
payload over 2,000 characters (the host's system prompt) is kept by its
length only. The harness replaces the token in the host's output and fails
if it reaches an export; the `relaymind-openclaw-v1-...` values are
Idempotency-Key payload digests, not secrets.
