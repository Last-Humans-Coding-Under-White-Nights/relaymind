# Scripts

## Current

- `gen_model_manifest.py` — generate a model artifact manifest from a
  GGUF file (RM18, spec section 6). Hashes are computed from the
  artifact, never written by hand. Manifests live in `models/`; the
  GGUF files themselves are not checked in (`*.gguf` is ignored).

## Planned

Evaluation and benchmark scripts (spec section 12):

- Correctness corpus runner: 40 bilingual scenario pairs against frozen
  fixture snapshots, in auto, external_only, and local_only modes.
- Latency experiment runner: warmed, short-context, concurrency-one runs;
  reports p50, p95, sample count, and timeout fraction per platform.
- Benchmark report generator with the full environment manifest (OS, CPU,
  GPU, RAM, backend, model hashes, quantization, runtime revisions).

Scripts are added together with their tests, following TDD.
