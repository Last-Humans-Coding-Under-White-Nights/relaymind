# RelayMind Specification for Spec Driven Development

Version 0.9 | 22 October 2026 | Proposed implementation baseline

RelayMind is a local AI gateway that lets a small model complete bounded tasks with skills and tools, and transfers complex work to an external model. This specification defines the prototype behavior, interfaces, acceptance criteria, and evidence required before making performance or quality claims. It is intended for engineers and coding agents implementing the prototype.

SDD means Spec-Driven Development here. MUST and MUST NOT identify acceptance requirements; SHOULD identifies a preferred implementation. Numerical budgets and evaluation thresholds below are proposed product decisions, not measured model capabilities. No prototype benchmarks have been run for this specification.

## 1 Product objective and scope

The prototype MUST demonstrate that the local model can select a skill, choose tools and arguments, consume actual execution results, and finish a simple task without an external model call. It MUST also demonstrate transfer of completed work to an external model, with visible attribution of skill selection, tool proposals, execution, and final output.

The agreed requirements are English interaction, with Chinese interaction supported on a best-effort basis, macOS and Windows execution, replaceable models, an OpenAI-compatible entry point, shared skills and tools, and observable routing. Qwen3-0.6B is the initial local candidate; the earlier approximate 300M size is no longer a constraint. The 500 ms first-response objective remains an experimental target.

The implementation baseline adds these decisions: a single-user local service, Python with FastAPI, a separate llama.cpp server process, SQLite execution records, trusted local skill packages, and a small deterministic demo tool service. The external provider and model are operator configuration, not hard-coded product dependencies.

| Priority | Included behavior |
| --- | --- |
| P0 | Local skill selection and tool loop; clarification; escalation; external completion; model adapters; trace UI; cancellation; simulated destructive action with confirmation; API conformance; evaluation (English required, Chinese best-effort) |
| P1 | One bounded parallel workflow with independent read-only branches; exact routing cache experiments |
| Outside this prototype | Token-level speculative decoding; replacing already streamed answer text; universal agent compatibility; arbitrary shell execution; production database deletion; production multi-tenancy; native desktop installers; general PDF ingestion; automatic skill installation; MCP connector marketplace |

The complete demo includes P0 and the bounded parallel workflow. Routing-cache experiments may follow the uncached baseline. General PDF processing is excluded because the earlier PDF example illustrated latency, while the agreed prototype now centers on skills and tools.

## 2 Verified foundations and limits

The following statements are externally grounded. Other implementation behavior in this document is a RelayMind design decision.

| Evidence | Established fact | Consequence for RelayMind |
| --- | --- | --- |
| S1 | The Qwen3-0.6B card documents non-thinking mode and an agentic example using this model identifier. | Use it as a candidate. This does not establish reliable bilingual skill execution or a 500 ms response on our hardware. |
| S2 and S3 | Tool calling requires model output formatting and application-side parsing and execution. llama.cpp documents tool-calling configuration. | Pin and test the model, chat template, parser, quantization, and runtime together. |
| S4 | llama.cpp documents Windows builds and a Metal backend for macOS. | Use a common runtime family, with separate platform qualification. |
| S5 and S6 | Chat Completions defines streamed deltas and function-call messages; the application executes custom functions and returns results. | Distinguish gateway-owned tools from client-owned tools. The supported contract is explicitly bounded below. |
| S7 | Agent Skills defines a SKILL.md package and progressive loading. Its allowed-tools field is experimental. | Reuse the document shape; enforce permissions in RelayMind configuration, not merely in skill prose. |
| S8 | Provider prompt caching can reuse matching prompt prefixes under provider-specific rules. | A local instruction cache is a different mechanism and cannot guarantee faster remote inference. |
| S9 | Speculative decoding uses draft proposals and target-model verification during decoding. | Model handoff over Chat Completions is not token-level speculative decoding. |

Selecting a skill means selecting and loading instructions into model context. It does not mean that the model executes a skill file as a program. A skill can suggest actions; only the gateway or a client tool executor performs them.

No universal speedup, cost saving, minimum RAM figure, or accuracy level is asserted. The selected GGUF file, actual hardware, provider location, context length, and tool latency materially affect results.

## 3 Components and ownership

| Component | Responsibility |
| --- | --- |
| API layer | Validate requests; expose the supported chat contract and native run API; authenticate callers. |
| Orchestrator | Own state transitions, budgets, routing, handoff, cancellation, and the final-answer owner. |
| Model adapter | Translate normalized messages, tools, and generation options into provider-specific requests and events. |
| Skill registry | Validate packages; expose descriptions; load selected instructions and approved resources. |
| Tool registry and executor | Validate arguments and permissions; execute registered functions; record outcomes. |
| Run store | Persist ordered events, tool outcomes, pending approvals, and versioned execution state in SQLite. |
| Demo UI | Display model decisions and execution evidence; collect clarification and confirmation; compare modes. |

The model produces proposals. The orchestrator decides whether a proposal is admissible. The executor owns side effects. The UI renders evidence; it does not infer hidden model reasoning. Before external dispatch, operator-configured policy MUST permit sending the effective prompt and selected tool data to that endpoint. Credentials and fields classified local-only MUST never be included; if required evidence cannot be shared, return needs_external with the disclosure restriction.

The local gateway binds to loopback by default and requires a generated application token. Local inference also binds to loopback. External credentials remain server-side in environment variables or an OS credential store. Native Windows operation MUST NOT require WSL or Docker. The initial platform matrix is Apple Silicon macOS and x86-64 Windows; other architectures are optional qualification targets.

## 4 Execution requirements

### 4.1 Routing and local autonomy

**RM01** The default policy MUST be local-first. A request successfully handled locally MUST cause zero external inference calls, including routing or validation calls. The trace MUST make this verifiable.

**RM02** In managed execution, the local model MUST select among an advertised skill catalog and produce a normalized routing decision. Supported decisions are local, external, clarify, and parallel. Parallel is admissible only for an enabled fixed workflow. Unknown skills, invalid fields, or unavailable capabilities MUST NOT trigger tool execution. Client-owned tool mode uses capability and context checks without claiming managed skill selection.

**RM03** Local execution MUST be bounded. Initial defaults are one routing generation, up to three subsequent local generations, two executed tool calls, one format-repair generation within those limits, and a four-second active local budget including routing and tools. A model generation can return a tool proposal or a final answer. The limits are configuration values and MUST be recorded in each run.

**RM04** Before answer output is committed, escalation MUST occur on exhausted local budget, unrepaired output format, a skill marked external-required, unavailable local capability, oversized context, or an explicit valid escalation proposal, provided external dispatch is permitted. Otherwise return needs_external. After output is committed, failure terminates the stream as specified in section 7.3. The trace MUST use a reason code. A model's self-reported confidence MUST NOT be treated as a calibrated probability or the sole routing safeguard.

