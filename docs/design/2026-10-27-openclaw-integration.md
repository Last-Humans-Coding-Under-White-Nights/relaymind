# M9: OpenClaw integration feasibility

27 October 2026. Planning analysis; no plugin has been built or run.
The normative contract is specification 0.46, RM33, AC24, sections 7.5
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
| Requests | A strict text-only Chat Completions subset; no `parallel_tool_calls: true`, `strict: true`, or arbitrary provider parameters. | Capture the pinned host's real payloads. Adapt supported defaults or reject unsupported requests explicitly; revise the spec first if widening is needed. |
| Streaming | The client backend validates and buffers a whole generation before replaying SSE; early headers are optional. | Handle SSE framing and error events without claiming live token forwarding or counting headers as useful output. |
| Routing | Local inference, fallback before output, explicit external-only, and no-local operation exist. No managed skill selection or prefetch runs for this alias. | Preserve those behaviors. Managed RM27 placement is not automatically applied by `client_loop.py`; do not promise it through the plugin. |
| Context | Host conversations and tool schemas can exceed the small local input limit. | Test large tool catalogs and history; preserve bounded fallback or explicit context errors without increasing budgets silently. |
| Authentication | The loopback gateway generates a fresh token at each launch. | Use the host credential mechanism; provide reconnect instructions after restart and never log credentials. |
| Cancellation and retries | Chat disconnect cancels its owned run; each tool round trip is a new run. Idempotency deduplicates an identical inference request. | Propagate abort, preserve stable retry identity, prevent output replacement, and distinguish inference retry from host tool execution. |
| Evidence | The native trace records actual calls and per-model usage; compatible responses omit unified usage. | Correlate host requests with all returned run IDs; preserve unknown usage and distinguish a call proposal from actual host execution. |
| Distribution | RelayMind is Python; an OpenClaw native plugin uses the host's JavaScript runtime. | Ship a separate package with built entry points, locked dependencies, declared compatibility, and a repeatable installation test. |

The gateway remains operator-started. Automatic process management, a remote
public listener, managed-run bridging, multimodal support, parallel host
calls, and a replacement agent runtime are outside this milestone. The
OpenClaw host must share the gateway's loopback network environment; Docker
or WSL2 networking needs explicit qualification, not an implied support claim.

## Backlog and completion evidence

The six existing open issues have no discussion to migrate. Retain their
numbers and mark their former scope in the revised bodies:

1. #135: pin the host and SDK, capture protocol fixtures, qualify the seam.
2. #136: build provider registration, catalog, configuration, and token setup.
3. #137: support text and complete host-tool round trips over buffered SSE.
4. #138: cancellation, errors, bounded retries, and trace correlation.
5. #145: installable artifact, CI, platform qualification, and operator docs.
6. #139: live end-to-end evaluation against direct external and external-only
   baselines, plus auto and no-local operation.

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
