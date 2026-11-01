# #259 owner-run early installed-skill trial on the Apple M1 8 GiB MacBook

**Status: provisional early-trial bundle, graded `blocked`; record pending,
not preregistered under section 12.6.6 and non-qualifying.** One frozen task,
three planned rows, run on the owner's MacBook on 1 November 2026 by the
operator tooling on the owner's instruction, with no
person in the loop. Spec 0.66 sections 12.6, 12.6.5, 12.6.6; RM16, RM39, RM40,
RM44; AC30, AC31, AC36. An early trial never asserts the final gate, and this
one-task, one-sample result says nothing general about the arms.

| Item | Value |
| --- | --- |
| Machine | `target_macos_m1_8gib`: Darwin arm64, `Apple M1`, 8589934592 bytes, macOS 27.0.1 (26A434); hostname only as SHA-256 in the record |
| Host | pinned OpenClaw 2026.10.7 (`npm ci` of `integrations/openclaw/package-lock.json`), isolated home under the operator root `/private/var/tmp/relaymind-trial-259` |
| Plugin | `relaymind-openclaw-provider-0.1.0.tgz`, SHA-256 `b066ae27…f35d` (`release/`, and in the bundle) |
| External model | the owner's OpenAI-compatible endpoint (base URL and key never written), `glm-5.3-flash` for all three arms |
| Frozen inputs | `frozen/inputs.json`, committed in `21e93e3` before the first row (09:41:39Z). This is not a section 12.6.6 preregistration (see "Preregistration gap" below), so the record's `preregistration` is null. The record's `runs_started_at` 09:46:02.976Z is the end of the native row's first model turn, the earliest recorded attempt time: the host transcript has no dispatch time, so it is an upper bound on the first dispatch. The native row began no earlier than 09:45:21.5Z (its last turn ended 09:46:38.717Z after 77.2 s elapsed), still after the commit |
| Bundle | `bundle/` (`report.json`, `grade.json`, `inputs.json`, `SHA256SUMS`); `grade.json` was graded without the record (see "Grade and record" below) |
| Record | `../m10-early-trial-m1-openclaw-2026.10.7-20261101.json`, scope `host_trial`, pending, not preregistered and non-qualifying; host_trial approval does not itself require preregistration (see below) |

## Profile (spec 0.66 section 12.6.6)

- **Prompt:** `machine_drafted_owner_approved`. One case (`trial-en-001`,
  `en_to_en`, held_out) drafted by `machine-drafter` (`glm-5.3-flash`,
  temperature 0, `machine/drafter.md`) from the offered catalog projections and
  the case plan (`machine/case-plan.json`) only. The request and response bytes
  are the authorship evidence (`inputs/evidence/drafting.*.json`). The draft was
  kept as written: "We're planning a road trip to the coast this Saturday and I
  need to decide whether to leave in the morning or afternoon. Can you check the
  forecast and tell me if rain is expected before noon?" It names no place, and
  Saturday is six days after the run date.
- **Labels:** `machine_dual_owner_approved`, `same_family` (the provider lists
  only the `glm` family, `owned_by: z-ai`). Annotator A `glm-5.3-flash`
  (`machine/annotator-a.md`), annotator B `glm-4.7` (`machine/annotator-b.md`,
  written independently), adjudicator `glm-5.3` (`machine/adjudicator.md`).
  Both annotators judged the case positive with the `weather` skill; the
  adjudicator included it. Agreement 1/1, Cohen's kappa null (chance agreement 1).
- **Model pinning:** the provider publishes no dated IDs. Each model is pinned
  by the exact ID its response returned (`machine/calls/*.response.json`), the
  date of use (2026-11-01) and the provider listing `machine/models-listing.json`
  (SHA-256 `2c3b6fe54616a7e666f2bbbeaea2358e9dcecd6980fdda89f8c0b82225719c3c`).
- **Owner:** recorded as `reviewer` only (approver), not author or annotator.
  Spec 12.6.6 needs the owner's approval of the cases and labels before
  preregistration; this trial had none before its rows, so the record stays
  pending and lifts nothing (see "Preregistration gap").
- **Success judging:** both machine annotators judged the anonymized transcripts
  blind (arm markers replaced, order by SHA-256 of `{seed,task_id,blind_id}`);
  the blind-map digest was committed in `b5ef7a9` before judging and the map
  (`machine/blind-map.json`) opened afterwards. They agreed on both rows, so no
  adjudication call was made (`machine/success-verdicts.json`).