In the MVP the only explicit escalation proposal is the routing decision route=external with reason code complex_skill (section 8), produced in the routing state. The external route with reason code parallel_workflow (section 4.4) is not a proposal: the gateway produces it when the selected skill's parallel workflow is not admissible, as one of the gateway-detected triggers above (unavailable capability). The local model has no escalation action during local_running, so there a stop comes only from the gateway-detected triggers above. A model-visible escalation action for local_running is out of scope for the prototype; if one is added later it is an internal control action and MUST NOT be exposed as an application tool (RM09).

**RM05** A task with missing required identifiers MUST request clarification. Resource names such as database IDs MUST come from the request, established conversation context, or tool results. The system MUST NOT invent an object identifier to make a tool call possible.

The router receives caller instructions, relevant conversation context, and compact skill descriptions. The 500 ms experiment limits this complete input to 2,048 tokens measured by the local tokenizer. Longer inputs bypass the fast experiment and go to the external path if permitted; they MUST NOT be silently truncated or counted as short-context successes. No full document or full tool history is loaded into the fast router by default.

### 4.2 Skills and instruction loading

**RM06** A skill MUST have a stable name, description, version, content hash, and an instruction body. The model selects the skill by name. The gateway then loads its body and the permitted tool schemas. Every load MUST emit a skill.loaded event containing the name, version, and hash.

The package uses SKILL.md with Agent Skills name and description frontmatter. RelayMind-specific settings live in a separate registry manifest: version, supported languages, local eligibility, allowed tool IDs, resource paths, completion predicate ID, and optional parallel-workflow ID. Completion predicates are registered gateway validators, not executable text supplied by the model. This is a documented RelayMind extension, not a claim of complete compatibility with every skill host. [S7]

The registry manifest is the file `skill.manifest.json` in the package directory, next to SKILL.md. It is a JSON object with a `schema_version` field, following the `*.manifest.json` convention of the model artifact manifests in section 6. The skill's content hash covers both files.

Skill locations are operator configuration, not code. The registry loads from an ordered list of skill roots. The default list is `["skills/"]`, the `skills/` directory of the checkout. An operator MAY replace the list, for example with a single isolated directory in a container or a test fixture, or extend it with further local directories such as a team-shared folder; a configured list replaces the default, it does not add to it. Each root is scanned one level deep, and every subdirectory of a root is a candidate package. A candidate that lacks SKILL.md or `skill.manifest.json`, or whose files fail validation, is a startup error, never skipped silently. Plain files in a root, such as a README, are ignored. Skill names MUST be unique across all roots; a duplicate is a configuration error, not a precedence rule. Each run records the configured roots and the skill catalog offered to the router in the run.accepted payload (section 8); this is the version snapshot that RM07 preserves. Roots are local filesystem paths. Fetching packages from a URL or a remote registry is automatic skill installation, which section 1 leaves out of the prototype.

The MVP uses at most eight short skill descriptions per router prompt and one selected skill body at a time. Resource reads are restricted to the selected package directory. Skills cannot install dependencies, execute bundled scripts, change permissions, or alter model configuration. Any required executable operation must be an explicitly registered tool.

**RM07** Updating a skill or its tool schema MUST change its version or content hash and invalidate dependent routing-cache entries. Existing runs retain their original version snapshot; a required missing version causes an explicit failure rather than silent substitution.

### 4.3 Tools and execution boundaries

**RM08** Registered tools MUST declare a name, input schema, output schema, execution timeout, side-effect classification, handler, and authorization rule. Tool availability is the intersection of the skill allowlist, operator policy, and caller permissions. Names in a client-supplied tools array do not register executable handlers.

**RM09** The gateway MUST validate the full tool name and arguments before execution, reject undeclared fields where the schema disallows them, and validate returned data. Partial streamed argument JSON MUST never execute. Internal control actions such as skill selection and escalation MUST NOT be exposed as ordinary application tools.

**RM10** The executor MUST persist a logical operation ID before a side effect. Replays use the recorded result for that operation. A changed argument set is a new proposal and requires revalidation. A timed-out write with an unknown outcome MUST be marked unknown_outcome and MUST NOT be retried automatically. This provides controlled deduplication; it does not claim universal exactly-once delivery to external services.

**RM11** Mutating demo actions MUST require confirmation tied to the run, tool, canonical arguments, target, and current state version. Confirmation expires after five minutes and is single-use. Changing the target or arguments invalidates it. A textual yes in unrelated chat history is insufficient authorization.

**RM12** Tool output is data and MUST NOT override gateway rules or grant new tool permissions. The prototype MUST test malicious instructions embedded in tool output. Logs and traces MUST redact configured secret fields.

### 4.4 Handoff and parallel operation

**RM13** Handoff MUST preserve the original user request, applicable caller instructions, selected skill version, structured tool results, completed operation IDs, pending work, permitted tools, language, and remaining budget. Both models use the same logical tool registry. Local hidden reasoning, KV cache, and model-specific token IDs are not transferred.

Handoff creates a new external request using normalized messages and evidence. Local assumptions are identified as proposals, never recast as tool facts. If evidence exceeds the external context budget, the run fails with context_limit_exceeded unless a skill defines a tested bounded evidence-selection method. The MVP does not silently summarize away evidence or permissions.

The gateway builds the envelope from the run's recorded state, not from model memory. The tool operations in the run store supply the completed operation IDs and the structured results, each with its source reference and truncation flag. Rejected local tool proposals and rejected local draft answers go in a separate local_proposals list, and every entry is labeled as a proposal. Pending work lists the permitted tools of the selected skill that have no completed operation, then the final answer. The envelope is sent as normalized messages: the gateway rules and the envelope fields that are not tool output in a system message, then the original conversation, then each completed operation as a tool call with its tool result message, so tool output stays data (RM12).

Before dispatch the gateway measures the whole external request, messages and tool schemas, against the context limit of the external adapter. It uses the adapter's tokenizer when it has one. Otherwise it uses the UTF-8 byte length of the request, which is an upper bound on the token count, so a request that does not fit is never passed. A request over the limit ends the run as failed with error code context_limit_exceeded and reason code context_limit, before any external call. The same check runs again before every external generation, because tool results accumulate. Handoff needs external dispatch to be permitted: an effective execution mode other than local_only, a configured external model, and room in the entire-run limits. Otherwise the run ends as needs_external (RM04). A tool operation of the run that is still in flight, or that ended unknown_outcome, blocks handoff (RM14): the run ends as needs_external with reason code tool_failure, and the partial evidence stays in the trace. A stop that the external stage could not resolve either does not hand off: a local proposal of a mutating tool that needs the confirmation flow (RM11), or a selected skill none of whose allowed tools is permitted to the caller (RM08). The run ends as needs_external with reason code capability_missing and makes no external call. Two more stops end without a handoff. An admitted route=parallel ends as needs_external with reason code parallel_workflow while the parallel workflow (RM15) is not implemented, because one external actor is not that workflow. An unavailable local model, a missing local capability or a failed local generation, ends as needs_external with reason code capability_missing, because no operator policy permits degraded mode yet (section 11).

