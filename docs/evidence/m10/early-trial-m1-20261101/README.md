# #259 owner-run early installed-skill trial on the Apple M1 8 GiB MacBook: preparation

**Status: inputs prepared, trial not run.** The frozen trial needs inputs only
the owner can supply. This directory has the operator setup, the drafts and the
host captures from the owner's MacBook. It has **no trial bundle, no trial row
and no qualification record yet**. It claims no gate, cost result or task
success (spec 0.66 sections 12.6, 12.6.5, 12.6.6; RM16, RM39, RM40, RM44;
AC30, AC36).

Machine: `target_macos_m1_8gib` (Darwin arm64, `Apple M1`, 8589934592 bytes,
macOS 27.0.1). Host: pinned OpenClaw 2026.10.7 (`npm ci` from
`integrations/openclaw/package-lock.json`). Plugin: `npm run pack:release`,
`relaymind-openclaw-provider-0.1.0.tgz`, SHA-256
`b066ae273c00ec4caa09dbe92a0f1eae020e5c4c51e2603c7f3a201e1b39f35d`
(`release/`). External model: the owner's OpenAI-compatible endpoint, model
`glm-5.3-flash`. The base URL and the key are never written here: the gateway
TOML preimages omit `base_url`, and the native host configuration reads both
from the environment (`${RELAYMIND_EXTERNAL_BASE_URL}`, env SecretRef
`RELAYMIND_EXTERNAL_API_KEY`).

## What ran on this machine (1 November 2026)

| Step | Result |
| --- | --- |
| `npm ci` (pinned host), `npm run pack:release` | done; `release/SHA256SUMS`, `release/release.json` |
| `trial-install` on a fresh host | **failed** (`install-unconfigured.json`): the pinned host installs the plugin disabled ("requires configuration first"), so `plugins inspect` reports `disabled`, not `loaded` |
| configure `plugins.entries.relaymind.config`, `plugins enable relaymind`, then `trial-install` | **passed** (`install.json`): loaded, version 0.1.0, no diagnostics |
| native `openclaw config validate --json` of the three arm configurations | valid (`inputs/captures/config-validate-*.json`); `check_host_config` also `valid` with validator SHA-256 `a79aac9f…6889` (`inputs/captures/host-config-check.json`) |
| `openclaw skills list --json` (native inventory) | identical for all three arms; of the eight catalog skills only `weather` is eligible (the other seven need binaries this Mac lacks) |
| `trial-preflight` | **all seven checks passed** (`preflight.json`); see the note below |
| two smoke turns and a three-row rehearsal | see "Rehearsal" below; none of it is trial evidence |

`preflight.json` was produced by `trial-preflight` over the rehearsal's frozen
manifest. Its checks (machine, environment lock, `openclaw --version`, release
checksums, frozen package) do not depend on the corpus; the real `trial`
command runs the preflight again over the owner's frozen inputs.

## Findings for the owner

1. **Kit install order.** `trial-install` (and the `install` step of `trial`)
   fails on a fresh host: OpenClaw 2026.10.7 installs this plugin disabled
   until it is configured. Configure the plugin entry and run
   `openclaw plugins enable relaymind` first; the forced reinstall then keeps it
   enabled and `inspect` reports `loaded`. The kit is unchanged; this is an
   operator-guide gap.
2. **RelayMind admits 8 of the 49 OpenClaw bundled skills.** The
   `local_directory` source rejects 32 originals with `invalid_markup` (a `<`
   followed by a letter, for example `<path>` placeholders) and 9 with
   `repeated_line`; one rejected skill fails the whole source scan
   (`inputs/captures/projection-exclusions.json`). The trial catalog is the 8
   admitted skills; the native host is restricted to the same 8 with
   `skills.allowBundled`. The 41 exclusions are listed in `inputs/catalog.json`.
