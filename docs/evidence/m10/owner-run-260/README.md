# #260 owner-run cohort: runs, grades and pending records

Spec 0.66 section 12.6.6 (RM16, RM40, RM42, RM44; AC31, AC33, AC36). Frozen
inputs are in [evaluation/assets/owner-run-260/](../../../../evaluation/assets/owner-run-260/);
preregistration commit `6b1c676b918b3308c434ff85c5e04bd00eea4f9d`
(2026-11-01T10:01:54Z). Prompts are `machine_drafted_owner_approved` and labels
`machine_dual_owner_approved`. Both qualification records are **pending**:
they have no pull request review yet and lift nothing. No gate has passed.

## Adaptation episodes (`adaptation/`)

100 preregistered episodes on the Ubuntu VPS (`linux_host`), through the
journaled episode driver against the real recommendation service (lexical
primary) with the multilingual-e5-small ONNX adaptation stage.

| Measure | Result | Requirement |
| --- | --- | --- |
| Completed episodes | 100/100 | |
| Corrected paraphrase top-1 (on arm) | 51/200 = 0.255 (Wilson 95% 0.200–0.320) | ≥0.90 |
| Unrelated control top-1, off / on | 38/100 / 36/100 | |
| Control loss | 0.02 | ≤0.02 |
| Byte-identical reset | 100/100 | all |

`grade-adaptation` (with the pending record): `graded`, gate `not_applicable`,
assessed `provisional` with reasons `adaptation_config_semantics`,
`corpus_unqualified` and `qualified_evidence_review`. The paraphrase rate is
far below the 0.90 target, so this would be a known failure even if the
inputs were qualified.

Limits of this run, each of which keeps it provisional:

- The corpus is the 412-asset development catalog, not the 300-asset
  acceptance catalog.
- Corrections went through the driver's evaluation-only action registration,
  not the host's native `relaymind.recommendations` feedback RPC that section
  12.6.6 requires for episode corrections.
- The ONNX runtime build hash on the VPS differs from the reviewed registry
  identity (the implementation changed since that review), so the run used a
  run-local manifest with the actual build and device.

To regrade: rebuild the private catalog (see the cohort README), rebuild the
private resolver with `evaluation/assets/owner-run-260/bundle.py adaptation`
into a scratch directory (it rewrites the same `inputs.json`), then run
`python scripts/asset_whole_task.py grade-adaptation --report report.json
--inputs inputs.json --resolver PRIVATE/resolver.json --qualification-record
../../m10-owner-run-260-adaptation.json --output graded.json`.

## Host tasks: blocked

The preregistered plan has 100 tasks × 3 arms on the MacBook
(`target_macos_m1_8gib`, OpenClaw 2026.10.7, external model `glm-5.3-flash`),
run through the journaled `run_host_plan` driver with the in-process executor
in `evaluation/assets/owner-run-260/run_host.py`.

- `recommendations` cannot run unattended: explicit choice needs a signed-in
  person in the native Control UI, and skill/MCP use is disabled on this host
  version (the two pending negative records). Its rows are `not_run` with
  reason `person_choice_unavailable`.
- Tasks h001–h014 ran (28 started rows) before the run was stopped:
  native 11 completed, 2 failed, 1 timeout; provider_only 3 completed,
  11 failed. Most provider-arm failures are the gateway's default
  `external_seconds` (30 s) per external generation running out, after which
  the host gives up (`deadline_exceeded`). That gateway configuration was not
  bound in the host freeze; it is the shipped default.
- Executed rows cost about 30k–150k provider tokens each (the native arm
  re-sends a growing context with cached reads; the provider arm sends the
  full context on each call). The external-token cap would have stopped the
  run after about 35 tasks; a full 100-task, two-arm run would need roughly
  13M tokens (14 tasks used about 1.86M).
- The private run directory (journal, per-row evidence, host state, gateway
  store and the private catalog copy) was deleted from the shared scratch
  area while the run was in progress. Only the runner's in-memory report
  (`report-partial.json`, with every row's attempts, counts and evidence
  digests) and the evidence of h014 native survive. The final answers needed
  for blind success judging are gone, so no row was judged and the report was
  not graded: `grade-host` would block on the unresolved evidence. Rerunning
  the 14 tasks would cost about 1.9M tokens, which the 5M budget no longer
  covers after this loss.
- The native arm's zstd-compressed transcript events were not parsed for
  h005 at first, so that row has no attempts (unknown cost); the parser was
  fixed for later rows.

The deletion was caused by a cleanup of the shared scratch area outside this
run, not by the runner.

No host qualification record is written: there is no gradeable report to
bind.

### What a rerun needs

- **Budget.** About 13M provider tokens for all 100 tasks in the native and
  provider_only arms, or about 1.9M to redo h001–h014 alone. Blind success
  judging adds about 0.3M.
- **A fresh journal and private run directory** outside any shared cleanup
  area. The preregistered inputs (`evaluation/assets/owner-run-260/`,
  commit `6b1c676b918b3308c434ff85c5e04bd00eea4f9d`) are unchanged and can be
  reused; the journal binds them, so the 14 lost rows are rerun, not restored.
- **The private catalog and plugin release, rebuilt.** Rebuild the catalog
  as in the cohort README. Rebuild the plugin tarball with
  `npm run pack:release` in `integrations/openclaw`; its SHA-256 must equal
  the frozen package (`b066ae27…`).
- **The run itself.** Run `run_host.py`, then `judge.py`: blind items, a
  committed map digest, then both judges, adjudication and apply. Then run
  `bundle.py host` and `grade-host`. The VPS keeps `~/rm260` (repository copy,
  Python 3.12 venv, model files, release root) for the episode side; it holds
  no credentials.
- **The run guards added after review.** `run_host.py` takes `--lock` and
  `--release-root` instead of a cases file. It dispatches only prompts of the
  verified release whose lock digest the tasks and freeze name, checked on
  every start and resume. The external-token cap is enforced per row: a row
  starts only if its whole `--row-budget` (default 250k) fits. A turn is
  stopped as `cancelled`/`budget_cap` before another call (`--call-allowance`,
  default 64k) could pass it. Unknown consumption is charged at its worst case.
  The first run had none of these guards: it reserved 40k per row and charged
  usage after the turn, so one row (h010 native) used 245k. `judge.py`
  refuses to adjudicate or apply verdicts unless both judges gave a valid
  verdict for every blind item and every final verdict is consistent with
  them. The guards have deterministic tests only
  (`tests/test_owner_run_260_scripts.py`).
- **A person-driven recommendations arm.** Without it the final gate stays
  blocked: it needs a signed-in person in the native Control UI to choose, and
  qualified skill/MCP use, which is disabled on OpenClaw 2026.10.7 (the two
  pending negative records).
- **Gateway budgets bound in the freeze.** The provider_only failures come
  from the gateway's default 30 s `external_seconds`. A rerun that keeps the
  default should record it in the host freeze; changing it is a new
  preregistration.
- **Qualification.** It stays below `qualified` until there is a 300-asset
  acceptance catalog, configuration semantics (#240) and a reviewed record.

## Token use

[token-usage.json](token-usage.json): about 3.18M of the 5M budget (drafting
0.13M, two annotators 0.74M, adjudication 0.15M, host rows 2.12M including
the pilot, plus a 43k estimate for unlogged drafting calls).
