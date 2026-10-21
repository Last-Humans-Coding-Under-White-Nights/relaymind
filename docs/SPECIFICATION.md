# RelayMind Specification for Spec Driven Development

Version 0.18 | 22 October 2026 | Proposed implementation baseline

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

Confirmation flow. A registered mutating tool declares the required input argument that names its target (confirmation_target, for example database_id); a mutating tool without one cannot be confirmed, so the gateway rejects its proposals back to the model as data. A proposal of a mutating tool passes the same checks as any proposal (RM05, RM08, RM09) and is then not executed. The gateway moves the run to waiting_confirmation, issues a random single-use token, stores only its SHA-256 hash with the approval (section 5), and records confirmation.required (section 8). The plaintext token is returned once to the caller that started or resumed the run and is never written to the store, the trace, or a log. A target under a configured secret field is redacted like the arguments (RM12). The approval binds the run, the tool, the canonical arguments (which include the fixture revision for a deletion, section 9), the target, and the run's state version in waiting_confirmation. Only a resume that presents the approval ID and the token, with the run at that version, can use it, and a use of it, approving or denying, consumes it. A proposal for different arguments or another target is a new proposal and needs its own confirmation. A proposal that repeats an operation of the run that already has a recorded outcome gets that outcome and asks for no confirmation, because it runs no handler (RM10). The sandbox handler still compares expected_revision atomically (section 9), so a fixture that changed after the proposal is a conflict with no mutation.

**RM12** Tool output is data and MUST NOT override gateway rules or grant new tool permissions. The prototype MUST test malicious instructions embedded in tool output. Logs and traces MUST redact configured secret fields.

### 4.4 Handoff and parallel operation

**RM13** Handoff MUST preserve the original user request, applicable caller instructions, selected skill version, structured tool results, completed operation IDs, pending work, permitted tools, language, and remaining budget. Both models use the same logical tool registry. Local hidden reasoning, KV cache, and model-specific token IDs are not transferred.

Handoff creates a new external request using normalized messages and evidence. Local assumptions are identified as proposals, never recast as tool facts. If evidence exceeds the external context budget, the run fails with context_limit_exceeded unless a skill defines a tested bounded evidence-selection method. The MVP does not silently summarize away evidence or permissions.

The gateway builds the envelope from the run's recorded state, not from model memory. The tool operations in the run store supply the completed operation IDs and the structured results, each with its source reference and truncation flag. Rejected local tool proposals and rejected local draft answers go in a separate local_proposals list, and every entry is labeled as a proposal. Pending work lists the permitted tools of the selected skill that have no completed operation, then the final answer. The envelope is sent as normalized messages: the gateway rules and the envelope fields that are not tool output in a system message, then the original conversation, then each completed operation as a tool call with its tool result message, so tool output stays data (RM12).

Before dispatch the gateway measures the whole external request, messages and tool schemas, against the context limit of the external adapter. It uses the adapter's tokenizer when it has one. Otherwise it uses the UTF-8 byte length of the request, which is an upper bound on the token count, so a request that does not fit is never passed. A request over the limit ends the run as failed with error code context_limit_exceeded and reason code context_limit, before any external call. The same check runs again before every external generation, because tool results accumulate. Handoff needs external dispatch to be permitted: an effective execution mode other than local_only, a configured external model, and room in the entire-run limits. Otherwise the run ends as needs_external (RM04). A tool operation of the run that is still in flight, or that ended unknown_outcome, blocks handoff (RM14): the run ends as needs_external with reason code tool_failure, and the partial evidence stays in the trace. A stop that the external stage could not resolve either does not hand off: a selected skill none of whose allowed tools is permitted to the caller (RM08). The run ends as needs_external with reason code capability_missing and makes no external call. Two more stops end without a handoff. An admitted route=parallel ends as needs_external with reason code parallel_workflow while the parallel workflow (RM15) is not implemented, because one external actor is not that workflow. An unavailable local model, a missing local capability or a failed local generation, ends as needs_external with reason code capability_missing, unless the operator policy permits degraded mode (section 11), in which case it hands off like any other stop.

The external stage. On handoff the run enters external_running and records handoff.started. From then on the local actor holds no dispatch authority and cannot publish the final answer (RM14). The external model may propose tools from the envelope's allowed tools. The gateway validates each proposal (RM09), checks identifier provenance (RM05), and runs it through the same executor as a local proposal. A proposal that repeats a completed operation, the same tool with the same canonical arguments, gets the recorded result and runs no handler (RM10). A mutating tool proposed in the external stage goes through the same confirmation flow as a local proposal (RM11, section 5): the run suspends in waiting_confirmation from external_running, and an approved resume returns it to external_running. When a skill is selected, the external answer must meet its completion predicate. The answer is buffered, validated, and then published as answer.delta by the external model, which is then the final-answer model (RM16, RM20). External generations are limited by external_generations (default 6) and by external_seconds for each generation (default 30, section 11), inside the entire-run limits. The per-stage local limits do not apply to the external stage. When no skill was selected, for example in external_only, the envelope carries skill null, the external model gets the skill catalog descriptions and the permitted tools of the offered skills, and the external model does not load a skill body in this version.

**RM14** Only one actor may dispatch a given logical tool operation or publish the final answer. Escalation revokes the local actor's dispatch authority. Late results may be retained as evidence, but cannot trigger additional local side effects. In-flight writes with uncertain outcomes block automatic continuation that could repeat them.

