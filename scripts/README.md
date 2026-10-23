# Scripts

## Current

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
- `latency_benchmark.py` — the latency experiment (#33, spec 0.28
  section 12.2) on the local path: one gateway started with
  `relaymind serve --config`, routing cache off, no external model,
  concurrency one. It sends the twelve prompts of
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

- `run_corpus.py --regrade-draft-pass` — the offline draft-pass
  counterfactual (#115; RM31, spec 0.31). It grades the stored traces of
  a corpus export as if the skill-stage draft had been published as the
  answer: for each auto and local_only run with a final generation it
  takes the draft, applies the RM31 guards (no caller answer option, the
  run's language, no tool-call JSON; the language and tool-call guards are
  the functions of `relaymind.orchestrator.performance` that the gateway
  uses; a run with no draft text, or with no language in its first
  decision, is blocked too, and the export's `guards` explains every block
  reason), and grades a trace that ends with the draft as the local answer,
  whose added events pass `validate_trace_event` (`run.completed` records
  `answer_pass_applied` `draft`). A blocked run keeps its measured grade,
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
