# Scripts

Evaluation and benchmark scripts live here.

## compare_adapters.py

Runs the same prompt list against two or more adapter configurations
and writes one JSON export per config: spec 12.1 metrics per
generation (first transport event, first model action, first answer
token, completion latency, finish reason) plus per-call token usage,
with the adapter identity (model id, quantization, runtime version
from the model manifest) attached.

```bash
python scripts/compare_adapters.py config.json --out-dir exports/
```

See the module docstring for the config format. This is engineering
instrumentation for development comparisons (issue #37): sequential,
unwarmed, and never publishable as measured results (spec 12.3). The
formal benchmark with warm-ups, randomized order, and percentiles is
the M6 runner below.

## Planned (spec section 12)

- Correctness corpus runner: 40 bilingual scenario pairs against frozen
  fixture snapshots, in auto, external_only, and local_only modes.
- Latency experiment runner: warmed, short-context, concurrency-one runs;
  reports p50, p95, sample count, and timeout fraction per platform.
- Benchmark report generator with the full environment manifest (OS, CPU,
  GPU, RAM, backend, model hashes, quantization, runtime revisions).

Scripts are added together with their tests, following TDD.