3. **Native usage is in the host's session record.** The pinned host persists
   every assistant message with provider usage in
   `agents/main/agent/openclaw-agent.sqlite` (`transcript_events`; large events
   are zstd-compressed in `event_zstd`): `input` (prompt minus cached),
   `cacheRead`, `output`, `reasoningTokens`, `totalTokens` and
   `contextUsage.promptTokens`. That is the spec 12.6.6 native capture source;
   `observe_row.py` reads it. For RelayMind-provider turns the same host
   record shows **zero usage** (the plugin stream reports none), so RelayMind
   arms must use the gateway TraceEvent usage, as the spec says.
4. **Prompt overhead of the relay is nil on the first call.** Both arms sent a
   first request of 7,732 prompt tokens for the same prompt.
5. **The recommendations arm needs the person UI.** Headless `agent exec`
   cannot ask for a recommendation or record a choice. The explicit
   recommend/choose step goes through the native Control UI page (signed-in
   person, HTTPS or trusted loopback, `recommendations` bindings, application
   and action tokens, `[user_actions]` registration; see
   `integrations/openclaw/README.md`, "Native recommendations and explicit
   actions"). Exact skill loaded use stays disabled
   (`m10-negative-skill-loaded-use-openclaw-2026.10.7`, pending review): the
   host loads the skill itself.

## Rehearsal (scratch only, not committed, not evidence)

To check the whole chain before the owner's inputs exist, the inputs were
frozen in a scratch copy with **synthetic stand-in labels** (both "judgments"
and the adjudication were written by the operator tooling, marked
`REHEARSAL STAND-IN`). On that copy: `freeze`, `trial-preflight`, `plan-host`,
three live rows with `observe_row.py`, `trial` (preflight, install, run,
bundle, grade) and `record` with `load_record` all ran, and `grade-host
--qualification-record` accepted the pending record. The rehearsal grade was
`provisional` (`configuration_semantics`, `corpus_unqualified`,
`qualified_evidence_review`), success `blocked` (verdicts `null`), cost
coverage complete (no unknown-cost row), and the cost gate `fail` from one
sample: native 34,281 tokens / 4 calls, provider_only 48,045 / 5,
recommendations 57,124 / 5. The recommendations row had no explicit
recommendation or choice (finding 5), so it measured only the provider path.
One sample of one example prompt says nothing about the arms; an earlier run of
the same three rows (lost to a helper bug, since fixed) used 51,193, 45,812 and
43,625 tokens. Two fixes came out of the rehearsal: `source_revision` must
equal the record's `source_version` (null here), and `observe_row.py` now
decompresses `event_zstd` and saves the host output before parsing.

## External usage on this machine

Provider-reported prompt (including cached) plus output tokens, all
`glm-5.3-flash`:

| Run | Tokens |
| --- | ---: |
| smoke, provider_only (5 calls) | 46,108 |
| smoke, native (5 messages) | 53,127 |
| rehearsal round 1, three rows (helper crashed after the turns) | 140,630 |
| rehearsal round 2, three rows | 139,450 |
| **total** | **379,315** |

The gateway stores agree (provider_only 93,857 and recommendations 100,749
over both rehearsal rounds).

The owner's cap is about 1,000,000 tokens. One frozen task (three rows) costs
roughly 140,000 tokens on this prompt. About 620,000 tokens remain under the
cap: four more tasks (twelve rows) at most, plus the machine annotator's
labelling and success judging.

## Files

- `inputs/catalog.json`: draft AssetCatalogSetV1 (purpose `development`), the
  8 originals under `inputs/originals/`, the MIT `LICENSE` as license and
  notice, and the statements under `inputs/evidence/` (those marked DRAFT need
  the owner's confirmation).
- `inputs/cases.DRAFT.json`: one **example** case (`en_to_en`), drafted in the
  agent session, `authorship: generated`. Not owner-authored.
- `inputs/label-template-owner.DRAFT.json`,
  `inputs/label-template-machine-annotator.DRAFT.json`: blind templates from
  `label-template --labelling owner_machine` over the draft case.
- `inputs/rubric-trial-en-001.md`, `inputs/setup.md`: DRAFT rubric and row setup.
- `inputs/configs/`: the three arm host configurations (exact preimages, no
  secret) and the two gateway TOMLs (`base_url` omitted).
- `inputs/captures/`: native validation, inventories, distribution identity,
  projection exclusions.
- `machine-annotator-prompt.DRAFT.md`: the machine annotator's prompt template.
- `usage-relationship.md`: the token relationship evidence for the attempts.
- `build_trial_inputs.py` (`draft`, `freeze`, `record`) and `observe_row.py`:
  evidence helpers for this trial; they only arrange files for the existing kit
  and checker.

## What the owner must do next

1. **Write the prompt(s).** Replace the example in `inputs/cases.DRAFT.json`
   with your own prompt(s): set `authorship: original`, `author_ids: ["owner"]`,
   and save as `inputs/cases.json`. Check `people` in `inputs/catalog.json`
   (your roles and competencies; add `native_zh` only if it is true, and only
   then add a Chinese lane case). Confirm or edit every statement marked DRAFT
   in `inputs/evidence/`, the rubric and `inputs/setup.md`. If anything in
   `inputs/` changes, rerun `label-template` for both annotators.
2. **Label.** Fill your own template blind. Run the machine annotator with
   `machine-annotator-prompt.DRAFT.md` on its own template at temperature 0
   with a **pinned, dated** model ID (`glm-5.3-flash` is an alias-like ID with
   no date: pick a dated one or record why it is pinned). Then run
   `label-worksheet`, adjudicate every case in the worksheet (reason on every
   disagreement) and `label-freeze --labelling owner_machine` to `labels.json`.
3. **Set up the recommendations arm** (finding 5): add the `recommendations`
   object to `inputs/configs/host-recommendations.json` and to
   `<root>/host/arm-recommendations.json` with your real signed-in profile
   facts, and validate it natively again.
4. **Freeze.** `python docs/evidence/m10/early-trial-m1-20261101/build_trial_inputs.py
   freeze --root /private/var/tmp/relaymind-trial-259 --cases inputs/cases.json
   --labels labels.json` writes `frozen/inputs.json` and the private resolver.
   Commit `frozen/` before the first row runs.
5. **Run the rows.** Start both gateways from `<root>/gateway-*/relaymind.toml`
   with the credentials sourced only in their shells; `plan-host` on the frozen
   tasks and freeze; for each ordinal run `observe_row.py` (source the
   credentials only for the native rows). For each recommendations row first
   recommend and choose in the Control UI, save the choice receipt and pass it
   with `--recommendation-receipt`, `--interactions` and `--human-wait-ms`.
6. **Judge success blind.** Anonymize the transcripts, judge each row against
   the rubric yourself and with the machine annotator, adjudicate, and set
   `success` in each `row-NNNN.json`. Unknown stays `null`.
7. **Bundle and grade.** `scripts/asset_whole_task.py trial --inputs
   frozen/inputs.json --tarball <root>/release/relaymind-openclaw-provider-0.1.0.tgz
   --observations <obs> --bundle bundle --work <new private dir>
   --resolver <root>/resolver/resolver.json --qualification provisional --
   <root>/cli/node_modules/.bin/openclaw`, with `OPENCLAW_CONFIG_PATH` set to
   `<root>/host/openclaw.json` so the reinstall leaves the arm files alone. The
   early trial grades `blocked` at best (exit 3).
8. **Record.** `build_trial_inputs.py record --bundle bundle --record-id
   m10-early-trial-m1-<date> --machine-model <dated id> --machine-prompt
   machine-annotator-prompt.DRAFT.md` writes the pending QualificationRecordV1
   in `docs/evidence/m10/` and checks it with `load_record`. Grep every new file
   for the key and the base URL host before committing. Open the pull request;
   one org member other than you approves it; afterwards fill `pull_request`,
   `reviewer_logins` and `approved_at`.
9. **Walkthroughs** (#238 recorder) need the Linux person-ingress harness
   (the VPS lacks `libgbm`/`libasound` and a private interface): still pending.
