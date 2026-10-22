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

## Planned

Evaluation and benchmark scripts (spec section 12):

- Correctness corpus runner: 40 bilingual scenario pairs against frozen
  fixture snapshots, in auto, external_only, and local_only modes.
- Latency experiment runner: warmed, short-context, concurrency-one runs;
  reports p50, p95, sample count, and timeout fraction per platform.
- Benchmark report generator with the full environment manifest (OS, CPU,
  GPU, RAM, backend, model hashes, quantization, runtime revisions).

Scripts are added together with their tests, following TDD.