## Rows (frozen plan, seed 259)

| Ordinal | Arm | Status | Success | Calls | External tokens | Elapsed |
| ---: | --- | --- | --- | ---: | ---: | ---: |
| 1 | native | completed | true | 2 | 19,246 | 77.2 s |
| 2 | provider_only | failed (`host_exit_nonzero`) | false | 7 | unknown (26,006 over the 3 finished calls) | 207.1 s |
| 3 | recommendations | not_run (`person_ingress_unavailable_macos`) | null | 0 | none | none |

Each row's `peak_bytes` is `ru_maxrss` of `RUSAGE_CHILDREN` read in that row's
`observe_row.py` process: the peak RSS of its largest finished child. On the
RelayMind arms that includes the earlier `openclaw models auth paste-api-key`
child, and it never includes the separately started RelayMind gateway. It is
not a per-turn or whole-arm memory figure and is not comparable across arms.

Grade (`bundle/grade.json`): `blocked`, assessed `provisional`
(`configuration_semantics`, `corpus_unqualified`, `qualified_evidence_review`).
The success gate is blocked by the not-run row. The cost gate is blocked by
unknown usage. Gains 0, losses 0.

- **Row 1, native:** the host read the `weather` skill, asked which coast is
  meant and said its forecast source does not reach Saturday. Both judges: success.
- **Row 2, provider_only:** three external calls finished. The next four were
  each cut off after 30.0 s by RelayMind's default per-generation budget
  (`external_seconds` 30.0, recorded in every run; `model.finished` error
  `time_budget`). The host retried, then ended with "LLM request timed out" and
  no answer. Their usage is unknown, so the arm's token total is null. This is a
  real outcome, kept as it is. It also shows that the arms' per-generation
  limits were not equal: the native provider entry had no 30 s cap.
- **Row 3, recommendations:** not started. The explicit recommend-and-choose
  step needs a host-verified person signed in to the native Control UI (spec
  7.6.1). The only sign-in harness (`host-person-qualification`, AuthCrunch 1.5.0
  Linux x86-64) refuses this machine (`linux_runner_required`). A shared token or
  owner pairing is not a person (spec 12.6.5).

## Findings

1. **Kit install order.** On a fresh host, `trial-install` fails because the
   pinned host installs the plugin disabled until it is configured
   (`install-unconfigured.json`). Configure the plugin entry and run
   `plugins enable relaymind` first; the trial's own install then passes.
2. **8 of 49 bundled skills admitted.** RelayMind's `local_directory`
   projection rejects 32 OpenClaw originals with `invalid_markup` and 9 with
   `repeated_line`, and one rejection fails the whole scan
   (`inputs/captures/projection-exclusions.json`). The catalog is the 8 admitted
   skills; the native host is restricted to the same 8 (`skills.allowBundled`).
3. **Native usage source.** The host persists provider usage for each assistant
   message in `agents/main/agent/openclaw-agent.sqlite` (`transcript_events`,
   large events zstd-compressed). For RelayMind-provider turns that record shows
   zero, so RelayMind arms use TraceEvent usage (`usage-relationship.md`).
4. **Default 30 s external budget.** It ended the provider_only row (above). A
   fuller run needs one equal per-generation limit on every arm in a new freeze.
   The gateway does not raise a budget silently (RM03).
5. **Gateway working directory.** Without `[sandbox]`, the gateway resolves its
   default sandbox seed relative to its working directory. Start it from the
   checkout root, or name `[sandbox]` in the config.
6. **Machine name in captures.** The host's system prompt, forwarded to the
   gateway and kept in its trace, contains the Mac's computer name.
   `observe_row.py` replaces the computer, local host and host names with
   `<machine-name>` in every kept evidence file. The row 2 trace was redacted
   this way before bundling.
7. **`source_revision`.** The corpus verifier requires it to equal the record's
   `source_version` (null for these skills), which `label-freeze` does not check.

## External usage (provider-reported prompt plus output tokens)

| Step | Tokens |
| --- | ---: |
| preparation: two smoke turns and two rehearsal rounds (scratch, not evidence) | 379,315 |
| drafting, two annotators, adjudicator | 6,499 |
| trial rows 1 and 2 (finished calls) | 45,252 |
| blind success judging (two judges) | 15,080 |
| **known total** | **446,146** |

Four timed-out row 2 calls have unknown usage. If each used a full prompt,
that is about 40,000 more. The owner's cap was about 1,000,000.

