# M9: OpenClaw integration feasibility

27 October 2026. Planning analysis; no plugin has been built or run.
The normative contract is specification 0.47, RM33, AC24, sections 7.5
and 12.5. This replaces the planned M9 YAML workflow expansion.

## Finding

Integration is technically feasible without a fork of OpenClaw or a rewrite
of RelayMind. A TypeScript provider plugin can connect the host's inference
loop to RelayMind's authenticated loopback Chat Completions endpoint. The
existing `relaymind-client-tools` alias has the right ownership boundary:
RelayMind proposes and validates a call; OpenClaw executes it and submits the
result in its next request. This is a source-based feasibility finding,
not proof of protocol compatibility, installation, model quality, or speed.

OpenClaw's provider guide documents provider registration, credential setup,
model catalogs, and the `openai-completions` transport. Its release guide
requires a package manifest and explicit host/SDK compatibility metadata.
The reviewed release was
[v2026.10.7](https://github.com/openclaw/openclaw/releases/tag/v2026.10.7),
using its
[provider plugin guide](https://github.com/openclaw/openclaw/blob/v2026.10.7/docs/plugins/sdk-provider-plugins.md).
The public
[runtime hooks](https://docs.openclaw.ai/plugins/sdk-provider-plugins/runtime-hooks)
allow stream wrapping for headers and request adaptation. The
[building guide](https://docs.openclaw.ai/plugins/building-plugins)
describes package registration and runtime inspection; loading alone does
not demonstrate a successful inference or tool round trip. #135 must qualify
the actual release transport, rather than assuming moving documentation
matches the pinned SDK.

## Verified in the pinned source

The following was read in the v2026.10.7 tree on 27 October 2026 (spec
source S12). It is still a source reading; #135 confirms it on the wire.

| File at the tag | Finding | Consequence for the plugin |
| --- | --- | --- |
| `packages/ai/src/transports/openai-completions-compat.ts` | Compatibility defaults are derived from the base URL. For a local endpoint `supportsUsageInStreaming` is true; for a configured non-OpenAI endpoint `supportsStrictMode` and `supportsDeveloperRole` are false and `supportsStore` is off for a non-standard endpoint. | Without a declared switch, every streamed request carries `stream_options`, which the gateway rejects. Declare the switches instead of relying on URL detection. |
| `packages/ai/src/transports/openai-completions-params.ts` | Sends `stream_options: {include_usage: true}` when the switch is on, `store: false` when `supportsStore`, `strict: false` in direct mode when strict is supported, the token field from `maxTokensField`, `tool_choice`, `temperature`, `top_p`, and `response_format` only when requested. | `store: false` and `strict: false` are accepted by section 7.1; `stream_options` is not. |
| `packages/ai/src/openai-completions-messages.ts` | The system prompt is `developer` only when the model is marked reasoning and the switch is on; runtime context messages are emitted mid-conversation as `developer` or, with the switch off, `user`; `requiresAssistantAfterToolResult` can insert a synthetic assistant message. | Declare `reasoning: false` and the developer switch off, because section 7.1 accepts system and developer messages only before the first user message. |
| `src/config/zod-schema.model-compat.ts` | The per-model `compat` object: `supportsStore`, `supportsDeveloperRole`, `supportsReasoningEffort`, `supportsUsageInStreaming`, `supportsStrictMode`, `supportsJsonSchemaResponseFormat`, `maxTokensField`, `strictMessageKeys`, `requiresToolResultName`, and others. | The seam for section 7.5's host compatibility switches. |
| `src/agents/embedded-agent-runner/extra-params.ts` | `parallel_tool_calls: true` is injected by default only for OpenAI gpt-5 models; an operator-set value is applied to any `openai-completions` model. | The gateway's rejection of `true` is reachable through operator configuration; document it, do not rewrite it. |
| `docs/plugins/sdk-provider-plugins/model-catalogs.md` | A catalog hook should omit `contextWindow` when the provider does not report it; the runtime then supplies an estimate marked synthetic. | `/v1/models` reports no limits, so the entry declares the configured limits or omits them; it never invents a window. |
| `packages/ai/src/transports/openai-completions-transport.ts` | The caller's abort signal is forwarded to the SDK request and watched by an idle timer. | Cancellation can reach the gateway's owned run (#138). |
| `package.json` | Node `>=24.22.1 <25 \|\| >=26.12.1`; `openai` 7.27.0. | Platform qualification records the Node range (#145). |

The host's system prompt and tool catalog are large. With the local
model's input limit, most host requests may exceed it and go external in
auto, so the local-inference arm of section 12.5 could show few or no
local completions. #135 measures the captured request sizes against the
limit first, so the owner can judge the local-value hypothesis before
#136 to #139 are built.

## Compatibility qualification (#135)

27 October 2026. The pinned transport ran against the real gateway over
model doubles. A real host (OpenClaw 2026.10.7 on macOS) then ran against a
capture server in the host probe, whose raw evidence and answers are in
[`docs/evidence/2026-10-27-openclaw-mac-probe/`](../evidence/2026-10-27-openclaw-mac-probe/REPORT.md);
the real host has not run against the gateway. Spec 0.49 records the facts
below in section 7.5. Where the host probe and the transport tier disagree,
the host probe decides.

### What ran, and where

| Item | Status | Where |
| --- | --- | --- |
| Pinned packages | `openclaw@2026.10.7` and `@openclaw/ai@2026.10.7` from npm (published 24 October 2026; the CLI banner reads `2026.10.7 (157d8c2)`), `openai` 7.27.0, Node v24.22.1 | `integrations/openclaw/package-lock.json`, `test/fixtures/v2026.10.7/VERSIONS.json` |
| Pinned transport against the real gateway (doubles) | ran, Linux x86-64 VPS | `tests/test_openclaw_host_transport.py`, 16 transport fixtures in `integrations/openclaw/test/fixtures/v2026.10.7/transport/` |
| Real host (`openclaw agent exec`) against the doubles gateway | not_run | `scripts/openclaw_qualification.py host -- <command>` (operator, CI) |
| Real host request capture (system prompt, tool catalog) | ran, macOS 27.0.1 arm64, Node v26.12.1, `OpenClaw 2026.10.7 (157d8c2)`, against the probe's own capture server | `docs/evidence/2026-10-27-openclaw-mac-probe/requests/`; checked by `tests/test_openclaw_host_captures.py` |
| Captured request sizes, UTF-8 upper bound | ran offline on the sanitized host captures (`--sanitized`): undecided (below) | `docs/evidence/2026-10-27-openclaw-request-size-mac-probe-utf8.json` |
| Captured request sizes, tokenizer count | not_run (needs llama-server with the pinned model and fresh, unsanitized captures) | `scripts/openclaw_request_size.py --base-url` |
| Switches on a plugin-supplied model (catalog or `resolveDynamicModel`) | not_run; the probe's plugin model declared none | #136 |

The transport tier is the host's own `openai-completions` stream function,
`createOpenAICompletionsTransportStreamFn` from the public
`@openclaw/ai/transports` export, under the host transport policy that
the public subpath `openclaw/plugin-sdk/provider-transport-runtime`
installs on import. Under that policy the host classifies a loopback URL
as a local endpoint, as the full host does. The driver
(`test/host-transport/drive.mjs`) plays the host: it owns the conversation
and executes the sandbox tool `relaymind_test_echo`. A transparent
recording proxy captures the bytes; a client disconnect closes the
upstream request. What this tier does not cover is the host's agent loop
around the transport: its system prompt, tool catalog, compaction, the
operator `params` wrapper (simulated through the transport's own
`onPayload` hook), and the plugin hooks. Those need the real-host run.

### SDK seams at the pin

- Public and callable alone: `@openclaw/ai/transports`
  (`createOpenAICompletionsTransportStreamFn`,
  `resolveOpenAICompletionsCompat`, `buildOpenAICompletionsParams`), and
  `openclaw/plugin-sdk/provider-transport-runtime`.
- Typed: `openclaw/plugin-sdk/plugin-entry`, `provider-auth`, `core`.
  Published without any `.d.ts`: `provider-entry`, `provider-model-types`,
  `provider-stream`, `provider-transport-runtime`. #136 builds on the typed
  entry or declares the few types it needs; this is not a blocker.
- Every switch below is a key of the host's strict `ModelCompatSchema`.
- The transport sends `model.headers` on the request, so the execution
  mode header reaches the gateway (fixture `external-only`).
- npm skipped the host package's install scripts (npm 11's script
  allowlist); the transport tier does not need them. The host probe's
  npm 11.20.0 install skipped them too (openclaw's
  `postinstall-bundled-plugins.mjs`), and nothing it tested failed
  because of that; a host where that script ran may differ.

### Corrections to the plan and the source reading

- `strictMessageKeys` must stay **false**: it strips every message to
  role and content, which drops `tool_calls` and `tool_call_id` and
  breaks the round trip.
- The developer role is **off** by default for a loopback URL, so the
  host's defaults do not send a late developer message there; with the
  switch on they do (fixture `developer-switch-on`, 400 invalid_request).
  The plugin still declares it off.
- `requiresToolResultName` and `requiresAssistantAfterToolResult`
  default to false; they are not derived from the URL. They are declared
  false anyway.
- With tools, the host sends `tool_choice: "auto"` to a loopback URL even
  when the agent did not ask for a choice; 7.1 accepts it.
- The host enforces the advertised `contextWindow` itself: it estimates
  the input and refuses before sending ("Context window exceeded"), so the
  gateway never sees the request (fixture `advertised-window-host-refusal`).
  The plugin must advertise the larger limit of the effective mode
  (spec 0.49). Transport tier only; the host probe did not reach a window.

### What the real host showed (host probe, macOS)

From [`REPORT.md`](../evidence/2026-10-27-openclaw-mac-probe/REPORT.md)
and the captured requests; each fact names its run in `requests/`.

- Every switch run declared its switches on a config-file provider
  (`models.providers.relaymind-capture`). The one run whose model a plugin
  supplied (p1-plugin-hello-storedkey) declared none and sent
  `stream_options`, so whether the host honors switches on a
  plugin-supplied model (catalog or `resolveDynamicModel`, the way the
  RelayMind plugin will supply it) is **not_run**. #136 must show it on the
  wire before it claims compatibility (spec 0.49).
- `strictMessageKeys: true` strips `tool_calls` from the assistant message
  and `tool_call_id` from the tool message (b2-readfile-compat); the
  gateway's validator rejects that turn as invalid_request. With the base
  switches the linkage is intact (c2-readfile-compat-base7). `compat.ts`
  keeps it false, as the transport tier already found.
- The host's `openclaw config validate` accepted all ten switches it was
  given and rejects an unknown key. Three keys `compat.ts` also declares
  (`supportsPromptCacheKey`, `requiresStringContent`, `supportsTools`)
  were not in the host config; they are checked against the pinned
  package's schema only (`compat.test.ts`).
- The host rewrites the tool call IDs it replays: the capture server sent
  `call_probe_1`, and the host sent `callprobe1` back in both the
  assistant `tool_calls[].id` and the tool message's `tool_call_id`
  (c2-readfile-compat-base7, d2-readfile-nostream). The gateway's
  same-request check passes, but the gateway issues `call_<run_id>_<n>`
  and records that string as the proposal ID, so nothing may match the
  host's next request to the earlier proposal by the exact string
  (spec 0.49; the handling is #137's). Only underscore removal was
  observed.
- Every request has `model`, `messages`, `stream`, eleven `tools`,
  `tool_choice: "auto"`, and `max_completion_tokens` (even with no
  switches). `stream_options: {include_usage: true}` is sent to a loopback
  URL unless `supportsUsageInStreaming: false`. `strict`,
  `reasoning_effort`, `store`, `prompt_cache_key`, `parallel_tool_calls`,
  `response_format`, `temperature`, and the developer role never appeared.
  A tool message never carries `name`, and no assistant message follows
  tool results, with or without those two switches.
- A response without `usage` is accepted silently, streamed and buffered.
  The host sends `stream: false` only when the model sets
  `params.streaming: false` (d1, d2).
- The user message is not the raw prompt: the host puts a timestamp in
  front and a `Runtime: agent=main | session=... | host=... | ...` line
  after it. Any prompt-identity check (#139) must allow for it.
- A loopback `baseUrl` was never refused, for a config provider or a
  plugin provider.
- A model in the host config needs `name`; without it validation fails.
- Plugin seams for #136 to #138: `wrapStreamFn` headers reach the wire;
  `resolveTransportTurnState` is called on this transport (`transport:
  "stream"`, attempt 1) and its headers are sent; headless `agent exec`
  finds a plugin model only through `resolveDynamicModel`; `catalog.run`
  gets no abort signal; SIGINT closes the upstream request about 150 ms
  later (exit 130). These are recorded here and used by those issues.

### Field table

Categories: **supported** (section 7.1 accepts what the host sends),
**switch** (a declared host switch keeps the host inside 7.1; named),
**adaptation** (the plugin does something on the host side),
**rejection** (the gateway refuses it by name and the host sees a
failure), **revision** (a gateway spec revision would be needed). The
revision column is empty: nothing here needs a gateway contract change.
Evidence is a fixture name in `transport/`, or "source" where only the
pinned source was read.

| Area | Field or behavior | Category | Switch or handling | Evidence |
| --- | --- | --- | --- | --- |
| Text | system prompt role | switch | `supportsDeveloperRole: false`, `reasoning: false`: always `system` | text-streamed |
| Text | runtime context after the first user message | switch | `supportsDeveloperRole: false`: sent as `user`; with the switch on, `developer` is 400 invalid_request | runtime-context, developer-switch-on; host: no developer message in any run |
| Text | user and assistant string content; assistant `content: null` with `tool_calls` | supported | | text-streamed, tool-round-trip |
| Text | text content parts | supported | `requiresStringContent: false`; 7.1 joins text parts | source |
| Text | image or video parts | switch, rejection | `input: ["text"]`; a non-text part is unsupported_parameter | source |
| Text | `reasoning_content`, `reasoning_details` on assistant messages | switch | `reasoning: false`; sent only for thinking blocks or DeepSeek-style providers | source |
| Text | extra message keys | switch | `strictMessageKeys: false` (true drops tool fields, and the next turn is invalid_request); no other key was sent | tool-round-trip; host: b2-readfile-compat, c2-readfile-compat-base7 |
| Text | user message decorated by the host (timestamp before, runtime line after) | supported | accepted as user text; the gateway never sees the raw prompt | host: every run |
| Tools | `tools[].function` name, description, parameters | supported | the host sorts tools by name; schemas unchanged | tool-round-trip |
| Tools | `strict` on a function | switch | `supportsStrictMode: false`: no key | tool-round-trip |
| Tools | `tool_choice` | supported | `auto` sent with tools; none, required, and named are 7.1 values | tool-round-trip |
| Tools | `parallel_tool_calls: true` from operator params | rejection | 400 unsupported_parameter, named; the plugin documents it and never rewrites it | operator-parallel-tool-calls |
| Tools | tool call IDs and arguments | supported | the transport preserves IDs; the real host removes `_` from a replayed ID, the same in the call and the result, so the same-request check passes; no exact-string match across requests (#137) | tool-round-trip; host: c2-readfile-compat-base7, d2-readfile-nostream |
| Tools | tool message `name` | switch | `requiresToolResultName: false` (the host sent none either way) | tool-round-trip; host: c2-readfile-compat-base7 |
| Tools | synthetic assistant after tool results | switch | `requiresAssistantAfterToolResult: false` (the host inserted none either way) | source; host: c2-readfile-compat-base7, b2-readfile-compat |
| Tools | host executes the call | supported | once, by the host; the gateway has no tool.started or tool.finished | tool-round-trip |
| Stream | `stream_options: {include_usage: true}` | switch | `supportsUsageInStreaming: false`; without it 400 unsupported_parameter | host-default-loopback, text-streamed; host: a1-hello-nocompat, a2-readfile-nocompat, c1-write-allow (sent); c2-readfile-compat-base7, d1-hello-nostream (absent, base switches), b1-hello-compat (absent, all ten switches); config provider only, a plugin-supplied model with the switch is not_run |
| Stream | streamed and buffered responses | supported | role chunk, content or tool-call chunks, finish chunk, `[DONE]`; the host streams unless the model sets `params.streaming: false` | text-streamed, text-buffered, tool-round-trip; host: d1, d2 |
| Stream | no `usage` in chunks or body | adaptation | the host accepts it silently and records zeros it did not receive; the plugin must report usage as unknown (#138) | text-streamed; host: b1 (all ten switches), d1 (base switches) |
| Stream | SSE comments (early-header keep-alives) | supported | counted as liveness, not content (source); early_headers is off by default | source |
| Stream | streamed error events | not exercised | the buffered backend errors before the first byte; live answer streaming is not advertised | |
| Request | `reasoning_effort` | switch | `supportsReasoningEffort: false`, `reasoning: false`; host default with reasoning on is 400 | host-default-reasoning; host: never sent with reasoning off |
| Request | `store` | switch | `supportsStore: false` (store false would be accepted) | text-streamed |
| Request | `prompt_cache_key` | switch | `supportsPromptCacheKey: false` | text-streamed |
| Request | `response_format` json | switch, rejection | `supportsJsonSchemaResponseFormat: false`; a requested json format is unsupported_parameter | source |
| Request | token field | switch | `maxTokensField: "max_completion_tokens"`, value from the entry's `maxTokens`; over the gateway's limit is 400 invalid_request | token-limit; host: `max_completion_tokens` in every run |
| Request | `temperature`, `top_p` | supported | only when the agent sets them; applied per 7.1 | source |
| Request | `frequency_penalty`, `presence_penalty`, `seed`, `stop` | rejection | sent only when the agent sets them; nondefault values are unsupported_parameter | source |
| Errors | 400, 401, 422 JSON errors | supported | the host ends with `stopReason: "error"` and the gateway message, never empty success; error-class mapping is #138 | host-default-loopback, stale-token, oversized-local-only, oversized-both |
| Auth | `Authorization: Bearer` | supported | from the host credential (`apiKey`); never recorded | all |
| Auth | stale token after a restart | supported | 401 unauthenticated; the reconnect instruction is #136 | stale-token |
| Modes | `X-RelayMind-Execution-Mode` | adaptation | sent from the model entry's headers | external-only, oversized-local-only |
| Cancel | host abort signal | supported | the request closes; the run ends cancelled with cancel_requested; no run ID reached the host | abort-buffered; host: SIGINT closed the request about 150 ms later (s1-sigint, capture server only) |
| Limits | loopback base URL | supported | the host never refused it | host: every run |
| Limits | over the local limit in auto | supported | handoff.started reason context_limit, external answer | oversized-auto |
| Limits | over the local limit in local_only | rejection | 422 needs_external | oversized-local-only |
| Limits | over both limits | rejection | 400 context_limit_exceeded | oversized-both |
| Limits | advertised context window | adaptation | the host refuses before sending; advertise the larger limit of the mode | advertised-window-host-refusal |

### Baseline frozen for #136 to #138

- Installation and runtime: `openclaw` and `@openclaw/ai` 2026.10.7 (exact
  pins), Node `>=24.22.1 <25 || >=26.12.1` (the host's own range), ESM.
- Request fixtures: `transport/` above; the host captures stay in
  `docs/evidence/2026-10-27-openclaw-mac-probe/requests/` (not copied
  into `host/`).
- Timeout and abort: the host's abort signal closes the HTTP request; the
  gateway cancels the run it owns (section 7.4). No host-side timeout is
  set by the transport tier (`timeoutMs` unset); #138 maps 504
  deadline_exceeded.
- Retry and idempotency: the transport sends `x-stainless-retry-count: 0`
  and runs with `maxRetries: 0`; one attempt, no Idempotency-Key. Any
  bounded retry is #138's, under section 7.5. On the real host
  `resolveTransportTurnState` is called per model request (attempt 1);
  whether its turn ID stays the same on a retry is not_run.

### Request size against the local limit

Measured only as an upper bound. `scripts/openclaw_request_size.py` read
the 26 captured host chat requests
([`docs/evidence/2026-10-27-openclaw-request-size-mac-probe-utf8.json`](../evidence/2026-10-27-openclaw-request-size-mac-probe-utf8.json)):

| Item | Value |
| --- | --- |
| Rejected by the gateway's validator | 19: 18 sent without the switch (`stream_options`, unsupported_parameter), 1 the strictMessageKeys turn (invalid_request) |
| Measured (sent with the switches) | 7 requests, 2 or 4 messages, 11 tools |
| Size, UTF-8 bytes of the prompt fields | 37,544 to 37,841 (median 37,576) |
| Local limit | 8192 tokens (`qwen3-0.6b-q8_0` manifest `ctx_size`) |
| Fits by the bound | 0 of 7 proved; all 7 undecided |
| Tokenizer count | not_run (needs llama-server with the pinned model) |

A byte count bounds the token count from above, so a request over 8192
bytes may still fit; the bound cannot decide these requests, and the
report counts them as undecided, never as needs_external. At a ratio of
three to five bytes per token, which is an assumption and not a
measurement, 37,500 bytes would be about 7,500 to 12,500 tokens, on both
sides of the limit, so only the tokenizer count can decide. If the host
turns do not fit, auto sends every OpenClaw turn external and local_only
answers needs_external, unless the local context is raised. The system prompt alone is about 26,700
characters in the stored, sanitized config-provider captures (REPORT.md
counts 29,942 for the same runs; the probe's sanitizer replaced local
paths with shorter placeholders, so the stored captures, and the byte
sizes above, are likely smaller than what the host sent: about 40,450
body bytes as received); a plugin model that declared `contextWindow` and
`maxTokens` got a shorter one (about 20,000 characters stored, the probe's
p1 run; the cause was not investigated). Because the stored captures are
smaller than what the host sent, a tokenizer count on them is only a lower
bound on each host request (`--sanitized`): it can prove a request is over
the limit, never that it fits. The deciding count runs on fresh captures
as the host sent them. That count is the number #139's local-value
hypothesis needs; the operator steps are in
`integrations/openclaw/README.md`.

## Approaches considered

| Approach | Fit and cost | Decision for M9 |
| --- | --- | --- |
| Configure a custom provider URL only | Fast compatibility probe using the existing endpoint, but leaves token setup, advertised capabilities, protocol adaptation, and diagnostics to the operator. | Useful in #135; does not fulfill the installable plugin objective alone. |
| Installable provider plugin plus the existing gateway | Reuses client-owned execution and OpenClaw's tool loop; requires a small separate TypeScript package and host transport qualification. | Recommended scope, reflected in the revised milestone. |
| Expose managed RelayMind runs as OpenClaw tools or an agent runtime | Can delegate whole installed skills, but adds a second execution loop, native resume/approval bridging, and more cancellation and ownership rules. | Deferred; not needed for the initial provider integration. |

## Existing foundations and gaps

The relevant code is `api/chat_request.py`, `api/chat.py`,
`orchestrator/client_loop.py`, and the client-owned, SDK, streaming,
idempotency, and cancellation tests. Sections 7.1 to 7.4 remain the API
contract; compatibility work must not silently relax it.

| Surface | Current RelayMind behavior | M9 work |
| --- | --- | --- |
| Tools | Client-owned mode exposes one validated call; it executes no caller tool. Managed mode rejects a host tool catalog. | Select only the client-owned alias; qualify schemas, IDs, results, and the host's approvals with sandbox tools. |
| Requests | A strict text-only Chat Completions subset; no `parallel_tool_calls: true`, `strict: true`, `stream_options`, or arbitrary provider parameters. | Declare the host's per-model compatibility switches so its defaults stay inside the subset (see the verified table above). Capture the pinned host's real payloads. Reject unsupported requests explicitly; revise the spec first if widening is needed. |
| Streaming | The client backend validates and buffers a whole generation before replaying SSE; early headers are optional. | Handle SSE framing and error events without claiming live token forwarding or counting headers as useful output. |
| Routing | Local inference, fallback before output, explicit external-only, and no-local operation exist. No managed skill selection or prefetch runs for this alias. | Preserve those behaviors. Managed RM27 placement is not automatically applied by `client_loop.py`; do not promise it through the plugin. |
| Context | Host conversations and tool schemas can exceed the small local input limit; `/v1/models` reports no limits. | Measure captured request sizes against the local limit in #135. Test large tool catalogs and history; preserve bounded fallback or explicit context errors without increasing budgets silently. Advertise only configured limits, or omit them. |
| Authentication | The loopback gateway generates a fresh token at each launch. | Use the host credential mechanism; provide reconnect instructions after restart and never log credentials. |
| Cancellation and retries | Chat disconnect cancels its owned run; each tool round trip is a new run. Idempotency deduplicates an identical inference request. | Propagate abort, preserve stable retry identity, prevent output replacement, and distinguish inference retry from host tool execution. |
| Evidence | The native trace records actual calls and per-model usage; compatible responses omit unified usage. | Correlate host requests with all returned run IDs; preserve unknown usage and distinguish a call proposal from actual host execution. |
| Distribution | RelayMind is Python; an OpenClaw native plugin uses the host's JavaScript runtime (Node 24.22.1 or later below 25, or 26.12.1 or later, at the pinned release). | Ship a separate package with built entry points, locked dependencies, declared compatibility, and a repeatable installation test. |

The gateway remains operator-started. Automatic process management, a remote
public listener, managed-run bridging, multimodal support, parallel host
calls, and a replacement agent runtime are outside this milestone. The
OpenClaw host must share the gateway's loopback network environment; Docker
or WSL2 networking needs explicit qualification, not an implied support claim.

## Backlog and completion evidence

The six existing open issues have no discussion to migrate. Retain their
numbers and mark their former scope in the revised bodies:

1. #135: pin the host and SDK, capture protocol fixtures, record the
   compatibility switches and request sizes, qualify the seam.
2. #136: build provider registration, catalog with declared switches and
   limits, configuration, and token setup.
3. #137: support text and complete host-tool round trips over buffered SSE.
4. #138: cancellation, error-class mapping, bounded retries, and trace
   correlation.
5. #145: installable artifact and CI (after #136), platform qualification
   and operator docs (after #138).
6. #139: live end-to-end evaluation against direct external and external-only
   baselines, plus auto and no-local operation.

The removal of the demo UI's workflow steps placeholder is #173, a
cleanup of the retired scope that does not wait for the plugin.

The workflow runner, YAML schema, demo packages, steps UI, and plan benchmark
are removed from the future deliverables. RM30 and AC23 are retired without
reusing their identifiers. RM15/AC14 and the already implemented fixed
parallel demo remain; deleting that API is a separate behavior change.
Legacy schema fields and the existing UI placeholder do not establish
workflow-package support.

M9 closes on AC24, reproducible installation, and honest live evidence.
The evaluation must expose compatibility overhead and local-model failures,
not assume that routing through RelayMind improves OpenClaw. Measure complete
host tasks, including tools and every inference round trip. Preserve the
existing frozen skill/tool benchmarks and their results. No performance gain
or platform support is claimed until the corresponding run is recorded.
