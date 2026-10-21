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

## Planned

Evaluation and benchmark scripts (spec section 12):

- Correctness corpus runner: 40 bilingual scenario pairs against frozen
  fixture snapshots, in auto, external_only, and local_only modes.
- Latency experiment runner: warmed, short-context, concurrency-one runs;
  reports p50, p95, sample count, and timeout fraction per platform.
- Benchmark report generator with the full environment manifest (OS, CPU,
  GPU, RAM, backend, model hashes, quantization, runtime revisions).

Scripts are added together with their tests, following TDD.
