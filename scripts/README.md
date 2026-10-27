# Scripts

## Current

- `openclaw_qualification.py` — the OpenClaw qualification harness
  (#135; RM33, AC24). It serves the real gateway over the model doubles
  of `tests/`. `fixtures` runs every scenario through the pinned host
  transport (`integrations/openclaw/test/host-transport/drive.mjs`) and
  writes the sanitized, deterministic fixtures; `host -- <command>` runs
  an operator command such as a real `openclaw agent exec` with the
  gateway URL and token in its environment only, and exports every run
  and trace event without the token (a command that cannot start is
  not_run). Deterministic doubles, not live evidence.

  ```bash
  python scripts/openclaw_qualification.py fixtures \
      --out-dir integrations/openclaw/test/fixtures/v2026.10.7/transport
  python scripts/openclaw_qualification.py host --out <export.json> -- <command>
  ```
- `openclaw_install_check.py` — the OpenClaw install check (#145; RM12,
  RM19, RM33; AC24). It serves the real gateway over model doubles and
  runs a host command through the release artifact's lifecycle in a fresh
  host home: install (`--accept-capabilities`), configure, enable, inspect,
  the token through `paste-api-key` on standard input, discovery, a text
  and a host-tool (`read`) exchange, disable, uninstall, and removal of the
  config entry and the credential uninstall leaves. The gateway trace and
  the sandbox decide a pass, never the host's `"ok": true`; a host that
  cannot start is not_run. A functional check, never live evidence; a
  platform qualification only when recorded as one.

  ```bash
  python scripts/openclaw_install_check.py \
      --tarball release/openclaw/relaymind-openclaw-provider-0.1.0.tgz \
      --out <export.json> [--label "<machine and runtime>"] \
      -- node integrations/openclaw/node_modules/openclaw/openclaw.mjs
  ```
- `run_openclaw_eval.py` — the M9 OpenClaw evaluation (#139; RM16, RM25,
  RM26, RM33; AC24; spec 0.50 section 12.5). It checks
  `evaluation/openclaw_tasks.v1.json` against its lock and runs every slot
  of the frozen order through a real host (`openclaw agent exec --json`) in
  four arms: `direct` (the host's own entry for the external model),
  `relaymind_external_only`, `relaymind_auto`, and `relaymind_no_local`
  (the plugin from the release artifact against its own `relaymind serve`
  gateway). Each arm has its own isolated host home; each attempt a fresh
  sandbox under the task's exec policy. The export holds every attempt raw,
  the grades and summary of `relaymind.evaluation.openclaw`, and a Markdown
  report; an arm without its endpoint, credential, or local model is
  not_run. Live evidence only when run with a real host and models
  (operator step, not run here).

  ```bash
  RELAYMIND_EXTERNAL_API_KEY=... python scripts/run_openclaw_eval.py \
      --tarball release/openclaw/relaymind-openclaw-provider-0.1.0.tgz \
      --external-base-url <url> --external-model <model> --external-context-limit <n> \
      --out <dir> -- node integrations/openclaw/node_modules/openclaw/openclaw.mjs
  ```
- `openclaw_request_size.py` — the size of captured OpenClaw requests
  against the local input limit (#135; RM13, RM26, section 7.5). Each
  request is validated as the gateway does and measured with
  `measure_request`: tokenizer counts through a running llama-server
  (`--base-url`), or the labeled UTF-8 upper bound, under which a request
  over the limit is undecided. It reads the fixture layout and the host
  probe's one-request-per-file layout, and reports the share that fits the
  manifest's `ctx_size` and what auto and local_only do with the rest.
  The deciding tokenizer count runs on fresh captures as the host sent
  them (`integrations/openclaw/README.md`); the stored probe captures are
  sanitized and smaller, so a run on them passes `--sanitized` and every
  size is a lower bound.

  ```bash
  python scripts/openclaw_request_size.py \
      --fixtures <unsanitized captures> \
      --manifest models/qwen3-0.6b-q8_0.manifest.json \
      [--base-url http://127.0.0.1:8033] [--sanitized] --out <report.json>
  ```
- `gen_model_manifest.py` — generate a model artifact manifest from a
  GGUF file (RM18, spec section 6). Hashes are computed from the
  artifact, never written by hand. Manifests live in `models/`; the
  GGUF files themselves are not checked in (`*.gguf` is ignored).
- `compare_adapters.py` — run the same prompt list against two or more
  adapter configurations and write one JSON export per config: spec
  12.1 metrics per generation (first transport event, first model
  action, first answer token, completion latency, finish reason) plus
  per-call token usage, with the adapter identity attached. A llamacpp
  config names its model manifest; model id, quantization, and runtime
  version are read from it, never typed into the config.

  ```bash
  python scripts/compare_adapters.py config.json --out-dir exports/
  ```

  See the module docstring for the config format. This is engineering
  instrumentation for development comparisons (issue #37): sequential,
  unwarmed, and never publishable as measured results (spec 12.3). The
  formal benchmark with warm-ups, randomized order, and percentiles is
  the latency experiment runner below.
- `probe_timing.py` — run one reproducible timing probe against the
  pinned local artifact and write the export a `docs/EVIDENCE.md` entry
  points at. It verifies the GGUF against its manifest, launches
  llama-server from the manifest's launch settings, records cold start
  (launch to the first `/health` 200), sends warm-up rounds of both
  prompt kinds, then runs one pass over the plain and tool-call prompts
  in the probe definition. The export carries the 12.1 metrics per
  generation and a `run` block with the command, prompts, system
  prompt, tool schema, warm-up ids, server argv, and each generation's
  answer or tool proposal (RM16, section 12). Probe definitions live in
  `scripts/probes/`.

  ```bash
  python scripts/probe_timing.py scripts/probes/m1_timing.json \
      --out docs/evidence/<date>-m1-timing-<machine>.json
  ```

  Single pass, sequential, concurrency one: observations, never p50 or
  p95 (spec 12.3).

- `probes/probe_routing.py` — the routing probe spike (issue #48). It
  launches llama-server like `probe_timing.py`, then asks the pinned
  model to route the prompts in `probes/m2_routing.json` among the six
  demo skills with three mechanisms: native function calling (one
  function per skill), a JSON envelope, and the envelope under
  grammar-constrained decoding. Each output is judged by the production
  RouteDecision validator; router input is counted with the local
  tokenizer, and a prompt over the spec 4.1 cap of 2,048 tokens takes
  the bypass and is marked. The probe definition states llama-server's
  `cache_prompt` setting, which the export records. The export backs
  the routing entry in `docs/EVIDENCE.md`.

  ```bash
  python scripts/probes/probe_routing.py scripts/probes/m2_routing.json \
      --out docs/evidence/<date>-m2-routing-<machine>.json
  ```

  A spike: minimal tests, single pass, observations only (spec 12.3).

- `live_acceptance.py` — the live acceptance harness (#88, #104). It
  starts llama-server from the manifest, then starts the gateway with
  `relaymind serve --config` once per launch plan, runs the M3 to M5
  acceptance scenarios over HTTP (native run API, chat endpoint with the
  OpenAI SDK, demo UI in Chromium), and writes one export with the
  launcher output (token not recorded), each scenario's checks and
  findings, and the full traces, plus a statistics export per routing
  cache setting. Scenarios that need a real external endpoint run only
  when one is configured; otherwise they are recorded as not run.

  ```bash
  python scripts/live_acceptance.py \
      --out docs/evidence/<date>-live-acceptance-<machine>.json
  ```

  Single pass, sequential: observations, never benchmark results (spec
  12.3). See `docs/TESTING.md` for the scenario list.

- `run_corpus.py` — the correctness corpus runner (#32, spec 0.28
  section 12.2). It refuses a corpus that no longer matches
  `evaluation/corpus.v1.lock.json`, starts llama-server from the
  manifest, and for every case and mode (auto, external_only,
  local_only) starts a new gateway with `relaymind serve --config` from
  the case's sandbox seed and permissions, routing cache off. It answers
  a clarification with the case's answer, approves a confirmation only
  when the case says approve and the pending call is acceptable, grades
  the run from its trace (`relaymind.evaluation.grading`), and writes one
  export with every grade and trace plus a Markdown report with exact
  numerators and denominators per mode, language, and class. Without an
  external endpoint only local_only runs.

  ```bash
  source ~/.config/relaymind/external.env   # exports the endpoint and the key
  python scripts/run_corpus.py --out docs/evidence/<date>-corpus-<machine>.json \
      --external-base-url "$RELAYMIND_EXTERNAL_BASE_URL" \
      --external-model "$RELAYMIND_EXTERNAL_MODEL" --external-context-limit 128000
  ```

  `--cases`, `--modes`, and `--languages` run a subset; the export
  records the selection and whether it is the full corpus.
  `--external-request-options '{"reasoning_effort": "low"}'` (both
  runners) declares `external_model.request_options` for the gateways
  they start; the export configuration records it only when given.
- `latency_benchmark.py` — the latency experiment (#33, spec 0.28
  section 12.2) on the local path: one gateway started with
  `relaymind serve --config`, routing cache off, concurrency one, and no
  external model unless the external endpoint arguments below name one.
  It sends the twelve prompts of
  `evaluation/latency_prompts.v1.json` over the compatible chat stream:
  a cold request, three warm-ups, then ten runs per prompt in a seeded
  random order. First transport event and first answer token come from
  the client's clock, first model action and first useful content from
  the trace. Errors and timeouts are kept. The export has the
  environment manifest (machine, backend devices, runtime log facts,
  llama-server `/props`, model hashes, revisions, test time), the prefix
  cache record, every sample, and p50, p95, sample count, and timeout
  fraction; a Markdown report is written next to it. Each sample's model
  calls also carry `cached_tokens`, `reasoning_tokens`, and
  `first_token_ms` from the trace (#120). The adapter now sends
  `cache_prompt`, so the prefix cache record says so, and a call whose
  runtime reported no cached count is counted as not measured
  (`calls_not_measured`), never as 0; a total over no measured call is
  `null`. Exports made before #120 hold the adapter's default of 0 and
  are not comparable on this field.

  ```bash
  python scripts/latency_benchmark.py \
      --out docs/evidence/<date>-latency-<machine>.json [--seed N]
  ```

  `--early-headers` (#140; RM25, RM32, spec 0.32 sections 7.3 and 12.1)
  starts the gateway with `performance.early_headers = true` and
  nothing else changed. The status line then arrives when the run is
  accepted, so the first transport event is the header time: the export
  records `design.early_headers` and `design.first_transport_event`, and
  the report labels the metric "header time". It is never first useful
  content. Without the flag the launch file has no `[performance]` table,
  which is the baseline. Under early headers a `routing` sample ends with
  the gateway's mid-stream error event and a deliberate connection close;
  the sample records the event code in `stream_error_code` and is checked
  against the expected outcome, not counted as a transport error.

  `--no-startup-warmup` (#124; spec 0.31 section 6) writes
  `placement.warmup = false`. The default launch warms the local model up
  before it serves, so the cold request is no longer the first the runtime
  sees; with the flag it is. The export records `design.startup_warmup`, and
  the launch file it keeps shows the table. With the warm-up on, the export
  also holds the launcher's warm-up line and the samples read from it in
  `cold_start.startup_warmup` (n/a stays `null`; no samples when the
  launcher printed no such line).

  Both runners reuse the live acceptance harness for the runtime check,
  the launch plan, and the trace read-back.

- `--runtime-extra-args` — llama-server arguments added after the
  manifest's launch settings (#117; RM18, spec 0.31 section 12.2), for
  `live_acceptance.py`, `run_corpus.py`, `latency_benchmark.py`, and the
  two timing probes below. One quoted text, for example
  `--runtime-extra-args='--n-gpu-layers 0 --threads 2'` for a CPU-only
  runtime with two threads. The default adds nothing. The arguments are
  recorded in every export next to the server argv
  (`llama_server.extra_args`; `llama_server_extra_args` in the latency
  benchmark export). Only the runtime-only settings of llama.cpp b11785
  are accepted, as long flags, each with one integer value given as
  `--threads 2` or `--threads=2`: `--n-gpu-layers` and `--gpu-layers`
  (at least -1, or `auto` or `all`), `--threads`, `--threads-batch`,
  `--batch-size`, and `--ubatch-size` (each at least 1). Anything else is
  refused, with the token named: a positional argument (llama-server
  reads one as the model path), a short flag, a missing, non-integer, or
  out-of-range value, a `--no-` form, a pinned setting of the manifest, a
  model, the bind address, and any other flag, including a spelling that
  could change what the model computes (the template arguments, the
  reasoning budget, metadata overrides, the slot count). b11785 has no
  `--mlock` or `--mmap` switch, so neither is accepted. So the manifest
  stays the one source of the qualified launch settings. A constrained
  configuration is reported as that configuration of that machine, never
  as another machine.

- `evaluation_config.py` — the runner configuration shared by
  `run_corpus.py` and `latency_benchmark.py` (#128). It defines the
  configuration options below, validates them before anything starts
  (an invalid option is a usage error; a failure while running keeps its
  traceback), and writes the configuration block of each export and
  report header. It is a module, not a command.

- Phase 2 runner configurations (#128; RM03, RM25, RM26, RM29, RM31,
  RM32): both `run_corpus.py` and `latency_benchmark.py` accept
  `--performance KEY=VALUE ...`, `--budgets KEY=VALUE ...`,
  `--selection-chain SELECTOR ...`, and `--no-local-model`.
  Repeat `--performance` or `--budgets` to add more distinct keys. Boolean
  values are `true` or `false`; keys, types, ranges, and implemented options
  are validated before runtime startup. `answer_stream=live` enables the
  managed statement gate; buffered remains the default. Every export
  and report header records the effective settings. Corpus grades also retain
  configured/effective stream policy and terminal error codes; report tables
  keep live/buffered task success and `predicate_unmet` failures separate.
  The actual paired provider corpus comparison is pending; this support is
  not parity evidence.

  ```bash
  python scripts/run_corpus.py --out docs/evidence/<date>-draft-many.json \
      --modes auto local_only --performance answer_pass=draft calls_per_generation=many \
      --budgets active_seconds=12 --selection-chain example_match model_router
  python scripts/latency_benchmark.py --out docs/evidence/<date>-early-headers.json \
      --performance early_headers=true
  ```

  The latency runner also accepts the corpus runner's external endpoint
  arguments: `--external-base-url`, `--external-model`,
  `--external-context-limit`, and optional `--external-region`; the three
  endpoint arguments go together, and `--external-region` alone is refused.
  The configuration block names the external model, the base URL's host
  (never its user info, path, or query), and the region, or `null` when no
  gateway was given an endpoint, so a local-only run and a local-plus-external
  run have different labels. The credential stays in
  `RELAYMIND_EXTERNAL_API_KEY`. With an external endpoint, a routing prompt
  is expected to complete, while clarification prompts still wait for input. `--no-local-model` requires that external endpoint and credential,
  defaults the selector chain to `example_match`, and starts no local runtime.
  Local runtime measurements are null; the corpus records `local_only` as
  not run and local completion as not applicable. It refuses `model_router`
  in this configuration. The existing `--early-headers` alias remains available
  and cannot conflict with `--performance early_headers=false`.

  Offline regrading preserves the source configuration and rejects new
  configuration flags. Older exports without a configuration remain labeled
  as not recorded. This runner change supplies no new live measurements or
  default decisions. Timing-sample import, placement and workflow options,
  the full metrics expansion, and the measured comparison matrix remain in #128.

- `run_corpus.py --regrade-draft-pass` — the offline draft-pass
  counterfactual (#115; RM31, spec 0.31). It grades the stored traces of
  a corpus export as if the skill-stage draft had been published as the
  answer: for each auto and local_only run with a final generation it
  takes the draft, applies the RM31 guards (no non-default caller answer
  option, the run's language, no tool-call JSON; the option, language, and
  tool-call guards are the functions of `relaymind.orchestrator.performance`
  that the gateway uses; a run with no draft text, or with no language in
  its first decision, is blocked too, and the export's `guards` explains
  every block reason), and grades a trace that ends with the draft as the
  local answer, whose added events pass `validate_trace_event`
  (`run.completed` records `answer_pass_applied` `draft`). A blocked run
  keeps its measured grade,
  and so does a run that changed the sandbox after the draft (its stored
  sandbox state holds a change that the draft would have prevented; the
  report counts it as not gradable). It calls no model, never changes the
  export it reads (an `--out` equal to it is refused), cannot be combined
  with `--regrade`, and writes a separate export and Markdown report
  labeled "offline counterfactual, not a measured run".

  ```bash
  python scripts/run_corpus.py \
      --regrade-draft-pass docs/evidence/2026-10-22-corpus-apple-m1.json \
      --out docs/evidence/<date>-corpus-draft-pass-<machine>.json
  ```

- `probes/count_multi_calls.py` — the multiple-calls spike (#119; RM09,
  RM11, spec 0.31 section 4.3). Over a corpus export it classifies every
  generation that proposed two or more calls (read-only or mutating,
  valid or not), applies the rule for several read-only calls per
  generation (locally, at most the room left in the stage's tool limit
  and in the run's), and counts the later one-call generations that re-proposed
  a refused call. It cross-checks its totals against the
  `multiple_tool_calls` refusals in the trace. `--export` defaults to
  the 2026-10-22 corpus export. Offline, no model.

  ```bash
  python scripts/probes/count_multi_calls.py \
      --export docs/evidence/2026-10-22-corpus-apple-m1.json \
      --out docs/evidence/<date>-multi-calls-<machine>.json
  ```

- `probes/probe_example_match.py` — the example-matching spike (#118;
  RM28, spec 0.31 sections 12.1, 12.2, and 12.4). A throwaway BM25
  matcher over each demo skill's description and example requests, with
  identifiers masked, word tokens for Latin text, and character bigrams
  for Chinese. It scores the 52 corpus prompts against the skill sidecar
  `evaluation/corpus.v1.skills.json` (precision, recall, and Jaccard per
  language and class, the score and margin of right and wrong picks), and
  with `--export` (optional) compares them with the local router's picks
  in a corpus export and proposes thresholds. Only a unique best skill
  is a pick: a tie for the top score is none (spec 0.32 section 14.1).
  The scores are raw BM25 values, not the [0, 1] scale of section 14.1,
  so the proposed thresholds are not configuration values. The scoring
  functions are pure so that the production selector can port them.
  Offline, no model.

  ```bash
  python scripts/probes/probe_example_match.py \
      --export docs/evidence/2026-10-22-corpus-apple-m1.json \
      --out docs/evidence/<date>-example-match-<machine>.json
  ```

  All three label their output as offline and not a measured run, and
  record the checkout's HEAD and whether its tree has changes
  (`regraded_with` in the draft-pass export, `analysed_with` in the
  other two). Each refuses an `--out` that is the export it reads. A file that an earlier command wrote
  into the checkout makes the tree count as changed, so write each
  output outside the checkout and copy it into `docs/evidence/` when the
  record needs a clean tree. Their results are in `docs/EVIDENCE.md`.

- `probes/probe_prefix_cache.py` — the prefix-cache probe (#114, #117;
  RM25, RM27, spec 0.31 sections 6 and 10). It launches llama-server from
  the manifest, builds the routing request with `Router.build_request`
  over the demo skills and the skill-stage request with
  `skill_stage_messages` (the function `LocalOrchestrator._local_setup`
  calls), encodes both with the llama.cpp adapter's `build_chat_request`,
  and sends them over raw HTTP with `cache_prompt` true and then false, so
  the whole `timings` and `usage` objects are read (`cache_n`, `prompt_n`,
  `prompt_ms`, `predicted_n`, `predicted_ms`, and
  `prompt_tokens_details.cached_tokens`; a count the runtime did not return
  is null). Each group (blocked and interleaved layout, each with the flag
  on and off) runs in a launch of its own, with a cold request, warm-ups,
  and ten different user texts per purpose. The export has the constant
  prefix of each purpose in tokens. Needs the pinned runtime on `PATH` and
  the model; the definition is `probes/m7_prefix_cache.json`.

  ```bash
  python scripts/probes/probe_prefix_cache.py scripts/probes/m7_prefix_cache.json \
      --out docs/evidence/<date>-prefix-cache-<machine>.json \
      [--runtime-extra-args='--n-gpu-layers 0 --threads 2']
  ```

- `probes/probe_cancel_prefill.py` — the cancel-during-prefill probe
  (#117; RM14, RM26, RM27). A prompt of about 6,000 tokens (sized with the
  runtime's tokenizer to fit the context of one slot from `/props`) goes
  through `LlamaCppAdapter`; the probe cancels it after 200 ms and sends a
  short routing-shaped request at once, then compares that request's wait
  with the same kind of request on an idle runtime. Five repetitions, each
  with a long prompt of its own, and one uncancelled run for the full
  prefill time. Definition: `probes/m7_cancel_prefill.json`; it lists
  exactly two short prompts per repetition (one idle, one after the
  cancel), and the probe refuses any other count, so the definition an
  export records lists only the prompts that were sent.

  ```bash
  python scripts/probes/probe_cancel_prefill.py scripts/probes/m7_cancel_prefill.json \
      --out docs/evidence/<date>-cancel-prefill-<machine>.json \
      [--runtime-extra-args='--n-gpu-layers 0 --threads 2']
  ```

- `probes/probe_external_latency.py` — the external first-token probe
  (#116; RM25, spec 0.31 sections 6 and 12.1). Streamed Chat Completions
  over raw HTTP to the configured endpoint; per request it records the
  time to the response headers, to the first `reasoning_content` delta,
  to the first content or tool-call delta, and to the end, with the whole
  `usage` object (a reasoning-token count the provider did not return is
  null). Two request shapes built by `external_messages` over a synthetic
  handoff envelope (an answer generation after a health tool exchange, and
  a tool-offering generation of `investigate-performance`), times the
  provider's reasoning default or its documented switch from the
  definition `probes/m7_external_latency.json` (with the documentation URLs
  checked on the day): four arms of five requests, interleaved in rounds.
  The endpoint and the model come from `--base-url` and `--model`
  (defaults: `RELAYMIND_EXTERNAL_BASE_URL` and `RELAYMIND_EXTERNAL_MODEL`),
  and `--region` records the region when it is known. The plan is refused
  when it is over `--max-calls` (default 20), a failed
  call is never retried, and the key comes from the environment variable
  named by `--api-key-env` (default `RELAYMIND_EXTERNAL_API_KEY`), goes
  only in the Authorization header, and is never printed or written. It
  calls a provider, so run it only when that is authorized.

  ```bash
  source ~/.config/relaymind/external.env
  python scripts/probes/probe_external_latency.py scripts/probes/m7_external_latency.json \
      --out docs/evidence/<date>-external-latency-<provider>.json
  ```

  The three probes label their output as single-pass observations from
  one run, never p50 or p95 (spec 12.3), and record the checkout's
  revision. As with the offline spikes, a file an earlier command wrote
  into the checkout makes the tree count as changed. Their results are in
  `docs/EVIDENCE.md`.

- `selection_benchmark.py` — the independent skill and tool selection benchmarks
  (RM02, RM08, RM09, RM25, RM28; section 12.4). The skills file and lock are
  checked in. ToolRet source rows stay in an operator cache because the dataset
  states no licence; install `pip install -e '.[bench]'` for parquet derivation.
  Every selector receives the published RM28 request and returns a validated
  result. HTTP selector addresses must be loopback and supply explicit expected
  `--selector-name` and `--selector-version`; both tracks use the production HTTP
  transport and report nullable unverified claims per attempt. The model router uses the
  production routing prompt, tokenizer and eight-skill cap. The tool proposal
  track sends the production first skill-stage prompt, validates complete calls,
  and never executes a tool handler.

  Skills case hashes exclude the embedded `sha256` field from their canonical
  payload; verification checks that digest against both the embedded value and
  the ordered case lock, in addition to checking the exact file bytes. ToolRet
  derivation and measurement share `~/.cache/relaymind/toolret` by default;
  explicit cache flags override it. `derive_toolret.py --verify --parquet`
  checks the six supplied shards in declared source order without fetching or
  rewriting the lock. The skills track requires `--proposal none`.

  The tools track measures `evaluation/tools.v2.lock.json` by default. Its
  derivation suffixes colliding tool names in the order of each declaration's
  digest, so a plain name never marks a positive. `--tools-lock-version 1`
  measures `tools.v1.lock.json`, the same rows with source-order suffixes, which
  is kept only to reproduce the runs recorded with it. `derive_skillsbench.py`
  caches its archives in `~/.cache/relaymind/skillsbench` by default and refuses
  a cached, supplied, or downloaded archive whose SHA-256 differs from the lock.

  HTTP reports distinguish the endpoint/transport from response-reported
  selector identities. A single observed identity is labeled as a response
  claim; mixed or absent observations leave the report's name/version unknown.
  Per-case selections retain the reported claims.

  ```bash
  python scripts/selection_benchmark.py --track skills --selector example_match \
      --output /tmp/skills-example-match.json
  python scripts/selection_benchmark.py --track skills --selector model_router \
      --local-url http://127.0.0.1:8033 --model Qwen3-0.6B-Q8_0 \
      --model-manifest models/qwen3-0.6b-q8_0.manifest.json \
      --output /tmp/skills-model-router.json
  python scripts/selection_benchmark.py --track tools --selector model_router \
      --toolret-cache /tmp/toolret --proposal local --output /tmp/tools-local.json
  python scripts/selection_benchmark.py --track tools \
      --selector http://127.0.0.1:9000/select --selector-name my_selector --selector-version 1 \
      --toolret-cache /tmp/toolret \
      --proposal external --context-limit 32768 --output /tmp/tools-external.json
  ```

  The tool baseline keeps every candidate. A custom selector narrows the synthetic
  skill's tools; the proposal track compares the full and narrowed sets. The
  external endpoint, model and credential environment variable follow the
  external latency probe above. External proposal runs default to a seeded
  100-case subset; local runs use the full 500-case lock. `--limit` records an
  explicitly smaller subset, while leave-one-task-out skill examples still use
  all other source tasks. Declare known training or tuning on either source with
  `--source-training trained|tuned|neither`; the default is `unknown`.

  Reports include per-case latency, failures and bypasses, file digests, selector
  versions, machine metadata, and macro/micro precision, recall and Jaccard.
  Skill reports group category and difficulty; tool reports separate narrowing
  and the two proposal arms. No result is merged with the ops corpus. The full
  ToolRet report includes the source instruction as export metadata only; keep
  it outside the repository and publish aggregate metrics with row IDs. Source
  derivation commands live in `scripts/benchmarks/` and require pinned revisions;
  a changed row or file fails verification before measurement.

- `example_selector.py` — standard-library loopback HTTP keyword example (RM28).
  Run `python scripts/example_selector.py --port 8051`. It offers only POST
  `/select`, selects an ID present in the request catalog using shared words,
  and reports `example_keyword/1`, `keyword_rule`, null confidence and no model
  calls. It executes no tools. Its keyword method has no accuracy guarantee.

  ```bash
  python scripts/selection_benchmark.py --track skills \
      --selector http://127.0.0.1:8051/select \
      --selector-name example_keyword --selector-version 1 --seed 42 \
      --source-training neither --output /tmp/skills-example-keyword-http.json
  ```

### M8 placement and streaming matrix (#134)

`run_m8_matrix.py` writes a fixed offline execution plan unless `--execute` is
present. The plan has 31 serial cells: two independent calibration corpora,
12 placement corpus cells and 12 latency cells (normal/constrained × three
policies × two selector chains), one local live corpus compared with the exact
normal buffered control, and four no-local buffered/live corpus/latency cells.
This is 2,548 corpus runs and 1,736 latency requests, including cold requests
and warm-ups. The conservative external inference ceiling is 21,024 calls;
there is no configured aggregate token ceiling. Actual failures and unknown
usage remain visible. Calibration cost is separate from candidate cost.

Run the offline plan from the reviewed checkout:

```sh
.venv/bin/python scripts/run_m8_matrix.py --out /tmp/relaymind-m8-measurements
```

After independent code review, the concrete execution command is:

```sh
.venv/bin/python scripts/run_m8_matrix.py \
  --out /tmp/relaymind-m8-measurements \
  --runtime /Users/dmitrykudryavtsev/llama-b11785-dl/x/llama-b11785/llama-server \
  --provider-env /Users/dmitrykudryavtsev/.config/relaymind/external.env \
  --execute
```

The optional provider file accepts literal assignments to the three
`RELAYMIND_EXTERNAL_API_KEY`, `RELAYMIND_EXTERNAL_BASE_URL`, and
`RELAYMIND_EXTERNAL_MODEL` environment variables. It is parsed without shell
execution. The key is passed only in the child environment, never argv or the
matrix index. Existing exported variables also work. Provider context remains
128,000, provider options remain their defaults, and baseline budgets are
unchanged. The runner checks the model artifact and runtime version before
launch. A clean committed checkout is required; child gateway imports explicitly
resolve that checkout's `src`, even with a shared virtualenv.

Every export and retained SQLite/log directory stays outside the checkout.
The matrix index records source bytes, runtime binary digest, settings, argv,
run order, UTC boundaries, attempts, and export digests. Each runner's optional
`--checkpoint FILE` writes fsynced request-start and request-result checkpoints.
Before resuming, the matrix driver reconciles each retained runner checkpoint
against its original CLI arguments, source, configuration, prior, request count,
and export digest. A finalized runner result is promoted into the matrix index
even if the driver was interrupted before publishing it; it never starts another
attempt for that completed cell. Recovery also reads retained SQLite usage for
older interrupted attempts, including crashes that bypassed exception handling.
The full traces preserve actual usage; an unfinished request's retained database
is evidence even when no client result was returned. Complete same-identity
cells can be skipped on resume. A partial cell cannot resume its lost runtime
or prefix-cache state: an explicit `--restart CELL=NEW_ATTEMPT` declares a new
attempt before any new outcome. The original attempt remains incomplete and
its costs remain in the retained evidence. Complete cells cannot be rerun for
result selection. Source, provider, settings, seed, or retained-prior mismatch
requires a separately labeled experiment; timestamps are never rebased.

Both runners accept `--placement policy=local_first|device_aware|expected_wait`
and `--timing-samples FILE`. The version-one `relaymind.placement_prior` bundle
contains exact configuration partitions and source export byte digests/run IDs.
It derives timings and terminal outcomes from validated actual traces through
the shared live outcome projector; it creates no synthetic runs. Setting a
partition's `include_outcomes` to false makes a timing-only prior, which cannot
establish expected-wait applicability. Calibration bundles are frozen before
candidate execution. Every case imports its same configuration's prior before
startup; genuine startup warm-up samples are recorded separately. Latency uses
one gateway and learns online after seeding.

The exports include measured local phase time, prediction-error observations,
selection set scores, generations per completed run, external usage per
successful task (including failed-attempt cost), and missing-count denominators.
Successful completion latency excludes clarification waits and failed streams;
`stream_duration_ms` retains their transport duration. None of these deterministic
checks is live model evidence or permission to change defaults.

Generation summaries report measured and unobserved completed-run counts. If any
completed run lacks generation observations, the overall generations-per-completed-run
ratio is null; the separately labeled measured-only ratio and observed actor/purpose
counts remain available. The prior importer validates every partition's structure
and checks accepted/call model identities and recorded provider/answer options
against its configuration. Legacy missing accepted local-model identity may use
actual local calls; an explicit conflicting identity cannot use that fallback.


### Recovering a finalized cell with a separate controller

Both matrix scripts accept `--measurement-root PATH` (spec 14.4.5, RM16/RM17/RM25).
Run the reviewed controller script from its own clean checkout and select the
original clean measurement checkout explicitly. Runner/configuration/package
imports and inference subprocesses use the measurement checkout; M7's shared
reconciliation helper stays in the controller checkout. Its separately declared
criteria checkout and revision still identify C6. Use the original interpreter
path so recorded runner argv remains exact.

`--reconcile-only` validates existing checkpoints, exports, source/configuration,
cost and priors without reading provider credentials or starting a model. It is
mutually exclusive with `--execute` and refuses `--restart`/`--provider-env`.
Only retained finalized cells are recovered; unstarted cells stay unstarted and
partial or stale evidence is refused. For example:

```bash
/path/to/original/python /path/to/controller/scripts/run_m8_matrix.py \
  --measurement-root /path/to/frozen-measurement \
  --out /path/to/existing-matrix --runtime /path/to/pinned/llama-server \
  --reconcile-only
```

The controller compares expected exported identity using the runners' existing
`<repo>`/`<work>` JSON serialization; raw checkpoint/source identity and artifact
bytes remain unchanged. The index retains a separate `controller_history` with
controller commit/script hashes, measured import paths, measurement identity,
original artifact digests and recovery time. Before recovery updates the index,
`controller-before-<sha256>.json` preserves its exact previous bytes, including
interrupted status and coordinator-failure timestamps. Later resumes verify this
snapshot and controller identity. Missing retained cost evidence is refused.

After reviewing reconciliation, resume with the same controller, measurement root,
output/runtime and original provider settings using `--execute`. Completed cells
are skipped. Keep the same M7 revision, criteria checkout/revision and reuse-index
arguments when reconciling or resuming M7. The fixed plans, counts and budgets do
not change; do not rename a completed calibration as a new attempt.

### M8 offline comparison report

`analyze_m8.py` reads a finalized M8 matrix (plan, index, attempt checkpoints and
exports, frozen priors) and writes a JSON analysis with a Markdown report next to
it (spec 12.1–12.3 and 14.4; RM16, RM25–RM28, RM32; AC19, AC21). It starts no
model, reads no credentials, opens or seeds no run store and never writes inside
the matrix. The JSON and Markdown outputs must both be new files: an existing
file, any path inside this checkout or the matrix, and (after the index is read)
any path under the matrix root or measurement checkout that the index recorded
are refused before anything is written. A file under the recorded checkout would
change its source fingerprint and break a later driver resume.

```sh
.venv/bin/python scripts/analyze_m8.py --matrix /path/to/m8 --out /tmp/m8-analysis.json
.venv/bin/python scripts/analyze_m8.py --inventory --matrix /path/to/m8 --out /tmp/m8-inventory.json
```

Acceptance comes before any comparison. All 31 cells must be complete: exact
driver argv, source/configuration identity, the exact retained prior identity
(compared with `load_prior` provenance of the relocated frozen prior, spec
14.4.5), frozen prior linkage to its own calibration export, export/checkpoint
byte digests, unique finalized request steps, the exact frozen `(case_id, mode)`
set with locked language and class, the seeded 120-request latency schedule with
its cold request and three warm-ups, required metrics, and frozen
corpus/prompt/fixture bytes equal to the measured revision. Incomplete or changed
evidence fails acceptance and is never turned into zero success, cost or timing;
the full report then refuses to write. Initial expected-wait prerequisites are
checked for presence only, because recomputing them needs a seeded run store.

A latency request without trace metrics stays in every denominator (spec 12.2) as
an unobserved finding when the runner recorded why: a timeout, an error without a
run (connection refused, transport error or an unexpected HTTP status), or an
error after the run started (including a failed trace read). The report counts
these per arm and per reason next to every latency verdict, and a placement or
streaming latency target is not met when the candidate arm has more timeouts,
errors or unobserved requests than its control. A request with no metrics and no
recorded timeout or error is missing evidence and fails acceptance (spec 14.4.4).

The matrix may sit under another root than the one its index recorded. Files are
found by the driver's `CELL/ATTEMPT/` layout; the recorded root is inferred from
the recorded argv, and recorded argv, paths and identities stay in the report as
provenance, unchanged. `--inventory` lists complete, incomplete, missing and
invalid cells with their attempts and physical cost, and produces no comparison
or verdict.

An accepted report pairs the 39 comparisons over the existing cells (placement
policies, example-first chain, local live corpus against its seeded buffered
control, none-class corpus and latency). Corpus pairs use `(case_id, mode)` with
gain, loss, both unchanged counts and measured class transitions; latency pairs
use the recorded seeded order, with each arm's full observed distribution and
finite-pair deltas. The streamed-failure headline counts `predicate_unmet` among
effective live rows only, overall, per measured class and per mode/language/class.
Missed confirmations sit next to reached confirmations and mutating tool starts
counted from the exported traces. Physical cost keeps calibration, candidate corpus and
candidate latency ledgers apart, includes interrupted attempts, and leaves unknown
usage unknown; latency cohort cost counts successful final answers, labeled as
such, never task success. Each section 12.3 hypothesis is met, not met or not measured, with
evidence cells and digests; hypotheses this matrix cannot measure name the
external evidence they need. Defaults are not changed by this report.

### Unsuitable detection on recorded evidence (#170)

`analyze_unsuitable.py` reads the phase 2 evidence package (spec 0.48 section
14.4.7; RM26, RM27). It unpacks each M8 corpus attempt into a temporary
directory, replays the two detection rules of spec 0.48 with the gateway's own
functions (`classify_warmup`, `project_stage_outcome`, `stage_status`) over the
recorded run stores, and measures the section 12.3 unsuitable hypothesis from
the recorded local-first constrained cells against the none-class cells with
the pairing code of `analyze_m8.py`. It starts no model and writes only its
output, which must not exist yet.

```sh
.venv/bin/python scripts/analyze_unsuitable.py \
    --evidence docs/evidence/2026-10-26-phase2 \
    --out docs/evidence/2026-10-27-unsuitable/analysis.json
```

`probes/probe_unsuitable_live.py` is the short live check of the class: it
starts the pinned runtime constrained (cold, then warm on a new gateway) and
normal, each gateway with `placement.detect_stages = true`, and records the
launcher lines, readiness, and a few auto and local_only requests. It needs
llama-server on PATH, the model artifact, and an external provider (the key is
read from `RELAYMIND_EXTERNAL_API_KEY`).

```sh
.venv/bin/python scripts/probes/probe_unsuitable_live.py \
    --out docs/evidence/2026-10-27-unsuitable/live-check-apple-m1.json \
    --external-base-url "$RELAYMIND_EXTERNAL_BASE_URL" \
    --external-model "$RELAYMIND_EXTERNAL_MODEL" --external-context-limit 128000
```

### M7 metric completion and fixed experiment matrix

`run_m7_matrix.py` plans the #128 experiments without reading credentials or
starting inference. Supply the full reviewed production revision R. The default
plan includes a separately reviewed criteria ablation revision; leave its hash
unset while planning, or use `--omit-criteria` to declare the historical-only
comparison. Execution refuses a missing experimental hash/checkout. The C6
checkout must implement the experiment-only section 14.4.4 exception; this
measurement patch does not create that revision or introduce a product flag.

```bash
.venv/bin/python scripts/run_m7_matrix.py \
  --out /tmp/relaymind-m7-measurements --revision FULL_REVIEWED_HASH \
  --criteria-revision FULL_EXPERIMENTAL_HASH
```

This is seven corpus cells (832 runs) and two latency cells (248 requests), with
baseline budgets and a conservative 3,120 external-call ceiling per uninterrupted
matrix. Omitting C6 is 728 runs and a 2,808-call ceiling. L0/L1 use the same seed
20261023, local runtime, no provider and no prior; only early_headers changes.
C1/C2/C3 change draft/many/prefetch individually; C4 combines those three; C5 has
no local model and uses example_match. All other settings remain the defaults.
Neither plan is measured evidence and token/currency cost is unknown.

Add `--execute --criteria-checkout /path/to/reviewed-ablation` and the existing
M8 `--runtime`/`--provider-env` options only after both revisions are reviewed and
clean. Outputs must be outside the checkout. `--restart CELL=NEW_ATTEMPT`
requires a new safe name and retains prior checkpoints, databases and costs.
Finalized runner checkpoints are reconciled without repeating the cell.

`--reuse-m8-index /path/to/m8/index.json` can reuse only the normal unseeded
calibration corpus for C0 and the none/buffered corpus for C5. Reuse checks source
and fixture bytes, runtime, model/manifest, effective settings, provider/context,
corpus/sidecar, modes and a null prior. It never accepts seeded placement as C0.
Valid reuse saves 260 corpus runs (572 incremental with C6), with an incremental
1,872-call ceiling. Foreign attempts retain their physical identities and costs.
M8 retains its 31 cells and existing options.

Reanalysis now requires a distinct output and preserves measured application,
configuration and previous analysis provenance, with source-byte SHA-256:

```bash
.venv/bin/python scripts/run_corpus.py \
  --regrade docs/evidence/2026-10-22-corpus-apple-m1.json \
  --out /tmp/relaymind-historical-reanalysis.json
.venv/bin/python scripts/analyze_m7.py \
  --matrix /tmp/relaymind-m7-measurements --out /tmp/relaymind-m7-analysis.json
```

The analysis is offline. It validates frozen case/mode coverage and immutable
checkpoint/export identity, renders all M7 metric tables plus historical
corrections by language/class/mode, and enumerates the section 12.3 hypotheses.
It writes a report even when required observations/cells are missing, then exits
unsuccessfully for failed reporting acceptance. A measured missed target is a
valid finding. Header-before-answer-token is a diagnostic: without a comparable
client/generation-start clock it does not prove the stronger generation-start
hypothesis. No default or live capability decision follows from these tests.
