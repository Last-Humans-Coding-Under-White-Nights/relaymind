# #259 owner-run early installed-skill trial on the Apple M1 8 GiB MacBook

**Status: provisional early-trial bundle, graded `blocked`; record pending
approval.** One frozen task, three planned rows, run on the owner's MacBook on
1 November 2026 by the operator tooling on the owner's instruction, with no
person in the loop. Spec 0.66 sections 12.6, 12.6.5, 12.6.6; RM16, RM39, RM40,
RM44; AC30, AC31, AC36. An early trial never asserts the final gate, and this
one-task, one-sample result says nothing general about the arms.

| Item | Value |
| --- | --- |
| Machine | `target_macos_m1_8gib`: Darwin arm64, `Apple M1`, 8589934592 bytes, macOS 27.0.1 (26A434); hostname only as SHA-256 in the record |
| Host | pinned OpenClaw 2026.10.7 (`npm ci` of `integrations/openclaw/package-lock.json`), isolated home under the operator root `/private/var/tmp/relaymind-trial-259` |
| Plugin | `relaymind-openclaw-provider-0.1.0.tgz`, SHA-256 `b066ae27…f35d` (`release/`, and in the bundle) |
| External model | the owner's OpenAI-compatible endpoint (base URL and key never written), `glm-5.3-flash` for all three arms |
| Frozen inputs | `frozen/inputs.json`, committed in `21e93e3` before the first row (09:41:39Z; first dispatch 09:46:02Z) |
| Bundle | `bundle/` (`report.json`, `grade.json`, `inputs.json`, `SHA256SUMS`); regrade with `grade-host --inputs bundle/inputs.json` and a resolver of the inventory preimages |
| Record | `../m10-early-trial-m1-openclaw-2026.10.7-20261101.json`, scope `host_trial`, pending (no review yet) |

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
  The owner approves the cases, labels, rubric and success judgments in the
  pull request; until then the record is pending and lifts nothing.
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

## What remains

1. Owner approval: a pull request review by one org member other than the
   owner approves the cases, labels, rubric, success judgments and the record.
   Afterwards fill `pull_request`, `reviewer_logins` and `approved_at` in the
   record.
2. The recommendations arm, which needs a person sign-in (above).
3. Walkthroughs (#238 recorder), which need the Linux person-ingress harness;
   the VPS lacks `libgbm`/`libasound` and a private interface.
4. Native configuration semantics (#240) keep the report below `qualified`.
