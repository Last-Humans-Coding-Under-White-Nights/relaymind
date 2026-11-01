# #260 owner-run cohort: runs, grades and pending records

Spec 0.66 section 12.6.6 (RM16, RM40, RM42, RM44; AC31, AC33, AC36). Frozen
inputs are in [evaluation/assets/owner-run-260/](../../../../evaluation/assets/owner-run-260/);
preregistration commit `6b1c676b918b3308c434ff85c5e04bd00eea4f9d`
(2026-11-01T10:01:54Z). Prompts are `machine_drafted_owner_approved` and labels
`machine_dual_owner_approved`. The one qualification record
(`../m10-owner-run-260-adaptation.json`) is **pending**: it has no pull
request review yet and lifts nothing. No gate has passed.

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
- The run did not execute the configuration its report binds. The ONNX
  runtime build hash on the VPS differed from the reviewed manifest, so the
  runner of that run wrote a run-local manifest (the reviewed manifest with
  `runtime` and `device` replaced by the VPS's identities) and a run-local
  adaptation config naming it. Neither document nor its digest was recorded
  or committed. What the evidence binds is the preregistered configuration:
  `report.json` `on_config_digest` `1580e097…` and the freeze's
  `adaptation_config_digest` `7b3cb420…` name the reviewed adaptation config
  with manifest `420a2fcd…`; these are fixed by the preregistered freeze and
  cannot be rebound. The only trace of the executed model is the per-probe
  `artifact_hash` `d43c2aed…`, the digest of the reviewed manifest's file list
  (weights, tokenizer, license); it does not cover the runtime build or the
  device. Section 12.6.6 makes such a change a new preregistration, so this
  run is not a run of its preregistered on-arm configuration. Its numbers are
  diagnostic only. `run_episodes.py` now executes only the frozen adaptation
  config and its manifest, checked by digest; the ONNX backend rejects a
  different runtime (`embedding_runtime_mismatch`), and a new runtime needs a
  new preregistration. The record's `revisions.harness_sha256` (`ddbe5f39…`)
  names the `run_episodes.py` that ran, at the preregistration commit.

To regrade: rebuild the private catalog (see the cohort README), rebuild the
private resolver with `evaluation/assets/owner-run-260/bundle.py adaptation`
into a scratch directory (it rewrites the same `inputs.json`), then run
`python scripts/asset_whole_task.py grade-adaptation --report report.json
--inputs inputs.json --resolver PRIVATE/resolver.json --qualification-record
../../m10-owner-run-260-adaptation.json --output graded.json`.

The record first lacked `machine.os_version`, which the packaged
`QualificationRecordV1` schema requires, so `load_record` rejected it
(`invalid_qualification_record`). It now carries the report environment's
`os_version` (`Linux-7.0.0-38-generic-x86_64-with-glibc2.43`) and a new
`recorded_at`; no subject changed. `record.py` takes `os_version` from the
report environment and loads the finished record before keeping it, and
`tests/test_owner_run_260_scripts.py` loads every committed #260 record.

## Host tasks: blocked

The preregistered plan has 100 tasks × 3 arms on the MacBook
(`target_macos_m1_8gib`, OpenClaw 2026.10.7, external model `glm-5.3-flash`),
run through the journaled `run_host_plan` driver with the in-process executor
in `evaluation/assets/owner-run-260/run_host.py`.

- `recommendations` cannot run unattended: explicit choice needs a signed-in
  person in the native Control UI, and skill/MCP use is disabled on this host
  version (the two pending negative records). In `report-partial.json` the
  13 recommendations slots the run reached (h001–h013) are `not_run` with
  reason `person_choice_unavailable`; the other 87 are `not_run` with reason
  `interrupted` because the run stopped.
- Tasks h001–h014 ran (28 started rows) before the run was stopped:
  native 11 completed, 2 failed, 1 timeout; provider_only 3 completed,
  11 failed. All 14 failed or timed-out rows have reason `host_failed`; the
  86 remaining native and 86 provider_only rows are `not_run`/`interrupted`.
  The report records no finer cause for the provider_only failures, and
  their per-row host results were lost, so their cause is not in the
  evidence. The gateway ran with its shipped budgets, which the host freeze
  does not bind.
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
- **The run itself.** Run `run_host.py` with a new `--ledger` path (it starts
  at zero when the file does not exist), then `judge.py`: blind items, a
  committed map digest, then both judges, adjudication and apply. Then run
  `bundle.py host` and `grade-host`. The episode side used a private working
  copy on the VPS (repository, Python 3.12 venv, model files, release root);
  it holds no credentials.
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
  them; `apply` marks every started row that did not complete (failed,
  timeout, or `cancelled` at the cap) as not successful. The host freeze is
  enforced: `--model` and the provider, sampling and host documents in
  `--configs` must have the freeze's model and digests (`FreezeMismatch`:
  `model_mismatch`, `config_digest_mismatch`), each host is configured from
  its frozen document (the gateway listens on the frozen plugin `baseUrl`, and
  the plugin config has no diagnostics file), the effective host config is
  checked against it (`host_config_mismatch`), and a row whose config is not
  its arm's frozen configuration rejects (`freeze_config_mismatch`). A failing
  usage read stops the host turn before the error is raised. The guards have
  deterministic tests only (`tests/test_owner_run_260_scripts.py`).
- **A person-driven recommendations arm.** Without it the final gate stays
  blocked: it needs a signed-in person in the native Control UI to choose, and
  qualified skill/MCP use, which is disabled on OpenClaw 2026.10.7 (the two
  pending negative records).
- **Gateway budgets bound in the freeze.** The host freeze does not bind the
  gateway's budgets (for example the shipped 30 s `external_seconds`). A rerun
  should record them in the host freeze; changing them is a new
  preregistration.
- **Qualification.** It stays below `qualified` until there is a 300-asset
  acceptance catalog, configuration semantics (#240) and a reviewed record.

## Private text in the h014 capture

`host/host-runs/h014-native/host-result.json` is kept as captured: its
SHA-256 (`0de12dfe…`) is bound in `report-partial.json`, so editing it would
break the report's evidence binding. The runner replaced the exact run
directory path with `<run-dir>`, but the host cut one path in `stderr_tail` at
its head, so one fragment escaped: the tail of a local scratch directory
path, `…paces-relaymind-Milesone-10/<session-uuid>/scratchpad/i260/host-run/work2/…`,
where `<session-uuid>` stands for a UUID, in the file, that names a local
working session. The fragment holds no user name, host name, address or
credential, and the directory no longer exists. The other paths in the file
are `<run-dir>`, and the other UUIDs are the host's own session and run IDs.
`run_host.py` now also replaces any tail of the run directory path of 16 or
more characters.

## Token use

[token-usage.json](token-usage.json): about 3.18M of the 5M budget (drafting
0.13M, two annotators 0.74M, adjudication 0.15M, host rows 2.12M including
the pilot, plus a 43k estimate for unlogged drafting calls).
