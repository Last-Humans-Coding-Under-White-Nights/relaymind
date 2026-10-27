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
