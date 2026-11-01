# Release record

The release record of spec section 14 (spec 0.30). It keeps four things
apart: what is implemented, what the deterministic tests show, what was
measured live, and which targets are unmet or not measured. A passing
model-double test is never a live result, and a live result on one
machine is never a result for another (section 12.1).

Historical measurement baseline as of 2026-10-23, including the supplied Windows exports at
`d18745d` (#110). Nothing here is a production reliability claim
(section 12.3).

## Implemented behavior

- M10 customer navigation updated 2026-10-31: [English](asset-selection.md),
  [简体中文](asset-selection.zh-CN.md), [source-backed run/compare](SOURCE_BENCHMARK.md),
  [formal JSON/Markdown comparison](ASSET_BENCHMARK.md) and
  [privacy/recovery](asset-privacy-security.md). Catalog discovery, v2 lexical/HTTP
  recommendations and the installed qualified GitHub refresh profile have their
  current scope in [ROADMAP](ROADMAP.md#m10--multilingual-asset-recommendations-spec-064).
  The exact Linux CPU embedding bundle is registered; other native model and
  platform qualifications, the installed M10 skill/MCP/approval/correction
  walkthrough and whole-task/platform gates are separate pending deliverables.

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

- `python -m pytest` for the external request options branch
  (`eval/external-request-options`, spec 0.46): 4854 passed, 1 skipped,
  13 live tests deselected, on Linux x86_64 (Ubuntu 26.04.1), Python
  3.14.4, without `llama-server` on PATH, so
  `tests/test_live_acceptance.py::test_gateway_start_and_healthy`
  skips. `ruff check .` and `ruff format --check .` pass. This is not
  the Apple M1 machine of the measured results; no run of this branch
  on it is recorded here.
- `python -m pytest` for the phase 2 evidence and spec 0.45 branch
  (`m8/evidence-decisions`): 4781 passed, 0 failed, 13 live tests
  deselected, on Apple M1, macOS 27.0.1, Python 3.14.9, with
  `llama-server` on PATH. Without it,
  `tests/test_live_acceptance.py::test_gateway_start_and_healthy` skips
  (4780 passed, 1 skipped). `ruff check .` and `ruff format --check .`
  pass.
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
| 2026-10-25, 2026-10-26 | Phase 2 M8 matrix (31 cells) and M7 matrix (9 cells), Z.ai `glm-4.6` (#134, #128, #125) | `docs/evidence/2026-10-26-phase2/` |
| 2026-10-25, 2026-10-26 | External model comparison in the default configuration: glm-4.6, glm-5.3-flash, and glm-5.3-flash with `reasoning_effort` low (corpus 88, 85 and 90 of 156) | `docs/evidence/2026-10-27-external-model/` |
| 2026-10-29 | M9 OpenClaw evaluation through the real host, four arms, `glm-5.3-flash` (#139): 144 of 144 attempts succeeded; 0 local turns in every RelayMind arm (relaymind_auto: `context_limit`); revision `2751124`, recorded dirty | `docs/evidence/2026-10-29-openclaw-eval-apple-m1/` |

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

### Phase 2 evaluation (#134, #128, #125)

Implemented: exact-configuration imported timing/outcome priors, measured
serial local active time, trace-derived placement/cost/selection metrics, M7
proposal and ownership metrics, durable matrix controllers with recovery, and
offline M7/M8 analyses (`scripts/analyze_m7.py`, `scripts/analyze_m8.py`).

Measured on Apple M1 at revision `741a191`, 25 to 26 October 2026, with Z.ai
`glm-4.6`: the full M8 matrix (31 cells) and M7 matrix (9 cells), one pass per
cell; package in `docs/evidence/2026-10-26-phase2/`, details in
[`EVIDENCE.md`](EVIDENCE.md). Specification 0.45 keeps every default:

- `local_first`: with the router chain, device_aware and expected_wait lost
  paired corpus success in both configurations (normal: 83/156 and 84/156
  against 88/156); with the example-router chain no policy gained more than one
  case, and constrained expected_wait lost ten.
- `model_router`: the example-router chain matched or beat it in five of six
  corpus pairs and answered sooner at p50, but its p95 was more than twice as
  slow, and its Jaccard benchmark gate is not measured.
- Buffered answers: live streaming 74/156 against 88/156 (14 `predicate_unmet`)
  and none class 73/104 against 77/104.
- Final answer pass, one call per generation, no prefetch: the narrow M7
  hypotheses for draft, many calls and prefetch were measured, but each lost
  auto task success against the baseline (36, 38 and 38 of 52 against 39).

Unmet or not measured: English simple local completion 4/12 normal and 0/12
constrained (target 80%);
p95 first model action 500 ms; the unsuitable-machine device-aware hypothesis
(no case was measured unsuitable, even on the CPU-only two-thread
configuration); none-class streaming; early headers before the first
generation; example-matcher Jaccard and tool narrowing on the section 12.4
benchmarks. No native Windows phase 2 measurement exists. No deterministic
test establishes useful model performance or provider parity.

### OpenClaw provider plugin (#145; RM33, AC24, section 12.5)

The artifact: `relaymind-openclaw-provider-<version>.tgz`, built with
`npm run pack:release -- <dir>` in `integrations/openclaw/`. Next to it are
`SHA256SUMS` and `release.json`, which names the pins, the engines, the
packed files, the npm integrity, and the Node that built it. Two builds of
one checkout are the same bytes (`tests/test_openclaw_install.py`). It holds
the built `dist/*.js`, `openclaw.plugin.json`, the README (the operator
guide), and `package.json`, and has no runtime dependency (`openclaw` is the
peer the host provides). It is not published to a registry. CI uploads it
from each runner. Declared compatibility: OpenClaw 2026.10.7 only
(`openclaw.compat.pluginApi` and the peer dependency), Node
`>=24.22.1 <25 || >=26.12.1`.

Supported environments. A row says what ran on that environment, never more.
The gateway in every host check below answered from model doubles. The
live model evidence for the plugin is two runs on macOS. One live turn at
`6fc0954`, through the packaged plugin with no external model, ended
needs_external / context_limit at the 8192 local context before any model
call, so it completes no task. The #139 measurement at `2751124` (recorded
dirty) ran the frozen tasks through the real host with the external model
`glm-5.3-flash`: 144 of 144 attempts succeeded, with 0 local turns
(`docs/evidence/2026-10-29-openclaw-eval-apple-m1/`; the section below).

| Environment | OpenClaw runtime | Node against the host's range | Status |
| --- | --- | --- | --- |
| macOS 27.0.1 arm64, Apple M1 (user's machine) | 2026.10.7 (157d8c2): npm prefix install at `a52f8bf`, the package's devDependency at `6fc0954` | v26.12.1, in `>=26.12.1` | an earlier revision (`a52f8bf`): package checks, install, inspect, token, 401 reconnect, and interrupt passed; `agent exec` could not find the model, and the tool policy scenarios failed. This revision (`6fc0954`) passed, over doubles: the install check of the release artifact with `--target-platform` (all 15 recorded steps), the harness scenarios `text-tool`, `tool-deny`, `exec-deny`, `exec-full`, `abort`, and `external-failed` (`tool-approve` failed correctly: no headless approval route); one live turn through the packaged plugin ended needs_external / context_limit with 0 model calls (`docs/evidence/2026-10-29-openclaw-mac-qualification/`) |
| Windows, native | 2026.10.7 documents a native Windows CLI and Gateway, WSL2 optional | `not_run` | `not_run`: no Windows machine. Support gap |
| Windows, WSL2 | not checked | `not_run` | `not_run`. Never reported as native Windows |
| Linux x86_64 VPS, Ubuntu 26.04.1 (not a target platform of section 1) | 2026.10.7 (157d8c2), the package's devDependency | v24.22.1, in `>=24.22.1 <25` | artifact packed with `npm run pack:release` at `393a6bf`, then the install check passed all 15 recorded steps (version, the log and Gateway port isolation, install, configure, enable, inspect, token, discovery, a text and a host tool round trip, disable, uninstall, removal of the config entry and the credential; `docs/evidence/2026-10-28-openclaw-vps-functional-145/`): a Linux VPS functional check (not a supported-platform qualification) over doubles |
| GitHub Actions `ubuntu-latest`, `macos-latest`, `windows-latest` runners (not a user's machine) | 2026.10.7, the devDependency | v24.22.1, in `>=24.22.1 <25` | CI run 40652444733 at PR #182's merge commit `78fc94d`: the install check passed all 15 recorded steps on Linux 6.17.0-1022-azure x86_64, Darwin 25.6.0 arm64, and native Windows 2025Server AMD64 (`docs/evidence/2026-10-28-openclaw-install-check-github-runners/`): a GitHub-hosted runner functional check over doubles (not a user-machine qualification). Later runs are on their pull requests |

Support gaps: no qualification of this revision on Windows exists, and
none is planned now (no Windows machine is available). Windows on a user's
machine (native, which the pinned host documents, and WSL2 if used) has
never run the host; the native `windows-latest` runner ran the install
check, which is not a user-machine qualification. Each stays a gap until
an export is recorded in [`EVIDENCE.md`](EVIDENCE.md#openclaw-plugin-platforms-145), with the OS
build, the host version, the Node version against the host's range, and
whether Windows was native or WSL2. The operator commands are in
`integrations/openclaw/README.md` ("Install check").

### M9 OpenClaw evaluation (#139; RM16, RM25, RM26, RM33; AC24; spec 0.50 section 12.5)

Implemented: the frozen tasks (`evaluation/openclaw_tasks.v1.json`, 6
English and 6 best-effort Chinese tasks: text, a host `read`, a host
`write`, and a host `exec` under `deny` and under `full`; 3 repeats, seed
139, digest and order digest in `evaluation/openclaw_tasks.v1.lock.json`),
grading and the arm summary (`relaymind.evaluation.openclaw`), and the
runner (`scripts/run_openclaw_eval.py`) for the four arms `direct`,
`relaymind_external_only`, `relaymind_auto`, and `relaymind_no_local`. They
are covered by deterministic tests over a stub host and doubles
(`docs/TESTING.md`).

Results: one run, on the user's Apple M1 (8 GiB, macOS 27.0.1 arm64,
Node v26.12.1, OpenClaw 2026.10.7 from the devDependency), recorded in
`docs/evidence/2026-10-29-openclaw-eval-apple-m1/` (`docs/EVIDENCE.md`, "M9
OpenClaw evaluation"). External model `glm-5.3-flash`; local model
`qwen3-0.6b` Q8_0; the full task set, 3 repeats, seed 139. All four arms
ran, and 144 of 144 attempts succeeded: 18 of 18 per arm and language,
English and Chinese, with 0 host failovers, timeouts, interrupts, or
ok-but-failed attempts. Every RelayMind arm had 0 local turns:
relaymind_external_only by `execution_mode` (65 gateway runs),
relaymind_auto by `context_limit` (70), and relaymind_no_local by
`no_local_model` (71); external calls equal gateway runs. Integration
overhead (relaymind_external_only against direct, 18 pairs each): median
completion difference -732 ms in English and -55 ms in Chinese, 18/18
against 18/18. The local-inference comparisons compare no local inference,
because no local turn ran. The revision `2751124` was recorded dirty: two
files unrelated to the run were being written in the checkout while it ran
(its evidence test module and the CI line that lists it); the runner, the
plugin, the gateway, and the host used neither. This is one run on one
machine: reduced latency, lower cost, and local completion quality remain
hypotheses (section 12.5). Windows (no machine) is the gap; a constrained
configuration (optional) and a plugin approval answered by an operator did
not run. The operator command is in `docs/TESTING.md` and
`integrations/openclaw/README.md` ("M9 evaluation").

Limits known before measurement: the local context stays at 8192 tokens
(user decision), and every OpenClaw request measured before the run (0 of
12 fit; 9,384 to 10,577 tokens with the pinned tokenizer, the macOS
qualifications of 2026-10-27 and 2026-10-29) is over it; the live AC24
turns in auto without an external model ended needs_external /
context_limit with 0 model calls. relaymind_auto was therefore expected to
send every OpenClaw turn to the external model. The run showed it: 0 local
turns of 70 gateway runs, every one with reason `context_limit`, reported
as the result, and its comparison with relaymind_external_only is one
without local inference. In the run the direct arm's host provider entry
worked (36 of 36, 0 host failovers); the `write` tool's argument names are
still not checked (the task accepts any arguments for `write`).

Measured on the macOS qualification: every OpenClaw request it measured
(3 host requests and 7 probe captures) was 10,500 to 10,577 tokens with the
pinned tokenizer, over the local limit of 8192. The 2026-10-29 live turns
through the packaged plugin measured 9,384 and 9,389 tokens, also over it.
An OpenClaw turn therefore goes external in `auto` and is `needs_external`
in `local_only`. The local context stays 8192.