The external stage. On handoff the run enters external_running and records handoff.started. From then on the local actor holds no dispatch authority and cannot publish the final answer (RM14). The external model may propose tools from the envelope's allowed tools. The gateway validates each proposal (RM09), checks identifier provenance (RM05), and runs it through the same executor as a local proposal. A proposal that repeats a completed operation, the same tool with the same canonical arguments, gets the recorded result and runs no handler (RM10). A mutating tool is not executed in the external stage until the confirmation flow exists (RM11); the proposal is rejected back to the model as data. When a skill is selected, the external answer must meet its completion predicate. The answer is buffered, validated, and then published as answer.delta by the external model, which is then the final-answer model (RM16, RM20). External generations are limited by external_generations (default 6) and by external_seconds for each generation (default 30, section 11), inside the entire-run limits. The per-stage local limits do not apply to the external stage. When no skill was selected, for example in external_only, the envelope carries skill null, the external model gets the skill catalog descriptions and the permitted tools of the offered skills, and the external model does not load a skill body in this version.

**RM14** Only one actor may dispatch a given logical tool operation or publish the final answer. Escalation revokes the local actor's dispatch authority. Late results may be retained as evidence, but cannot trigger additional local side effects. In-flight writes with uncertain outcomes block automatic continuation that could repeat them.

**RM15** Parallel mode MUST initially support one trusted workflow with independent read-only branches. For example, the local branch collects current health while the external branch drafts a diagnostic checklist from the user request. The external draft is provisional; after both branches finish, a new external call combines the observations and checklist. The extra call and its latency MUST be measured.

A running Chat Completions generation is not assumed to accept injected tool results. The implementation either waits for dependencies before starting it or makes a subsequent request. The default local-first route does not launch an external model merely to hide latency.

The parallel workflow is enabled by operator configuration, which names the enabled workflow ID; a skill joins it by declaring that ID as the parallel-workflow ID in its registry manifest (section 4.2). Both are required. route=parallel is admissible only when the run's effective execution mode is parallel_demo, or auto where operator policy explicitly allows the workflow in auto; by default auto does not run it, because the workflow ends in an external synthesis call and the local-first route does not launch an external model to hide latency. local_only and external_only never produce route=parallel. When the selected skill declares a parallel-workflow ID that configuration or the effective mode does not admit, the gateway routes it external with reason code parallel_workflow, and local_only then returns needs_external (section 7.4). The gateway produces that decision itself from the manifest, the configuration, and the effective mode, like the other gateway-produced decisions in section 8; the model's contribution is the skill selection (RM02), not the choice between parallel and external. A decision that reaches the validator with route=parallel in a mode or configuration that does not admit the workflow is therefore invalid output (section 8), never a parallel_workflow escalation. In parallel_demo a run whose configured workflow is unavailable, because configuration enables no workflow or the external endpoint the workflow needs is not available, ends as failed with error code workflow_unavailable; the compatible chat endpoint reports it as HTTP 503 with that code, as for an unavailable required model (section 11).

**RM16** A completed run MUST identify the final-answer model and every actual model call. Data provenance MUST be preserved: an unavailable health service cannot be reported as healthy, and a drafted checklist cannot be reported as a diagnosis supported by logs.

## 5 State and persistence contract

| State | Allowed next behavior |
| --- | --- |
| accepted | Validate configuration and request, then route or fail. |
| routing | Select local, external, clarification, or an approved parallel workflow. |
| local_running | Propose or execute permitted tools, finish, ask, or escalate. |
| external_running | Continue with evidence and remaining permissions, then finish or ask. |
| parallel_running | Execute approved read-only branches, join, and transfer final ownership. |
| waiting_input | Suspend inference and tools; resume on explicit new input. |
| waiting_confirmation | Suspend the proposed mutation; resume only with a valid approval. |
| completed | Terminal success; immutable answer and trace. |
| failed | Terminal failure; retain partial evidence and an error code. |
| cancelled | Terminal cancellation; retain any actual side-effect outcomes. |

**RM17** Each transition MUST be atomic against a run state version. Resume and cancel operations MUST reject stale versions with HTTP 409. No transition from a terminal state may restart execution. A new attempt creates a new run linked to the previous one.

SQLite stores runs, events, tool_operations, approvals, and artifact_refs. Event sequence numbers increase within a run. Approval records retain a token hash, not the plaintext token. The default retention is seven days for traces and demo evidence, with explicit deletion available. Secrets are excluded. Local storage is not claimed to be encrypted by the application.

After a process restart, unfinished runs MUST become failed with interrupted. In-flight mutating operations are reconciled or marked unknown_outcome. The MVP MUST NOT automatically resume interrupted model loops or replay writes. Pending approvals expire on restart.

A denied confirmation ends the run as cancelled with user_denied and performs no mutation. An expired waiting state ends as failed with input_expired or approval_expired. Waiting releases the active execution slot; a resume must reacquire it and may return 429 without consuming the approval.

Clarification and resume. A clarification suspends the run in waiting_input and records the question in clarification.required (section 8). The run's language is fixed by its first RouteDecision and is kept across every resume; the resume input never changes it, so an identifier-only reply does not re-detect the language. When a resumed run routes again, the gateway sets the new RouteDecision's language to the run's language before validation; a different language in the router's output is overridden there, not treated as invalid output (section 8). On resume the gateway appends the clarification question as an assistant turn and the resume input as a user turn to the conversation context, and records the input and the waiting time in a clarification.answered event (section 8). For RM05 identifier provenance only user turns and tool results count; the gateway's own question, which may list valid values, is not provenance. When the clarify decision carries a skill_id, the resumed run continues with that skill: the gateway checks the new input for the missing identifier without a routing generation and moves to local_running, or asks again if the input still lacks it. When the clarify decision carries skill_id null, no skill was chosen, so the resumed run routes again with a routing generation. Either way the resumed run records route.selected for the decision that continues it (section 8): a continuation records the gateway's decision made without a model call, a re-route records the new routing generation's decision. skill.loaded (RM06) is emitted when the run enters local_running; a clarify decision loads no skill body, so the clarification itself emits none. A resume opens a new local stage charged under section 11. A run asks for clarification at most the configured number of rounds (section 11); when a further clarification would be needed, the run ends as failed with error code missing_input.

## 6 Model adapter contract

**RM18** Model identity and provider configuration MUST be independent of orchestration logic. Swapping between two already supported adapters requires configuration changes and restart, not routing-code changes. Adding a new protocol may require a new adapter.

Every adapter exposes capabilities, generate, cancel, and health. Its capability record includes text streaming, tool calling, structured routing output, context limit, supported reasoning controls, usage reporting, and tokenizer availability. The orchestrator requests capabilities explicitly and rejects unsupported combinations.