**A fuller early trial would need**, per task: about 50,000 to 60,000 tokens for
native plus provider_only, about the same again for a recommendations row,
and about 15,000 for judging. It would also need an equal per-generation limit
on all arms (finding 4), a person sign-in for the recommendations arm (the Linux
person-ingress harness, or a qualified macOS path), and several tasks per lane.
The acceptance cohort (at least 100 paired tasks, at least 20 per lane) is far
beyond this cap.

## Files

- `frozen/`: the frozen companion manifest and every document and evidence file.
- `bundle/`: the kit's bundle, with the trial's own `preflight.json` (all seven
  checks passed) and `install.json` (passed).
- `inputs/`: the catalog with its 8 MIT originals and statements, `cases.json`,
  `labels.json`, the label templates, judged templates and worksheet
  (`inputs/labelling/`), the rubric, the row setup, the arm configurations and
  the host captures.
- `machine/`: every prompt template, every recorded model request and response,
  the model listing, the blind-map digest and map, and the success verdicts.
- `build_trial_inputs.py` (`draft`, `freeze`, `record`), `observe_row.py`,
  `machine_passes.py`: evidence helpers for this trial. They only arrange
  files and calls for the existing kit and checker.

**`machine_passes.py` after the run.** The success judging ran with the script
as committed in `b5ef7a9` (unchanged in `2b4e439`, the record's
`repository_commit`; the script is not in `bundle/SHA256SUMS` or the record
subjects, and `harness_sha256` binds `build_trial_inputs.py`). A later review of #276 hardened `judge`: each judge
must give exactly one verdict per blind ID with a JSON boolean `success`, every
disagreement needs exactly one adjudication with a nonempty reason, and the
opened blind map must cover the judged IDs; otherwise it stops with a closed
error before any row is written. The earlier script would have taken judge A's
verdict for an omitted adjudication and read the string `"false"` as true. The
committed `machine/success-verdicts.json` passes the stricter check unchanged
(both judges agreed on both rows with boolean verdicts, no adjudication), so
no committed output or bundle file changed
(`tests/test_m1_trial_machine_passes.py`).

The same review made two more changes for later passes, with no committed
output changed. `machine_passes.py` keeps an earlier call's bytes as
`<name>.superseded-<n>.*` instead of overwriting them (spec 12.6.6: a rejected
draft stays under `machine/calls`, but the archived builder does not include
superseded pairs in the catalog or lock), and `draft-cases` sets each case's
`language_tags` and `code_switched` from its lane (the one `en_to_en` case is
unchanged). `build_trial_inputs.py record` was brought up to the current
checker: the record machine carries `os_version` (added to the committed record
by hand in `025c813` when spec 0.66 began to require it), the post-write check
passes the catalog people to `check_labels`, and attempt times are ordered as
times. The record's `harness_sha256` (`533273ac…d6b3`) binds the script bytes
committed in `2b4e439` (unchanged through `d14ba19`), which wrote the committed
record before `os_version` was added; the current script has other bytes and
wrote nothing committed.

A second review of #276 found that `machine/success-adjudicator.md` asked for a
verdict on every blind ID while `judge` accepts adjudications of the
disagreements only. Spec 12.6.6 has the adjudicator settle disagreements, so
the template now asks only for the blind IDs in `raw`, and `judge` sends it only
the disagreeing transcripts. The template was never called in this trial (the
judges agreed), so no recorded call or output changed.

## Preregistration gap (spec 0.66 section 12.6.6)

This trial does not meet the section 12.6.6 sequence, and the record does not
claim it. The section requires the held-out cases and labels to be frozen in a
committed lock **before** the preregistration commit, and the owner to approve
the drafted cases and the `machine_dual` labels in the pull request that
commits that lock, **before** preregistration. Here the cases, labels, lock and
run freeze were committed together in `21e93e3`, and no owner approval
happened before the rows ran (the review is still pending; any approval now
comes after the run). `21e93e3` still shows that the inputs were frozen before
the first row, but it is not a preregistration, so the record's
`preregistration` is null. Section 12.6.6 requires preregistration for
`host_acceptance` and `adaptation`; it does not make a `host_trial` record
unapprovable merely because preregistration is absent. The verifier now follows
that scope rule. Lock approval and its ordering remain review assertions
audited through repository history; a preregistration object alone proves
neither. This committed record stays pending and lifts nothing: no review,
bound value or grade was changed. To address the documented trial gaps, use a
new freeze:
commit the lock, have the owner approve it in that pull request, then commit
the preregistration before the first dispatch.

