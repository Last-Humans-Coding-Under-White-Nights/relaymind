# Release record

The release record of spec section 14 (spec 0.30). It keeps four things
apart: what is implemented, what the deterministic tests show, what was
measured live, and which targets are unmet or not measured. A passing
model-double test is never a live result, and a live result on one
machine is never a result for another (section 12.1).

State as of 2026-10-23, including the supplied Windows exports at
`d18745d` (#110). Nothing here is a production reliability claim
(section 12.3).

## Implemented behavior

- M1 to M5, as listed in [`ROADMAP.md`](ROADMAP.md): the llama.cpp and
  external Chat Completions adapters, the bounded local skill-and-tool
  loop, handoff and confirmation, restart recovery, the compatible and
  native APIs, the demo UI, the operator launcher
  (`relaymind serve --config`), the parallel workflow, and the caches.
- M6 evaluation tooling (#32, #33): the frozen correctness corpus
  (`evaluation/corpus.v1.json`, 40 English cases and 12 best-effort
  Chinese simple cases, digest in `evaluation/corpus.v1.lock.json`),
  the corpus runner (`scripts/run_corpus.py`), the latency benchmark
  (`scripts/latency_benchmark.py`), and the grading, summary, and
  environment code in `relaymind.evaluation`.

## Passing deterministic tests

- `python -m pytest` for the #110 documentation and evidence PR on
  current master: 3188 passed, 1 skipped, 12 live tests deselected,
  macOS 26.7, Python 3.12.14. `ruff check .` and
  `ruff format --check .` pass. No live Windows run was repeated for
  this validation; the supplied exports retain their recorded revision.
- `pytest` (model doubles, no live model): 3066 passed, 12 deselected
  (the `live` tests), on Apple Silicon macOS, Python 3.14.7, at the head
  of the #109 branch. `ruff check .` and `ruff format --check .` pass.
- CI runs the same suite on the three platforms of the workflow; the CI
  result is on the pull request, not recorded here.

## Measured live results

Each result names its machine, date, and export. macOS results ran on
Apple M1, 8 GiB RAM, macOS 27.0.1, llama.cpp b11785, Qwen3-0.6B Q8_0
(plus Q4_K_M for the AC08 local swap). Windows results ran on Intel
i7-1165G7, 16 GiB RAM, Windows 11 Pro (build 26200), llama.cpp b11785
(built from source), Qwen3-0.6B Q8_0, CPU backend only. Details are in
[`EVIDENCE.md`](EVIDENCE.md).

| Date | What | Export |
|------|------|--------|
| 2026-10-20, 2026-10-21 | M1 smoke tests (macOS and native x86-64 Windows), M2 routing probe and local traces | `docs/evidence/2026-10-2*-m1-*`, `…-m2-*` |
| 2026-10-22 | AC08 local swap, Q8_0 to Q4_K_M (#90) | `docs/evidence/2026-10-22-ac08-local-trace-*` |
| 2026-10-22 | AC08 external swap, Z.ai to bigmodel.cn, both `glm-4.6` (#109): the swap passed; all 195 gateway checks of the run held, 9 model checks failed. Both endpoints are Zhipu's, so a second vendor's API is not covered | `docs/evidence/2026-10-22-live-acceptance-ac08-external-apple-m1*` |
| 2026-10-22 | M3 to M5 live acceptance through `relaymind serve --config`, with Z.ai `glm-4.6` (#88, #104): all 90 gateway checks held, 9 model checks failed | `docs/evidence/2026-10-22-live-acceptance-apple-m1*.json` |
| 2026-10-22 | Correctness corpus, all 52 cases in auto, external_only, and local_only (156 runs, 246 external calls) (#32) | `docs/evidence/2026-10-22-corpus-apple-m1.json` |
| 2026-10-22 | Latency benchmark, local path, 120 measured requests (#33) | `docs/evidence/2026-10-22-latency-apple-m1.json` |
| 2026-10-23 | Launcher start and live acceptance on x86-64 Windows (#110, AC15): 5 gateway starts, 67/68 gateway checks held, 6/30 model checks passed; no external model available (7 scenarios `not_run`); UI session errored (Playwright Chromium not installed) | `docs/evidence/2026-10-23-live-acceptance-windows-x64.json` |
| 2026-10-23 | Latency benchmark on x86-64 Windows, local path, 120 measured requests (#110, AC15) | `docs/evidence/2026-10-23-latency-windows-x64.json` |

Key measured numbers (Apple M1 only):

- Local completion, English simple subset: 4/12 in local_only and 4/12
  in auto. Chinese (best-effort): 3/12 and 2/12.
- Tool correctness, English simple subset: 11/12 in local_only, 12/12 in
  auto and external_only.
- Missed mandatory confirmations: 0 in all 156 corpus runs.
- Task success, all English cases: 10/40 local_only, 24/40 auto, 20/40
  external_only.
- First model action, measured runs: p50 442 ms, p95 508 ms (90 of 120
  samples had one); 0 timeouts; 50 of 120 samples ended other than
  their prompt label expects.

Key measured numbers (x86-64 Windows, Intel i7-1165G7, CPU backend):

- Launcher start to `/healthz`: 5.25 s to 10.23 s across 5 starts;
  `/readyz` 200 on all starts. Acceptance harness llama-server start to
  health: 1.609 s; latency-run start to health: 1.984 s, including
  1724 ms model load by the runtime log clock.
- Acceptance harness: 67/68 gateway checks held, 6/30 model checks
  passed. The one failed gateway check is a missing cache label on a
  run that ended before routing completed. No external model was
  available, so 7 of 22 scenarios are `not_run`. The UI session errored
  (Playwright Chromium not installed on this machine).
- First model action, latency benchmark: p50 2000 ms, p95 3610 ms (66
  of 120 samples had one); 0 timeouts; 76 of 120 samples ended other
  than their prompt label expects. All 60 simple-prompt runs and 16 of
  30 clarification runs ended `needs_external`; the latency export
  does not include reason codes or exhausted budgets.

## Unmet or not measured targets

- 80 percent local completion on the English simple subset (section
  12.3): **not met**, 4/12. Qwen3-0.6B is not suitable for that claim
  on this corpus.
- 95 percent correct tool-and-argument proposals on the English simple
  subset: met in auto (12/12), **not met** in local_only (11/12).
- 500 ms hypothesis, p95 first model action (section 12.1): **not met**
  on Apple M1 (p95 508 ms; 30 of 120 samples had no model action) or on
  x86-64 Windows (p95 3610 ms; 54 of 120 samples had no model action).
- Reproducible benchmark report from both target platforms (section
  12.3, AC15): **closed** (#110). The macOS latency benchmark is
  recorded (#33); the x86-64 Windows latency benchmark is recorded
  (#110). The 500 ms hypothesis is not met on either platform. No
  Windows correctness corpus ran; its 0/60 simple latency completions
  are separate from the macOS corpus's 4/12.
- Native review of the Chinese material: not done (not required, spec
  section 9).
- Monetary cost: not estimated (optional in section 12.2; no dated rate
  source was used).