For local inference (RM01, RM18), the llama.cpp adapter and runtime startup health polls MUST bypass environment and system proxies when the configured base URL names `localhost` (case-insensitive) or an IPv4 or IPv6 loopback address literal. Other hostnames MUST NOT be treated as loopback through DNS resolution or name matching. Non-loopback endpoints and the external Chat Completions adapter MUST retain normal HTTP client proxy handling. This bypass MUST NOT change process-wide proxy settings.

Normalized generation outputs are text_delta, tool_call, usage, completed, and error. A tool_call is emitted to the executor only after complete argument parsing. An adapter may use native function calling or a validated structured envelope for internal local control; the selected mechanism is visible in configuration and tests. Syntactically valid JSON is not evidence that the selected action is correct.

For Qwen3, non-thinking mode MUST be configured through the pinned runtime's supported reasoning controls and verified by a smoke test. A /no_think string alone is not the qualification criterion. Exact runtime switches MUST come from the selected build's documentation. Model-specific templates and parsing remain inside the adapter. Non-thinking is the default and the configuration used for the latency experiment; an operator MAY enable thinking mode through the selected runtime's supported launch and request controls. For llama.cpp build 11785 (`8baabd53b`), launch with `--reasoning off --reasoning-format deepseek`; the adapter derives `chat_template_kwargs.enable_thinking=false` from that launch setting and explicitly requests `reasoning_format=deepseek`. The operator may set `enable_thinking=true` per request (or launch with `--reasoning on`). The HTTP template boolean remains supported even though its command-line use is deprecated. Non-empty `reasoning_effort` alone does not enable thinking on a server started with reasoning off. Reasoning output MUST NOT appear as answer content in either mode, and latency results from the two modes MUST NOT be mixed. [S1, S3, S10, S11]

The artifact manifest MUST record model source, revision, file SHA-256, quantization, tokenizer and template hashes, runtime version and commit, launch settings, and license metadata. Q4 and Q8 are comparison candidates, not prequalified defaults. The selected artifact becomes the default only after the English tool tests; Chinese tool test results are reported when run but do not block the choice. External configuration similarly records endpoint, model identifier, supported parameters, and provider-returned version information when available.

## 7 Public API contract

### 7.1 Compatibility boundary

**RM19** The service MUST expose GET /v1/models and POST /v1/chat/completions. The compatibility claim is limited to the subset below. It does not include Responses, Realtime, file upload, vision, audio, or all provider extensions.

| Request element | MVP behavior |
| --- | --- |
| model | Required virtual alias: relaymind or relaymind-client-tools. Actual providers appear in the native trace. |
| messages | Text system, developer, user, assistant, and tool messages; validate supported ordering and tool-call references. |
| stream | Boolean; streaming and non-streaming supported. |
| n | Only 1. |
| max_completion_tokens | Positive final-response budget; internal routing and tool-loop budgets are separate. |
| max_tokens | Legacy alias for that budget; reject if supplied together with max_completion_tokens. |
| temperature and top_p | Applied to final generation if supported; routing settings remain controlled. Reject unsupported nondefault values. |
| tools and tool_choice | Client-owned mode only; function tools; auto, none, required, or a named supplied function. |
| parallel_tool_calls | Client-owned mode accepts false or omission; true is rejected in MVP. |
| response_format | Omitted or text only. Structured final-output modes are explicitly deferred. |
| execution_mode | Not a body field on this endpoint. Rejected as unsupported_parameter like any other unsupported field; the mode is selected with the request header below. |
| Other behavior-changing fields | Reject with a named unsupported_parameter error; never silently ignore them. |

Request headers are a separate surface from the JSON body:

| Request header | MVP behavior |
| --- | --- |
| Authorization | Required. Bearer application token (section 3). A missing or invalid token returns 401. |
| X-RelayMind-Execution-Mode | Optional. Carries the native execution_mode value (auto, local_only, external_only, parallel_demo); absent means auto. The same operator policy applies as for the native run API. An unknown value, or a value that is not admissible for the selected alias (section 7.2), is rejected with 400 and code invalid_execution_mode; a value the policy forbids is rejected with 403 and code execution_mode_forbidden. The mode is never downgraded silently. The model alias still selects tool ownership; the header selects only where inference runs. |
| Idempotency-Key | Optional; see section 7.4. |

The standard response uses one assistant choice with a stable request ID and virtual model alias. Streaming uses chat.completion.chunk content or tool-call deltas and the appropriate terminal finish_reason followed by [DONE]. Already emitted answer text is append-only. The schema foundation is Chat Completions; this table defines RelayMind's narrower supported surface. [S5]

### 7.2 Managed and client owned tools

The relaymind alias selects managed execution. It uses the installed skills and tool handlers. Supplying a nonempty tools array, tool_choice, or client tool-result messages is rejected to prevent ambiguous ownership. Managed tool activity is visible through native events, not through client-executed tool_calls. The chat response contains the answer, clarification, or an explanation that confirmation is pending.

The relaymind-client-tools alias returns tool_calls to the caller. The caller executes them and includes matching tool messages in the next request. The hub MUST NOT execute those tools or certify that caller-reported results are trustworthy. Gateway-installed business tools are disabled for this alias. A caller may supply procedural instructions; managed skill execution is demonstrated through the managed alias. Local routing may fall back to external generation before output is committed, but external fallback is not permitted after partial client tool calls have been exposed. [S6]

The client-owned alias MUST honor tool_choice, preserve tool call IDs, and reject tool results without matching calls. No global tool-execution authorization guarantee is claimed for tools executed by third-party clients.

The X-RelayMind-Execution-Mode header applies to both aliases. With the managed alias it selects the route for skill execution. With the client-owned alias, external_only sends the request to the external model directly, and local_only keeps the request on the local model; the rule that external fallback is not permitted after partial client tool calls are exposed is unchanged. local_only returns needs_external with both aliases when the local model cannot produce the answer or, for the client-owned alias, the tool calls. parallel_demo is admissible only with the managed alias, because its fixed workflow runs gateway tools and managed skills (RM15, section 9), which the client-owned alias disables; parallel_demo with relaymind-client-tools is rejected with 400 and code invalid_execution_mode. This is a request validation failure, like a tools array with the managed alias, not an operator policy decision, so it is not a 403.

On this endpoint needs_external is an error before any answer content is sent, because RM04 raises it before output is committed. The response is HTTP 422 with an error object carrying code needs_external, a message, the reason code, and the run ID; partial results stay in the native trace of that run. 422 is used because the request was valid and authorized and no service was unavailable: the task cannot complete under the effective execution mode, and a retry with the same request would give the same result.

### 7.3 Streaming and early feedback

