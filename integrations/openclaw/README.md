# RelayMind provider for OpenClaw

Status: provider package (#136; RM08, RM12, RM19, RM26, RM33, spec section
7.5) with text and host-owned tool round trips (#137) and cancellation, error
mapping, retries, and run-ID correlation (#138), on top of the #135
qualification, and a release artifact, CI, and the operator guide below
(#145). The plugin registers the `relaymind`
provider through the public plugin SDK of OpenClaw v2026.10.7, publishes the
client-owned alias `relaymind/relaymind-client-tools` when the gateway serves
it, and holds the gateway's application token in the host's credential store.
The installable artifact, CI, and the operator guide are #145. The real
pinned host loaded this package once, in a Linux VPS functional check (not a
supported-platform qualification) against the gateway over model doubles
([`docs/evidence/2026-10-28-openclaw-vps-functional-136/`](../../docs/evidence/2026-10-28-openclaw-vps-functional-136/REPORT.md)):
install, enable, inspect, token setup, `models list --refresh`, a text turn
through `agent exec`, the stale-token 401 line and recovery, and removal
all worked with the command forms below. This package on macOS and Windows
is `not_run`. The harness host scenarios (a host tool round trip, the
host's exec policy pair, and the test write tool's approval, which fails
closed headless) then ran on the same pinned host in a second Linux VPS
functional check
([`docs/evidence/2026-10-28-openclaw-vps-functional-137/`](../../docs/evidence/2026-10-28-openclaw-vps-functional-137/REPORT.md)).
A plugin approval answered by an operator, and these scenarios on macOS and
Windows, are `not_run`. The `abort` (SIGINT mid-turn) and `external-failed`
(503) host scenarios ran in a third Linux VPS functional check
([`docs/evidence/2026-10-28-openclaw-vps-functional-138/`](../../docs/evidence/2026-10-28-openclaw-vps-functional-138/REPORT.md)):
the run was cancelled, and the 503 was a named failure that the host
retried 7 times; on macOS and Windows at this commit they are `not_run`.

What has run on a real host, and what has not (details in
[`docs/EVIDENCE.md`](../../docs/EVIDENCE.md#openclaw-plugin-platforms-145)):
the whole install check of the release artifact
(`scripts/openclaw_install_check.py`) passed against the pinned host on a
Linux VPS, as a functional check over gateway doubles, not a
supported-platform qualification
([`docs/evidence/2026-10-28-openclaw-vps-functional-145/`](../../docs/evidence/2026-10-28-openclaw-vps-functional-145/REPORT.md)). macOS and Windows runs of this revision
are `not_run`; the CI job runs the same check on GitHub-hosted runners,
recorded at `78fc94d` as a functional check over doubles, not a
user-machine qualification. The M9 evaluation runner and grading (#139,
[M9 evaluation](#m9-evaluation-139-operator-not-run-yet)) are tested over
a stub host and doubles; the measurement is `not_run`.

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
| `src/config.ts` | The plugin settings: loopback `baseUrl`, `executionMode`, copied `limits`, `maxRetries`, `diagnosticsPath` |
| `src/compat.ts` | `relaymindModelCompat()` (the per-model switches) and `relaymindModelEntry()` (#135) |
| `src/stream.ts` | The provider's `wrapStreamFn`: the input guard before sending, and an empty answer or more than one call made a failure (#137); the Idempotency-Key, the bounded retry rule, the gateway error message, and one attempt report per request (#138) |
| `src/errors.ts` | Gateway error codes as the host's error classes, with one sanitized message each (#138) |
| `src/idempotency.ts` | The Idempotency-Key of an inference payload and the OpenClaw turn ID (#138) |
| `src/attempts.ts` | One diagnostics record per inference attempt, with its RelayMind run ID (#138) |
| `src/input-guard.ts` | Unsupported input refused by field, never removed (#137) |
| `src/diagnostics.ts`, `src/redaction.ts` | The capability statement and operator messages; the logger that scrubs the token |
| `test/*.test.ts` | vitest: manifest, config, catalog, provider, redaction, the switches, the frozen fixtures |
| `test/plugin-transport/drive-plugin.mjs` | plays the host around the plugin (the checkout's sources, or the `entry` of an unpacked artifact): register, catalog (or `resolveDynamicModel`), the conversation through the pinned transport wrapped by the plugin, the host's sandbox tools and approval decision, its abort signal, and a host retry of an unchanged request |
| `test/host-tool-plugin/` | test only, never packed: the `relaymind_test_echo` and `relaymind_test_write` host tools, every write behind the host's approval |
| `test/host-transport/` | the #135 transport driver and recording proxy |
| `test/fixtures/v2026.10.7/transport/` | the #135 deterministic transport fixtures, captured by `scripts/openclaw_qualification.py fixtures` |
| `test/fixtures/v2026.10.7/host/` | reserved for captures made with `capture_host_requests.mjs`; the macOS host probe's captures are in `docs/evidence/2026-10-27-openclaw-mac-probe/` |
| `scripts/capture_host_requests.mjs` | the controlled endpoint a real host is pointed at |
| `scripts/pack-check.mjs` | checks the packed tarball: built entry, manifest, README, and public SDK imports only |
| `scripts/release-pack.mjs` | `npm run pack:release -- <dir>`: the release artifact from a clean build, with `SHA256SUMS` and `release.json` (#145) |

## Operator guide (#145)

RM12, RM18, RM19, RM33; AC24; spec sections 7.5, 12.5, and 14. The steps use
the command forms the pinned host (OpenClaw 2026.10.7) accepted on the macOS
qualification at `a52f8bf` (its install, enable, refresh, and uninstall
corrections, and what uninstall leaves behind) and in the Linux VPS install
check of the release artifact, a Linux VPS functional check (not a
supported-platform qualification) over model doubles
([`docs/evidence/2026-10-28-openclaw-vps-functional-145/`](../../docs/evidence/2026-10-28-openclaw-vps-functional-145/REPORT.md)).
This revision on macOS and Windows is `not_run`. The sections after this one
hold the details. OpenClaw owns its sessions, tools, permissions, and
approvals; RelayMind owns the inference, its checks, and its run trace.

### 1. Requirements

- OpenClaw 2026.10.7 exactly (`openclaw --version`): the package declares no
  other release, and the host's own range check refuses it on another one.
- Node `>=24.22.1 <25 || >=26.12.1`, the pinned host's engine range.
- A RelayMind checkout with its Python install (`pip install -e .`), to start
  the gateway.
- The host and the gateway on the same machine: the plugin sends only to a
  loopback `baseUrl` (`localhost`, `127.0.0.0/8`, `[::1]`), and the gateway
  serves only loopback (section 7.1). A host in a container, a VM, or WSL2
  with the gateway outside it is a different network and is not qualified.

### 2. Start the gateway

```bash
relaymind serve --config relaymind.toml     # a copy of relaymind.example.toml
```

It prints `Serving on http://127.0.0.1:8080` and a new application token, once.
The plugin never starts or stops the gateway. Local and external models are
the gateway's configuration, not the plugin's: `[local_model]` (the pinned
llama-server, `models/README.md`) and `[external_model]` (with its
credential in `RELAYMIND_EXTERNAL_API_KEY` in the gateway's shell, never in
OpenClaw). An `external_only` setup needs no local model; `local_only` makes
zero external calls.

Local context limit: an OpenClaw turn carries the host's ~30,000-character
system prompt and its tool catalog. With the pinned tokenizer that is
10,500 to 10,577 tokens per request (macOS qualification, branch
`devnull.exile/m9-mac-qual`, commit `b575c01`), over the local model's 8192.
So in `auto` every OpenClaw turn goes to the external model, and with the
pinned host and the 8192 local context `local_only` cannot serve an OpenClaw
turn; without an external model an OpenClaw turn cannot run. What the
operator sees in `local_only` depends on `limits.localContextLimit`
("Configuration"):

- copied as the example shows (8192, with `maxCompletionTokens`): the plugin
  advertises 8192 as the window, and the host's transport refuses the turn
  itself before sending it, with its own context error ("Context window
  exceeded: estimated input ... within the 8192-token context."). There is
  no gateway run and no `X-RelayMind-Run-ID`; the plugin's attempt record
  has no run (`unavailable`) and no status (fixture
  `advertised-window-host-refusal`, captured with a smaller window). The
  transport checks only a request with a max-token budget; without one the
  turn is sent as below. What the host's agent does after that error was
  not checked;
- not set: no window is advertised, so the host sends the turn and the
  gateway answers 422 `needs_external` (`context_limit`) with a run ID. The
  plugin classes it `format`, so the host does not retry it on the same
  model and it is not sent again as a new run ("Cancellation, errors, and
  retries").

The local context stays 8192 (no runtime, manifest, or spec change).

### 3. Get the release artifact

Build it from a checkout (or take the CI job's uploaded artifact):

```bash
cd integrations/openclaw
npm ci
npm run pack:release -- ../../release/openclaw
# release/openclaw/relaymind-openclaw-provider-0.1.0.tgz, SHA256SUMS, release.json
```

`pack:release` builds from a clean `dist/`, checks the package, and records
the tarball's SHA-256, the pins, and the Node that built it. Two builds of
one checkout are the same bytes, so a SHA-256 names a release.

### 4. Install, configure, enable

```bash
openclaw plugins install npm-pack:release/openclaw/relaymind-openclaw-provider-0.1.0.tgz \
  --force --accept-capabilities
openclaw config set plugins.entries.relaymind.config.baseUrl http://127.0.0.1:8080/v1
openclaw plugins enable relaymind
openclaw plugins inspect relaymind --runtime --json   # status loaded, providerIds ["relaymind"], no diagnostics
```

Without `--accept-capabilities` the host refuses a local package (exit 1).
It installs the plugin disabled until it is configured, so the explicit
`plugins enable` is needed (the host warns "plugin disabled ... but config is
present" at the `config set` before it; that is expected). Other settings
(`executionMode`, `limits`, `maxRetries`, `diagnosticsPath`) are in
"Configuration" below. A value outside the
schema is refused by the host before the plugin runs (for example
`executionMode: parallel_demo`); a non-loopback `baseUrl` is refused by the
plugin at register (`plugins inspect` shows `status: error` with the reason).

### 5. Store the token, and after every gateway restart

```bash
printf "%s\n" "$TOKEN" | openclaw models auth paste-api-key --provider relaymind
openclaw models list --refresh --provider relaymind --json   # lists relaymind/relaymind-client-tools
```

The token is the one `relaymind serve` printed. Paste it on standard input:
never as an argument and never in the plugin config. The gateway makes a new
token at every start, so after a restart the stored one is stale. Requests
and discovery then fail with 401 `unauthenticated` and the reconnect line
("Token handover and reconnect" below). Store the new token the same way.
That is the rotation: there is no other token to rotate. `models list` needs
`--refresh` when no OpenClaw Gateway is running: without it the host shows
its cached catalog, which does not hold the alias yet. `paste-api-key` warns
that "the local Gateway could not refresh" the change when no OpenClaw
Gateway runs; the stored token is still used. To refresh the catalog after
storing a new token, run the same `models list --refresh` again.

### 6. Select the model

```bash
openclaw agent exec "Say hello" --model relaymind/relaymind-client-tools --json
```

Or set `relaymind/relaymind-client-tools` as the agent's model in the host
config. The plugin serves this one client-owned alias and nothing else.
Headless `agent exec` finds it through `resolveDynamicModel` ("How the host
finds the model").

On Windows, give `agent exec` an existing, empty state directory with
`--state-dir <dir>`. Without it the pinned host makes a temporary one and
deletes it after the turn, and on the GitHub windows-latest runner that
delete failed on a SQLite file the host still held (`EBUSY`): the answer
arrived, but the run ended `"ok": false`, exit 1 (28 October 2026). The
host keeps a directory given with `--state-dir`; remove it yourself.

### 7. Host permissions and approvals

They are OpenClaw's. The gateway proposes at most one call per response and
never runs, approves, or certifies a host tool (RM08, RM33). Headless
`agent exec` has no approval surface: `tools.exec.mode: "ask"` fails closed,
`deny` blocks, and `full` runs. The host's `"ok": true` is not task success:
it says so for a denied tool too. Check the tool's effect and the gateway
trace.

### 8. Cancellation and errors

Ctrl-C (SIGINT) on the host closes the request, and the gateway cancels the
run that request owns (exit 130, no JSON report). Every gateway error
reaches the host as one failure that names the gateway's code and run, never
an empty answer. See "Cancellation, errors, and retries" for the table.
Troubleshooting:

| Symptom | Cause and fix |
| --- | --- |
| `Unknown model: relaymind/relaymind-client-tools` | the plugin is not enabled or not loaded (`plugins inspect relaymind --runtime --json`), or it is a package older than `resolveDynamicModel` (#136) |
| catalog line `RelayMind gateway unreachable at ...` | the gateway is not running at `baseUrl`: start it, check the port |
| `RelayMind rejected the stored application token (HTTP 401 unauthenticated)` | stale token after a gateway restart: step 5 |
| `RelayMind needs_external (HTTP 422)`, reason `context_limit` | the turn is over the local limit in `local_only` (with `localContextLimit` not set), or no external model is configured; `local_only` cannot serve an OpenClaw turn at the 8192 local context, so use `auto` with an external model (step 2) |
| `Context window exceeded: estimated input ... within the 8192-token context.` | the host's own refusal before sending, in `local_only` with `localContextLimit` 8192: no gateway run, no run ID; `local_only` cannot serve an OpenClaw turn at that context, so use `auto` with an external model (step 2) |
| `RelayMind execution_mode_forbidden (HTTP 403)` | the gateway's `allowed_modes` does not permit the plugin's `executionMode` |
| `RelayMind overloaded (HTTP 429)` | another run holds the gateway's execution slot; retry, or set `maxRetries` |
| install exit 1, "requires capability consent" | add `--accept-capabilities` |
| `plugins uninstall` asks for confirmation | add `--force` without a terminal |

### 9. Trace inspection

Every response carries `X-RelayMind-Run-ID`. With `diagnosticsPath` the
plugin appends one JSON line per attempt with the run ID and the links
("Diagnostics"). The gateway's run view and events are
`GET /relay/v1/runs/<run_id>` and `/relay/v1/runs/<run_id>/events`, and with
the demo UI configured, `/ui/runs/<run_id>`. All of them need the
application token. One OpenClaw turn with a tool call is several RelayMind
runs (the call, then each result), and they share the record's `turn`.
The trace is authoritative for model calls. The host's usage fields for this
provider are not a measurement (unified usage is unknown).

### 10. Update and roll back

The package declares exactly one OpenClaw release. A host update to another
release makes the host refuse the plugin until a release qualified on it is
installed. Update by installing the new artifact over the old one, and roll
back by installing the previous tarball the same way. Check its SHA-256
against the `SHA256SUMS` you kept:

```bash
openclaw plugins install npm-pack:<previous>.tgz --force --accept-capabilities
openclaw plugins inspect relaymind --runtime --json
```

Whether an install over an existing one keeps `plugins.entries.relaymind`
and the stored token was not checked (`not_run`). After it, check with
`plugins inspect` and `models list --refresh`, and repeat steps 4 and 5 if
either is gone.

### 11. Disable and remove

```bash
openclaw plugins disable relaymind                  # keeps the install, config, and token
# without a terminal, uninstall asks for confirmation and exits 1; --force skips it
openclaw plugins uninstall relaymind --force
openclaw config unset plugins.entries.relaymind     # uninstall leaves {"enabled": false}
openclaw models auth logout relaymind:manual --yes  # uninstall leaves the profile and the stored token
openclaw models auth list                           # relaymind:manual is gone
```

Uninstall removes the package, its install record, and its directory. On
the pinned host it leaves the config entry `plugins.entries.relaymind:
{"enabled": false}` and the `relaymind:manual` auth profile, and the live
gateway token stays in the host's state database (macOS qualification, and
every Linux VPS functional check). `config unset` and `models auth logout`
remove them. Without them a token that still works stays on disk until the
gateway restarts (the gateway makes a new token at every start). The install
check verifies that afterwards no config entry, no profile, and no copy of
the token remain in the host home. The host's `openclaw.json.bak*` backups
hold no token. On the macOS probe an earlier install's folders were also
left (the macOS qualification left none); remove them by hand if
`plugins list` no longer shows the plugin.

### 12. What the integration does not do

- No live token streaming: the gateway buffers the answer and sends it whole
  (a streamed response replays a finished run).
- One call per response, and no parallel tool calls.
- Small local context: see step 2. The plugin never trims or summarizes a
  conversation to make it fit.
- No managed skill selection, prefetch, or skill stage, and no managed
  device-aware placement: client-owned execution has none (section 7.5).
- Usage is unknown: the chat responses carry none. The run trace is the
  record.

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
          },
          maxRetries: 0,                           // 0 (one attempt) to 2; see below
          diagnosticsPath: "/var/tmp/relaymind-openclaw/attempts.jsonl"  // optional, absolute
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
  `local_only`; a limit not set for the mode is not advertised. With
  `localContextLimit` 8192 in `local_only`, every OpenClaw turn (10,500 tokens
  or more) ends at the host with "Context window exceeded" and never reaches
  the gateway; without it, the gateway answers 422 `needs_external`
  (step 2, "Local context limit"). The model
  name states the mode, and the catalog log line states which limit the
  window is.
- `maxRetries` is the plugin's own retries of one request: 0 by default (one
  attempt), at most 2; a larger value is refused, never clamped. See
  "Cancellation, errors, and retries".
- `diagnosticsPath` is an optional absolute file path: one JSON line per
  inference attempt is appended to it (see "Diagnostics"). A relative path
  is refused, so the host's working directory never decides where the
  records go. Create the directory first; a file the plugin cannot write is
  one warning, never a failed request.
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

## Cancellation, errors, and retries (#138)

Section 7.5; RM14, RM16, RM17, RM20, RM21, RM33; AC11, AC12, AC24.

**Cancellation.** The plugin passes the host's abort signal to the pinned
transport unchanged, so closing the host's request closes the HTTP request,
and the gateway cancels the run that request owns (section 7.4). On the macOS
probe, SIGINT on `openclaw agent exec` closed the upstream connection about
150 ms later (exit 130, no JSON report). In the Linux VPS functional check
(not a supported-platform qualification) SIGINT during a buffered local
generation ended the gateway's run `cancelled` (`cancel_requested`), the
adapter asked to cancel, with exit 130, no JSON report, and no attempt
record (the host process ended first), as on macOS (Q11). What that means
for a run:

| When the host aborts | The gateway's run | The run ID in the plugin's record |
| --- | --- | --- |
| before the request is sent | none is created | `unavailable` |
| during a buffered generation | cancelled (`cancel_requested`); the adapter is asked to cancel; the execution slot is freed | `unavailable` (no headers yet) |
| during a generation under early headers | cancelled the same way | the run ID from `X-RelayMind-Run-ID` |
| while the host reads the replay of a finished run | stays completed; nothing is cancelled | the run ID |
| after the answer | unchanged | the run ID |

A disconnect from the native events stream (`/relay/v1/runs/{id}/events`)
never cancels a run; only the chat request does. Cancellation of in-flight
external work is best effort, and a cancelled run keeps its recorded model
calls.

**Errors.** Every gateway error is a failure, never an empty answer, and the
plugin never replaces emitted text or switches model after output. The host
sees one message that starts with `RelayMind <code> (HTTP <status>)` (or
`(stream error event)` for an error event inside a 200 stream), says what to
do, quotes the gateway's own message on one line with any token removed, and
names the gateway run (`Gateway run <id>` or `Gateway run unavailable`). The
gateway's code stays the message's `errorCode`. Whether the host shows that
message is the host's choice: in the Linux VPS functional check a context
overflow's message was the host's final error text, but after a 503
`external_failed` the host's final text was its own `⚠️ The AI service is
having trouble. Please try again in a moment.`, and the plugin's message
was only in the host's log (`rawError=`, `detail=`) with the run ID. The class is what the
plugin's `matchesContextOverflowError` and `classifyFailoverReason` hooks
answer:

| Gateway code (status) | Host class | Plugin retry (with `maxRetries`) |
| --- | --- | --- |
| `context_limit_exceeded` (400) | context overflow (`context_overflow`) | no |
| `invalid_request`, `unsupported_parameter`, `invalid_execution_mode` (400) | request format (`format`) | no |
| `unauthenticated` (401), with the reconnect instruction below | authentication (`auth`) | no |
| `execution_mode_forbidden` (403) | refusal by policy (`format`) | no |
| `idempotency_conflict`, `run_terminal` (409) | refusal of the key (`format`) | no |
| `needs_external` (422) with `reason_code` `context_limit`, `execution_mode`, or `invalid_output` | refusal under the effective mode (`format`); a local context limit here is not a context overflow | no |
| `needs_external` (422) with any other `reason_code` (`capability_missing`, `no_local_model`, `local_budget`, `local_slow`, ...), or none | failure of this provider (`server_error`): the local runtime's health or the load stopped the local stage, and a later attempt may succeed | no |
| `overloaded` (429): another run or an idle probe holds the slot; nothing was queued or created | temporary overload (`overloaded`) | yes, same key |
| `internal_error` (500), `workflow_unavailable`, `external_failed` (503), whatever the `reason_code` | failure of this provider (`server_error`) | no |
| `deadline_exceeded` (504) | timeout (`timeout`) | yes, new key |
| `predicate_unmet` (stream error event) | failure of this provider (`server_error`) | no |
| the plugin's own empty-answer failure | `empty_response` | no |

Errors that are not RelayMind's (a refused connection, a proxy's 502) keep
the transport's message and the host's own classification. Whether the host
then fails over to another provider or model is host policy: the plugin
neither triggers nor hides it, and section 12.5 records it as a host
decision, never as a RelayMind answer.

The class decides the pinned host's own same-model retry (spec 0.49,
section 7.5): its `maybeRetryTransient` retries only `rate_limit`,
`overloaded`, `server_error`, `timeout`, and `output_limit`, up to 8 times
in about 90 s, and rejects every other class as `non_transient`
(`test/errors.test.ts` reads that list and the class the host derives from
the pinned build). In the Linux VPS functional check, a 503 `external_failed`
got 7 same-model retries (`transient same-model retry 1/8` to `7/8`, delays
from 0.6 s to 26.5 s, about 100 s in all) before the host stopped and
surfaced the error. Each retry rewrote the conversation (the host added
`[Earlier unanswered user message. ...]` and similar text), so each had a
new turn and a new `Idempotency-Key` and was a new gateway run with its own
external calls: 8 runs for one `agent exec`. The plugin's `maxRetries` (0)
does not bound these; whether a host setting does was not checked.

So a refusal that follows from the gateway's configuration and the request
is `format`, the class the host itself gives an unlabeled 400 or 422: a
`needs_external` over the local context (every OpenClaw turn under
`local_only` while the local context stays 8192), one the effective mode
routes external, or one after the local model's output was refused, a
forbidden `executionMode`, and a held or spent key are not sent again as new
runs. A `needs_external` whose `reason_code` depends on load or on the local
runtime's health is not a refusal: `capability_missing` also covers a failed
local generation (a llama-server connection error or restart), and
`no_local_model`, `local_budget`, and `local_slow` can change on the next
attempt. Those, and a `reason_code` the plugin does not know, stay
`server_error`, which the host retries on the same model, and their message
never calls them a configuration refusal. `external_failed` stays `server_error`, and
the retries above remain: its `reason_code` is why the run left the local
path or why its external stage stopped, so `invalid_output` there does not
show that the external output was refused rather than the provider failing.
A `format` class leaves the host's credential health alone; whether the
host fails over to another configured model after it is still host policy.
The real host's retry count for a `format` refusal was not run.

**Idempotency keys.** Every request carries an `Idempotency-Key`: the SHA-256
of what the pinned transport builds the body from (the model entry with its
`X-RelayMind-Execution-Mode` header, the host session, the system prompt,
every message with the host's timestamps, the tools, and the body-changing
request options), as `relaymind-openclaw-v1-<64 hex>`. The token is not part
of it, and neither is the gateway's address: a key belongs to the gateway's
one caller, so it survives a restart (section 7.4). A key the host set itself
is kept. The host's own turn ID could not be used: the pinned transport
passes `resolveTransportTurnState` a new random `turnId` and `attempt: 1` on
every call (`@openclaw/ai` 2026.10.7), so it differs on every retry; whether
the real host keeps it across retries was not checked on the probe. So:

- the host sending the same conversation again sends the key that payload
  last used (the pinned host's own same-model retries did not: they rewrote
  the conversation, above, so each was a new key and run): a completed run is replayed (`Idempotent-Replayed: true`,
  the same run ID, no model call, no tool proposal repeated), including one
  that completed under a plugin retry key; a run still active is 409
  `idempotency_conflict`; a stored call whose arguments were redacted is
  409, never replayed altered;
- a retry after a failure uses a new key (section 7.4): the plugin keeps, per
  provider and in memory, the key each payload sent last (at most 256
  payloads). When that key's run ended failed or cancelled (a 504, a 503, a
  422, an abort, a transport failure, or a 409 `run_terminal`), the next
  request of the same payload sends `<key>-retry<n+1>` and is a new run. A
  429 and other refusals that created no run keep the key. A new host
  process starts empty: its first retry of a failed run's key gets 409
  `run_terminal`, and the retry after that gets a new key;
- the next turn (a new user message, or the tool result after a call) gets a
  new key and a new run, by construction;
- a request retry never asks the host to repeat a host tool: after the tool
  ran, the retried follow-up replays the stored answer.

Limit: an identical conversation sent again in the same session within the
gateway's seven-day key retention replays the stored answer; the host's
per-message timestamps make that unlikely, but the key cannot tell a
deliberate repeat from a retry.

Limit: the key is computed before the body exists (the pinned transport
builds the request headers, then the body, then runs `onPayload`), so it
covers what the body is built from, not the bytes sent. A host change that
is in the conversation, such as the timestamp and the `Runtime:` line the
macOS probe saw on the user message (the same bytes in both requests of one
turn), is covered. A field a host wrapper added per call in `onPayload`
would not be: a retry of that request would get 409 `idempotency_conflict`,
a named failure, never a different answer or a second run. No such wrapper
was seen on the probe.

**Retries.** The plugin makes one attempt by default (`maxRetries: 0`). With
`maxRetries` 1 or 2 it retries only a 429 `overloaded` (same key: the gateway
stored nothing) and a 504 `deadline_exceeded` (a new key,
`<key>-retry<n>`: the failed run holds the first one), only before any
answer content, never after an abort, and after 500 ms times the retry
number. A retried attempt is not shown to the host; the last failure is. An
abort during the wait ends the request as aborted, never as the failure that
was about to be retried, and its attempt record says `aborted`.
The host's own retries are bounded by the key as above.

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
env var. In the #137 Linux VPS functional check no token was stored, and the
host read it in every harness host scenario
([report](../../docs/evidence/2026-10-28-openclaw-vps-functional-137/REPORT.md));
the macOS probe did not check it. After a gateway
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

Each inference attempt logs one line, `RelayMind inference attempt {...}`,
and, with `diagnosticsPath`, appends the same JSON object as one line:

```json
{"event":"relaymind.inference_attempt","turn":"turn-3f2a9c0d1b2e4f56","attempt":1,
 "idempotencyKey":"relaymind-openclaw-v1-...","runId":"run_...","runIdSource":"header",
 "outcome":"answer","status":200,"code":null,"reasonCode":null,"replayed":false,
 "retried":false,"nativeRunUrl":"http://127.0.0.1:8080/relay/v1/runs/run_...",
 "uiUrl":"http://127.0.0.1:8080/ui/runs/run_...","usage":"unknown"}
```

- `turn` is one OpenClaw turn: the session and the conversation up to its
  last user message. The first request of a turn and each tool-result
  request after it share it, so one turn maps to several RelayMind runs.
- `runId` comes from `X-RelayMind-Run-ID`, else the gateway's error body,
  else the chat response's ID; with none (aborted before headers, a 429, a
  401) it is `unavailable`, never invented. `runIdSource` says which.
- `outcome` is `answer`, `tool_call`, `failed`, or `aborted`; `code`,
  `reasonCode`, and `status` are the gateway's; `replayed` is the gateway's
  `Idempotent-Replayed`; `retried` says the plugin retried after it.
- `nativeRunUrl` and `uiUrl` are the run's native view and demo UI page;
  both need the application token, and the links hold none.
- `usage` is always `unknown`: the gateway's chat responses carry none, and
  the run trace is authoritative for model calls and per-model usage. The
  host's own zero usage fields for this provider are not a measurement.

A record holds no message content, no trace event, and no secret, and
nothing of it goes into answer text.

## Checks

Node `>=24.22.1 <25 || >=26.12.1` (the pinned host's range).

```bash
cd integrations/openclaw
npm ci
npm run typecheck
npm test
npm run pack:check     # builds dist/ and checks the packed tarball
npm run pack:release -- ../../release/openclaw   # the release artifact, SHA256SUMS, release.json
```

`pack:release` refuses a destination inside the package (the build would
delete it, or the next pack could ship it); `release/` at the repository root
is ignored by git.

From the repository root, the Python tier runs the transport and the plugin
against the real gateway over model doubles (it skips without `npm ci`; set
`RELAYMIND_OPENCLAW_NODE=1` to make a missing dependency fail):

```bash
pytest tests/test_openclaw_host_transport.py tests/test_openclaw_plugin.py \
  tests/test_openclaw_round_trips.py tests/test_openclaw_cancel_errors.py \
  tests/test_openclaw_qualification_script.py tests/test_openclaw_install.py \
  tests/test_openclaw_install_check.py
```

`tests/test_openclaw_install.py` checks the locked pins, that two release
builds are the same bytes, that the tarball holds only the runtime files,
and that the plugin round-trips text and a host tool from the unpacked
artifact (not the checkout) through the pinned transport. It also checks that
CI runs every OpenClaw test module. `tests/test_openclaw_install_check.py`
checks the install check against a stub host. CI's `openclaw-plugin` job runs
all of it on ubuntu, macOS, and Windows runners, then the install check with
the pinned host.

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
files from each config change; they did not hold the token. The operator
guide's step 11 gives the same removal.

### Install check: the release artifact through a real host

`scripts/openclaw_install_check.py` runs the operator guide's lifecycle with
the pinned host against a gateway over model doubles, in a fresh host home
(`OPENCLAW_HOME`, `OPENCLAW_STATE_DIR`, `OPENCLAW_CONFIG_PATH`; `logging.file`
points the host's file log into it, but on the Linux VPS `agent exec` still
wrote to the documented default `/tmp/openclaw/`, outside the isolation):

1. version, the host's file log, and the closed Gateway port;
2. install with `--accept-capabilities`;
3. `baseUrl`, enable, and inspect (loaded, the `relaymind` provider, no
   diagnostics);
4. the token through `paste-api-key` on standard input;
5. `models list --refresh` listing the alias;
6. a text exchange and a host-tool exchange (`agent exec`, each with
   `--state-dir` on a fresh, empty directory in the host home, so the run
   never depends on the host deleting its own temporary state, which fails
   on Windows; see step 6 of the guide). In the tool
   exchange the doubles propose the host's own `read` tool for `notes.txt`
   in a sandbox, and the host's result must reach the next run with the
   file's text, matched through the host's call ID rewrite;
7. disable and uninstall;
8. the removal of the config entry and the credential uninstall leaves.

The token is never an argument, never in the host's environment, and never
written to the export. The host inherits none of the shell's `OPENCLAW_*`
settings (profile, Gateway URL, token, port) and no proxy. Its local Gateway
port is set to a closed loopback port in the isolated config
(`config set gateway.port`), so a running OpenClaw Gateway of yours is never
reached. The port is never passed as `OPENCLAW_GATEWAY_PORT`: the pinned host
takes an explicit port as a Gateway target, and `models list --refresh` then
fails with "gateway models.list requires credentials" instead of refreshing
the local catalog (Linux VPS functional check, 28 October 2026). The gateway trace and the sandbox decide a pass;
the host's `"ok": true` does not: both exchanges need completed gateway runs
and the gateway's own answer. A removal passes only when the config reads as
strict JSON without the entry and profile and no host file a host could read
back holds the token (SQLite files are read through SQLite, so a deleted
row left in free space does not count); anything the check cannot read is
unknown, never clean.

```bash
cd integrations/openclaw && npm ci && npm run pack:release -- ../../release/openclaw && cd ../..
python scripts/openclaw_install_check.py \
  --tarball release/openclaw/relaymind-openclaw-provider-0.1.0.tgz \
  --out docs/evidence/<date>-openclaw-install-check-<machine>.json \
  --label "<machine>, <OS and version>, Node <version>, native|WSL2" \
  -- node integrations/openclaw/node_modules/openclaw/openclaw.mjs   # or: -- openclaw
```

It prints `{"status", "passed", "failed"}` and exits 0 only when every step
passed. A host that cannot start is `not_run`. The export records each step's
command, exit code, duration, and output, the gateway runs with their
events, the host's report, and what was left after uninstall and after
removal. The gateway answers from doubles, so a pass is never live model
evidence. It is a platform qualification only when the label and
`docs/EVIDENCE.md` record it as one, with the OS, the host, Node, and, on
Windows, native or WSL2 (section 12.5).

A stale token on a running host (restart `relaymind serve`; `models list
--refresh` reports the 401 reconnect line; paste the new token; it recovers)
passed on the macOS qualification at `a52f8bf` and is not part of the
install check.

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
`Unknown model` failure seen on the wire. A second one ran the harness host
scenarios there (`docs/evidence/2026-10-28-openclaw-vps-functional-137/`):
`text-tool`, `tool-deny`, `exec-deny`, and `exec-full` passed, and
`tool-approve` failed, correctly, because headless `agent exec` has no
route for a plugin approval and nothing was written. A third ran the
`abort` and `external-failed` host scenarios
(`docs/evidence/2026-10-28-openclaw-vps-functional-138/`), and the install
check of the release artifact passed there
(`docs/evidence/2026-10-28-openclaw-vps-functional-145/`). Any macOS rerun
and any Windows run are not_run.

Host facts from that probe that the steps below rely on: a model in the
host config needs `name`; `strictMessageKeys` must stay false (true strips
the tool linkage); isolate the host with `OPENCLAW_HOME`,
`OPENCLAW_STATE_DIR`, `OPENCLAW_CONFIG_PATH`, and `--config` (the host
still writes its log to `/tmp/openclaw/`); the host removes underscores
from a replayed tool call ID (`call_probe_1` came back as `callprobe1`), so
the harness never matches a later request to a proposal by the exact ID the
gateway issued (`answers_call` in `scripts/openclaw_qualification.py`);
headless `agent exec` cannot answer an approval (`ask` fails closed without a
running Gateway), so the policy runs use `tools.exec.mode` `deny` and `full`;
and the host's `"ok": true` is not task success (it says so for a denied
tool).

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

The default scenario, `text-tool`, fits one `agent exec`: the gateway
proposes one call to the test echo tool, the host runs it, and the next
request gets the closing text. The test host tools must be installed and
offered to the model (next section).

```bash
python scripts/openclaw_qualification.py host \
  --out docs/evidence/<date>-openclaw-host-qualification-<machine>.json \
  -- openclaw agent exec --config <a config whose relaymind plugin baseUrl is
     "${RELAYMIND_BASE_URL}"> --json \
     --model relaymind/relaymind-client-tools "Echo lantern with the test echo tool"
```

The token comes from `RELAYMIND_APP_TOKEN`, which the harness puts in the
command's environment; no paste is needed. Set the URL once with
`openclaw config set plugins.entries.relaymind.config.baseUrl '${RELAYMIND_BASE_URL}'`
(the host fills the variable in when it runs).

Results so far: on macOS at `a52f8bf`
(`docs/evidence/2026-10-27-openclaw-mac-qualification/REPORT.md`, step 4)
the default, tool-deny, and tool-approve scenarios ended with
`checks.passed` false and only the abort scenario passed. The default
scenario then scripted a text turn before the echo call, so one exec could
never show the call (Q8), and the test host tools never registered (Q9).
Both are fixed; in the Linux VPS functional check (not a supported-platform
qualification) at `fd6aca1`
([`docs/evidence/2026-10-28-openclaw-vps-functional-137/`](../../docs/evidence/2026-10-28-openclaw-vps-functional-137/REPORT.md))
`text-tool`, `tool-deny`, `exec-deny`, and `exec-full` passed and
`tool-approve` failed with no line written (below). macOS and Windows
reruns are not_run.

The harness passes the gateway URL and token in the command's environment
only and never writes the token. A command that cannot start is `not_run`;
one that runs past `--timeout` (900 s by default) is `timed_out`, and the
export still holds the runs the gateway recorded.

### Host tool policy (#137, operator)

Headless `openclaw agent exec` has no approval surface: on the macOS probe,
`tools.exec.mode: "ask"` failed closed because approval registration needs a
running Gateway (`ECONNREFUSED`), `deny` blocked the host's `exec` tool, and
`full` ran it. The `exec-deny` and `exec-full` scenarios use that pair: the
gateway proposes one call to the host's own `exec` tool,
`echo written-by-the-host >> '<sandbox>/note.txt'`, and the host decides. The
path is absolute, so a command the host ran in another working directory still
lands in the sandbox, and a deny that did not hold never passes. Run the host in
the sandbox the harness creates, with a config whose `tools.exec.mode` is
`deny` in one run and `full` in the other (the rest as above). The proposed
command and the forms below are POSIX shell (`sh`, a single-quoted path); on
Windows these scenarios are `not_run`:

```bash
python scripts/openclaw_qualification.py host --scenario exec-deny \
  --out docs/evidence/<date>-openclaw-exec-deny-<machine>.json \
  -- sh -c 'openclaw agent exec --config <relaymind config, tools.exec.mode "deny"> \
     --cwd "$RELAYMIND_TEST_SANDBOX" --json \
     --model relaymind/relaymind-client-tools "Append a line to note.txt with exec"'
python scripts/openclaw_qualification.py host --scenario exec-full \
  --out docs/evidence/<date>-openclaw-exec-full-<machine>.json \
  -- sh -c 'openclaw agent exec --config <relaymind config, tools.exec.mode "full"> \
     --cwd "$RELAYMIND_TEST_SANDBOX" --json \
     --model relaymind/relaymind-client-tools "Append a line to note.txt with exec"'
```

The `tool-deny` and `tool-approve` scenarios script one call to the mutating
sandbox tool `relaymind_test_write` of the test host tools, whose
`before_tool_call` hook asks the host for a plugin approval. The host
exposes a plugin's tools to the model only when they are allowed and sent
directly: set `tools.toolSearch false` and name both tools in
`tools.alsoAllow`. That is the form the recorded Linux VPS functional check
used. Two more results were observed in exploratory runs that were not
recorded (see the check's `REPORT.md`): `tools.profile full` also exposed
the test tools, and without `tools.alsoAllow`, or with tool search on, the
host offered neither test tool and the gateway repaired the scripted call.

```bash
# without --accept-capabilities the host refuses the directory (exit 1);
# the plugin is enabled at install, no plugins enable needed
openclaw plugins install integrations/openclaw/test/host-tool-plugin --force --accept-capabilities
openclaw config set tools.toolSearch false
openclaw config set tools.alsoAllow '["relaymind_test_echo","relaymind_test_write"]' --strict-json
python scripts/openclaw_qualification.py host --scenario tool-deny \
  --out docs/evidence/<date>-openclaw-tool-deny-<machine>.json \
  -- openclaw agent exec --config <the relaymind config above> --json \
     --model relaymind/relaymind-client-tools "Write a note with the test write tool"
python scripts/openclaw_qualification.py host --scenario tool-approve \
  --out docs/evidence/<date>-openclaw-tool-approve-<machine>.json \
  -- openclaw agent exec --config <the relaymind config above> --json \
     --model relaymind/relaymind-client-tools "Write a note with the test write tool"
# without a terminal, uninstall asks for confirmation and exits 1; --force skips it
openclaw plugins uninstall relaymind-test-host-tools --force
```

Headless, the approval has no route: in the Linux VPS functional check the
host logged `Plugin approval unavailable: non-interactive CLI runs have no
approval-capable initiating surface` for both write scenarios and wrote
nothing, whatever `tools.exec.mode` was (that mode governs the host's `exec`
tool only). So `tool-deny` passes headless and `tool-approve` cannot: it
needs a running OpenClaw Gateway and an operator who answers allow-once,
not_run. The headless allow path is `exec-full`.

The tools write into `RELAYMIND_TEST_SANDBOX`, an empty directory the
harness creates and passes in the command's environment. The export's
`checks` read the sandbox and the trace: a denial left no line, an allowed
call exactly one, the gateway executed nothing and proposed the call, and
the host's result reached the next run, matched through the host's ID
rewrite. `checks.replayed_call_ids` keeps each ID as the host sent it back
(the Linux VPS functional check saw `callecho1` for `call_echo_1`, as on
macOS). `checks.host_report` keeps the host's own JSON report (`ok`,
`status`, tool calls and failures) as the host's statement: the pinned host
ends a run whose tool was denied with exit 0 and `"ok": true`, so neither
decides `passed`. `passed` is false for a scenario that did not complete.

### Host interrupt and error mapping (#138, operator)

The `abort` scenario holds the gateway's generation open over a gated double
and interrupts the host command once the generation has started (SIGINT, as
an operator's Ctrl-C; `CTRL_BREAK_EVENT` on Windows). It passes when every
recorded run ended cancelled with `cancel_requested`, the adapter was asked
to cancel, and no answer text was recorded; the export's status is
`interrupted`, and the host's exit code is recorded, not judged (130 without
a JSON report on the macOS probe):

```bash
python scripts/openclaw_qualification.py host --scenario abort \
  --out docs/evidence/<date>-openclaw-abort-<machine>.json \
  -- openclaw agent exec --config <the relaymind config above> --json \
     --model relaymind/relaymind-client-tools "Say hello"
```

The `external-failed` scenario scripts both doubles to call a tool no host
offers, on every generation, so each run fails with 503 `external_failed`.
It passes when every recorded run failed with that code and no answer text,
the gateway executed no tool, and the host's output (its log lines
included) holds `RelayMind external_failed (HTTP 503): ...` naming a
recorded run. `checks.attempts` keeps the plugin's attempt records, and
every run they name must be recorded. The host's exit code is recorded, not
judged (1 here):

```bash
python scripts/openclaw_qualification.py host --scenario external-failed \
  --out docs/evidence/<date>-openclaw-external-failed-<machine>.json \
  -- openclaw agent exec --config <the relaymind config above> --json \
     --model relaymind/relaymind-client-tools "Say hello"
```

Both ran in the Linux VPS functional check (not a supported-platform
qualification) on the pinned host
([`docs/evidence/2026-10-28-openclaw-vps-functional-138/`](../../docs/evidence/2026-10-28-openclaw-vps-functional-138/REPORT.md)):
`abort` passed (one run, `cancelled`, exit 130) and `external-failed`
passed (8 runs, all `external_failed`: the request and 7 same-model
retries by the host, about 100 s). The stale-token 401 line ran in the
#136 check. Other error codes, aborts under early headers or before the
request is sent, the host's turn ID across its retries, and a failover to
another model were not run on a real host (`not_run`); they are covered
over doubles by `tests/test_openclaw_cancel_errors.py`, and failover is
host policy. On macOS (Q11, at `a52f8bf`, through a config-file provider
row) the abort passed; macOS and Windows at this commit are `not_run`.

### M9 evaluation (#139, operator, not run yet)

The section 12.5 measurement (spec 0.50) runs the frozen tasks of
`evaluation/openclaw_tasks.v1.json` through a real host in four arms:
`direct` (the host's own provider entry for the external model),
`relaymind_external_only` and `relaymind_auto` (this plugin against a
gateway with the external model and the pinned local model), and
`relaymind_no_local` (this plugin against a gateway without a local
model). The runner starts each gateway with `relaymind serve --config`, and
llama-server from the manifest for the arms with a local model, and gives
each arm its own host home. Each attempt's `agent exec` gets `--state-dir`
on a fresh, empty directory in that home, as in the install check (without
it the pinned host fails every turn on Windows):

```bash
export RELAYMIND_EXTERNAL_API_KEY=...
python scripts/run_openclaw_eval.py \
  --tarball release/openclaw/relaymind-openclaw-provider-0.1.0.tgz \
  --external-base-url <url> --external-model <model> --external-context-limit <n> \
  --out docs/evidence/<date>-openclaw-eval-<machine>/ \
  -- node integrations/openclaw/node_modules/openclaw/openclaw.mjs
```

An arm without its endpoint, credential, or local model is recorded as
not_run; `relaymind_auto` needs both the external model and the local
model. The host's `"ok": true` never counts as a success; a host failover
(a reported provider or model other than the arm's) is counted apart, and
so is a host interrupt (exit 130 or SIGINT). Interrupting the runner, during an arm's
setup or an attempt, keeps the attempts that ended: the export is written
marked aborted and the runner exits 130; an interrupt during setup marks
that arm and every arm not yet set up not_run, never ran. At the 8192-token local context an OpenClaw request does
not fit the local model (macOS qualification), so `relaymind_auto` is
expected to show 0 local turns with reason `context_limit`; the report
states that as the result when it happens. The
direct arm's host keeps the operator's proxy variables, as the gateways
do, so both reach the provider the same way; the export names the
variables (not their values) and the Node that ran the host. The direct arm writes the provider's key into the
temporary host home's config file (removed at the end, never exported);
whether the pinned host accepts that provider entry as written is not
verified. Record the result in `docs/EVIDENCE.md` ("M9 OpenClaw
evaluation").