## Review corrections for future trials

The historical harness and all bound evidence remain unchanged. Its
`runs_started_at` is incorrectly an assistant end time, as the table above
explains. It cannot establish timely preregistration. The record has no
preregistration and remains pending; its timestamp is preserved as historical
data, not repaired with an inferred dispatch time. The verifier now checks a
preregistered host report against its bound dispatch evidence; an unknown first
dispatch rejects, and a commit after that dispatch rejects even if the record
supplies a later end time.

The archived builder also omits superseded drafting pairs from the frozen
catalog and lock. No rejected draft was recorded for this trial, so its bundle
has no missing rejected pair; the earlier claim that the rerun path kept those
pairs in *bound* drafting evidence was incorrect. For future trials,
`scripts/m1_trial_inputs.py` copies every current and superseded pair into the
catalog and every case's authorship evidence before labelling. Freeze checks
those bytes again and refuses a rerun that has not been incorporated. The lock
and companion bundle bind the retained pairs by digest and length.

Use the maintained `scripts/m1_trial_inputs.py` and
`scripts/m1_trial_observe.py` for a new trial directory; see
[scripts/README.md](../../../../scripts/README.md). The observer persists
`host-dispatch.json` immediately before invoking the host and includes it in
row evidence, independently of the native inference timestamps. Tests cover
both the archived and maintained observers' redaction of envelope, transcript,
gateway trace and recommendation receipt, using synthetic machine names.

## Grade and record

Spec 0.66 section 12.6.6 requires every report graded under the record to carry
the record's `labelling` and `prompt_authorship` in its qualification output.
The committed `bundle/grade.json` was made by the trial command without
`--qualification-record`, so both fields are null. It is bound by
`bundle/SHA256SUMS` and is kept as it was graded; it is **not** a grade under
the record, and `QualificationRecord.check_grade` rejects it
(`record_grade_mismatch`). It also cannot be regraded at the current checkout:
grading requires the installed `src/relaymind` tree to match the frozen runner
inventory, which it no longer does (`inventory_membership`), and the trial
revision's record checker predates the record's `machine.os_version`. A grade
under the record therefore does not exist for this trial. A regrade at a
matching revision would be:
`grade-host --report bundle/report.json --inputs bundle/inputs.json --resolver
<resolver> --qualification-record
../m10-early-trial-m1-openclaw-2026.10.7-20261101.json --output <new file>`.

## Record revisions

The rows and the record were made from a working tree at `b5ef7a9` with
uncommitted changes to `observe_row.py` (machine-name redaction, `--not-run`)
and `build_trial_inputs.py` (`--preregistration`). Those changes were committed
in `2b4e439`, which changes no product code against `b5ef7a9` (only these two
helpers, evidence and docs). The record first named `b5ef7a9`, whose
`build_trial_inputs.py` hashes to `b3ad745b…5ce46`, not to the recorded
`harness_sha256` `533273ac…d6b3`. The record now names `2b4e439`, the first
commit that holds those exact harness bytes and the same product code
(`tests/test_m1_trial_machine_passes.py` checks the pair when the commit is in
the checkout). The `record` command now stops before writing when the harness
or `src/` differ from `HEAD` (`harness_uncommitted`).

## Loaded skill use (later correction)

At the time of the run, `inputs/setup.md` (and its digest-bound copies under
`bundle/evidence/` and `frozen/evidence/`) cited the then-pending negative
record `m10-negative-skill-loaded-use-openclaw-2026.10.7`. That record was
later withdrawn (#267): the pinned host may offer a section 7.6.1 fence (the
`GatewaySessionAccessAuthority.retain()` borrow with the provider's
`onPayload`), so `stop_point_absent` is not established. The frozen inputs are
digest-bound and stay unchanged. Read any step that recorded loaded use as a
"host limitation" as "unqualified, disabled"; its qualification attempt is #280.

## What remains

1. A preregistered rerun to address the documented gaps. This record stays
   pending and non-qualifying (see "Preregistration gap"); use a new freeze with
   the owner's approval of the case and label lock before a separate
   preregistration commit, and a grade made with `--qualification-record`.
2. The recommendations arm, which needs a person sign-in (above).
3. Qualified walkthroughs. The en-US and zh-CN functional_fixture recordings
   are now complete (see [their README](../walkthroughs-20261101-vps/README.md));
   loaded skill use, MCP use and MCP approval remain blocked and unqualified.
4. Native configuration semantics (#240) keep the report below `qualified`.