**RM20** The compatible stream MUST contain only answer content or client-owned tool-call deltas, with protocol framing. Internal progress and a generic acknowledgment MUST NOT be injected into it to improve reported latency. The demo's early model-selected status is delivered through the native event stream. A standard chat client without RelayMind event support may therefore wait until a valid answer or tool call is ready.

Once answer streaming begins, final-answer ownership is fixed. A subsequent provider error fails the stream; the gateway MUST NOT silently append another model's replacement answer. A later correction is a distinct message or run.

In managed mode, tool-planning output is buffered and validated. After the skill's explicit completion predicate is met, or a validated finalize proposal is accepted, the orchestrator starts a final generation with tools disabled. This final generation counts against the model-call budget. Its text can be streamed without risking a later tool call in the same generation. A skill whose evidence predicate is unmet cannot finalize merely because the model requests it.

Usage is optional in the compatible response when a truthful unified count is unavailable. The native trace MUST retain usage per model and call, including cache counts when the provider returns them. Counts across different tokenizers MUST NOT be presented as if they belonged to one model.

### 7.4 Native run API

**RM21** The demo MUST use the following RelayMind-specific endpoints, backed by the same orchestration service as chat:

| Endpoint | Contract |
| --- | --- |
| POST /relay/v1/runs | Accept messages and execution_mode; return 202 with run_id, state_version, and events_url. Modes: auto, local_only, external_only, parallel_demo. |
| GET /relay/v1/runs/{id} | Return current state, output, pending question or approval, metrics, and version. |
| GET /relay/v1/runs/{id}/events | SSE events with increasing sequence IDs; Last-Event-ID replays retained later events without rerunning work. |
| POST /relay/v1/runs/{id}/resume | Accept expected_version and either input (the input text, recorded in clarification.answered, section 8) or approval_id plus token and approve boolean, matching the waiting state. Return 202. A body that does not match the waiting state, including a missing or blank input in waiting_input, is rejected with 400 and code invalid_resume_input; the run keeps waiting and its version is unchanged (section 11). |
| POST /relay/v1/runs/{id}/cancel | Accept expected_version; request cancellation; return current state. |
| GET /healthz and GET /readyz | Process liveness and readiness of the configured execution mode; model warm state is explicit. |

A native run takes no model alias and always uses managed execution.

Execution mode is constrained by operator policy. local_only never calls externally and returns needs_external when the task cannot complete locally. external_only bypasses local inference while using the same managed tools, skill catalog, and permissions. In external_only no local routing generation runs: the gateway records a RouteDecision it produced itself, route=external, skill_id=null, reason_code=execution_mode, in the same way as the bypass decision for an oversized router input (section 4.1), and skills_offered in run.accepted lists the managed catalog the external model receives. How the external model selects among those skills is part of the handoff (RM13); every load still emits skill.loaded (RM06). parallel_demo fails explicitly with error code workflow_unavailable when its configured workflow is unavailable (section 4.4). The compatible chat endpoint accepts the same values through the X-RelayMind-Execution-Mode request header (section 7.1), so chat and native runs share one mode enumeration and one policy check, and a standard chat client can run the auto versus external_only comparison without the native API. Both endpoints record the requested and effective mode in the run.accepted payload (section 8).

Chat responses include X-RelayMind-Run-ID for trace correlation. Confirmations for managed writes require the native resume endpoint or demo UI. Ordinary clients can read the pending-confirmation message but cannot approve it solely by sending a free-form yes. Clarification can be resumed natively, or submitted as a new chat request with complete context.

All execution-creating POSTs accept an Idempotency-Key. Reusing it with the same authenticated caller and request hash never creates new work; a changed payload returns 409. Native creation returns the existing run reference. Chat returns a stored completed response, replayed as compatible chunks if streaming; an unfinished duplicate returns 409 with its run ID. Keys remain valid for the seven-day run retention window. The key does not authorize repeating a completed side effect. Native SSE disconnects leave the run available for reconnection; a compatible chat disconnect requests cancellation. Cancellation is best-effort for in-flight external work and cannot undo an already committed action.

## 8 Internal data and event contract

The following objects define required fields. Implementations MUST publish equivalent versioned JSON Schemas and validate them at boundaries; examples are illustrative data, not execution evidence.