**RM15** Parallel mode MUST initially support one trusted workflow with independent read-only branches. For example, the local branch collects current health while the external branch drafts a diagnostic checklist from the user request. The external draft is provisional; after both branches finish, a new external call combines the observations and checklist. The extra call and its latency MUST be measured.

A running Chat Completions generation is not assumed to accept injected tool results. The implementation either waits for dependencies before starting it or makes a subsequent request. The default local-first route does not launch an external model merely to hide latency.

The parallel workflow is enabled by operator configuration, which names the enabled workflow ID; a skill joins it by declaring that ID as the parallel-workflow ID in its registry manifest (section 4.2). Both are required. route=parallel is admissible only when the run's effective execution mode is parallel_demo, or auto where operator policy explicitly allows the workflow in auto; by default auto does not run it, because the workflow ends in an external synthesis call and the local-first route does not launch an external model to hide latency. local_only and external_only never produce route=parallel. When the selected skill declares a parallel-workflow ID that configuration or the effective mode does not admit, the gateway routes it external with reason code parallel_workflow, and local_only then returns needs_external (section 7.4). The gateway produces that decision itself from the manifest, the configuration, and the effective mode, like the other gateway-produced decisions in section 8; the model's contribution is the skill selection (RM02), not the choice between parallel and external. A decision that reaches the validator with route=parallel in a mode or configuration that does not admit the workflow is therefore invalid output (section 8), never a parallel_workflow escalation. In parallel_demo a run whose configured workflow is unavailable, because configuration enables no workflow, the external endpoint the workflow needs is not available, or this build does not implement the workflow yet (issue #30), ends as failed with error code workflow_unavailable; the compatible chat endpoint reports it as HTTP 503 with that code, as for an unavailable required model (section 11). The gateway checks the first two causes before the routing generation, with reason code parallel_workflow in run.failed, so no model is called for a run that cannot succeed; it ends the run the same way when the run routes parallel, or routes external with reason parallel_workflow, in parallel_demo. The other modes keep their rules above: auto hands such a run off or, in local_only, returns needs_external.

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

SQLite stores runs, events, tool_operations, approvals, idempotency_keys, and artifact_refs. Event sequence numbers increase within a run. Approval records retain a token hash, not the plaintext token. The default retention is seven days for traces and demo evidence, with explicit deletion available. Secrets are excluded. Local storage is not claimed to be encrypted by the application.

After a process restart, unfinished runs MUST become failed with interrupted. In-flight mutating operations are reconciled or marked unknown_outcome. The MVP MUST NOT automatically resume interrupted model loops or replay writes. Pending approvals expire on restart.

Restart recovery. The gateway runs recovery once at startup, before it accepts any run; it never runs while a run of this process is active. Recovery finds every run that is not in a terminal state, in accepted, routing, local_running, external_running, parallel_running, waiting_input, or waiting_confirmation, and ends each one in a single store transaction, so a run is recovered completely or not at all. In that transaction it first settles every tool operation of the run that is still pending (RM10). The MVP has no source to reconcile a write against, so a pending mutating operation is finished as unknown_outcome, and a pending read-only operation is finished as failed; both carry the error code tool_interrupted, and an operation whose tool is no longer registered is treated as mutating. Each settled operation gets a tool.finished event with the recorded ToolResult, replayed false, and recovered true. Recovery never runs a handler, never retries an operation, and never offers a settled operation again as work to do: a later proposal of the same call replays the recorded outcome (RM10), and an unknown_outcome blocks handoff (RM14). Next it expires every approval of the run that is still pending, by moving its expiry to the recovery time, so no token can be consumed (RM11). Last it moves the run to failed with error code interrupted and records run.failed with error_code interrupted, interrupted_state (the state the run was in), the operations it settled, model_calls rebuilt from the run's model.started events, external_calls, and the measured active_ms and waiting_ms (downtime is not counted, RM25). The failed transition releases the execution slot. A terminal run is never touched, and recovery of a store with nothing unfinished changes nothing, so running it twice is safe. A recovered run is never resumed: a resume of it is a terminal-state error, and a retry is a new run linked to the previous one (RM17). Recovery does not call any model.

A denied confirmation ends the run as cancelled with user_denied and performs no mutation. An expired waiting state ends as failed with input_expired or approval_expired. Waiting releases the active execution slot; a resume must reacquire it and may return 429 without consuming the approval.

Confirmation and resume. A resume in waiting_confirmation (section 7.4) carries approval_id, token, and approve. A body of another shape, such as input text, is 400 invalid_resume_input and the run keeps waiting; a stale version is 409. An approval older than five minutes, checked against the approval record, ends the run as failed with approval_expired, whether the resume approves or denies, and the wait counts toward waiting_ms of the run.failed event. An approval ID or token that does not match the pending approval is 403 with code approval_invalid; the run keeps waiting, nothing is consumed, and nothing is mutated. This check comes first, so a resume without the token neither expires the run nor sees a 429. The approval is consumed in the same store transaction as the resume or the cancel, so a resume or cancel that fails keeps it; an approval whose run moved past its state version is void and no longer pending. A matching resume with approve=false consumes the approval and ends the run as cancelled with reason user_denied; its run.cancelled event carries the approval ID and the model-call and timing fields. A matching resume with approve=true first checks that the execution slot is free (429 otherwise, approval kept), consumes the approval, returns the run to the state that suspended it, and opens a new stage with the full per-stage limits of section 11, inside the entire-run limits, exactly as a resume after a clarification does. The gateway then executes the approved call once; its tool.started event carries the approval ID. The wait counts toward waiting_ms. A replay of a used token cannot execute again: the run has moved on, so the replay is 409 (stale version) or, for a terminal run, a terminal-state error, and the executor would replay the recorded outcome in any case (RM10). The gateway sweeps expired confirmations like expired clarifications.

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
| tools and tool_choice | Client-owned mode only; function tools whose parameters are a valid JSON Schema whose every $ref resolves inside it or to a published meta-schema (an invalid schema is invalid_request; the gateway never retrieves a schema); auto, none, required, or a named supplied function. |
| parallel_tool_calls | Client-owned mode accepts false or omission; true is rejected in MVP. |
| response_format | Omitted or text only. Structured final-output modes are explicitly deferred. |
| execution_mode | Not a body field on this endpoint. Rejected as unsupported_parameter like any other unsupported field; the mode is selected with the request header below. |
| Other behavior-changing fields | Reject with a named unsupported_parameter error; never silently ignore them. |

Request headers are a separate surface from the JSON body:

| Request header | MVP behavior |
| --- | --- |
| Authorization | Required. Bearer application token (section 3). A missing or invalid token returns 401. |
| X-RelayMind-Execution-Mode | Optional. Carries the native execution_mode value (auto, local_only, external_only, parallel_demo); absent means auto. The same operator policy applies as for the native run API. An unknown value, or a value that is not admissible for the selected alias (section 7.2), is rejected with 400 and code invalid_execution_mode; a value the policy forbids is rejected with 403 and code execution_mode_forbidden. The mode is never downgraded silently. The model alias still selects tool ownership; the header selects only where inference runs. |
| Idempotency-Key | Optional; deduplicates a retry of this request (section 7.4). 1 to 255 printable ASCII characters, sent once. |

Request validation (RM19, AC16). Every check below runs before any run is created, so a rejected request has no run and its error carries run_id null. The application token is checked first (401), then the body (400), then the Idempotency-Key header (400), then the execution mode header (400, then 403), then a stored key (409 or a replay, section 7.4), then the execution slot (429).

- The body is a JSON object. A body that is not JSON, not an object, or has a field of the wrong type or an out-of-range value is 400 invalid_request. The error message names the field.
- model is one of the aliases the gateway serves, which GET /v1/models lists. An alias that is not served is 400 invalid_request. The gateway serves relaymind-client-tools only when client-owned execution is configured (RM08, section 7.2), so it never lists an alias it cannot run.
- Every body field that is not in the table above is rejected with 400 unsupported_parameter, and the message names it, with these exceptions: a field set to null counts as omitted; user (a string) is accepted because it changes no behavior; and a field that the table does not list is also accepted when its value is the API default, so a client that sends presence_penalty 0, frequency_penalty 0, logprobs false, or store false is not refused. Message objects have the same rule: a field other than role, content, tool_calls (assistant), and tool_call_id (tool) is unsupported_parameter, and so is a content part that is not of type text. Content is a string or an array of text parts, which the gateway joins without a separator. The legacy functions and function_call fields are unsupported_parameter.
- Idempotency-Key is blank, longer than 255 characters, holds a character that is not printable ASCII, or is sent more than once: 400 invalid_request (section 7.4).
- n is an integer; any value other than 1 is unsupported_parameter. stream is a boolean. max_completion_tokens and max_tokens are positive integers no larger than the operator's limit (default 32,768, a configurable limit, not a claim about any model), and supplying both is invalid_request. temperature is a number from 0 to 2 and top_p is a number above 0 and at most 1; a value outside these ranges is invalid_request. response_format is omitted or {"type": "text"}; json_object and json_schema are unsupported_parameter.
- Sampling and budget values are applied to the final-response generation and to the external generations that produce the answer. An external generation that is offered tools may propose a tool call instead of the answer, so it is part of the tool loop: it takes temperature and top_p but keeps its own token budget, because the final-response budget would cut off the call. Routing, skill, and format-repair generations keep their controlled settings. The operator declares which of max_output_tokens, temperature, and top_p the configured adapters accept. A value for an option that is not accepted is unsupported_parameter, except that the API default (temperature 1, top_p 1) is accepted and not applied. The default is all three accepted.
- messages is a nonempty array. system and developer messages come first, before any other message. The first message after them is a user message, and the last message is a user message (managed alias) or a user or tool message (client-owned alias). An assistant message has text content, tool_calls, or both. In a tool_calls entry, id is a nonblank string that no earlier message used, type is function, and function.arguments is a string holding a JSON object. A tool message follows the assistant message whose tool_calls it answers, directly or after other tool messages for the same assistant message. Its tool_call_id names exactly one of those calls, once; every call of that assistant message is answered before any other message. A tool message with no matching call is invalid_request. user, system, and developer text must not be blank.
- With the relaymind alias (section 7.2), a nonempty tools array, any tool_choice, an assistant message with tool_calls, and a tool message are each 400 invalid_request. parallel_tool_calls true is unsupported_parameter with either alias; false and omission are accepted.
- With the client-owned alias, tools is an array of {"type": "function", "function": {name, description, parameters}} with unique names of 1 to 64 letters, digits, underscores, or hyphens. A tool of another type, or a function with strict true, is unsupported_parameter. tool_choice is auto, none, required, or {"type": "function", "function": {"name": ...}} naming a supplied function; any other value is invalid_request, and so is a tool_choice other than none without tools. Omitted tool_choice means auto.

The standard response uses one assistant choice with a stable request ID and virtual model alias. Streaming uses chat.completion.chunk content or tool-call deltas and the appropriate terminal finish_reason followed by [DONE]. Already emitted answer text is append-only. The schema foundation is Chat Completions; this table defines RelayMind's narrower supported surface. [S5]

Response shape. The request ID is chatcmpl- followed by the run ID, and the model field is the requested alias, never a provider model. The body is object chat.completion, with created, one choice with index 0, an assistant message, and finish_reason stop, or tool_calls for client-owned tool calls. The response omits usage, because no truthful unified count exists across the local and external tokenizers (section 7.3), and omits system_fingerprint. A clarification (section 5) is the assistant content with finish_reason stop, and so is the explanation of a pending confirmation, which names the tool and the run ID and says that the native API or the demo UI approves it. It never contains the approval token. The stream is chat.completion.chunk objects with the same ID and model: a first chunk with the assistant role and empty content, one content chunk for each answer.delta event of the run in order, a final chunk with empty delta and the finish_reason, and [DONE]. The run finishes before the first byte is sent, because managed tool planning is buffered (section 7.3); an error before that is a normal error response with its status. Both forms carry X-RelayMind-Run-ID, and an error response carries it too when the failure has a run. A run that ends cancelled during a chat request, or failed with an error code that section 11 reports as run state (missing_input, input_expired, approval_expired, or interrupted), is 409 run_terminal; the message names that error code, and the run is never reported as a 500.

Binding and token (section 3, RM19). The gateway accepts a bind address only when it is localhost or an IPv4 or IPv6 loopback literal (the rule of section 6); any other address is refused at startup, not downgraded. A bracketed IPv6 literal such as [::1] is bound without its brackets. The application token is generated by the gateway with a cryptographically secure random source at startup, has at least 32 characters, and is required on every endpoint of this section. No caller or configuration supplies it. An unexpected failure is a fixed 500 internal_error; the operator log names its type and the code location that raised it, never its text (RM12).

### 7.2 Managed and client owned tools

The relaymind alias selects managed execution. It uses the installed skills and tool handlers. Supplying a nonempty tools array, tool_choice, or client tool-result messages is rejected to prevent ambiguous ownership. Managed tool activity is visible through native events, not through client-executed tool_calls. The chat response contains the answer, clarification, or an explanation that confirmation is pending.

The relaymind-client-tools alias returns tool_calls to the caller. The caller executes them and includes matching tool messages in the next request. The hub MUST NOT execute those tools or certify that caller-reported results are trustworthy. Gateway-installed business tools are disabled for this alias. A caller may supply procedural instructions; managed skill execution is demonstrated through the managed alias. Local routing may fall back to external generation before output is committed, but external fallback is not permitted after partial client tool calls have been exposed. [S6]

The client-owned alias MUST honor tool_choice, preserve tool call IDs, and reject tool results without matching calls. No global tool-execution authorization guarantee is claimed for tools executed by third-party clients.

Client-owned run (RM08, RM19, AC10). Each request of the alias is its own run. The caller sends the whole conversation each time, so a response with tool_calls completes its run, and the next request, with the matching tool messages, starts another. The run has no routing generation, because the offered skill catalog is empty. It records route.selected with a decision the gateway made itself: route local with reason code simple_skill, or route external with reason code execution_mode in external_only, and skill_id null. The model sees the conversation as sent and only the functions the caller supplied, narrowed by tool_choice: none offers no function, a named function offers only that one, and auto and required offer all of them. The gateway installs no tool of its own. A generation is admissible when it is non-blank text, or when its first tool call names an offered function with arguments that are valid against that function's parameters schema (the same strict check as RM09). With none the generation must be text; with required or a named function it must be a tool call, and the named one. Text where a call is required, a call where none was allowed, an unknown name, or arguments that break the schema is invalid output: one format-repair generation is allowed inside the local limits, and then the stop is an escalation (RM04) with reason code invalid_output, also when the limits leave no room for the repair. The repair exchange grows the request, so it is measured again before the next generation (RM13). Every generation of this run may be the response, so max_completion_tokens, temperature, and top_p apply to each of them, local and external; there is no tool loop to exempt. A tool_calls response carries exactly one call, because parallel_tool_calls true is refused; further calls proposed by the same generation are not exposed, and the model proposes the next one after the result. The call ID is the one the model produced and is preserved in the response; the gateway replaces it only when it is blank, was used earlier in the conversation, or repeats within the response, so the IDs of the conversation stay unique. A tool message is passed to the model as data, unchanged (RM12). The trace records tool.proposed for the exposed call (the proposal ID is the call ID, arguments redacted) and never tool.started or tool.finished, because the gateway executes nothing; run.completed names the model that proposed the call. The response is buffered and complete before any byte is sent, so external fallback (RM04) can only happen while nothing has been exposed, and one response never mixes local and external output. This backend does not forward a live provider stream (section 7.3); a text answer is published as answer.delta events and replayed.

The X-RelayMind-Execution-Mode header applies to both aliases. With the managed alias it selects the route for skill execution. With the client-owned alias, external_only sends the request to the external model directly, and local_only keeps the request on the local model; the rule that external fallback is not permitted after partial client tool calls are exposed is unchanged. local_only returns needs_external with both aliases when the local model cannot produce the answer or, for the client-owned alias, the tool calls. parallel_demo is admissible only with the managed alias, because its fixed workflow runs gateway tools and managed skills (RM15, section 9), which the client-owned alias disables; parallel_demo with relaymind-client-tools is rejected with 400 and code invalid_execution_mode. This is a request validation failure, like a tools array with the managed alias, not an operator policy decision, so it is not a 403.

On this endpoint needs_external is an error before any answer content is sent, because RM04 raises it before output is committed. The response is HTTP 422 with an error object carrying code needs_external, a message, the reason code, and the run ID; partial results stay in the native trace of that run. 422 is used because the request was valid and authorized and no service was unavailable: the task cannot complete under the effective execution mode, and a retry with the same request would give the same result.

### 7.3 Streaming and early feedback

**RM20** The compatible stream MUST contain only answer content or client-owned tool-call deltas, with protocol framing. Internal progress and a generic acknowledgment MUST NOT be injected into it to improve reported latency. The demo's early model-selected status is delivered through the native event stream. A standard chat client without RelayMind event support may therefore wait until a valid answer or tool call is ready.

Once answer streaming begins, final-answer ownership is fixed. A subsequent provider error fails the stream; the gateway MUST NOT silently append another model's replacement answer. A later correction is a distinct message or run.

The stream is built in one of two ways. A buffered stream, which the managed alias always uses, is the replay of a finished run: the run has already validated and published its answer, so the stream sends the role chunk, one content chunk for each answer.delta event in order, the finish chunk, and [DONE]. No provider is called while it is sent. A live stream is produced by a backend that forwards answer text while the provider is still generating it (the client-owned backend of section 7.2 buffers and validates its generation instead, so it does not produce one). It sends the role chunk, then a content chunk for each non-empty piece in the order received, and the finish chunk and [DONE] only after the provider finished. The role chunk is held until the first non-empty piece arrives or the provider finishes, so nothing is sent before there is answer text. In both, an emitted content chunk is never revised, reordered, or retracted, and the joined content chunks are the answer. A request without stream collects the whole live answer before it responds. A client that disconnects from a live stream makes the gateway close the provider stream, which requests cancellation (section 7.4); a disconnect is not a provider failure.

Mid-stream failure (RM20, section 11). When the provider fails after the first chunk was sent, the status line can no longer change. The gateway then (1) asks the backend to record the run as failed (run.failed, error_code external_failed; a run that already ended never restarts, RM17), (2) sends one data event whose body is {"error": {"code": "external_failed", "message": <fixed text>, "run_id": <run ID>}}, which standard SDKs raise as an error, and (3) closes the connection without a chunk that has a finish_reason and without [DONE]. The message is fixed and carries no provider text (RM12). The gateway does not send a replacement answer, and it does not retry on another model. A failure before the first chunk, or of a request without stream, is a normal error response with its status: the run is recorded as failed in the same way and the response is external_failed (503) with the run ID and the same fixed message (section 11).

In managed mode, tool-planning output is buffered and validated. After the skill's explicit completion predicate is met, or a validated finalize proposal is accepted, the orchestrator starts a final generation with tools disabled. This final generation counts against the model-call budget. Its text can be streamed without risking a later tool call in the same generation. A skill whose evidence predicate is unmet cannot finalize merely because the model requests it.

Usage is optional in the compatible response when a truthful unified count is unavailable. The native trace MUST retain usage per model and call, including cache counts when the provider returns them. Counts across different tokenizers MUST NOT be presented as if they belonged to one model.

### 7.4 Native run API

**RM21** The demo MUST use the following RelayMind-specific endpoints, backed by the same orchestration service as chat:

| Endpoint | Contract |
| --- | --- |
| POST /relay/v1/runs | Accept messages and execution_mode; return 202 with run_id, state_version, and events_url. Modes: auto, local_only, external_only, parallel_demo. |
| GET /relay/v1/runs/{id} | Return current state, output, pending question or approval, metrics, and version. |
| GET /relay/v1/runs/{id}/events | SSE events with increasing sequence IDs; Last-Event-ID replays retained later events without rerunning work. |
| POST /relay/v1/runs/{id}/resume | Accept expected_version and either input (the input text, recorded in clarification.answered, section 8) or approval_id plus token and approve boolean, matching the waiting state. Return 202. A body that does not match the waiting state, including a missing or blank input in waiting_input, or a missing or non-boolean field in waiting_confirmation, is rejected with 400 and code invalid_resume_input; the run keeps waiting and its version is unchanged (section 11). An approval_id or token that does not match the pending approval is 403 with code approval_invalid; the run keeps waiting (section 5). |
| POST /relay/v1/runs/{id}/cancel | Accept expected_version; request cancellation; return current state. |
| GET /healthz and GET /readyz | Process liveness and readiness of the configured execution mode; model warm state is explicit. |

A native run takes no model alias and always uses managed execution.

Native bodies and views (spec 0.17 and 0.18, RM21, AC11). Every native endpoint, /healthz, and /readyz require the application token (section 3, 401). The checks of POST /relay/v1/runs run in this order, all before a run exists: the token (401), the body (400), the Idempotency-Key header (400 invalid_request), the execution mode (400 invalid_execution_mode, then 403 execution_mode_forbidden under the same operator policy as the chat header), then a stored Idempotency-Key (a replay, or 409 idempotency_conflict), then the execution slot (429, nothing created). The body is a JSON object with messages (the managed-alias message rules of section 7.1: user, assistant, system, and developer text messages, the last one from the user) and an optional execution_mode (absent or null is auto). Any other field, including model, is 400 unsupported_parameter, named. The 202 response has run_id, state, state_version (the run version when it was accepted), and events_url (/relay/v1/runs/{run_id}/events). The work continues after the response: the run is not tied to the request that created it.

GET /relay/v1/runs/{id} returns run_id, state, state_version, requested_execution_mode and effective_execution_mode (from run.accepted), output (the joined answer.delta texts of a completed run, otherwise null), error_code and reason_code (a failed run's codes from run.failed, so needs_external, workflow_unavailable, and the run-state failures of section 11 are visible here, otherwise null), pending_question (while the run is in waiting_input: question, language, reason_code, skill_id, missing, and waiting_seconds from the latest clarification.required, otherwise null), pending_approval (while the run is in waiting_confirmation: approval_id, tool_name, proposal_id, arguments, target, state_version, expires_at from the latest confirmation.required, plus token, otherwise null), metrics, and events_url. metrics has active_ms and waiting_ms (the terminal event's values for a finished run, otherwise measured from the store's state intervals, RM25), model_calls, and external_calls (counted from model.started events by actor). The token is returned once: it is in the first read of the run that sees the pending approval and is null in every later read, so a caller that loses it can only cancel the run or let the approval expire. It is never written to the store or the trace (RM11). The same read serves a run started by the chat endpoint, because a chat reply cannot carry the token. Reading the run first ends any expired wait (section 5), so a run is never shown as waiting past its limit.

GET /relay/v1/runs/{id}/events is text/event-stream. Each stored event is one SSE message with id set to its seq, event set to its event_type, and data set to the TraceEvent JSON object (section 8). The stream first sends the retained events with a seq greater than the Last-Event-ID header (all of them when it is absent), in order, then sends each new event as the run records it, and ends after the run's terminal event. It stays open while the run waits, and sends an SSE comment line as a keep-alive when it is idle. Replay reads stored events only: it calls no model, runs no tool, and writes nothing, so reconnecting never repeats work. A Last-Event-ID that is not a non-negative integer is 400 invalid_request. A client that disconnects leaves the run untouched and can reconnect with the last ID it saw; only a compatible chat disconnect requests cancellation.

POST /relay/v1/runs/{id}/resume validates in this order: the run exists (404), the body has an integer expected_version (400 invalid_request), the run is not terminal (409 run_terminal), the version is current (409 stale_version), the body matches the waiting state (400 invalid_resume_input), and the approval matches (403 approval_invalid). A run that is not waiting is 409 invalid_state. A body that mixes input with approval fields is 400 invalid_resume_input. The 202 response has the shape of the creation response, and the continuation runs in the background. A resume of an expired wait is 202 with the run already in failed (input_expired or approval_expired); that is run state, not an error (section 11).

POST /relay/v1/runs/{id}/cancel takes expected_version and returns 200 with the run view of GET. A waiting run is cancelled at once, with reason cancel_requested in run.cancelled and the pending approval voided. A running run is asked to stop: the gateway cancels its work and, for a bounded time, waits for the run to reach cancelled before it answers, so the returned state can still be active if the work did not stop in time (best effort). A terminal run is 409 run_terminal and a stale version is 409. A run that is active under a request that owns it, such as a compatible chat request, is 409 invalid_state: closing that request is its cancellation. Cancelling during an in-flight write is specified by issue #29.

GET /healthz returns 200 with status ok while the process serves requests. GET /readyz reports, for each execution mode the operator policy permits, whether its required models are ready, and for each configured model its availability and warm state (available, warm, and the adapter's detail). warm is false while a local model is still loading. A mode is ready when its models are available and warm: auto and local_only need the local model, external_only needs the external model, and parallel_demo needs the local model, an external model, and an enabled workflow that this build implements (none does until issue #30, so parallel_demo is not ready). The response is 200 only when every permitted mode is ready, and 503 otherwise, with the same body; the gateway never reports an unavailable or cold model as ready (section 12).

Execution mode is constrained by operator policy. local_only never calls externally and returns needs_external when the task cannot complete locally. external_only bypasses local inference while using the same managed tools, skill catalog, and permissions. In external_only no local routing generation runs: the gateway records a RouteDecision it produced itself, route=external, skill_id=null, reason_code=execution_mode, in the same way as the bypass decision for an oversized router input (section 4.1), and skills_offered in run.accepted lists the managed catalog the external model receives. How the external model selects among those skills is part of the handoff (RM13); every load still emits skill.loaded (RM06). parallel_demo fails explicitly with error code workflow_unavailable when its configured workflow is unavailable (section 4.4). The compatible chat endpoint accepts the same values through the X-RelayMind-Execution-Mode request header (section 7.1), so chat and native runs share one mode enumeration and one policy check, and a standard chat client can run the auto versus external_only comparison without the native API. Both endpoints record the requested and effective mode in the run.accepted payload (section 8).

Chat responses include X-RelayMind-Run-ID for trace correlation. Confirmations for managed writes require the native resume endpoint or demo UI. Ordinary clients can read the pending-confirmation message but cannot approve it solely by sending a free-form yes. Clarification can be resumed natively, or submitted as a new chat request with complete context.

All execution-creating POSTs accept an Idempotency-Key: POST /relay/v1/runs and POST /v1/chat/completions (spec 0.18, RM17, RM21, AC11). Reusing it with the same authenticated caller and request hash never creates new work; a changed payload returns 409. Native creation returns the existing run reference. Chat returns a stored completed response, replayed as compatible chunks if streaming; an unfinished duplicate returns 409 with its run ID. Keys remain valid for the seven-day run retention window. The key does not authorize repeating a completed side effect. Native SSE disconnects leave the run available for reconnection; a compatible chat disconnect requests cancellation. Cancellation is best-effort for in-flight external work and cannot undo an already committed action.

Idempotency keys (spec 0.18, RM17, RM21, AC11). The header is optional, and a request without it behaves as before. A key is 1 to 255 printable ASCII characters and is sent once; any other value is 400 invalid_request, checked after the body and before the execution mode. A key belongs to the authenticated caller. The prototype has one caller, the holder of the application token (section 3), so its keys do not change when the gateway restarts and generates a new token. The request hash is the SHA-256 of the canonical JSON (RM10) of the endpoint and the decoded body, with, for chat, the X-RelayMind-Execution-Mode header value; a different endpoint, body, or mode is a different payload, and a stream flag is part of the body. The same key on the other endpoint is therefore a changed payload.

A key is stored only when a run is created, in the same store transaction as the run and its run.accepted event. A request that is refused before a run exists (400, 401, 403, 429) stores nothing, so the client can send it again under the same key. A request whose key is already stored creates no run, makes no model call, and runs no tool. If the stored hash differs, the response is 409 idempotency_conflict with run_id null. If it matches:

- Native creation answers 202 with the shape of the creation response for the existing run: its run_id, its current state and state_version, and events_url. The response carries the header Idempotent-Replayed: true. It is the same whether the run is still active, waiting, or finished.
- Chat answers from the stored run. A completed run is answered with its stored response and the same X-RelayMind-Run-ID: the joined answer.delta texts as the assistant message (or, for the client-owned alias, the calls recorded by tool.proposed with owner client), and, when the request streams, the same chunk sequence as a live response (section 7.1), one content chunk for each stored answer.delta in order. The id is the stored run's, and created is the time the run ended, so replays of one response are equal. Client-owned tool-call arguments are replayed as stored; if the store holds redacted arguments (RM12), the replay is refused with 409 idempotency_conflict instead of returning arguments that differ from the original response. A duplicate of a run that has not ended (active, or waiting for input or confirmation) is 409 idempotency_conflict with run_id set to that run, and a duplicate of a run that ended failed or cancelled is 409 run_terminal with run_id set to that run; a retry after a failure uses a new key and is a new run linked to the previous one (RM17). Both responses carry X-RelayMind-Run-ID.

Keys leave with their run: the seven-day retention (section 5) and explicit deletion remove the key with the run, and after that the key is unknown and a request that carries it creates a new run. A reused key never authorizes a repeated side effect: a duplicate never reaches the orchestrator, and a completed operation is replayed from its record by the executor in any case (RM10). Resume and cancel do not read the header: expected_version already makes a retry of them safe (RM17).

## 8 Internal data and event contract

The following objects define required fields. Implementations MUST publish equivalent versioned JSON Schemas and validate them at boundaries; examples are illustrative data, not execution evidence.

| Object | Required fields and constraints |
| --- | --- |
| RouteDecision | schema_version=1; route enum; skill_id or null; language en or zh; reason_code; clarification or null. Unknown fields rejected. |
| ToolProposal | proposal_id; actor local or external; tool_name; arguments object; skill_version; run_state_version. |
| ToolResult | operation_id; tool_name; status succeeded, failed, or unknown_outcome; validated data or null; error or null; observed_at; duration_ms; source_ref; truncated boolean. |
| HandoffEnvelope | schema_version=1; run_id; original_messages (role and text, in order); skill (name, version, content_hash) or null; completed_operations (operation_id, tool_name, arguments, status); evidence (for each completed operation: operation_id, source_ref, truncated, and the ToolResult); local_proposals (each labeled as a proposal); pending_work; allowed_tools; reason_code (an RM04 reason code); remaining_budgets; language en or zh. Unknown fields rejected. |
| handoff.started payload | reason_code (the RM04 reason for the escalation); model (the external model); skill (name, version, content_hash) or null; operation_ids (the completed operations the envelope carries); allowed_tools; request (size, limit, and method, tokenizer or utf8_bytes, of the measured external request); degraded_mode (true) and local_failure (a non-blank cause) only when the local model was unavailable and the operator policy permitted degraded mode (section 11). |
| TraceEvent | schema_version=1; run_id; seq; timestamp; elapsed_ms; event_type; actor; payload. |
| run.accepted payload | requested_execution_mode (the header or native value, or null when absent); effective_execution_mode (the mode applied after defaults and policy); model_alias (the virtual alias from the chat request's model field, relaymind or relaymind-client-tools, or null for a native run (section 7.4)); budgets (the section 11 limits in force, RM03); skill_roots (the configured roots in order); skills_offered (array of name, version, and content_hash for every skill in the catalog offered to the router, or in external_only to the external model (section 7.4), empty for the client-owned alias). |
| clarification.required payload | question (the clarification text in the run's language); language; reason_code; skill_id (the clarify decision's skill_id: the skill whose required identifier the gateway found absent, or null for a clarification the model requested, per the cross-field rules below); missing (a non-blank name of the input the run waits for: the argument name of the absent required identifier when skill_id is set, or skill_selection when skill_id is null, because the model requested clarification before any skill was chosen and the gateway knows no narrower input); waiting_seconds (the section 11 waiting limit in force). |
| confirmation.required payload | approval_id; tool_name; proposal_id; arguments (canonical, redacted per RM12); target (the value of the tool's confirmation target argument); state_version (the run version the approval is bound to); expires_at (UTC time, five minutes after issue); model (the proposing model). Never the token or its hash. |
| clarification.answered payload | input (the resume text, redacted per RM12); missing (copied from the clarification.required it answers); waited_ms (monotonic time from clarification.required to the accepted resume, RM25). |
| run.completed payload | final_answer_model (RM16); model_calls (every actual model call: call_id, model, actor, purpose; RM16); external_calls (how many of those calls went to an external model, RM01); active_ms and waiting_ms (non-negative integers, milliseconds from the monotonic clock, RM25: active execution time and the sum of the recorded waits, reported separately, section 12.1). |
| run.cancelled payload | active_ms and waiting_ms as in run.completed; model_calls and external_calls when the run made model calls; reason (user_denied for a denied confirmation, cancel_requested for a native cancel request) and approval_id (when a confirmation was pending). |
| run.failed payload | error_code (the terminal error code, section 11); reason_code (the RM04 reason code when the failure has one, for example needs_external or missing_input); model_calls, external_calls, active_ms, and waiting_ms as in run.completed. A run that restart recovery ends has error_code interrupted, interrupted_state, and settled_operations (an array of operation_id and status; section 5). |

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

The 420 ms example is a schema illustration, not a measured latency. Events containing tool results MUST be redacted before persistence and rendering. Redaction is by configured secret field name, at any depth. A value (a string or a number) found under a secret field is also replaced wherever the same whole token is repeated in the data or error message of that result (for example a log message), and in later tool results, model text, proposed tool-call arguments, and resume input of the run, so a secret that sits in a log line next to its field, or that a model copies, is not stored or shown. The result envelope fields (tool name, status, timestamps, source reference) are not free text and are never scrubbed. Redaction runs before the first write, and the model sees only the redacted result. Tool output stays data: text in it, such as an instruction to call another tool or to print a credential, never changes the allowed tools, the caller permissions, or an approval (RM08, RM11, RM12, AC06). Large results use artifact references with explicit size and truncation metadata; a truncated result cannot silently satisfy a skill that requires the complete result.

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

All listed input fields are required unless explicitly described otherwise. A health tool's unavailable status is a successful observation of the service state; failure to contact the tool itself produces a failed ToolResult. The deletion handler compares expected_revision atomically and returns a conflict without mutation if the fixture changed. The pending approval binds that revision as well as the database ID. delete_demo_database declares database_id as its confirmation target (RM11).

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

Error contract. Every error response has the JSON body {"error": {...}}, where the error object has a stable code, a message, run_id (the run reference, or null when no run exists), and reason_code only when the failure has an RM04 reason code. The code is the contract; the message is for people. The statuses and codes are:

| Status | Codes | Cause |
| --- | --- | --- |
| 400 | invalid_request, unsupported_parameter, invalid_execution_mode, invalid_resume_input, context_limit_exceeded | A validation failure, including a resume whose body does not match the waiting state (section 7.4) and evidence over the external context limit (RM13) |
| 401 | unauthenticated | A missing or invalid application token |
| 403 | execution_mode_forbidden, approval_invalid | Denied access, including an approval ID or token that does not match (section 5) |
| 404 | run_not_found | A run that does not exist |
| 409 | stale_version, run_terminal, invalid_state, idempotency_conflict | A stale state version (RM17), a resume of a terminal run or a chat duplicate of a failed or cancelled one, a resume in a state that cannot take it, a reused idempotency key with a changed payload, or an unfinished chat request under it (section 7.4) |
| 422 | needs_external | A task that cannot complete under the effective execution mode (section 7.2) |
| 429 | overloaded | Another run holds the execution slot; the request is rejected, not queued |
| 500 | internal_error | An unexpected failure |
| 503 | workflow_unavailable, external_failed | An unavailable required model, or in parallel_demo an unavailable workflow (section 4.4) |
| 504 | deadline_exceeded | An exhausted time limit before streaming starts |

A failed run maps to this table by its error code: needs_external is 422, context_limit_exceeded is 400, workflow_unavailable is 503, internal_error is 500, and external_failed is 503, except that an external_failed run stopped by an exhausted time limit (the entire-run active time, or external_seconds) is 504 with code deadline_exceeded; its run.failed event keeps error_code external_failed and names the limit. A run that ended as missing_input, input_expired, approval_expired, or interrupted, or as cancelled, is not an error response to the request that created it: the native API reports it as run state with its error code (section 7.4). Internal errors carry a fixed message and omit credentials, exception text, and secret values (RM12).

After streaming starts, an unrecoverable failure is signaled as an error and connection termination, and recorded as run.failed. It MUST NOT produce a successful finish marker. The compatible client behavior for this failure is part of integration tests because SDK handling may vary.

An unavailable local model, meaning a missing local capability or a failed local generation, may route directly to external only when the operator policy permits degraded mode and external dispatch is permitted (an effective mode other than local_only, a configured external model, and room in the entire-run limits). Degraded mode is off by default. When it is on, the run hands off with reason code capability_missing, with the evidence it has so far; when the failure came before any routing decision, the gateway records its own decision first, route=external, skill_id=null, reason_code=capability_missing, in route.selected with mechanism degraded_mode. The handoff.started payload then carries degraded_mode true and local_failure (the cause), so the trace records degraded mode. Without the policy, or when dispatch is not permitted, the run ends as needs_external with reason code capability_missing. An unavailable external model does not make a complex request locally solvable. If external use is disabled, return needs_external with partial results. Read-only retries are limited to one for a transient failure within all budgets; model repair also counts toward generation limits.

An external stage that cannot finish ends the run as failed with error code external_failed. Causes are an adapter error, an unreachable endpoint, an answer that does not meet the completion predicate within external_generations, and an exhausted entire-run limit. The partial evidence stays in the trace, no local answer replaces the external one, and no result is invented (AC07). The run.failed event carries an RM04 reason code: the reason the external stage stopped when it stopped on one, otherwise the reason for the escalation. Evidence over the external context budget is a separate code, context_limit_exceeded (RM13). The HTTP mapping of both codes is in the error contract above.

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