| Object | Required fields and constraints |
| --- | --- |
| RouteDecision | schema_version=1; route enum; skill_id or null; language en or zh; reason_code; clarification or null. Unknown fields rejected. |
| ToolProposal | proposal_id; actor local or external; tool_name; arguments object; skill_version; run_state_version. |
| ToolResult | operation_id; tool_name; status succeeded, failed, or unknown_outcome; validated data or null; error or null; observed_at; duration_ms; source_ref; truncated boolean. |
| HandoffEnvelope | schema_version=1; run_id; original_messages (role and text, in order); skill (name, version, content_hash) or null; completed_operations (operation_id, tool_name, arguments, status); evidence (for each completed operation: operation_id, source_ref, truncated, and the ToolResult); local_proposals (each labeled as a proposal); pending_work; allowed_tools; reason_code (an RM04 reason code); remaining_budgets; language en or zh. Unknown fields rejected. |
| handoff.started payload | reason_code (the RM04 reason for the escalation); model (the external model); skill (name, version, content_hash) or null; operation_ids (the completed operations the envelope carries); allowed_tools; request (size, limit, and method, tokenizer or utf8_bytes, of the measured external request). |
| TraceEvent | schema_version=1; run_id; seq; timestamp; elapsed_ms; event_type; actor; payload. |
| run.accepted payload | requested_execution_mode (the header or native value, or null when absent); effective_execution_mode (the mode applied after defaults and policy); model_alias (the virtual alias from the chat request's model field, relaymind or relaymind-client-tools, or null for a native run (section 7.4)); budgets (the section 11 limits in force, RM03); skill_roots (the configured roots in order); skills_offered (array of name, version, and content_hash for every skill in the catalog offered to the router, or in external_only to the external model (section 7.4), empty for the client-owned alias). |
| clarification.required payload | question (the clarification text in the run's language); language; reason_code; skill_id (the clarify decision's skill_id: the skill whose required identifier the gateway found absent, or null for a clarification the model requested, per the cross-field rules below); missing (a non-blank name of the input the run waits for: the argument name of the absent required identifier when skill_id is set, or skill_selection when skill_id is null, because the model requested clarification before any skill was chosen and the gateway knows no narrower input); waiting_seconds (the section 11 waiting limit in force). |
| clarification.answered payload | input (the resume text, redacted per RM12); missing (copied from the clarification.required it answers); waited_ms (monotonic time from clarification.required to the accepted resume, RM25). |
| run.completed payload | final_answer_model (RM16); model_calls (every actual model call: call_id, model, actor, purpose; RM16); external_calls (how many of those calls went to an external model, RM01); active_ms and waiting_ms (non-negative integers, milliseconds from the monotonic clock, RM25: active execution time and the sum of the recorded waits, reported separately, section 12.1). |
| run.failed payload | error_code (the terminal error code, section 11); reason_code (the RM04 reason code when the failure has one, for example needs_external or missing_input); model_calls, external_calls, active_ms, and waiting_ms as in run.completed. |

RouteDecision fields MUST also satisfy these cross-field rules:

- route=clarify requires a non-blank clarification. Every other route requires clarification=null.
- A non-null skill_id MUST name a skill in the catalog offered to the router for this run (skills_offered in the run.accepted payload).
- In managed execution, route=local requires a non-null skill_id. route=parallel requires a non-null skill_id whose registry manifest declares a parallel-workflow ID that operator configuration enables, and an effective execution mode that admits the workflow (section 4.4, RM15); in local_only and external_only route=parallel is invalid. route=external allows skill_id null; when set, it carries the selected skill into the handoff (RM13). route=clarify carries a skill_id only when the gateway found a required identifier of that skill absent (RM05); a clarification the model requests carries skill_id null, and its clarification.required names skill_selection as the missing input.
- In client-owned mode the offered catalog is empty, so skill_id MUST be null; route=parallel is invalid (section 7.2).
- reason_code follows the route: route=local carries simple_skill; route=clarify carries missing_input; route=parallel carries parallel_workflow; route=external carries the reason for leaving the local path: complex_skill, parallel_workflow, context_limit, execution_mode, or capability_missing. local_budget, invalid_output, and tool_failure are escalation reasons recorded in the trace (RM04), not routing decisions.
- language: the first RouteDecision of a run fixes the run's language; every later decision of that run carries the run's language, which the gateway sets before validation (section 5), so the validator never sees a conflicting value.
- A decision the gateway produces itself, the oversized-input bypass (section 4.1), the external_only decision (section 7.4), and the external route of a selected parallel skill whose workflow is not admitted (section 4.4), follows the same rules.

A decision that breaks any rule is invalid output (reason code invalid_output): it never triggers a tool and is handled under RM03/RM04. The RouteDecision validator at the routing boundary owns these rules and checks every decision before route.selected is recorded; the skills_offered membership check reads the run.accepted payload, whose shape the run.accepted validator guarantees. The validator checks the decision it is given; the language rule holds because the gateway sets the language of every later decision (section 5), so a resumed routing generation cannot flip it.

Reason codes include simple_skill, complex_skill, missing_input, local_budget, invalid_output, tool_failure, context_limit, capability_missing, parallel_workflow, and execution_mode (a route fixed by the effective execution mode, section 7.4). They are operational labels, not generated chains of thought.

**RM22** Required events are run.accepted, route.selected, skill.loaded, model.started, model.finished, tool.proposed, tool.started, tool.finished, clarification.required, clarification.answered, confirmation.required, handoff.started, answer.delta, run.completed, run.failed, and run.cancelled. clarification.answered follows every resume that supplies input text and records the input and the waiting time (section 5); run.completed and run.failed report active time and waiting time separately as active_ms and waiting_ms (table above, section 12.1); run.cancelled carries the same timing fields. Cached routes, when enabled, emit cache.hit with the originating version. Every tool event includes its operation or proposal ID and originating model. run.accepted is the first event of a run; its payload is defined in the table above.

Example event:

```json
{
  "schema_version": 1,
  "run_id": "run_demo_001",
  "seq": 7,
  "timestamp": "2026-10-20T03:00:00Z",
  "elapsed_ms": 420,
  "event_type": "tool.proposed",
  "actor": "local",
  "payload": {
    "proposal_id": "proposal_01",
    "tool_name": "get_service_health",
    "arguments": {"service": "staging-api"},
    "skill_id": "check-service-health"
  }
}
```

The 420 ms example is a schema illustration, not a measured latency. Events containing tool results MUST be redacted before persistence and rendering. Large results use artifact references with explicit size and truncation metadata; a truncated result cannot silently satisfy a skill that requires the complete result.

## 9 Demo skills and tools

**RM23** The demo MUST expose real gateway function executions against a clearly labeled sandbox data service. Model decisions must be live in live mode. Recorded replay and deterministic model doubles are separate modes with persistent visible labels.

| Skill | Tools | Expected route |
| --- | --- | --- |
| check-service-health | get_service_health(service) | Local completion from status and observation time. |
| lookup-latest-deployment | get_latest_deployment(service) | Local completion from deployment ID and timestamp. |
| summarize-incident-counts | get_incident_counts(service, window_minutes) | Local summary of returned counts, without inferred causes. |
| investigate-performance | get_latest_deployment, get_metrics, get_recent_logs | Local preparation or immediate escalation; external evidence-based assessment. |
| delete-demo-database | list_demo_databases, delete_demo_database(database_id) | Resolve target, require confirmation, mutate only the sandbox fixture. |
| health-and-diagnostic-checklist | get_service_health | Explicit parallel-demo workflow, followed by external synthesis. |

Tool schemas MUST use explicit service or resource IDs. Time windows, log limits, and service values are bounded. get_recent_logs returns a capped list with timestamp and source. Tool outputs carry fixture scenario and observation metadata. delete_demo_database only modifies a disposable local fixture; it never accepts a production connection string.

The initial tool contract uses service IDs staging-api and payments-api, windows of 15, 60, or 1440 minutes, UTC timestamps, and additionalProperties=false for input objects. These are synthetic demo identifiers. The following tool-specific payloads sit inside the common ToolResult envelope from section 8.

| Tool | Input fields | Output data |
| --- | --- | --- |
| get_service_health | service, required enum | service; status enum healthy, degraded, unavailable; checked_at |
| get_latest_deployment | service, required enum | service; deployment_id and deployed_at, nullable together; revision, nullable when no deployment exists |
| get_incident_counts | service; window_minutes | service; window_minutes; total, critical, warning, info as nonnegative integers; category sum equals total |
| get_metrics | service; window_minutes | service; samples array containing timestamp, p95_latency_ms, error_rate from 0 to 1, and nonnegative request_count |
| get_recent_logs | service; limit integer from 1 to 100 | service; entries with timestamp, level, and message; truncated flag |
| list_demo_databases | Empty object | databases array with database_id, display_name, and integer revision |
| delete_demo_database | database_id from lookup; expected_revision | database_id; deleted boolean; resulting fixture revision |

All listed input fields are required unless explicitly described otherwise. A health tool's unavailable status is a successful observation of the service state; failure to contact the tool itself produces a failed ToolResult. The deletion handler compares expected_revision atomically and returns a conflict without mutation if the fixture changed. The pending approval binds that revision as well as the database ID.

Each skill includes English examples and completion criteria. Chinese examples are best-effort: they are kept where they exist, but they are not required. For health checks, the final response must preserve the exact returned health state and time. For diagnostics, it must distinguish observations from hypotheses and may report insufficient evidence. Native Chinese review is not required. Evidence must state honestly when Chinese material has not been natively reviewed (RM16). Family-level multilingual claims do not replace task evaluation.

The UI shows the original prompt, selected mode, active model, loaded skill, tool name and arguments, execution result, escalation reason, final answer, and timing. The comparison view runs equivalent inputs in auto and external_only modes against reset fixtures. A user can cancel a run and inspect failed operations.

## 10 Caching policy

**RM24** Skill parsing and template loading MAY be cached immediately by content hash. Routing-result caching MUST be disabled for the first model benchmark. If enabled later, it uses exact request and context matching, a five-minute TTL, and a maximum of 256 entries.

The routing cache key includes the effective messages, language, caller permissions, skill and tool hashes, model revision, routing prompt version, and policy version. Only local read-only route choices are eligible. No semantic cache, authorization cache, mutation route cache, or cross-user reuse is included. A cached route still performs current authorization and executes required fresh tools; it is not a cached answer.

Prefix or KV caching is controlled by the inference runtime or provider and is recorded separately. Reusing a prompt string in SQLite does not imply reuse of inference computation. Cache-on results MUST be labeled and reported separately from the uncached baseline. [S8]

## 11 Budgets and failure behavior

Initial engineering defaults are listed below. They are configurable limits, not predicted timings.

| Budget | Default |
| --- | --- |
| Routing input for fast experiment | At most 2,048 local tokens |
| Routing output | At most 128 tokens |
| Local execution | Four seconds active time; routing plus three further generations; at most two tool executions |
| Entire run | 60 seconds active time; at most eight model generations including routing and repair; at most six executed business tools |
| Tool timeout | Two seconds unless a tool declares a shorter timeout |
| External generation | At most 30 seconds per generation and six generations (external_seconds, external_generations), also bounded by the remaining run budget |
| Simultaneous requests | One active run in the baseline; reject additional work with 429 rather than hide queue time |
| Waiting state | Five minutes per wait, recorded as waiting_seconds and excluded from active execution time |
| Clarification rounds | Two per run; a third needed clarification ends the run as failed with missing_input |

The Local execution row is a per-stage limit. A run's first local stage starts at routing; a resume after a clarification opens a new local stage with the full per-stage limits, so a resumed run that routes again has its routing generation (section 5). The Entire run row bounds the sum of all stages, and waiting time counts toward none of them. The two model branches in parallel_demo are allowed within a single active run. The local-first per-stage limits do not constrain external-only baseline routing, but the total run limits apply to every mode. A skill that requires more local work is escalated instead of quietly increasing budgets.

Validation failures return 400, including a resume whose body does not match the waiting state (code invalid_resume_input, section 7.4); unauthenticated requests 401; denied access 403; missing runs 404; stale state or idempotency conflict 409; a task that cannot complete under the effective execution mode 422 with code needs_external (section 7.2); overload 429; unavailable required model, or in parallel_demo an unavailable workflow (code workflow_unavailable, section 4.4), 503; exhausted deadlines 504 before streaming starts. Errors use a stable code, message, and run reference where available. Internal errors must omit credentials.

After streaming starts, an unrecoverable failure is signaled as an error and connection termination, and recorded as run.failed. It MUST NOT produce a successful finish marker. The compatible client behavior for this failure is part of integration tests because SDK handling may vary.

An unavailable local model may route directly to external only when policy permits; the trace records degraded mode. An unavailable external model does not make a complex request locally solvable. If external use is disabled, return needs_external with partial results. Read-only retries are limited to one for a transient failure within all budgets; model repair also counts toward generation limits.

An external stage that cannot finish ends the run as failed with error code external_failed. Causes are an adapter error, an unreachable endpoint, an answer that does not meet the completion predicate within external_generations, and an exhausted entire-run limit. The partial evidence stays in the trace, no local answer replaces the external one, and no result is invented (AC07). The run.failed event carries an RM04 reason code: the reason the external stage stopped when it stopped on one, otherwise the reason for the escalation. Evidence over the external context budget is a separate code, context_limit_exceeded (RM13). The HTTP mapping of both codes belongs to the API work.

## 12 Performance and quality evaluation

### 12.1 Metrics

**RM25** Timings MUST use a monotonic clock for elapsed durations and UTC for event timestamps. Record both server arrival and UI submission; never subtract timestamps from unsynchronized hosts.

| Metric | Definition |
| --- | --- |
| First transport event | First received byte or status event; reported as transport latency only. |
| First model action | First validated model-produced route, clarification, or tool proposal available to the client. |
| First useful content | First displayed result supported by task evidence, or the first necessary clarification question. Generic acknowledgments do not qualify. |
| First answer token | First nonempty answer-content delta in the compatible stream. |
| Completion latency | Request to successful final answer; active time and human waiting time reported separately. Waiting time is the sum of every recorded wait of the run, reported as waiting_ms in run.completed and run.failed (section 8), never taken from client timestamps: each clarification contributes the waited_ms of its clarification.answered event, so a run with two clarification rounds (section 11) sums two values; each confirmation contributes the monotonic time from confirmation.required to the resume that approved or denied it. A wait that ends the run (input_expired, approval_expired, user_denied) counts toward the waiting_ms of its terminal event. |
| Local completion rate | Simple-task cases successfully completed with zero external inference calls, divided by all simple-task cases. |
| Tool correctness | Correct tool and semantically correct arguments against fixture expectations; schema validity reported separately. |
| Escalation quality | Missed necessary escalations and unnecessary escalations, reported separately by class and language. |
| External usage | Actual requests and per-model input and output tokens, including abandoned parallel calls. |

The 500 ms hypothesis is p95 first model action at or below 500 ms for the warmed, short-context, concurrency-one experiment with routing-result caching disabled. The UI displays this as model action latency, never as completed-answer latency. Report whether it is met on each tested machine; do not average a slow Windows result into a fast Mac result. A missed target is a finding, not grounds to fabricate an early response.

### 12.2 Reproducible experiment

The correctness corpus contains 40 English scenarios: 12 simple, 10 complex, 6 ambiguous, 6 requiring mutation confirmation, and 6 adversarial or fault scenarios. Simplified Chinese versions of the scenarios are optional and best-effort; when present, results are reported per language and never merged into the English results. Each case includes fixture state, acceptable tools and arguments, required evidence, expected permissions, and a grading rubric. Freeze the corpus before comparing candidates and keep development examples separate.

Run auto, external_only, and local_only against the same fixture snapshots. Baselines use the same skills, tool descriptions, execution rules, and output criteria. Select identical relevant external data where the route permits it. Count provider calls; do not infer cost savings from latency. Monetary estimates require actual provider rates and a dated rate source and are optional.

For the latency experiment, use 12 short English prompts, including routing, clarification, and simple tool tasks. Perform three warm-up requests per model configuration, then ten measured runs per prompt in randomized order. Record errors and timeouts rather than dropping them. Chinese prompts may be added on a best-effort basis and are reported per language. Report p50, p95, sample count, and timeout fraction. Report cold startup separately, including model load time. Exact-route caching stays off; runtime prefix-cache behavior is explicitly recorded.

Record OS version, CPU, GPU, RAM, backend, thread settings, model hashes, quantization, context settings, sampling parameters, runtime and application revisions, external endpoint region if known, and test time. Shared provider load remains a potential source of variability. Actual hardware is filled into the benchmark report after execution; no reference machine performance is assumed here.

### 12.3 Gates and experimental targets

Hard acceptance gates are all deterministic contract and authorization tests passing, successful live local skill-and-tool traces for all three simple demo skills in English (Chinese live results are reported but do not block acceptance), correct handoff without duplicate completed operations, a working model substitution test, and a reproducible benchmark report from both target platforms.

Proposed model-quality targets are at least 80 percent successful local completion on the English simple subset, at least 95 percent correct tool-and-argument proposals on that subset, and no missed mandatory confirmation in the bounded test corpus. Chinese results, when present, are reported per language against the same targets but do not block acceptance. Report exact numerators and denominators. Passing a small corpus does not establish a production reliability guarantee.

The 500 ms target and quality percentages are experimental outcomes, not pre-existing facts. If a target fails, retain the evidence, qualify the candidate as unsuitable for that claim, and compare another model or narrower skill scope. Do not silently substitute a larger model while continuing to label results Qwen3-0.6B.

## 13 Acceptance scenarios and traceability

The following scenarios define the implementation acceptance suite. Model doubles verify orchestration deterministically; real models verify capability separately. Passing doubles alone does not satisfy the live-demo requirements.

| Test | Given and when | Required result | Requirements |
| --- | --- | --- | --- |
| AC01 | A simple health request with a valid service in English (Chinese requests are reported best-effort and do not block acceptance) | Local skill selection, real sandbox tool call, correct final status, zero external calls | RM01 to RM03, RM06, RM16, RM23 |
| AC02 | A complex diagnostic request after local metrics collection | External request contains the original task and completed evidence; completed calls are not replayed | RM04, RM13, RM14 |
| AC03 | A request omits the target service, then the caller resumes with it | Clarification names the missing input; the resumed run continues with the chosen skill in the run's language; no guessed target or mutation | RM03, RM05, RM17, RM22 |
| AC04 | The model proposes an unknown tool or invalid arguments | No execution; bounded repair or explicit escalation; recorded reason | RM02, RM08, RM09 |
| AC05 | Delete a sandbox database, then approve, deny, replay, expire, or alter arguments | Only a matching fresh approval executes once; other cases do not mutate | RM10, RM11, RM17 |
| AC06 | A tool result contains instructions to expose credentials or invoke an unallowed tool | Tool permissions stay unchanged and secrets are not exposed | RM08, RM12 |
| AC07 | Local generation exceeds its budget or external generation fails | Bounded escalation or explicit failure; no invented result | RM03, RM04, RM16 |
| AC08 | Switch to a second qualified local model and a second compatible external endpoint | Same orchestrator and skill packages; configuration changes only for supported adapters | RM18 |
| AC09 | Standard SDK sends text with stream true and false | Valid supported responses, stable IDs, correct final markers, no internal progress in content | RM19, RM20 |
| AC10 | A client-owned function call is followed by its tool result | Valid call IDs and finish reason; caller executes; gateway does not execute the function | RM08, RM19 |
| AC11 | Native client reconnects to events or resubmits an idempotency key | Events replay in order; no duplicate inference or side effect | RM17, RM21, RM22 |
| AC12 | Cancel during generation and restart during a write | Dispatch stops; actual or unknown outcomes remain recorded; no automatic write replay | RM10, RM14, RM17 |
| AC13 | Change a skill or permission with caching enabled | Cache invalidates; current permissions apply; baseline remains cache-off | RM07, RM24 |
| AC14 | Execute the fixed parallel workflow | Independent read-only branches are attributable; synthesis uses results in a later call | RM14, RM15, RM22 |
| AC15 | Repeat the benchmark on both platforms | Reproducible manifest and separate platform results, and separate language results where Chinese prompts are present, including failures | RM18, RM25 |
| AC16 | Unsupported parameters, oversized context, or stale resume version | Explicit documented error; no silent feature downgrade or lost context | RM17 to RM21 |

## 14 Delivery and SDD change control

Implementation deliverables are the gateway source, pinned dependencies, setup instructions for both platforms, model artifact manifest, skill packages, registered demo tools, native demo UI, versioned schemas, API examples, deterministic acceptance suite, evaluation corpus (English, with best-effort Chinese), and benchmark report. Platform verification must use actual target machines; this specification does not claim those checks have already happened.

An implementation plan should proceed through adapter and protocol qualification, the local skill-and-tool loop, handoff and confirmation, observability and UI, the bounded parallel workflow, and evaluation. This ordering is dependency guidance, not a substitute for a reviewed task plan.

Every implementation task and acceptance test MUST reference requirement IDs from this document. Changes to tool ownership, approval rules, API behavior, supported platforms, or evaluation definitions require a specification revision before implementation changes. Implementation-only refactoring may preserve the specification when observable behavior is unchanged.

The release record MUST distinguish implemented behavior, passing deterministic tests, measured live-model results, and unmet experimental targets. A demo with prerecorded decisions must not be presented as autonomous live model execution.

## 15 Sources

Primary sources checked on 20 October 2026. These references support platform and protocol facts; they do not validate RelayMind performance. Mutable upstream branches must be pinned in the implementation manifest.

- S1 Qwen3-0.6B model card: https://huggingface.co/Qwen/Qwen3-0.6B
- S2 Qwen function calling documentation: https://qwen.readthedocs.io/en/latest/framework/function_call.html
- S3 llama.cpp function calling documentation: https://github.com/ggml-org/llama.cpp/blob/master/docs/function-calling.md
- S4 llama.cpp build documentation: https://github.com/ggml-org/llama.cpp/blob/master/docs/build.md
- S5 OpenAI Chat Completions API reference: https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create
- S6 OpenAI function calling guide: https://developers.openai.com/api/docs/guides/function-calling
- S7 Agent Skills specification: https://agentskills.io/specification
- S8 OpenAI prompt caching guide: https://developers.openai.com/api/docs/guides/prompt-caching
- S9 Leviathan et al, Fast Inference from Transformers via Speculative Decoding: https://arxiv.org/abs/2211.17192
- S10 llama.cpp server documentation: https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md
- S11 llama.cpp b11785 controls (checked 21 October 2026): https://github.com/ggml-org/llama.cpp/blob/8baabd53b77d1c48c6ef8d3b620436292f3f6231/tools/server/README.md and request parser https://github.com/ggml-org/llama.cpp/blob/8baabd53b77d1c48c6ef8d3b620436292f3f6231/tools/server/server-common.cpp
