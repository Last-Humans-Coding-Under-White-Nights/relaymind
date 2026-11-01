# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- #255 host use settlement (RM16/RM37/RM39; AC30/AC35; spec 0.66 sections
  7.6.1, 7.6.2, 12.6.6) adds one pending negative `host_operation` record for
  OpenClaw 2026.10.7, with the VPS host surface inventory and the CI/VPS
  captures (`docs/evidence/m10/`):
  - `mcp_use` is `exact_route_absent`. No supported route invokes an
    independent, non-App MCP tool by its exact native identity: `tools.invoke`
    is name-only and the harness runtimes are private; the App-scoped
    `mcp.app.callTool` does not count. These verdicts rest on the pinned host
    source; the inventory's `attempted_routes` lists the corrected VPS3
    attempts (`mcp.app.discover`, `tools.effective`, `mcp.app.listTools`) and
    what was not called. Its older CI/VPS2 entries omit their
    `tools.effective` reads; the settlement document records that gap.
  - `skill_loaded_use` has **no** negative record. Hooks, middleware and the
    provider can stop a read or a request but cannot re-verify fresh person
    authority, the session incarnation and the pinned bytes on their own. The
    host's `GatewaySessionAccessAuthority.retain()` borrow, however, re-checks
    the person's grant and the exact session incarnation at call time and
    could back a provider `onPayload` fence, which spec 7.6.1 admits once
    qualified. `stop_point_absent` is therefore not established, and the
    pending record that asserted it was withdrawn after review. The inventory
    records the borrow's source, its open questions and the
    `before_prompt_build`/`ctx.toolAuthority` hook.

  Both operations stay disabled. The MCP record lifts nothing until it is
  approved. `tests/test_m10_host_use_records.py` loads it with the checker,
  checks cited hashes against the inventory and, in the OpenClaw CI job,
  against the installed pinned host files (including the reset policy sources).
  It ties listed attempts to captures and checks the documented older-route
  omissions, ties the record's commit and harness to the VPS3 capture and its
  tracked sources to that commit's bytes, and rebuilds that historical adapter
  to verify its emitted files and recorded revision. Both CI jobs retain full
  history; a missing record commit fails. Privacy checks reject plaintext
  hostnames and personal home paths, allowing only the documented VPS
  placeholder and the standard CI home. The MCP record also binds the
  second VPS run's MCP capture (`openclaw-mcp-use-20261101-vps2.json`,
  checkout 62c4dcd), with the user name and hostname rewritten. Person ingress
  passed there, and MCP use was `blocked`. That run's loaded-use capture exited
  2 on the 62c4dcd single-pin check, because the host lists the whole
  profile's pins; the selected-skill pin check (5d92b64) is already in this
  branch and accepts the committed capture, which a test now checks. The MCP
  record also binds a third VPS run at bba0cf1
  (`openclaw-mcp-use-20261101-vps3.json`) with the #249 discovery fix:
  `tools.effective` lists both colliding tools, and the result is still
  `blocked`. The earlier "empty list" was the old harness projection
  (`payload.tools` instead of `groups[].tools`), not a host result; the
  settlement document records the correction.

- Walkthrough recorder fonts (RM39; AC30): `--font-dir` on
  `integrations/openclaw/test/host-walkthrough/record.py` copies the `.otf`,
  `.ttf` and `.ttc` files of a directory once into the private work root, and
  each language's install step copies that snapshot into the isolated browser
  home's `$XDG_DATA_HOME/fonts` (fontconfig), so Chinese labels render on a host
  without a system CJK font. `manifest.json` lists the snapshot's file names,
  SHA-256 digests and sizes under `fonts`. An empty, missing or non-directory
  font directory is the structured refusal `walkthrough_font_dir` before
  anything is created, and an environment whose font data directory
  (`XDG_DATA_HOME`, else `HOME/.local/share`) is unset or relative fails the
  install step (`walkthrough_font_home`) rather than installing elsewhere.
  `tests/test_openclaw_walkthrough.py` covers these cases.

- Owner-run qualification records and `owner_machine` labelling (#251; RM16,
  RM39, RM40, RM42, RM44; AC30, AC31, AC33, AC35–AC37; spec 0.66 section
  12.6.6): the closed `asset_qualification_record.v1.json` schema and
  `asset_qualification_record.py` verify every bound SHA-256, the machine role,
  preregistration order, labelling and the review (one org member other than
  the owner). `grade-*` take `--qualification-record`; only an approved record
  binding the report's exact bytes removes `qualified_evidence_review`, and the
  output reports its `labelling`. Label commands take `--labelling
  owner_machine` (`owner_machine.v1`: the owner may also tune, the machine
  annotator never); the record's Cohen's kappa is recomputed from the labels.
  Every bound freeze, tasks, episodes or labels subject must be the report's
  resolved input, platform records included; a platform record may carry the
  corpus review; Time fields must be real UTC instants
  (`invalid_record_time`). Under `owner_machine` only the owner custodian may
  also tune. `known_host_limitations` returns approved negative
  `host_operation` records for the roadmap, docs and reports to list; nothing
  consumes it until such a record is approved. Fixture-tested only; no
  record exists and no gate has passed.

- Native host configuration preflight (#250; RM16/RM39/RM40/RM44;
  AC31/AC35/AC36/AC37): a bounded Node checker uses pinned OpenClaw 2026.10.7
  validation, including defaults, strict refinements and plugin schemas, and
  returns a closed verdict with the SHA-256 of every pinned host entry point it
  imports. The Python wrapper enforces a timeout and accepts only that pinned
  validator identity; input resolution rejects invalid native preflights and
  retains validator identities by config digest. Offline resolution can consume
  captured verdicts without Node; native fixture tests run in the OpenClaw CI job.
  The isolated state (no operator HOME or credentials) installs the packaged
  RelayMind plugin when the config names `relaymind`, so ordinary installed
  discovery validates its settings without `plugins.load.paths`. A referenced
  plugin (in `plugins.entries` or `plugins.allow`) whose schema the host did
  not inspect (undiscovered, or a discovery owner the host preserved
  uninspected) is `unavailable` with reason
  `plugin_schema_not_inspected`, never `valid`; every verdict carries a closed
  `reason`.
  Qualified captured-evidence verification remains open in #240.
- Platform runs on GitHub-hosted runners (#241, #235; RM38, RM39, RM44; AC29,
  AC37): a separate `Platform runs` workflow on `macos-latest`,
  `windows-latest` and `ubuntu-latest`.
  - `scripts/asset_operations_run.py operations` runs the lexical portion of
    the AC29 protocol as a fixture (it does not close AC29): ten clean
    startups, a recommendations-disabled memory baseline, and the 60-second
    2 req/s workload with four 25-record metadata-import batches and a derived
    rebuild. The workload goes through the existing `run_workload` and
    `summarize_operations` over an evaluation-only gateway control channel
    (`asset_operations_child`).
  - `scripts/asset_platform_run.py` runs the ordinary host step (Linux-only
    harness honestly recorded elsewhere) and builds the platform report over
    fixture companion inputs locked to the runner. Its `verify` grades
    downloaded artifacts again.
  - Platform reports now name `mcp_use_disabled`, `restart_not_observed` and
    `ordinary_harness_linux_only`, and can bind the operations report as
    `operations_evidence`.
  - Only harness errors fail the job. Reports stay `fixture`; CI runners are
    not the acceptance platforms, and no gate is claimed.
  - Review fixes: a gateway that exits before readiness is a recorded harness
    error, and the report is still written. A failed ordinary UI action is a
    failed platform step, not a harness error. Memory phases name only what
    was sampled. The sampled PID is the gateway's own, which is recorded.
    A missing install-check record is written as `platform-not-built.json`
    and fails `verify`. An existing bundle directory is refused, and
    `asset_platform_run.py` exits 2 with a reason on input errors and on an
    ordinary-step timeout. The gateway control child answers every command
    even when its job store fails, and its reader ignores a closed loop.
  - Second review fixes: a startup launch that fails before readiness is a
    harness error (`startup_<n>_<reason>`, exit 2) in `operations` and
    `startup`; `verify` fails an operations report with harness errors, and a
    platform report without its bundle or a bundle without its report. The
    control child binds the job the request awaited (`job_id`), never the
    newest listed job, and reports an unbindable job as a harness error. The
    bundle registers `platform-runner-files`, the digests of the driver
    scripts, workflow, ordinary harness and test helpers the run executes,
    and `verify` checks them against the checkout. The workflow's path filter
    covers the run's dependency closure, and every step has a time limit that
    fits inside the job's. A non-object environment lock exits 2, and a
    reused `--work` starts from the requested catalog and new stores. The
    docs call the run the lexical AC29 fixture portion and no longer quote
    uncommitted local numbers.
  - Third review fixes: an operations harness error no longer skips the
    release and lifecycle steps; its exit code is kept in `operations.exit`
    and a final `Harness result` step fails the job on it. The ordinary step
    passes `--timeout 780`, so its own `ordinary_timeout` fires before the
    step limit. The test files and workflow the run binds check out with LF
    line endings on Windows, so a `windows-latest` report verifies on another
    checkout. The control channel reads lines up to 16 MiB, a control call
    after the reply stream ended fails at once with `control_closed`, a job
    reported in two replies is kept once in its latest state, and memory
    phases are listed only when a workload sample is known.
  - Fourth review fixes: the verify step writes its job summary even when
    verify fails, then fails with verify's exit code. `platform-runner-files`
    binds only tracked files, so local files such as `.DS_Store` never change
    it, and a test checks that every bound file checks out with LF (or as
    exact bytes). A gateway that exits before its token is `gateway_exited`,
    not a token timeout; the gateway child ends quietly when the parent has
    closed its pipe; ordinary-step failure messages go to stderr.
- Episode and host-task run kit (#236, #237; RM40–RM44; AC31–AC36): blind
  label templates, adjudication worksheet and `AssetLabelsV1` freeze;
  `preregister-host`/`preregister-episodes` checks; a resumable, crash-safe
  journaled `run-host` over an operator executor program, and journaled
  episode runs; `capture-costs` from RelayMind trace events. Runs are only
  `fixture` or `provisional`; no real episode, host run or gate result is
  claimed. Review fixes: the worksheet requires two distinct annotators,
  malformed worksheets, unwritable outputs and a lone `--evidence-root`
  reject with exit 2, traces with out-of-order or repeated call events reject,
  and `run-host --executor-timeout` bounds a hung executor program. Second
  review: the run journal binds the execution identity (environment,
  artifacts, input manifest, qualification) and is owner-only; cancelled and
  gateway-bounded calls keep their own outcome, with dispatch counted only
  when evidenced; `--events`/`--artifacts` use the bounded strict reader;
  executor output is capped while read; host task sets missing a required
  lane are not acceptance-eligible. Third review: episode probes outside the
  `held_out` split make the set ineligible (`probes_not_held_out`); the
  episode journal also binds the corpus release lock; an executor `not_run`
  row is invalid; a journal with only a torn header starts again; a second
  signal stops `run-host` at once and kills the executor's group; dispatch
  needs a received token or a known usage count, also for
  `provider_unreachable`.
- Early installed-skill trial kit (#234; RM39, RM44; AC30, AC36; spec 0.65
  section 12.6.5): `scripts/asset_whole_task.py trial-preflight`,
  `trial-install`, `trial-run` and `trial`.
  - Preflight checks macOS arm64, Apple M1, 8 GiB, the environment lock, the
    pinned OpenClaw 2026.10.7 host and the plugin release checksum against the
    frozen package.
  - The plugin is installed with the pinned host CLI.
  - The frozen plan runs through the existing driver over operator-captured row
    observations. Missing rows stay `not_run`.
  - Every original is bundled with `SHA256SUMS` and a companion manifest and
    graded offline with the unchanged grader.
  - Output is fixture or provisional only; no trial has been run.
  - Review follow-ups: the bundle and copied originals are private (`0700`/`0600`)
    and appear only when complete; the install record keeps closed inspect codes
    and a diagnostics count, never host text; the provisional gate checks the
    closed record shapes, recomputes the machine checks and binds the records to
    the frozen package; output paths under a symlinked parent (macOS `/tmp`)
    work; evidence keeps the 10 MiB original limit; `plan.json` names the lane
    and prompt case.
- Bilingual packaged walkthrough recorder (#238; RM39; AC30, AC35):
  `integrations/openclaw/test/host-walkthrough/record.py` runs the unchanged
  ordinary installed-host qualification in `en-US` and `zh-CN`.
  - It uses the plugin's own labels, with screenshots, video and a digest
    manifest.
  - A step is recorded only when the harness predicate accepts the capture.
    Loaded skill use, MCP use and host MCP approval are always `blocked`.
  - The tier is `functional_fixture`, it is not in CI, and no walkthrough has
    been recorded yet.
  - Every partial screenshot is listed by digest, and a torn or unsafe media
    set still writes an incomplete manifest with fixed `media_problems`.
- Qualification assessment for the section 12.6.5 exports (#240; RM16, RM40,
  RM42, RM44; AC31, AC33, AC36, AC37): graded host, platform and adaptation
  results record the claimed label, the tier assessed from the resolved inputs
  and fixed safe reasons. A provisional, qualified or measured label over
  fixture inputs is rejected (`qualification_contradiction`), and no
  unqualified tier passes a gate. The specification defines no reviewed S3
  qualification record yet, so no report can be assessed qualified.
  `plan-host` records the preregistered input and freeze digests, and the new
  `plan-episodes` command verifies frozen episodes and writes their probe plan
  (#236, #237). No measured or qualified result is claimed.
- Native loaded-use fence and conditional MCP use path in the OpenClaw adapter
  (#239/#204; spec 0.65 sections 7.6.1, 7.6.2 and 14.5; RM16/RM37/RM39;
  AC28/AC30/AC35). The reviewed registry now names `skill_loaded_use` and
  `mcp_use` as separate operations; an `mcp_use` entry must declare its exact
  route, transport, tool class and finite maximum approval wait, and a
  malformed entry qualifies nothing. The loaded-use fence rechecks fresh
  native authority for the same session incarnation, registration, the
  unexpired choice, the eligible asset and operator association, a nonremoved
  library entry and the immutable selected bytes before loading, after the
  exact read and immediately before delivery, keeps R0 across a library head
  update and blocks the conversation after a fence failure. The `relaymind.mcp`
  handler implements the closed request, the 60-second preparation/dispatch
  deadline with the separately bounded approval wait, exact native binding with
  a recomputed canonical schema hash, closed argument validation, approval
  binding to the bound server/tool/schema/session and single use, the closed
  receipt and same-scope idempotency. Key claims, consumed allow-once
  approvals and blocked conversations are kept in a durable private ledger
  synced before dispatch (`src/recommendation-native-ledger.ts`): after a
  restart an unresolved key replays as indeterminate and never dispatches
  again, and a full or unreadable ledger refuses. Without a ledger nothing is
  dispatched. An admitted key is bound to its request at admission, so its
  pre-dispatch refusal is its historical receipt and a different request
  under it is `idempotency_conflict`. Host schema
  patterns are admitted only in a provably bounded anchored subset, a schema
  whose gateway hash cannot be recomputed here (for example `0.0`) is
  `unavailable`, and all native RPC requests are capped at 512 KiB. Neither
  operation is in the shipped registry, no production MCP route exists, and
  `relaymind.mcp` is not registered; the tests use injected fixture resolvers
  and routes only. The shipped direct-action/exact-selection entry is re-pinned
  to the new adapter revision `943e1f0e2ba6e9e8dad45456e6eb8122a475c91ced0c9e1e3f8d249471fe240f`; its evidence revision names
  the fresh capture of this revision (CI run 41318389858, SHA-256
  `35436e519245…`). A release-artifact test now fails when the registry
  and the built adapter digest drift. After review, the ledger never writes a
  record recovery would reject and has `close()` (on gateway stop); a
  same-key request during preparation recovers that attempt's actual receipt;
  gateway transport, overload and deadline failures report `unavailable` or
  `timeout`; only failures attributable to the verified person and session
  block a conversation; and each native method registers on its own entry.
  A second review added: one live ledger owner per path (an exclusive lock
  file, taken over only from an exited owner); separate dispatch markers, so a
  key bound before a restart that never dispatched recovers as
  `not_dispatched`; reserved block capacity, with managed use refused once no
  further block could be recorded; and a fence recheck of registration,
  choice and operator association after the exact native read. A third
  review made the stale-lock takeover race-safe (per-owner lock files, never
  removing a live owner's lock) and made each ledger record complete across
  short writes before it is acknowledged; the shipped entry is re-pinned to
  adapter revision `b49d94d8267076516430fb2e4fb64d41fad2961fc8009c84e60921cf43e9e98e`,
  whose evidence revision names its fresh capture (CI run 41322540111, SHA-256
  `6b8947df711e…`). A fourth review rejects malformed request text and
  non-string session fields as `invalid_request`, drops a duplicate
  registration check and keeps the native deadline live, and the ledger cuts
  a torn final record through a separate read-write handle (a Windows append
  handle cannot truncate); the entry was re-pinned to adapter revision
  `a29cac121c5e01932b6b3ee75e9bab2214e5e9e72cbc36d8f802f62b917c7cfe`. A fifth
  review rereads a same-key MCP request's history after its registration
  check (a receipt settled meanwhile is recovered, never reported as
  `not_dispatched`), keeps a host `UNAVAILABLE` exact-read result from
  blocking a conversation, digests the adapter bytes once per resolver, takes
  over a stale ledger lock naming this process's PID that this process did
  not create (PID 1 in a container), reserves exact room for each binding's
  marker and longest receipt under the byte cap and compiles each host schema
  pattern once per operation; the entry is re-pinned to adapter revision
  `37f9b415c47260065d42453b5d85474375a796b8a7416af78732668863357f2d`, whose
  evidence revision names its fresh capture (CI run 41335800361, SHA-256
  `1212d0e28080…`). The host qualification harnesses then built their backend
  on the real wall clock instead of the test default `FakeClock`, so the
  capture was recaptured with them at the same adapter revision (CI run
  41351781170, SHA-256 `58c6df0ba6dc…`; unmodified, no runner hostname in it).

- Native loaded-skill observation harness (#239; RM39/AC30): retain native
  `skills_read` bytes, their receiving gateway request and host/run correlation,
  with isolated stale/deleted/reset observations over deterministic inference.
  This is evidence tooling only; loaded-use qualification remains unavailable.
  Observation now requires `--observe-loaded-use` (default off); the ordinary
  direct-action/exact-selection exit criterion is unchanged. CI runs a separate
  six-minute observation step and artifact: only harness/fixture errors fail it,
  never truthful observation outcomes, without `continue-on-error`. Native
  records tolerate only a torn trailing line with one bounded re-read; retained
  pin names, advertised revisions, socket receipts and independent provider/run
  records are checked, and incomplete hooks cannot label a turn `not_loaded`.
  Request and content retention limits preserve later control windows.
- Add a disposable MCP use capability tier (#204; RM39/AC35), raw-evidence
  accounting tests, bounded sandbox servers and an optional CI capture. Exact
  use, native approval trials and human acceptance remain blocked/not_run.
- Harden the MCP capability tier (#204; RM39/AC35): fail CI on harness or
  discovery errors, bind source/artifact evidence, join real server call/effect
  records, observe approval identity and resolution, and rescan final captures
  for secrets. The CI timeout is four minutes; missing run traces and native
  exact-use receipts still prevent qualification.
- Fix the MCP capability tier's `tools.effective` projection (#204; RM39/AC35):
  the pinned host returns grouped entries (`groups[].tools[].id`), so the old
  flat `payload.tools` projection always recorded an empty list. The probe now
  records an explicit `listed`/`empty`/`malformed` outcome, the host's MCP
  server/original mapping and notice IDs, and discovery requires both colliding
  fixture tools in the session inventory. Earlier captures' empty
  `tools.effective` lists came from the projection, not the host. The person
  probe now starts the session's MCP runtime with public `mcp.app.discover`
  before the read (MCP Apps enabled, the shipped RelayMind model selected, zero
  heartbeat). A Linux x86-64 VPS run of the CI command at bba0cf1 listed both
  colliding tools; exact use stays blocked. That capture is now committed as
  `docs/evidence/m10/openclaw-mcp-use-20261101-vps3.json` (#255).
  A refused `chat.send` retains its request window, correlated provider records
  and native hooks; any such activity fails the case closed
  (`ordinary_skill_refusal_contradicted`) instead of an activity-free refusal.
  Provider attempt records are read by their own per-turn window, so a
  failure before HTTP reached the gateway is retained with its outcome, and an
  unexplained record fails the case (`ordinary_skill_provider_unexplained`).
  The turn drains asynchronous hooks until every tool call is resolved
  (`ordinary_skill_tool_unresolved` otherwise), including after a refused
  send. The committed VPS2 observation capture predates this change and must
  be recaptured.

- Operations and platform run kit (#242, toward #241, #235, #234; RM38, RM39, RM44; AC29, AC37):
  - `scripts/asset_operations_run.py startup` measures ten real `relaymind serve` startups over a generated 300-skill bilingual catalog, using the existing AC29 driver. It waits for recommendations to be ready and samples memory on POSIX and Windows.
  - `platform` builds an `AssetPlatformReportV1` from install-check and ordinary installed-host records, bound to the companion input manifest. Skill use is never inferred from a selection receipt.
  - All output is labeled `fixture`.
- Exact native Skill Library selection through the ordinary installed host
  (#197; RM39, AC30): the qualification saves a skill with
  `skills.library.save`, maps it to the chosen asset, selects it with
  `relaymind.skill` and checks `skills.library.list`. The evidence predicate
  binds skill ID, revision, asset ID, content hash and source version, and
  requires the unmapped-skill, cross-session and own-session foreign-person
  refusals with their exact error codes. Browser failures and the overall
  deadline remain failures through skill selection. The listing must show the
  selection itself: its `session.sessionKey` is the authenticated target
  session and its `session.selections` are exactly the selected skill at the
  selected revision, both when the harness captures it and in the predicate,
  so a no-op or wrong-session activation fails. The committed capture is a
  recapture with this harness (CI run 41317645014, SHA-256 `3a063ab37411…`),
  which the registry's evidence revision names.

- CI: the core `check` job runs tests in parallel with pytest-xdist, one file per worker at a time, and caches the Chromium shell per OS and Playwright version. The Windows test step took about 29 minutes in one process. Tests that build real recommendation or native-preparation evidence from timing use fixed clocks or synthetic timestamps, so a loaded runner cannot turn them into a deadline error.
- CI: the shared asset benchmark fixture uses a deterministic recommendation
  budget instead of the production 500/150 ms pair, so a loaded runner cannot
  expire a confident HTTP selector attempt and flatten its paired coverage.
  Deliberate timeout cases keep explicit small budgets, the HTTP cancellation
  test uses the shared fixed clock, and a slowed-selector regression test
  proves the old deadline failed on every host.
- CI: a small `plan` job picks the platforms (and logs its decision); both
  `check` and `openclaw-plugin` take them from it and run stacked PRs on Linux only to
  conserve CI capacity, with no base-branch filter. Every PR must target
  `master` and pass the full Linux/macOS/Windows matrix before merge; pushes
  also run all three platforms. `openclaw-plugin` now shares the Chromium
  shell cache keyed by OS and Playwright version; its test tiers remain serial.
- Stabilize installed OpenClaw qualification on loaded runners (RM16/RM39;
  AC30): use bounded ten-second host-event waits, keep fixture holds alive
  through retirement observation, and capture only fixed ordinary substeps and
  error classes. Browser failures and the overall deadline now remain failures
  through UI actions and finalization; the 570-second harness and 12-minute CI
  bounds and all evidence predicates remain unchanged. Fresh evidence must bind
  the changed harness sources.

- Section 12.6.5 verifier closure and episode driver (#229, #202, #205, #207):
  - The frozen corpus is verified from the input manifest, and host tasks and episodes are bound to it.
  - Off/on adaptation configurations are checked as a named ablation.
  - Every dispatched probe's request and offered-catalog digests are recomputed.
  - Spec 0.64 reset evidence lets the command verify resets.
  - A driver runs adaptation episodes through the real recommendation lifecycle on the locked corpus projection; it is evaluation-only and never qualified.
  - A Simplified Chinese command guide is added.
- Spec 0.60 section 12.6.5 exports (#198/#202/#204/#205/#206; RM16, RM38–RM44;
  AC29–AC37): closed host freeze, task, report, adaptation episode and platform
  schemas; a deterministic three-arm plan and conserving host driver; offline
  graders that recompute every summary; companion input-manifest resolution with
  blocked/rejected exits (`scripts/asset_whole_task.py`). Fixture-verified tooling
  only; no measured result or acceptance is claimed. Remaining verifier gaps: #229.

- Activate two independently reviewed exact Linux CPU embedding identities;
  record ordinary native launch and provisional 412/128 benchmark results,
  including partial indexing, lexical fallback and the observed quality regression.

### Fixed

- The loaded-use observation harness records the exact person-scope
  `sessions.reset` refusal (`operator.admin` required on the pinned host) as
  `control_refused`, with no turn, instead of raising
  `ordinary_skill_control_failed`. CI run 41286397320 hit that error. Any other
  refusal, and any turn evidence on a refused control, still fails the
  predicate (#255). The second VPS run observed the refusal as
  `control_refused`. [CI run 41340879424](https://github.com/LHCWN/relaymind/actions/runs/41340879424)
  at ed21e007 also completed loaded-use observation with that reset result and
  confirmed corrected MCP discovery; exact use stayed blocked. These newer CI
  captures are workflow artifacts, not committed evidence.

### Changed

- Specification 0.65 → 0.66 (#251; RM16/RM38–RM44; AC29–AC37): adds the
  owner-run acceptance profile (section 12.6.6). Names the owner's M1 8 GiB
  MacBook and native x86-64 Windows PC as the target machines and the Ubuntu
  VPS as the Linux functional host; defines `owner_machine` labelling with a
  recorded machine annotator, Cohen's kappa and owner adjudication; held-out
  split, commit-based preregistration, at least 20 tasks per lane within the
  100, blind arm-anonymized success judging, disjoint usage components and
  their capture sources, native-RPC episode corrections, rank 4–10 choice
  receipts, the MCP ledger location/retention, the open-source MCP acceptance
  catalog, and the closed qualification record with PR approval. A reviewed
  negative record closes the loaded-use or MCP-use gate on the pinned host with
  the operation disabled as a known host limitation (sections 7.6.1, 7.6.2).
  `stop_point_absent` means no host point can fence the next model request as
  section 7.6.1 requires (a stop-only point without fresh person authority,
  session incarnation and pinned-byte re-verification does not count);
  `exact_route_absent` means no supported route invokes an independent,
  non-App MCP tool by its exact native identity (App view routes such as
  `mcp.app.callTool` do not count). The record's `corpus_review` is allowed
  on host, adaptation and platform records (a platform report resolves the same
  frozen corpus) and is null on walkthrough and host_operation records; under
  `owner_machine` the tuner exclusion is waived for the owner custodian only.
  "Customer" wording about who runs trials becomes owner/operator. No metric,
  threshold or budget changes.
- Specification 0.64 → 0.65 (#239/#204/#240; RM16/RM34/RM37/RM39/RM40/RM44;
  AC25/AC28/AC30/AC31/AC35–AC37): defines native skill loaded-byte delivery,
  immutable-pin versus removal semantics and the later-use authority fence;
  separates the deterministic-model functional tier from live-model capability
  and allows a reviewed exact-composition entry only after all controls pass.
  Defines the conditional direct MCP-use request, exact native binding,
  lifecycle/approval fences and qualification gates without activating it;
  automated native UI decisions remain functional evidence, never human approval.
  Reconciles registration with the existing qualified direct-action exception,
  places the 512 KiB native request cap in section 14.5, and bounds approval
  waiting separately from the 60-second preparation/dispatch deadline without
  changing existing budgets. Clarifies pre-dispatch timeout/cancellation
  receipts, the native RPC error envelope for pre-admission refusals, and
  that the outer native request cap applies before the inner create-body cap;
  both bounds hold. Repairs roadmap links and aligns the bilingual validation
  notices and historical qualification notes with these unreleased rules.
  Requires source-bound pinned native host configuration validation with
  the captured native regex dialect and explicit provider/sampling/budget
  schema binding.
  Runtime snapshots and all outstanding acceptance gates remain closed.
- Specification 0.63 → 0.64 (RM42/RM44; AC33/AC36): names the reset evidence
  document of section 12.6.5 (`reset:<episode_id>`, holding the before/after
  preimages), so adaptation reports can be graded offline end to end.
- Specification 0.62 → 0.63 (RM37/RM39; AC28/AC30): adds the optional expected-scope
  headers and the scope_mismatch error, checked before any read or write; requires the
  native adapter to send its binding's scope on every gateway request; clarifies that
  write privilege and target-agent access are resolved for the actual caller, not
  required of unrelated roles, and that exact skill selection rechecks choice expiry
  before each native dispatch.
- Specification 0.61 → 0.62 (RM37/RM39; AC28/AC30): names the pinned
  authenticated native action channel and explicit profile/workspace registration,
  preserves direct choice/feedback/privacy and distributed-commit semantics, and
  specifies exact native Skill Library selection/read separately from loaded-use.
  Runtime snapshots and native MCP activation retain their existing qualification gates.



- Specification 0.55 → 0.56 (RM40/RM44; AC31/AC36; review #219):
  names `Case.conversation` and attempt/embedding `cost_observations` as
  order-preserving arrays for canonical hashing. Types `Rate.value` and Wilson
  bounds as `nonnegative_number`, consistent with their existing [0, 1] limits.

- The 0.57 reconciliation carried these master 0.56 benchmark clarifications
  and the parallel source-grounded 0.56 additions below as distinct histories.

- Specification 0.52 → 0.53 (RM16, RM38/AC29; #196, review #218):
  fixed 500 ms request slots admit measured dispatch delay within the original
  deadline, with a separate scheduled-to-response p95 ≤300 ms gate. Service and
  dispatch-to-response timings keep their existing meanings. Contention evidence
  requires foreground overlap with each sequential source/rebuild job and actual
  inference contention, without requiring two jobs on one worker to run together.
  All samples, failure denominators and outstanding live qualification gates remain.

### Added

- Native retrieval benchmark tooling (#201; RM16/RM35/RM38/RM40/RM41/RM44;
  AC26/AC29/AC31/AC32): shared semantic factory/parser and offline preimages,
  explicit local preparation, native/floor cost conservation and self-contained
  runtime comparison evidence. The registry now holds the two exact reviewed
  Linux CPU identities; other artifacts, actual cross-platform native controls
  and final acceptance remain separate.

- Customer recommendation guides (#207; RM34–RM44/AC25–AC37): English and
  Chinese entry/navigation, v1/v2 migration, current HTTP index preparation,
  source JSON and formal JSON/Markdown workflows, explicit choice receipts and
  source/privacy/recovery boundaries. Installed M10 host walkthrough acceptance
  remains separate; no runtime, qualification or budget change.

- Specification 0.60 → 0.61 (#201/#200; RM16/RM35/RM38/RM40/RM41/RM44;
  AC26/AC29/AC31/AC32): add explicit cold local native preparation using existing
  worker events/readiness, actual setup/cleanup timing and unknown-aware costs.
  Define narrow native-versus-unused runtime applicability, a closed self-contained
  comparison runtime display, and invariant native build/device/execution controls. Preserve none/HTTP, semantic/artifact/Row,
  source-proof and 0.60 episode/host/platform contracts and all acceptance gates.
  This publishes tooling contracts, not model activation or measured acceptance.

- Specification 0.59 → 0.60 (#198/#202/#204/#205/#206; RM16/RM38–RM44;
  AC29–AC37): define closed adaptation episode, whole-host three-arm and
  platform exports, exact freeze/companion resolution, conserved observations
  and deterministic cost/quality/reset arithmetic. Reuse native execution,
  existing product rows and the operational grader. Tooling implementation is
  authorized; host activation remains denied until a later qualified
  person/session/immutable-use contract. No provider or budget change.

- Local semantic retrieval and correction backends (#201/#202; RM38/RM41/RM42;
  AC29/AC32/AC33): pinned local artifacts, deterministic segmented representations,
  revisioned hybrid vectors/fusion, measured inference and bounded private
  correction voting/reset share the existing source/derived worker. Native load
  CPU ownership and cancellation remain explicit. At this backend checkpoint the
  production embedding qualification registry was empty; it now holds the two
  reviewed Linux CPU identities (above). Host correction authority, formal
  episodes and final quality/platform gates remain open.

- #203 (RM34/RM38/RM43; AC25/AC29/AC34): activate the reviewed installed
  public GitHub skills profile through bounded immutable acquisition and the
  existing source worker. Preserve explicit owned removals, stale/recovery
  evidence, profile-keyed provider cooldown across restart/rescan, isolated
  credentials and unchanged job budgets. Record two actual scheduled source
  jobs separately from fixture lifecycle tests and outstanding full-M10 gates.

- Specification 0.58 → 0.59 (#203; RM34/RM38/RM43; AC25/AC29/AC34):
  define the installed public GitHub Git skill-source profile with immutable
  original/manifest/rights verification, complete scoped removal proof, closed
  origins/credentials and mandatory persisted cooldown. Reuse the existing single
  worker, atomic publication and stale/recovery evidence. Matrix market refresh
  and independent corpus, host, platform/resource and full-M10 gates remain
  separately assessed. No provider, budget or public wire revision. The actual
  six-skill acquisition qualifies this narrow profile; runtime activation and
  repeated scheduled product refresh remain pending.

- Dormant OpenClaw recommendation libraries (#197; RM36, RM37, RM39;
  AC27/AC28 and partial AC30): bounded single-attempt clients, lossless revision
  and event decoding, closed protocol checks, inert recorded-evidence rendering
  and a default-deny controller. Explicit fixtures exercise the actual gateway;
  production command registration, snapshots, exact skill/MCP use and full host
  acceptance remain unavailable pending qualification and a normative revision.

- Specification 0.57 → 0.58 (#201/#202; RM37/RM38/RM41/RM42;
  AC28/AC29/AC32/AC33): publish the closed local embedding manifest and
  hybrid/adaptation semantic configurations. Specify deterministic representation,
  byte and ordered-segmentation identities, vector/fusion and bounded correction
  voting, reviewed activation with no initially qualified artifact, and fail-closed
  adaptation with actual inference evidence on failure. Existing recommendation
  wire versions/errors and corpus/benchmark contracts are retained. Model/runtime/
  device quality and operational qualification remain open.

- Source-grounded asset benchmark delivery (#191/#199/#200; RM35/RM40/RM44;
  AC26/AC31/AC36): pinned acquisition/build and a 412-asset working catalog
  (87 Matrix skills, 194 original-English SkillsBench skills, 131 genuine MCP
  tools), 128 preregistered Chinese/English/mixed references, and shared
  production-path builtin/team run/compare with explicit index readiness.
  Preserve actual primary timeouts, blocked setup, unknown costs and historical
  report hashes. Developer-authored references support the working development
  profile; independent formal corpus, host/human and platform gates remain
  separate. Reproduction, team integration and shortlist guides are included.

- Specification 0.56 → 0.57 (#191/#199/#200; RM35/RM40; AC26/AC31):
  optional explicit loopback index prepare/status/cancel binds the exact offered
  catalog and scope, with bounded cold/reuse preparation and separate readiness,
  wall-time and observed/reported costs. Stateless mode remains explicit;
  selection v2 stays unchanged. Closed benchmark config/report/comparison
  preparation records and protocol identities preserve historical truth.
  Other original skill origins are permitted only in source-grounded development;
  independent qualified sources, minima and all numerical gates are unchanged.

- Specification 0.55 → 0.56 (#191/#199/#200; RM34/RM35/RM40/RM44;
  AC25/AC26/AC31/AC36): source-grounded development benchmarks and actual Matrix
  examples use documented author-defined references without invented human
  judgments or fixed-size delivery prerequisites. Bounded standalone in-memory
  ZIP inspection preserves originals separately from projections. Shared mixed
  catalogs support available language pairs and team algorithms; formal
  independent qualification, product source choices and resource limits remain.

- Acceptance corpus input tooling (#199; RM40/AC31): assess exact 200-to-300
  source continuity, per-lane and all-target hard-subset cohorts through the
  existing external custodian gate. Pinned offline MCP reproduction reuses the
  four-document export engine without source acquisition or execution. Corpus
  readers follow the corrected related-path, ordering, qualification evidence,
  stored-agreement and schema-search-text contracts. Fixtures remain fixtures;
  genuine originals, human labels and source/platform acceptance stay outstanding.

- Frozen M10 retrieval input infrastructure (#191; RM40/AC31): four closed
  schemas, bounded offline local release/lock verification, immutable raw double
  annotation and adjudication, recomputed agreement and cross-release leakage and
  Latin-token diagnostics. External custodian review is separate from JSON/byte
  integrity; fixtures never qualify real baselines. The offline supplied-export
  CLI preserves exact originals and locked exclusions without acquiring assets.
  Genuine originals, human annotations, rights/access evidence and real acceptance
  measurements remain outstanding.

- Specification 0.53 → 0.55 (#200; RM35/RM40/RM44; AC26/AC31/AC36):
  publishes the closed M10 retrieval configuration, report and paired comparison
  contracts, deterministic planned slots, independent repetition-zero quality
  denominators, production v2 ownership and complete failure/fallback/cost coverage.
  Digest-bound offline companion inputs preserve private custodian resolution;
  sanitized effective configuration and source/generator proofs make comparison
  reproducible. Partial cost subtotals and arm descriptors keep its renderer
  self-contained; catalog revision zero remains valid. The explicit quality-only
  exception relaxes machine differences alone.
  Registers future schemas; runtime implementation and genuine cohort, model,
  whole-task and platform qualification remain separate work. Corpus contracts,
  operational clarification, historical protocols and numerical gates are unchanged.

- Specification 0.52 → 0.53 (#191; RM40/AC31): defines closed M10 catalog,
  case, immutable double-annotation, adjudication and transitive lock contracts,
  exact hard-subset derivation, evidence/rights checks and held-out split rules.
  Fixture validation remains separate from genuine external qualification;
  missing inputs do not block loader development or satisfy acceptance gates.
  Historical evaluation formats, numerical gates and host authority are unchanged.

- Lexical startup and index lifecycle (#196; RM38/AC29): enabled launchers serve
  committed lexical recommendations before generative warm-up, with an independent
  pending inference fence and owned shutdown. Source changes prepare matching
  offers/indexes before atomic publication; policy eligibility has a durable public
  revision binding. Source scans, derived rebuilds and failure repair share one
  cooperative worker with actual job/batch/overlap/drain evidence. Restart rebuilds
  derived state from safe metadata without resetting feedback or its protected key.
  Deterministic checks do not close the real 300-asset, platform, memory or
  embedding/inference contention gates.

- Backend direct actions and feedback privacy (#195; RM37/AC28): separate
  operator registrations and current bearer checks, exact retained-offer choice,
  independent feedback revisions, event-ID deduplication, scoped inspection,
  delete/reset and atomic retention. Erased replay lookups use a protected
  external instance HMAC key; parent/choice receipts remain independently
  retained. Production action contracts remain unqualified and disabled.

- Independent recommendation lifecycle and JSON API (#194; RM36/AC27):
  separate SQLite objects, events, slot and exact idempotency receipts;
  bounded create, read, event polling and readiness routes; owned cancellation,
  restart recovery, choice expiry and 30-day parent retention. Real source jobs
  emit separate index-job events, and compatible lexical offers follow committed
  configured-source acquisition. Backend choice/feedback is supplied by #195;
  SSE, qualified host execution and readiness measurements remain later work.

- Recommendation-only selector v2 (#193; RM35/AC26): closed versioned contracts,
  deterministic projection-only lexical ranking, strict loopback HTTP replacement
  and example, shared deadline/cancellation fences, current admission and one
  separately attributed lexical floor. Configuration constructs the v2 selector
  separately from managed v1. The spec 0.52 follow-up revisions effective
  stale transitions with stored projection bytes and preserves capture-only
  bytes through restart; computing offers require exact current revision.
  No calibrated confidence,
  qualified host binding, inference, useful startup-readiness measurement or
  final M10 quality claim is introduced.

- Independent asset catalog discovery (#192; RM34/AC25): ordinary local
  `SKILL.md` folders and closed, hash-verified metadata imports; bounded safe
  projections, durable atomic SQLite generations, source ownership/tombstones,
  stale failure observations, restart recovery, and one cancellable background
  worker. Configuration remains disabled by default. Assets have unknown host
  availability; no recommendation endpoint, execution, installation, remote
  adapter, useful lexical readiness or live M10 acceptance is delivered here.
  Development follows the explicitly authorized reviewed-spec PR stack while
  #187's merge remains pending; later specification changes must be reconciled.

- Specification 0.50 → 0.51 (#187): adds M10 multilingual skill and MCP
  recommendations as RM34–RM44 and AC25–AC37 (new sections 4.6, 5.1, 7.6,
  8.1, 11.1, 12.6 and 14.5) and amends RM02 and sections 3, 10 and 14.1.
  Managed selection v1 (AC22), AC24, the retired RM30 and AC23, and historical
  evidence are unchanged. The rationale and qualification record are in
  `docs/design/2026-10-29-multilingual-asset-routing.md`; status, blockers and
  issue order are in `docs/ROADMAP.md` (M10). Specification and backlog change
  only; no product code.
  - Review clarifications in the same revision: a ready no_match supports a
    correction but not a rejection; the feedback listing is ordered by a
    per-scope insertion sequence, not by UUID; the create body cap is 512 KiB;
    digests kept after erasure are keyed; the 0.51 runtime create 503 comes
    before the idempotency claim; a partial import marks omitted records
    stale; every new M10 path key resolves against the configuration
    directory.

- The M9 OpenClaw evaluation (#139; RM16, RM25, RM26, RM33; AC24; spec
  0.50 section 12.5), recorded in
  `docs/evidence/2026-10-29-openclaw-eval-apple-m1/` and checked by
  `tests/test_openclaw_eval_apple_m1.py`. One run of
  `scripts/run_openclaw_eval.py` on the user's Apple M1 (8 GiB, macOS
  27.0.1 arm64, Node v26.12.1, OpenClaw 2026.10.7 from the devDependency),
  with the plugin artifact built at `2751124` (sha256 `0eac2b43...`), the
  external model `glm-5.3-flash` (Z.ai), and the pinned local model
  `qwen3-0.6b` Q8_0, over the full frozen task set (3 repeats, seed 139).
  All four arms ran and 144 of 144 attempts succeeded (18 of 18 per arm and
  language), with 0 host failovers, timeouts, or interrupts. Every RelayMind
  arm had 0 local turns: `relaymind_external_only` by `execution_mode`,
  `relaymind_auto` by `context_limit` (the result expected at the 8192
  local context), and `relaymind_no_local` by `no_local_model`.
  Integration overhead (external_only against direct): median completion
  difference -732 ms in English and -55 ms in Chinese over 18 pairs each;
  the local-inference comparisons compare no local inference. The revision
  was recorded dirty: two files unrelated to the run were being written in
  the checkout while it ran (the evidence test module and the CI line that
  lists it); the runner, the plugin, the gateway, and the host used
  neither. After the run the checkout's home-relative path and the Mac's
  computer name were replaced in the export with `<repo>` and `<host>`,
  because the runner's sanitizer replaced neither; both digests are in
  `docs/EVIDENCE.md`. One run on one machine: reduced latency, lower cost,
  and local completion quality remain hypotheses. Windows has not run.
- The macOS qualification of the OpenClaw plugin at the M9 tip (#135 to
  #138, #145; RM33, AC24; section 12.5), recorded in
  `docs/evidence/2026-10-29-openclaw-mac-qualification/` and checked by
  `tests/test_openclaw_mac_m9_qualification.py`. On the user's Apple M1
  (macOS 27.0.1 arm64, Node v26.12.1, OpenClaw 2026.10.7 from the
  devDependency), at the clean commit `6fc0954`, with the release artifact
  built there: the install check passed all 15 recorded steps; the #137
  host scenarios `text-tool`, `tool-deny`, `exec-deny`, and `exec-full`
  passed, and `tool-approve` failed, correctly (no headless route for a
  plugin approval, nothing written); the #138 `abort` (exit 130, run
  cancelled) and `external-failed` (8 runs failed, 7 host retries) passed.
  These ran over gateway doubles and are not live model evidence. One live
  turn through the packaged plugin's own resolved model, against the real
  `relaymind serve` with the pinned llama-server and no external model,
  ended needs_external / context_limit with 0 model calls in auto and in
  local_only (9,384 and 9,389 tokens against 8192); it completes no task.
  The macOS rows of `docs/EVIDENCE.md` and `docs/RELEASE.md` now name the
  run; Windows (native and WSL2) stays `not_run`. The install check takes
  `--target-platform` (needs `--label`), which records the run as the
  section 12.5 platform qualification of the install lifecycle
  (`"platform_qualification": true` in the export, still not live model
  evidence).
- OpenClaw plugin release artifact, CI, install check, and operator guide
  (#145; RM12, RM18, RM19, RM33; AC24; sections 7.5, 12.5, 14):
  `npm run pack:release -- <dir>` (`integrations/openclaw/scripts/release-pack.mjs`)
  builds the tarball from a clean `dist/` after the pack check and writes
  `SHA256SUMS` and `release.json` (pins, engines, files, integrity, the
  building Node); two builds of one checkout are the same bytes.
  `tests/test_openclaw_install.py` checks the locked pins, the
  reproducible build, the runtime-only contents, a text and a host-tool
  round trip from the unpacked artifact (the plugin driver now takes the
  entry to load), and that CI runs every OpenClaw test module.
  `scripts/openclaw_install_check.py` runs the artifact through a host's
  lifecycle in an isolated host home against a gateway over doubles:
  install with `--accept-capabilities`, configure, enable, inspect, the
  token through `paste-api-key` on standard input, discovery, a text and a
  host `read` tool exchange, disable, `uninstall --force`, then
  `config unset` and `models auth logout` for what uninstall leaves (the
  config entry and the stored token). The trace and the sandbox decide a
  pass, never the host's `"ok": true` (`tests/test_openclaw_install_check.py`,
  over a stub host): both exchanges need completed gateway runs and the
  gateway's answer; a removal passes only on a config read as strict JSON
  and no token in a file a host could read back (SQLite through SQLite, so
  a deleted row's bytes in free space or WAL do not count), and anything
  unreadable is unknown, never clean. The host inherits no `OPENCLAW_*`
  setting or proxy of the shell and its local Gateway port is a closed
  loopback port, set as `gateway.port` in the isolated config (an
  `OPENCLAW_GATEWAY_PORT` made the pinned host's `models list --refresh`
  demand Gateway credentials, found in the Linux VPS functional check); a host that stops starting mid-run leaves the rest
  not_run and still cleans up; the export is written even when the
  temporary host home cannot be removed. The npm and pack-report helpers
  (`scripts/npm.mjs`) and the run records, proposals, tool executions, and
  loopback environment (`openclaw_qualification.py`) are shared, not
  copied. It passed against the pinned host (OpenClaw 2026.10.7)
  on a Linux VPS, recorded in `docs/evidence/` as a functional check over
  doubles, not a supported-platform qualification. A new CI job
  `openclaw-plugin` (ubuntu, macOS, Windows runners; Node 24.22.1) runs
  typecheck, vitest, the release build, every OpenClaw pytest tier with
  Node required, and the install check with the pinned host. The operator
  guide (`integrations/openclaw/README.md`) covers gateway startup, local
  and external configuration and the local context limit, loopback, token
  transfer and rotation, model selection, host permissions, cancellation,
  troubleshooting, trace inspection, update and rollback, and full removal.
  `pack-check.mjs` spawns npm through node, so the Windows `npm.cmd` shim
  needs no shell; `release-pack.mjs` treats a child folder named like
  `..release` as inside the package. macOS and Windows runs of this revision on a user's
  machine are not_run (`docs/RELEASE.md` support gaps). The install check
  ran again at `393a6bf` as a Linux VPS functional check (not a
  supported-platform qualification) over doubles and passed every step
  (`docs/evidence/2026-10-28-openclaw-vps-functional-145/`, read by
  `tests/test_openclaw_vps_functional_install.py`): the artifact was packed
  first, then all 15 recorded steps passed. The install check's sanitizer
  also replaces the home-relative `~/...` form the host prints, and the
  three such skill locations left in each of the two Linux VPS exports were
  redacted after the run (recorded in the run's `REPORT.md`). The operator guide
  states the macOS qualification's install, enable, refresh, and
  `uninstall --force` forms (Q4) and removes what uninstall leaves: the
  config entry, the `relaymind:manual` profile, and the stored token (Q12).
  `docs/RELEASE.md` and `docs/EVIDENCE.md` list macOS and Windows, native
  and WSL2, as `not_run` support gaps. The CI job on the windows-latest
  runner found two portability bugs: the transport and plugin drivers
  built a `file://` URL from `process.argv[1]` by hand, which never matches
  `import.meta.url` on Windows, so they exited 0 without running a
  scenario (they now compare with `pathToFileURL`); and the pinned host's
  `agent exec` failed deleting its own temporary state directory (`EBUSY`
  on a SQLite file it still held) after an answered turn, so the install
  check gives each exchange a fresh `--state-dir` in the host home, and
  the operator guide says to do the same on Windows. The `openclaw-plugin`
  job's install check exports of CI run 40652444733 (PR #182) are kept in
  `docs/evidence/2026-10-28-openclaw-install-check-github-runners/` and read
  by `tests/test_openclaw_ci_runner_install.py`: all 15 steps passed on the
  `ubuntu-latest` (Linux 6.17.0-1022-azure), `macos-latest` (Darwin 25.6.0
  arm64), and native `windows-latest` (Windows 2025Server) runners, recorded
  in `docs/EVIDENCE.md` and `docs/RELEASE.md` as a GitHub-hosted runner
  functional check over doubles (not a user-machine qualification); the
  user-machine macOS and Windows rows stay `not_run`. The Windows runner's
  artifact had CRLF text files, so the release build is the same bytes per
  checkout, not across platforms. `docs/TESTING.md` says how to refresh the
  exports from a later run. The operator guide's local context limit,
  `limits` bullet, and troubleshooting table (and `docs/TESTING.md`) state
  that with the pinned host and the 8192 local context `local_only` cannot
  serve an OpenClaw turn (decision D1), and what the operator sees: with
  `localContextLimit` copied, the host's own "Context window exceeded" before
  sending and no gateway run; without it, 422 `needs_external`
  (`context_limit`), classed `format` and not sent again
  (`tests/test_openclaw_mac_qualification.py`). No spec, gateway, budget, or
  default change.
- OpenClaw cancellation, errors, retries, and run trace correlation (#138;
  RM12, RM14, RM16, RM17, RM20, RM21, RM22, RM25, RM33; AC11, AC12, AC24):
  the plugin maps every gateway error code to the host's error class
  through `matchesContextOverflowError` and `classifyFailoverReason`
  (`context_limit_exceeded` a context overflow; `execution_mode_forbidden`,
  `idempotency_conflict`, `run_terminal`, and `needs_external` with reason
  code `context_limit`, `execution_mode`, or `invalid_output` the request
  format class `format`, which the pinned host does not retry on the same
  model; `internal_error`, `workflow_unavailable`, `external_failed`,
  `predicate_unmet`, and `needs_external` with any other reason code (a
  failed local generation, an unavailable local model, a time budget, the
  placement deadline) failures of this provider, `server_error`; 429 an overload, 504 a timeout, 401 authentication) and gives each failure one sanitized,
  actionable message with the gateway run (401 carries the reconnect
  instruction); errors that are not RelayMind's keep the host's own
  classification (`integrations/openclaw/src/errors.ts`). Every request
  carries an `Idempotency-Key`, the digest of the inference payload, mode,
  and session, never the token or the gateway's address
  (`src/idempotency.ts`): the pinned transport gives
  `resolveTransportTurnState` a new random turn ID on every call, so the
  host's turn ID cannot name a retry. A host retry of the same request
  replays a completed run (also one that completed under a plugin retry
  key); a payload whose run ended failed or cancelled sends
  `<key>-retry<n+1>` next, a new run (section 7.4), so a host retry after a
  504 or an abort is never stuck on 409 `run_terminal`; the next turn gets
  a new key. An abort during the retry wait ends the request aborted. The
  plugin makes one attempt by default; the new `maxRetries` setting (at
  most 2) retries only a 429 under the same key and a 504 under a new key,
  before any answer content. Each attempt is recorded with its turn, key, outcome,
  code, and RelayMind run ID (or `unavailable`), with native run and UI
  links and usage unknown, through the redacting logger and the optional
  `diagnosticsPath` JSON lines file (`src/attempts.ts`); nothing goes into
  answer text. `tests/test_openclaw_cancel_errors.py` drives the plugin
  against the gateway over doubles: aborts before sending, during a
  buffered or early-header generation, while the replay is read, and after
  completion; a native events disconnect; 401, 403, 409, 422, 429, 503,
  504, and a stream error event; replays, conflicts, a redacted call, a
  cancelled run's key, and a token change. The qualification harness's host
  mode gains `--scenario abort` (SIGINT once the generation started; the
  host's output goes to files, so a verbose host cannot block on a full
  pipe; the gated double's count is fixed, so a host-sized request reaches
  the held generation) and `--scenario external-failed` (both doubles call
  a tool no host offers on every generation: 503 `external_failed`, checked
  from the runs, the host's message, and the plugin's attempt records). A
  Linux VPS functional check (not a supported-platform qualification,
  `docs/evidence/2026-10-28-openclaw-vps-functional-138/`, read by
  `tests/test_openclaw_vps_functional_cancel_errors.py`) ran both on the
  pinned host over doubles: SIGINT cancelled the run (`cancel_requested`,
  exit 130, as Q11 on macOS); the 503 failed every run, and the host
  retried the same model 7 times with a rewritten conversation, so 8 keys
  and 8 runs, and showed its own generic message with the plugin's in its
  log. Other error codes on the real host and the host's failover are not
  run.
- OpenClaw text and host-owned tool round trips (#137; RM01, RM03, RM04,
  RM08, RM09, RM12, RM19, RM20, RM26, RM33; AC10, AC24): the provider's
  `wrapStreamFn` (`integrations/openclaw/src/stream.ts`) refuses, by field
  and before sending, input the pinned host would change silently (an image
  part becomes "(image omitted: ...)" text) or the gateway refuses
  (a structured response format, `parallel_tool_calls: true`, a strict
  tool, `n` other than 1, a dropped host tool), and turns an answer with no
  text and no call, or a response with more than one call, into a failure;
  it never trims or rewrites. `tests/test_openclaw_round_trips.py` drives
  the plugin's wrapped transport against the gateway over doubles: text and
  a host tool round trip, streamed and buffered; tool_choice; schema
  validation; matching IDs; the host's deny and allow decisions on a
  sandbox mutation (no side effect, then exactly one); tool results as data
  and redacted secrets; fallback, local_only, external_only without a local
  model, an oversized catalog; budgets and no managed selection;
  early-header comments that are not content. The qualification harness's
  host mode gains `--scenario tool-deny|tool-approve` with checks read from
  the sandbox and the trace, and `test/host-tool-plugin/` holds the
  test-only host tools. The macOS host probe's facts are applied: the host
  rewrites the call IDs it replays (`call_probe_1` came back as
  `callprobe1`), so the harness's host checks and the round-trip tests
  match a result to its proposal through the host's rule
  (`host_replayed_call_id`, `answers_call`), never by the exact issued ID,
  and the plugin driver replays IDs the same way; headless `agent exec`
  cannot answer an approval (`ask` fails closed without a Gateway), so the
  host mode adds `--scenario exec-deny|exec-full` (the host's `exec` tool
  under `tools.exec.mode` deny and full), and keeps the host's own JSON
  report as `checks.host_report`, never deciding a pass (the host said
  `"ok": true` for a denied tool). The macOS run at `a52f8bf` is applied
  too: the test host tools' manifest declares `contracts.tools` (the host
  refused to register them without it, Q9); the default `text-tool`
  scenario scripts the echo call before the closing text, so one
  `agent exec` covers it (it could not pass before, Q8); and the checks
  keep the replayed IDs as `replayed_call_ids`. A Linux VPS functional check
  (not a supported-platform qualification,
  `docs/evidence/2026-10-28-openclaw-vps-functional-137/`, read by
  `tests/test_openclaw_vps_functional_round_trips.py`) ran the scenarios on
  the pinned host over doubles: `text-tool`, `tool-deny`, `exec-deny`, and
  `exec-full` passed, `tool-approve` failed with no line written (a
  headless plugin approval has no route), and the host rewrote every call
  ID again. The host offers a plugin's tools only with
  `tools.toolSearch false` and the names in `tools.alsoAllow`. An approval
  answered by an operator is not run.
  No specification or gateway change.
- OpenClaw provider plugin package (#136; RM08, RM12, RM19, RM26, RM33):
  `integrations/openclaw/` now has the plugin manifest
  (`openclaw.plugin.json`), the entry and the `relaymind` provider built on
  the public plugin SDK of v2026.10.7, the api-key method that holds the
  gateway's application token (`RELAYMIND_APP_TOKEN`), and a catalog that
  publishes `relaymind-client-tools` only when the gateway lists it. The
  settings are a loopback-only `baseUrl`, `executionMode` (auto,
  local_only, external_only, sent as `X-RelayMind-Execution-Mode`;
  parallel_demo refused), and limits copied from the gateway configuration
  (the window of the mode, never estimated). Diagnostics tell an
  unreachable gateway, a stale token (401, with the reconnect
  instruction), an absent alias, and the configured mode's readiness
  apart, and never log the token. `npm run pack:check` checks the packed
  tarball. The declared compatibility is exactly the qualified release
  (`openclaw.compat.pluginApi` and the `openclaw` peer dependency are
  `2026.10.7`), so the host refuses to load it on an unqualified release.
  `tests/test_openclaw_plugin.py` drives the plugin and the pinned
  transport against the gateway over doubles. The transport fixtures were
  recaptured unchanged because `package.json` changed. No specification
  or gateway change.
  From the macOS host probe (OpenClaw 2026.10.7): the provider also
  resolves the alias through `resolveDynamicModel` (synchronous, no
  request, the catalog's entry), because headless `openclaw agent exec`
  found a plugin model only that way; the catalog stays. Discovery keeps
  its own 5 s timeout per request, since the host gave `catalog.run` no
  abort signal. The README documents `--accept-capabilities` for the
  install, `paste-api-key` for the token, the TTY that `models auth login`
  needs, and what uninstall leaves behind. From the macOS run at
  `a52f8bf` and a Linux VPS functional check (not a supported-platform
  qualification) of this package on the real pinned host against the
  gateway over doubles (`docs/evidence/2026-10-28-openclaw-vps-functional-136/`):
  the README gives the working forms (`--accept-capabilities`, `plugins
  enable relaymind` after the install, `models list --refresh`, `plugins
  uninstall --force` without a TTY), says the host's own `configSchema`
  validation refuses parallel_demo before plugin code runs (a manifest test
  keeps it out of the enum), and removes the credential and the entry that
  uninstall leaves (`models auth logout relaymind:manual --yes`, `config
  unset plugins.entries.relaymind`). On that check `agent exec` answered
  through `resolveDynamicModel` with no catalog refresh (the `Unknown
  model` fix), the host sent no `stream_options` or other switched-off
  field for the plugin's model (spec 0.49, section 7.5), and a stale token
  gave the 401 reconnect line and recovered after a new paste
  (`tests/test_openclaw_vps_functional.py`). This package on macOS and
  Windows, the env var token path, and the host's own bound on a slow
  catalog hook are not_run.
- OpenClaw compatibility qualification (#135; RM33, AC24):
  `integrations/openclaw/` pins `openclaw` and `@openclaw/ai` 2026.10.7
  and declares the host switches (`src/compat.ts`); a Node driver runs the
  pinned `openai-completions` transport against the real gateway over model
  doubles (`tests/test_openclaw_host_transport.py`); 16 sanitized,
  reproducible wire fixtures are frozen under
  `integrations/openclaw/test/fixtures/v2026.10.7/`; the field-by-field
  compatibility table is in `docs/design/2026-10-27-openclaw-integration.md`.
  `scripts/openclaw_qualification.py` captures the fixtures and runs an
  operator's real host against a doubles gateway;
  `scripts/openclaw_request_size.py` reports captured request sizes against
  the local limit. The real-host probe on macOS (OpenClaw 2026.10.7,
  `docs/evidence/2026-10-27-openclaw-mac-probe/`) captured the host's own
  requests; `tests/test_openclaw_host_captures.py` checks them with the
  gateway's validator. The macOS real-host qualification
  (`docs/evidence/2026-10-27-openclaw-mac-qualification/`, commit tested
  `a52f8bf`) counted them with the pinned tokenizer: 0 of 3 fresh host
  requests and 0 of 7 probe requests fit the 8192-token local limit
  (10,500 to 10,577 tokens), and a live AC24 turn in auto without an
  external model (config-row workaround, #136: a `models.providers` row,
  not the plugin's model) ended needs_external / context_limit with 0 model
  calls (`tests/test_openclaw_mac_qualification.py`). The harness host
  scenarios against the gateway over doubles failed at `a52f8bf` (#136,
  #137). The local context stays
  8192, so in practice OpenClaw turns go external in auto and get
  needs_external in local_only. The plugin itself is #136's.
- OpenClaw evaluation tasks, grading, and runner (#139; RM16, RM18, RM25,
  RM26, RM33; AC24; spec 0.50 section 12.5): `evaluation/openclaw_tasks.v1.json`
  (6 English and 6 best-effort Chinese tasks: text, host `read`, `write`, and
  `exec` under `deny` and `full`; 3 repeats, seed 139) with its digest and
  order digest in `evaluation/openclaw_tasks.v1.lock.json`;
  `relaymind.evaluation.openclaw` loads and checks them, derives the paired
  sampling order from SHA-256 ranks, grades an attempt from the host's exit
  status, answer, tool report, sandbox, the plugin's diagnostics, and the
  gateway runs, and summarizes the arms with exact counts and unknown usage
  kept unknown; `scripts/run_openclaw_eval.py` runs the four arms through a
  real host, each in its own host home and gateway, and writes the raw
  export and a Markdown report without the token or the credential. Tests:
  `tests/test_openclaw_eval_fixtures.py`, `tests/test_openclaw_eval_grading.py`,
  and `tests/test_openclaw_eval_runner.py` (stub host, gateway doubles), also
  in the CI `openclaw-plugin` job. The measurement itself is a separate
  entry (`docs/evidence/2026-10-29-openclaw-eval-apple-m1/`).
- Specification 0.50, the OpenClaw evaluation files, arms, and grading (#139;
  RM16, RM18, RM25, RM26, RM33; AC24; section 12.5): the tasks are
  `evaluation/openclaw_tasks.v1.json`, frozen by its digest in
  `evaluation/openclaw_tasks.v1.lock.json`, apart from the managed-skill
  corpus; the four arms are `direct`, `relaymind_external_only`,
  `relaymind_auto`, and `relaymind_no_local`, in a seeded paired order; an
  attempt succeeds on the host's exit status, its final answer, its tool
  calls, the sandbox, and the gateway trace, never on the host's `"ok"`; a
  host failover is counted apart, and only a reported provider or model
  other than the arm's is one (an error report naming neither is a failed
  attempt); both arms of a paired comparison share one success rule, so
  a call's arguments and a call outside the expected ones are reported
  (unexpected host tools in every arm, unexpected proposals in RelayMind),
  never a failure; `relaymind_auto` needs the external model and a local model,
  and is not_run without either; a host tool call is graded from the host's
  tool report and the sandbox, never by comparing call IDs (the trace holds
  no host result); the report gives exact counts per arm and language, the
  two clocks apart, and tokens per model with unknown counts kept unknown,
  and the export names the Node that ran the host; the report gives the
  reason codes of the runs that left the local stage, so 0 local turns (what
  the 8192-token local context gives an OpenClaw turn, macOS qualification)
  is reported as a result with its reasons, and a local-inference comparison
  without a local turn says so; a host that exits 130 or dies of SIGINT (on
  Windows, ends with STATUS_CONTROL_C_EXIT) is a host interrupt, apart from
  other exit statuses; an evaluation the operator
  interrupts still writes its export, marked aborted, with the remaining
  slots not run, also when the interrupt lands in an arm's setup (the arm
  and every slot not run) or in the grading of an attempt that ended (the
  attempt is kept and graded). No gateway contract change.
- Specification 0.49, OpenClaw host facts recorded on the wire (#135; RM33,
  section 7.5): at v2026.10.7 the host's defaults for a loopback URL send
  `stream_options` and, for a reasoning model, `reasoning_effort`; the
  developer role is off by default there, and a developer switch turned on
  places runtime context after the first user message. The plugin must also
  keep the strict message keys, tool result name, and assistant-after-tool-result
  switches off. The host refuses a request over the advertised context window
  before sending it, so the window the plugin advertises is the larger limit
  of the effective mode (external in auto and external_only). The real host
  confirmed the loopback defaults and the switches on macOS for a config-file
  provider (switches on a plugin-supplied model are not_run; #136 must show
  them on the wire), and showed that
  it rewrites the tool call IDs it replays (underscores removed, the same in
  the call and its result): nothing may match a later request to an earlier
  proposal by the exact ID string. The host decorates the user message, and
  a request over the UTF-8 byte bound is undecided, never needs_external,
  until a tokenizer count; a count of sanitized captures is a lower bound and
  never proves a fit. Amended by #138: section 7.5 names the host class of
  each gateway error code with its retry consequence. The pinned host
  retries the same model only for rate_limit, overloaded, server_error,
  timeout, and output_limit, so a refusal that follows from the
  configuration and the request (execution_mode_forbidden,
  idempotency_conflict, run_terminal, and needs_external with reason_code
  context_limit, execution_mode, or invalid_output) is the host's `format`
  class, and internal_error, workflow_unavailable, external_failed,
  predicate_unmet, and needs_external with a reason_code that depends on load
  or the local runtime's health (capability_missing also covers a failed
  local generation) stay `server_error` (external_failed's reason_code
  cannot tell a refused external output from a transient provider failure).
  No gateway contract change.
- Specification 0.48, unsuitable detection from local stages (#170; RM26, RM27;
  section 14.4.7): the run store records, for every local stage, whether it ran out of
  local time, and handoff.started names the budget that stopped the local stage.
  With the new launcher key `placement.detect_stages = true` (default false) the
  startup check holds the routing-shaped and skill-shaped warm-ups to
  active_seconds together, and a ready model whose three latest local stages
  all ran out of local time is unsuitable until that streak is older than
  `probe_after_seconds`. `scripts/analyze_unsuitable.py` replays both rules on
  the phase 2 package and measures the section 12.3 unsuitable hypothesis from
  recorded cells: not met (managed external execution 43/52 against 45/52 auto
  successes and higher p95 latency), because a local stage that runs out of time
  still runs the tool work the external stage reuses. No default changes.

### Changed

- Specification 0.51 → 0.52 (#187; RM24, RM34, RM38): section 14.5 defines the
  offer projection cache, and section 4.6 now says a stale, availability or
  eligibility change is a changed committed generation that increments
  catalog_revision, so a result on an older offer is stale; a capture-time-only
  observation keeps the revision. Projections key catalog_revision,
  selection_scope and the requested_kinds set (section 10), and a missing usable
  generation fails a create request before the idempotency claim (section 7.6). Wire schemas
  are unchanged. Specification change only; no product code.

- Documentation brought up to date with M9 at #139: the spec version and
  M9 status in `AGENTS.md` (and the OpenClaw scripts in its map), the
  OpenClaw row and the #139 evaluation pieces in `docs/ARCHITECTURE.md`,
  the M9 issue status and exit criteria in `docs/ROADMAP.md`, an OpenClaw
  entry in `README.md`, the M9 gaps in `docs/TESTING.md`, the run record
  in `integrations/openclaw/README.md`, the status line of the OpenClaw
  design note, and the status word in the spec's section 7.5 heading. What
  ran over doubles, in Linux VPS functional checks, and on GitHub-hosted
  runners stays apart from what is not_run (macOS and Windows
  qualification, a plugin approval on a real host, the #139 measurement).
  No behavior change.
- Specification 0.47 redirects M9 from YAML workflows to OpenClaw
  integration (#135 to #139, #145, #173): retire the unimplemented RM30 and AC23
  without reusing their identifiers; add RM33, AC24, the provider ownership
  and compatibility contract, and a separate host-tool evaluation protocol.
  Section 7.5 names the host compatibility switches the plugin declares,
  the request-size and advertised-limit rules, and the error-class
  mapping; AC24 adds an oversized request.
  The fixed parallel demo (RM15, AC14) and existing benchmark evidence remain.
  This is a specification and backlog change; the plugin is not implemented.

### Removed

- The demo UI trace no longer shows the "Workflow steps" section, a placeholder
  for the retired YAML workflow runner that only said "not recorded" (#173;
  RM23, AC14; spec 0.47 retired RM30). The fixed parallel demo's workflow
  summary (RM15) is unchanged, and a stored trace with the reserved `workflow`
  tool mechanism (section 8) still renders. No spec change.

### Fixed

- `.gitattributes` pins the OpenClaw plugin package
  (`integrations/openclaw/`) to LF, so a Windows checkout no longer packs
  CRLF text files into the release tarball (#145; RM18); a Windows runner
  export has yet to show the digests agree.
- Loopback gateway health polls and API/SDK client calls in live acceptance
  and evaluation scripts bypass environment and system proxies without
  changing process-wide settings (#167; RM01, RM18, RM19). Non-loopback endpoints
  keep normal proxy handling.

### Added

- Runner provider request options (RM25; spec 0.46, sections 12.2 and 14.1):
  `--external-request-options JSON` on the corpus and latency runners writes
  `external_model.request_options`, uses the launch file's checks, enters the
  prior partition, and is recorded in the export configuration only when
  declared, so earlier configurations keep their identity.

- External model comparison evidence (RM25): glm-4.6, glm-5.3-flash, and
  glm-5.3-flash with `reasoning_effort` low in the default configuration on
  Apple M1, one pass each, with per-attempt archives in
  `docs/evidence/2026-10-27-external-model/`. Operator configuration only; no
  specification default changes.

- Phase 2 live evidence (#134, #128, #125; RM16, RM25–RM29, RM31, RM32; AC19):
  the complete M8 matrix (31 cells) and M7 matrix (9 cells) on Apple M1
  with Z.ai glm-4.6 at revision 741a191, one pass per cell, with both offline
  analyses, per-attempt archives and a digest manifest in
  `docs/evidence/2026-10-26-phase2/`. EVIDENCE and RELEASE record the results,
  the unmet and unmeasured targets, and the conditions of the run.

- Specification 0.45 keeps every phase 2 default with the measured numbers
  (section 14.4.6): local_first, model_router, buffered answers, final answer
  pass, one call per generation and no prefetch. Section 12.3 now changes a
  default only when the option's paired full auto corpus cohort has at least
  as many task successes as the baseline in the same run.

- Matrix controller recovery (#128, #134; RM16, RM17, RM25): explicit immutable
  measurement roots, exact corpus/latency export normalization, credential-free
  finalized-cell reconciliation, original-index snapshots and separate controller
  provenance. M7 exact unseeded C0/C5 reuse retains the same validation and costs.

- Specification 0.44 defines separate reviewed matrix controllers and immutable
  measurement checkouts, exact serialized export identity, durable recovery
  provenance and credential-free finalized-cell reconciliation for M7/M8
  (RM16, RM17, RM25). Plans, budgets, defaults and measured evidence are unchanged.

- M7 evaluation completion (#128; RM16, RM17, RM25–RM29, RM31, RM32):
  proposal/prefetch accuracy, answer ownership, joined first-token observations,
  initial selector attempts, explicit missing correction coverage, immutable
  regrading, offline matrix planning, exact C0/C5 reuse and comparison reports.
  The live matrix, experimental ablation revision and default decisions remain pending.

- Specification 0.43 defines M7 metric coverage, initial-selection attribution,
  provenance-preserving reanalysis and the offline/explicit-execute comparison
  matrix (#128; RM16, RM17, RM25–RM29, RM31, RM32). Defaults remain unchanged.

- M8 evaluation infrastructure (#134; RM17, RM25–RM28, RM32): validated
  configuration-specific measured priors, serial local active time, honest
  cost/selection/placement metrics and durable matrix checkpoints. Provider
  measurements and the default decision remain pending.

- Specification 0.42 defines validated frozen placement-history imports, measured
  serial local active time, honest evaluation denominators and durable M8 matrix
  execution (#134; RM03, RM16, RM17, RM22, RM25–RM28, RM32/AC19).
  Defaults and live evidence remain unchanged pending reviewed measurements.

- Opt-in verified managed answer streaming (#125; RM12, RM16, RM17, RM20,
  RM32; AC09, AC19): cumulative predicate gates, exact generation ownership,
  persisted live deltas, safe terminal SSE errors, and configured/effective
  corpus labels. Buffered stays the default; provider corpus parity is pending.

- Specification 0.41 defines verified live answer publication, cumulative predicate
  safety/completeness, exact generation commitment, full-answer redaction, effective
  stream policy, terminal SSE behavior and labeled corpus comparison (#125;
  RM12, RM16, RM17, RM20, RM32; AC09, AC19). Buffered remains the default.

- Loopback HTTP selectors (#133; RM12, RM22, RM25, RM28; AC22): fixed
  configured identity, proxy/redirect isolation, bounded cancellable requests
  with a 1 MiB response cap, immutable unverified claims and trace fallback on
  gateway stops. Evaluation separates HTTP attempts from gateway-observed calls.
  Both selection benchmark tracks reuse the production transport; a
  standard-library keyword example demonstrates catalog-only proposals without
  a model or tool execution.

- Specification 0.40 defines configured HTTP selector identity, loopback transport
  policy (plain http only) and response size cap, immutable unverified per-attempt
  claims, fallback trace provenance, gateway-observed grading of runs that reached
  no selector, and production transport reuse in selection benchmarks (#133; RM28,
  AC22).

- Specification 0.39 freezes device placement prediction, trace and readiness
  fields, selector declines, deadline precedence and exclusive idle-probe
  admission/cancellation (#130; RM03, RM04, RM17, RM21, RM22, RM27, RM28; AC21).
- Device placement (#130): actual-request token predictions, production-output
  medians, device-aware deadlines and experimental expected-wait admission.
  Runs record policy and prediction evidence; readiness labels its representative
  estimate. Idle routing probes share the SQLite execution slot with every run
  acquisition, retain failed attempts and cooldowns, and keep the reservation
  until cancellation cleanup ends. The default remains local_first.
- Specification 0.38.1 clarifies rejected optional cache measurements
  (RM14, RM16, RM25, RM27): an actual call reporting more cached tokens
  than known input tokens records only that cached count as unavailable,
  with a fixed credential-free warning. Execution outcomes and other
  measurements are preserved; imported timing samples remain strictly validated.
  It also keeps the parallel workflow out of local skill outcomes (RM15,
  RM27): the planned synthesis join and a failed local branch are not local
  escalations, so they no longer lower the skill's local success rate.
- Persisted timing samples and skill outcome estimates (#129; RM17, RM25,
  RM27, spec 0.38): a versioned sample schema, an additive SQLite migration,
  seven-day retention and a 1,000-sample bound per model/purpose. Every
  managed, client-owned, fixed-workflow and startup warm-up call records
  success or failure with reported measurements; estimates keep warm,
  cold and unknown prefixes separate. Terminal skill outcomes are counted
  once and external costs stay grouped by provider model. Partial provider
  usage now preserves unknown input/output counts as null instead of zero.
- Demo UI phase 2 evidence (#127; RM12, RM23, RM25–RM29, RM31, AC20):
  each trace and the shell show the displayed run's recorded local model
  status and performance options. Every route shows its selector attempts,
  recorded placement and routing-call duration. Model calls show first-token
  timing and distinguish predicted from actual duration; the answer names the
  applied pass. Missing historical fields say “not recorded”, while a value
  recorded as null shows its meaning (none, no token, none selected, no
  routing call). The shell keeps the local model, its status and reason, the
  placement policy, the answer pass, and the answer stream on screen. Workflow
  steps have a labeled placeholder pending #138.
- Reproducible selection benchmarks (#143, #144; RM02, RM08, RM09, RM25,
  RM28, section 12.4): pinned SkillsBench derivation and attribution, a
  digest-locked skill catalog with leave-one-task-out examples, and a pinned
  ToolRet sample lock with no dataset rows checked in. The runner compares
  built-in and loopback HTTP selectors, exports per-case diagnostics and
  macro/micro precision, recall and Jaccard, and evaluates full versus
  narrowed tool proposals without executing any tool. Tool candidates are
  shuffled reproducibly so their presentation does not reveal positives.
  Parquet derivation uses the optional `bench` dependency extra.
  Recorded Apple M1 runs cover 87 skill cases, 500 local tool cases in both
  arms, and 100 external tool cases in both arms. The evidence keeps errors
  and timeouts, distinguishes abstention from narrowing, and changes no defaults.
- Validated phase 2 corpus and latency runner configurations (#128; RM03,
  RM25, RM26, RM29, RM31, RM32): `--performance`, `--budgets`,
  `--selection-chain`, and `--no-local-model` reach generated launch files
  and are recorded in exports and report headers. No-local runs skip the
  local runtime and report local completion as not applicable. Gateway-owned
  final answers no longer count as local-model completions. The live matrix
  and evidence-backed default decisions remain follow-up work for #128.
  Review repairs preserve repeated selector flags, trim settings whitespace,
  export all selector limits and consistent null model metadata, and reject
  incomplete local launch inputs before producing TOML. Local-completion
  grading uses the no-local configuration for failures before acceptance;
  recorded trace evidence takes precedence over a conflicting label. The
  configuration label names the external endpoint (model, host, region; no
  credential), a region without an endpoint is refused, and only option
  errors are reported as usage errors.
- Operation without a usable local model (#126; AC20, RM01, RM05, RM11,
  RM21, RM26, RM28, RM29, spec 0.37 sections 7.4, 8, 11, and 14.1). The
  `[local_model]` table is optional: without it `allowed_modes` defaults to
  `auto` and `external_only` and the selector chain to `example_match`, and
  a file with neither model table, or with `local_only`, `parallel_demo`,
  or `model_router` but no local model, stops startup. `[placement] detect`
  (default true) classifies a configured local model after recovery:
  unavailable when the runtime does not answer with the model loaded within
  `warmup_seconds`, unsuitable when the routing-shaped warm-up takes longer
  than `active_seconds`; the launcher prints one `Local model:` line with
  the class and the cause, and readiness repeats the availability check.
  In auto, a class other than ready is managed external execution: the
  chain runs without `model_router`, the gateway asks for a missing
  identifier itself, otherwise the run hands off with the skill loaded
  (reason `no_local_model`), its prefetch calls first, and a mutation still
  waits for confirmation; a request no selector places hands off with the
  catalog, and the client-owned alias runs on the external model.
  `local_only` and `parallel_demo` are 403 `execution_mode_forbidden` while
  the class forbids them (`local_only` still runs locally while the model is
  unsuitable). `run.accepted` records `local_model`, `local_model_status`,
  and `local_model_reason`, `route.selected` records `local_model_status`,
  and `/readyz` reports a `local_model` object; auto is then ready when the
  external model is. None of this is degraded mode, `external_only` is
  unchanged, and with `detect = false` the class never changes. A skill that
  routes external with its skill ID now runs its prefetch calls before the
  first external generation. The UI label is #127.
- The selection interface (#131; RM02, RM05, RM08, RM22, RM24, RM28, AC22
  in process, spec 0.36 sections 8, 10, 11, and 14.1). The new
  `relaymind.selection` package publishes `selection_request.v1.json` and
  `selection_result.v1.json` (closed, `schema_version` 1, optional
  `tools` and `alternatives`) and checks a request and a result at the
  boundary. The chain tries the configured selectors in order; none, a
  timeout (`selection.timeout_seconds` for a selector that is not a model),
  an error, an invalid result, and an inadmissible result fall through and
  are recorded. The gateway admits a result as the router admits its own
  choice: catalog membership, the route from the registry and the mode, the
  RM05 identifier check with its clarification, and the narrowing
  intersected with the allowlist, so it only removes tools; alternatives are
  ignored. `model_router` wraps the router and runs its generation through
  an injected callback, the run's own `_generate` in a gateway run, so the
  baseline chain keeps its decisions, routing request, events, budgets, and
  cache behaviour. `route.selected` gains `selector` (name, version,
  mechanism, elapsed_ms, attempts) and `tools_narrowed`; another
  selector's choice is recorded with actor `gateway`. A chain that chooses
  nothing routes external with reason `selection_failed` (mechanism
  `selector_chain`) and hands off with the catalog, or ends as
  `needs_external`; a rejected routing output still escalates with
  `invalid_output`. `run.accepted` records `selection`,
  `example_match_score`, `example_match_margin`, and
  `selection.timeout_seconds` inside `budgets`.
- The `example_match` selector (#132; RM01, RM05, RM28, spec 0.32 section
  14.1): the method of the #118 spike, standard library only, no model
  call. It indexes each offered skill's description and its
  `examples.json` requests in both languages, rebuilt when a content hash
  changes, masks identifiers, uses word tokens and Chinese bigrams, and
  scores with BM25 divided by the query's summed idf and capped at 1. It
  picks only a unique best skill at or above `example_match_score` with a
  lead of at least `example_match_margin` (defaults 0.5 and 0.1,
  provisional choices, not accuracy claims; #118 measured raw-scale
  candidates of 2.8164 and 0.1268 as evidence only), reports the score as a
  selector-reported confidence and the other skills that reach the score
  threshold as alternatives. Held out one at a time, the twenty demo
  example requests give 5 right, 2 wrong, and 13 none at the defaults, so
  the default chain stays `model_router`; making it the chain without a
  usable local model is #126.
- The `[selection]` launch table (#131, #132; section 14.1): `chain`
  (default `["model_router"]`; items `model_router` or `example_match`),
  `timeout_seconds` (default 2), `example_match_score` (default 0.5), and
  `example_match_margin` (default 0.1). A wrong type or range is exit
  status 2, and an `http` selector stops startup as not available until
  #133. `relaymind.example.toml` documents the table.
- Completion criteria in the external request, and corrections per completed
  run (#141; RM06, RM12, RM13, RM16, RM25, spec 0.35 section 4.4,
  finding F6). Beside each completion predicate, `skills/predicates.py` now
  holds its failed checks and its criteria, in a registry keyed like
  `COMPLETION_PREDICATES` (`COMPLETION_CRITERIA`); the predicate is true when
  no check fails. The fixed part of the criteria (labels, forbidden patterns,
  what to keep exactly) follows the skill instructions in the first system
  message of the external request. The value part is one gateway rules
  message moved to the end of the conversation before each external
  generation once a successful result exists, refreshed instead of piled up
  and measured with the request (RM13). Its values come only from
  schema-typed fields of the latest successful result (a service or database
  ID, the status enum, timestamps, counts, a boolean, and a deployment ID or
  revision only when it is a plain token), never from free text, so the
  injected log line of the AC06 fixture never reaches it (RM12). A rejected
  external answer is corrected with the failed checks, in gateway text that
  does not quote the answer; the local correction and the parallel synthesis
  request are unchanged (AC14). `relaymind.evaluation.grading` counts the
  external answer generations before the published one in a completed run
  and their summed duration, and `relaymind.evaluation.metrics` reports them
  per block and in the corpus report; a missing duration leaves the sum
  unmeasured, and a grade without the figure is not reported as zero.
- Local warm-up at startup and the external prompt layout (#124; RM18,
  RM24, RM25, RM27, spec 0.31 sections 6, 10, and 14.1). A `[placement]`
  table takes `warmup` (default true) and `warmup_seconds` (default 30); the
  other placement keys stay unknown keys until they exist. After restart
  recovery and before the address and the token are printed, `main` sends one
  routing-shaped and one skill-shaped generation to the local model on a
  short-lived adapter of its own, built by `Router.build_request` and
  `skill_stage_messages`. They create no run and are bounded by
  `warmup_seconds`; their timings (duration, first token, cached tokens, with
  `None` for a count the runtime did not report) are in `Launched.warmup` and
  one stdout line. A runtime that does not answer in time or fails is one line
  on standard error, not a startup error. `build_app` sends nothing. In the
  external request the system message now ends with the handoff JSON (run ID,
  remaining budgets). The local layout change (the skill body before the
  conversation) was measured live and not kept (see Changed).
  `scripts/latency_benchmark.py` gains `--no-startup-warmup`, and with the
  warm-up on its export holds the launcher's warm-up line and the samples
  read from it (`cold_start.startup_warmup`; spec 0.31 section 12.2), with
  `None` for a figure the launcher printed as n/a and no samples when it
  printed no such line.
- Admitted calls per generation (#122; RM05, RM08 to RM11, section 4.3,
  AC18): under `performance.calls_per_generation = "many"` a generation may
  propose several read-only calls. In a local stage the limit is the
  per-stage `tool_calls`; in the external stage it is the remaining
  `run_tool_executions`. Each call is validated on its own (RM05, RM09) and
  runs in proposal order through the executor with its own `tool.proposed`,
  `tool.started`, and `tool.finished`, is charged, and is answered by its own
  tool message after one assistant message that holds every call. A mutating
  call with others, more calls than the limit, one invalid call, or a set
  with more new executions than the remaining tool budget has room for is
  rejected as a whole before anything runs: `multiple_tool_calls`, or the
  first invalid call's own rejection code, through the format repair, and
  the rejection answers every call with what its `tool.proposed` records
  (the call that failed its own validation with its error and the others
  `not_executed`; for the limit, a mutating call, or the room, every call
  with that rejection; spec 0.34 section 4.3). The room
  counts one execution per call, so the retry of a transient read failure
  can still use the last of it in the middle of a set; the run then ends on
  budget as it would for a single call. A repeated call in a set is a
  replay and costs no execution (RM10). The default `one`, the
  client-owned alias (section 7.2), and the fixed parallel workflow's local
  branch (RM15) keep the one-call rule and their old exchange.
  `BudgetMeter.tool_room()` and `admitted_calls_limit()` give the room.
- Prefetch (#123; RM05, RM07 to RM10, RM12, RM14, RM29, AC18). A skill
  manifest may carry `prefetch`, an array of `{tool, arguments}` where each
  argument is `{"from": "request"}` (an identifier with a closed list of
  values) or `{"constant": value}` (an argument that is not an identifier);
  the registry checks it at load, without a request: a mutating tool, a
  tool outside `allowed_tools`, a constant for an identifier, a request
  binding for a non-identifier, an unknown parameter or binding, a required
  argument with no binding, constants the full tool schema refuses for any
  allowed value of a request binding (spec 0.34), or the same call declared
  twice is a startup error. Under `performance.prefetch =
  true` the gateway resolves each call from the user turns by the RM05 rule (exactly one allowed value
  appears; otherwise that call is skipped, not the skill), runs the
  resolved calls together through the executor with every check of a model
  proposal, records `tool.proposed` with actor `gateway`, mechanism
  `prefetch`, and model null (the store keeps the label `gateway` for the
  operation, and restart recovery maps it back to null), charges them to the
  budgets, and adds one tool exchange in the declared order before the first
  skill generation. When every tool the skill allows has a result, that
  generation offers no tools. Dispatch authority does not move (RM14), and a
  later proposal of the same call replays the record (RM10). A prefetch the
  tool budget has no room for (one execution per call) is not started. At
  construction and in the launcher, a skill whose declared calls exceed
  `tool_calls` (a skill that can run locally) or `run_tool_executions` stops
  startup under `prefetch = true` (exit status 2, `[performance] prefetch: ...`). The demo
  skills `check-service-health`, `lookup-latest-deployment`, and
  `investigate-performance` declare their calls (section 9) and move to
  version 0.2.0 (RM07). `run.accepted` records `prefetch_declarations`, an
  object keyed by offered skill with an array for each, empty for a skill
  with none. The prompt wording of a stage that starts with prefetched
  evidence has no live qualification yet; the corpus run of #128 will give it.
- The draft answer pass (#121; RM16, RM19, RM31, AC17): under
  `performance.answer_pass = "draft"` the skill-stage draft that met the
  completion predicate is published as the answer without the second
  generation, when it is in the run's language (`identifiers.has_cjk`: a
  Chinese run needs Chinese text, an English run none), holds no tool-call
  JSON or tag, and the chat request set no `max_completion_tokens` and no
  `temperature` or `top_p` other than its API default of 1 (section 7.1);
  otherwise the final generation runs as before. `run.accepted` records the
  answer options as before.
  `run.completed` records `answer_pass_applied` (`draft` or `final`) for a
  local answer and leaves it out for an external answer, a workflow render,
  or a client-owned completion. The default stays `final`.
- Early response headers (#140; RM14, RM19, RM20, RM21, RM32, AC12): under
  `performance.early_headers = true` a streamed chat request is accepted
  first (authentication, body, mode, idempotency, and the 429 slot still come
  before any header), gets `X-RelayMind-Run-ID` and the new
  `X-RelayMind-Events-URL` at once, then an SSE comment keep-alive every
  `[native] heartbeat_seconds`, and exactly the chunks of a buffered
  response when the run ends. A run that ends without an answer sends the
  mid-stream error event (`code`, fixed `message`, `run_id`, and `reason_code`
  when it has one) with no finish chunk and no `[DONE]`. A disconnect still
  cancels the run, even before the run's task first ran, and the response
  waits for the run to stop up to `[native] cancel_wait_seconds`, as the
  buffered disconnect does, so the execution slot is free when the
  connection closes. Both aliases use it; a request without `stream` and an
  idempotent replay are unchanged. `scripts/latency_benchmark.py
  --early-headers` runs the gateway with the option, labels the first
  transport event as the header time, and reads the mid-stream error event
  (and the deliberate close after it) as the end of a `routing` sample:
  the sample keeps its completion time and records the event code in
  `stream_error_code`; a close without that event is still a transport
  error (RM25, section 12.2).
- The `[performance]` launch table (`answer_pass`, `answer_stream`,
  `calls_per_generation`, `prefetch`, `early_headers`) and
  `PerformanceSettings`; every default is the earlier behavior. A wrong value
  is exit status 2. `answer_stream = "live"` is refused until gated answer
  streaming exists (RM32). `calls_per_generation` (section 4.3) and
  `prefetch` (RM29) are recorded and acted on; see the two entries above.
  `run.accepted` records the five options in force; the trace schema stays
  at version 1 with these fields optional.
- A failed run with error code `predicate_unmet` is run state: a chat
  request names it in a 409 `run_terminal` (section 7.1). No code produces it
  yet.

### Changed

- Specification 0.38 clarifies the timing records and estimates required by
  #129 (RM17, RM25, RM27): actual call actor and kind, nullable measurements,
  bounded retention, separate warm/cold effective throughput, and terminal
  skill outcomes. Predictions remain estimates and no execution default changes.
- Specification 0.37 → 0.37.1 (RM03, RM09, RM25, RM28; section 12.4):
  a first generation exceeding its call budget receives no set-score credit
  while preserving raw proposals, usage and independent schema validity. A failed
  narrowing arm records its failure without model work and stays in the denominator;
  valid abstentions retain the empty-set experiment. Ordinary schema-invalid names
  retain their existing independent name metrics. Colliding ToolRet tool names
  get suffixes in declaration-digest order, so a plain name no longer marks the
  positive: `evaluation/tools.v2.lock.json` (the same 500 rows; 5 case digests
  differ) is the default, and `tools.v1.lock.json` stays only to reproduce the
  recorded runs.

- RouteDecision and HandoffEnvelope accept the reason codes
  `selection_failed`, `no_local_model`, and `local_slow` under schema version
  1, and the RouteDecision validator and schema now check that the reason
  code follows the route (spec 0.31 section 8): local carries
  `simple_skill`, clarify `missing_input`, parallel `parallel_workflow`, and
  external one of the reasons for leaving the local path, so `local_budget`,
  `invalid_output`, and `tool_failure` are never a route's (#131).
- The routing cache key holds the selector chain (each selector's name and
  version) and the cache policy version is 2 (spec 0.31 section 10, #131).
  An entry is stored only when `model_router`'s routing generation chose the
  skill. `run.accepted` `budgets` holds `selection.timeout_seconds` next to
  the section 11 limits.
- The skill-stage layout is unchanged (#124, spec 0.35 section 10). Two
  layouts of the first system message were measured on 2026-10-23 on the
  pinned model. The description and tool instruction first, then the skill
  body (`7bfe3fa`), kept live AC01 at 3 of 4 but lowered the local_only
  English simple corpus subset (4/12 local completion and 11/12 tool
  correctness before, 1/12 and 7/12 after, twice each). The skill body first
  (`4db75b9`) lowered live AC01 to 2 of 4, then 1 of 4, and was not run on the
  corpus. `docs/EVIDENCE.md` has the runs.
- A tool event of a gateway-made call has model null and actor `gateway`
  (RM22, spec 0.32 section 8); `tool.proposed` has an optional `mechanism`
  (`model`, the default, `prefetch`, or `workflow` with its `step`). The
  trace schema stays at version 1 with these fields optional. `tool.started`
  and `tool.finished` of a gateway-made call carry the same `mechanism`, and
  a null model is valid only with actor `gateway` and a `prefetch` or
  `workflow` mechanism, so the check that a model-made call names its model
  (RM22) still holds for the events of every other call. Restart recovery
  copies the mechanism onto the recovered `tool.finished`.
- First model action (the demo UI and `scripts/live_acceptance.py`) and tool
  correctness (`relaymind.evaluation.grading`) count model-made proposals
  only (section 12.1); a prefetch proposal is counted in the new
  `gateway_proposals` of a run grade, and a required call is met by a model
  proposal or by an admitted gateway call (spec 12.2 now says so: with
  prefetch on the evidence the case needs was gathered either way, and
  prefetch correctness stays a separate measure). The trace view lists the
  `mechanism` of each proposal.
- `scripts/run_corpus.py` takes the draft guards (run language, tool-call
  JSON) from `relaymind.orchestrator.performance`, so the offline regrade
  and the gateway apply one rule (#115, #121).
- `Usage.cached_tokens` is `None` when the source returned no count, not
  0 (#120; RM16, spec 0.31 section 6, finding F9), and the same holds for
  `reasoning_tokens`. The recorded `usage` of an event, the timing
  statistics, the latency benchmark's model calls, and the live harness
  keep `null`. In the latency benchmark's prefix cache record, a call with
  no reported count is counted as not measured and a total over no
  measured call is `null`. Exports made before this change still hold the
  adapter's default of 0.
- The launcher sets the read timeout of each real adapter to at least the
  budget in force: `active_seconds` for the llama.cpp adapter and
  `external_seconds` for the external adapter, never under the earlier
  30 seconds (#142, #120; RM03). Before, a raised limit could be cut off
  by an unrecorded 30 second read timeout. A file without `[budgets]`
  behaves as before.
- Adapter event streams now start a generation with a `FirstToken` marker
  before its first answer text, reasoning text, or tool-call fragment. The
  timing statistics ignore it, so their definitions are unchanged; a
  consumer that lists every event of a stream sees one more (#120).

- Specification 0.36 → 0.37 (#126; RM22, RM26, RM28, RM29; sections 7.4
  and 8): it states what a gateway without a usable local model does where
  0.31 left it open. Startup detection measures the routing-shaped warm-up
  cold, because it is the first request after the model loaded; a warm-up
  that fails, or that does not finish within `placement.warmup_seconds`
  when that is shorter than the stage budget, classifies the model
  unavailable. Without a usable local model the chain runs without
  `model_router`, and with `example_match` alone when that leaves no
  selector. A model last measured unsuitable comes back unsuitable when
  readiness sees it answer again. Every `route.selected` of an auto run in
  managed external execution carries `local_model_status`; `/readyz`
  reports the class in a `local_model` object and reports a mode the class
  forbids as not ready. A skill that routes external with its skill ID runs
  its prefetch calls after `handoff.started` and before the first external
  generation (RM29).

- Specification 0.35 → 0.36 (#131, #132; RM22, RM24, RM28; sections 8 and
  10): it states what the selection interface records. A selector chain that
  chooses nothing makes the gateway record its own decision (route external,
  skill_id null, reason `selection_failed`) in `route.selected` with the new
  mechanism `selector_chain`, a `selector` with a null name, version, and
  mechanism, the whole chain's `elapsed_ms`, and every attempt; a rejected
  model router output still records no `route.selected` and escalates with
  `invalid_output`. `tools_narrowed` is the narrowing intersected with the
  skill's allowlist, in allowlist order. A `route.selected` of `model_router`
  keeps the local model's actor, and any other selector's choice has actor
  `gateway`. A result that names another selector, or claims a mechanism of
  the gateway or of another built-in selector, is an error. The selection
  time limit is recorded inside `budgets` as `selection.timeout_seconds`.
  Section 10 names `selector_chain` among the gateway-made routes that are
  a cache miss and never stored, and a skill another selector chose is not
  stored either.

- Specification 0.34 → 0.35 (#141, #124; RM06, RM12, RM13, RM16, RM24,
  RM25; AC14): section 4.4 gives the order of the parts of the external
  system message (the rules, the skill instructions, the fixed completion
  criteria, the catalog, the answer instruction, then the handoff JSON),
  splits the completion criteria into a fixed part and a value part that
  is refreshed before each external generation and measured with the
  request, says that an external correction names the failed checks, and
  leaves the synthesis request of the parallel workflow unchanged.
  Section 10 no longer asks for the skill body before the conversation in
  the skill stage: the live measurement of #124 lowered local completion and
  tool correctness, so that layout stays as it was.

- Specification 0.33 → 0.34 (PR #153 review; RM05, RM09, RM10, RM15,
  RM16, RM22, RM29; sections 4.3, 4.4, 8, 11, and 12.2; AC18): it states
  what #122 and #123 implement. Section 4.3: under
  `calls_per_generation = many` a set within the limit is also rejected
  whole, with `multiple_tool_calls`, when the tool budget has no room left
  for it (the room is the tighter of the per-stage and the entire-run limit
  left in a local stage, the entire-run limit left in the external stage;
  the room counts one execution per call that needs one, so a replay needs
  none, while the limit on the number of calls counts every call); the
  rejection of a set is one exchange that answers every call, and each
  tool message carries what the trace records for that call: the call
  that failed its own validation gets its rejection and the others
  `not_executed`, and a rejection about the set (the limit, a mutating
  call, no room) is recorded on every call and answers every call; a retry
  is not reserved for, and exhausted time can stop a set in the middle; an
  admitted set runs in proposal order, a failed call of the local actor
  escalates with `tool_failure` and the later calls do not run, and the
  external actor gets every result as data; under `one` a generation with
  several calls keeps its old one-call exchange. Sections 4.3 and 4.4: the
  `local_health` branch of the fixed parallel workflow keeps the one-call
  rule under either value (RM15). Section 11: the admitted-calls row names
  the room rejection. RM29: the declarations are checked against the
  budgets at startup, only when `performance.prefetch` is true, the
  per-stage limit only for a skill not marked external-required (0.31 to
  0.33 said a manifest validation error at load); resolved calls the tool
  budget has no room for are not started or recorded; nothing is pending
  when every tool the stage offers has a succeeded result; constants are
  checked against the tool's full schema with every combination of the
  allowed values of the request-bound identifiers; and the same call
  declared twice is a manifest validation error. Section 8: `tool.started`
  and `tool.finished` of a gateway-made call carry its `mechanism` and
  model null, and a null model is valid only with actor `gateway` and a
  `prefetch` or `workflow` mechanism. Section 12.2: an admitted
  gateway-made proposal counts toward a required call group of tool
  correctness and is reported apart as `gateway_proposals`.

- Specification 0.32 → 0.33 (PR #150 review; RM03, RM12, RM16, RM22;
  sections 6, 8, and 14.1): `model.finished` holds `cached_tokens` and
  `reasoning_tokens` inside `usage`, next to the input and output counts,
  and records a count the source did not report as `null`, never a
  defaulted 0 (0.31 put them at the top level and said absent);
  `first_token_ms` is integer milliseconds, `null` when no token arrived;
  `model.started` records `request_options` only when at least one option
  is declared, so a local call and an external call without declared
  options both leave it out. Section 14.1 also states the two rules #150
  added before this revision: a sampling key (`max_tokens`,
  `temperature`, `top_p`) in `[external_model.request_options]` is
  refused, and the launcher sets each real adapter's read timeout to at
  least the budget in force; a request option key, at any depth, must be
  non-blank text.

- Specification 0.31 → 0.32 (PR #146 review; RM05, RM17, RM22,
  RM26, RM28 to RM32; AC11, AC17 to AC20, AC22, AC23): distinguish
  an absent local model from an unavailable or unsuitable configured
  one; separate the prefetch option from its declarations; complete
  the answer-pass event and terminal-error contracts; define selector
  threshold keys, ranges, and provisional defaults; separate prefetch
  binding validation from request-time resolution; and specify workflow
  package loading before workflow-ID checks. Documentation only;
  phase 2 remains unimplemented.

- Specification 0.30 → 0.31 (phase 2; RM26 to RM32, AC17 to AC23):
  operation without a usable local model, with detection; placement
  from recorded timing samples; one replaceable selection interface;
  prefetch; workflow packages; the draft answer pass; gated answer
  streaming and early response headers; several read-only calls per
  generation; completion criteria in the external request; timing and
  cached-token recording; a budgets launch table; and the selection
  benchmarks of section 12.4. Every new option defaults to the earlier
  behavior. Reasoning: `docs/design/2026-10-23-performance-first-routing.md`;
  findings: the 2026-10-23 analysis in `docs/EVIDENCE.md`; plan: M7 to
  M9 in `docs/ROADMAP.md`. Documentation only: no code or observable
  behavior changes.
- `README.md` is now a short user guide (what it is, install,
  configure, start, use); project status lives in `docs/ROADMAP.md`
  and `docs/RELEASE.md`.

### Fixed

- Selection benchmark review (RM03, RM09, RM16, RM25, RM28): clear stale
  diagnostics before early bypasses, classify HTTP timeouts, preserve failed
  narrowed arms without inference, and remove score credit for generations
  exceeding their call budget while retaining proposal evidence. Export distinct
  HTTP response identity claims, reject skills proposal options before work,
  share the persistent ToolRet cache, and enforce source/case locks without
  changing the frozen files or historical measurement exports. Count only
  `unknown_tool` and `invalid_arguments` against proposal schema validity,
  check SkillsBench archives against the lock digests in a per-user cache, and
  pin `docs/evidence/*.json` to LF line endings.

- Correct the skill guide's workflow-key description, repair the testing
  guide's README link, and install the client SDK before the Python
  quickstart example (PR #146 review).

- `docs/EVIDENCE.md`: the 2026-10-22 latency entry reported "0 cached
  prompt tokens" as a runtime figure; it is the adapter's default
  (finding F9 of the 2026-10-23 analysis).
- Demo skill completion predicates reject answers that break spec
  section 9 (#13; RM05, RM16, RM23): negated or mixed health statuses,
  inferred incident causes or a missing service, invented deployment IDs,
  negated or extra-database deletion reports, and empty or mid-sentence
  diagnostic section labels. Example notes and `docs/EVIDENCE.md` state
  that the Chinese material has not been natively reviewed (spec 0.7
  section 9); all six packages move to version 0.1.1 (RM07).
- Loopback llama.cpp inference and runtime startup health polls bypass
  environment and system proxies without changing process-wide settings
  (#57; RM01, RM18). Non-loopback endpoints and the external Chat
  Completions adapter keep normal proxy handling. Deterministic tests
  cover localhost, IPv4/IPv6 loopback, and remote hostnames; the historical
  Windows qualification remains evidence for its recorded revision only.

### Added

- Native Windows launcher and latency evidence (#110; RM12, RM17 to
  RM19, RM25, AC15; sections 12.1 to 12.3 and 14.1): supplied exports
  from a physical Windows 11 x86-64 machine with the pinned Q8_0 model
  and llama.cpp b11785, built from source. Five gateway configurations
  reached `/healthz` and `/readyz`; the local latency benchmark retained
  all 120 samples, including 76 errors and zero request timeouts. Its
  p95 first model action was 3610 ms, so the 500 ms hypothesis was not
  met. AC15 closes with separate macOS and Windows latency reports;
  acceptance failures, missing external scenarios, and the unavailable
  browser driver remain recorded. A hosted-runner workflow is no longer
  needed to obtain the Windows result.
- Budgets table in the launch file (#142; RM03, RM17, RM19, spec 0.31
  sections 11 and 14.1): `[budgets]` sets the section 11 limits, with the
  `LocalBudgets` field names as keys. A wrong type, a non-finite number, a
  limit that must be positive and is not, `repair_generations` above
  `local_generations`, and an unknown key stop startup with one message and
  exit status 2; every run records the values in force in `run.accepted`.
  A file without the table keeps the defaults. `relaymind.example.toml`
  shows the block, commented.
- First-token time, cached and reasoning tokens, and provider request
  options per model call (#120; RM12, RM16, RM18, RM25, RM27, spec 0.31
  sections 6, 8, and 14.1): `model.finished` carries `first_token_ms`
  (integer milliseconds from the request start on the run's monotonic
  clock, `null` when no token arrived) and, inside `usage`, `cached_tokens`
  and `reasoning_tokens` (`null` when the runtime or provider returned no
  count). The adapters read `usage.prompt_tokens_details.cached_tokens`,
  `usage.completion_tokens_details.reasoning_tokens`, and llama-server's
  `timings.cache_n` (the usage details win when both are sent); the
  llama.cpp adapter sends `cache_prompt`. An internal `FirstToken` marker
  dates the first generated fragment even when a tool call is only emitted
  after the finish chunk (RM09). `[external_model.request_options]` is a
  table of provider request fields sent verbatim and recorded in
  `model.started` (only when at least one is declared, spec 0.33, so a
  local call and an external call without options record none alike); a
  credential, a header, a key that would replace
  `model`, `messages`, `tools`, `stream`, or `stream_options`, a sampling
  key (`max_tokens`, `temperature`, `top_p`), an empty or blank key at any
  depth, and a value that is not JSON stop startup with a message that names the key and never
  the value. The trace schema stays at version 1 with optional fields. The
  pinned runtime (build 11785) reported 558 of 559 prompt tokens as cached
  on a repeated prompt and no reasoning count; the live check is in
  `docs/EVIDENCE.md`.
- Runtime and provider timing probes (#114, #116, #117; RM18, RM25, RM26,
  RM27, spec 0.32 sections 6, 10, and 12.2):
  `scripts/probes/probe_prefix_cache.py` sends the gateway's own routing
  and skill-stage requests to llama-server with `cache_prompt` on and off
  and reads the whole `timings` and `usage` objects;
  `scripts/probes/probe_cancel_prefill.py` cancels a prompt of about 6,000
  tokens during prefill and measures the wait of the next request (its
  definition lists exactly two short prompts per repetition);
  `scripts/probes/probe_external_latency.py` records the time to headers,
  to the first reasoning delta, to the first content or tool-call delta,
  and to the end of streamed provider calls, with and without tools and
  with the provider's reasoning default and off. `--runtime-extra-args`
  adds llama-server arguments after the manifest's launch settings (for a
  CPU-only runtime with two threads) in the live harness, the corpus
  runner, the latency benchmark, and the probes; the default adds none,
  the arguments are recorded in every export, and only runtime-only
  settings (GPU layers, threads, batch sizes), each with one integer value
  in its range, are accepted; a positional argument (a model path), a
  missing or invalid value, a `--no-` form, and any other flag are refused. `skill_stage_messages` in
  `orchestrator/local_loop.py` is the skill-stage conversation as a pure
  function, with no change in behavior. The results are in the 2026-10-23
  runtime and provider timing entry of `docs/EVIDENCE.md`. No gateway
  default changes.
- Offline S0 spikes on the 2026-10-22 corpus export (#115, #118, #119;
  RM28, RM31, spec 0.31 sections 4.3, 12.1, 12.2, and 12.4):
  `scripts/run_corpus.py --regrade-draft-pass` grades the stored traces as
  if the skill-stage draft had been the answer, after the RM31 guards, and
  writes a separate export labeled as an offline counterfactual;
  `scripts/probes/count_multi_calls.py` counts the round trips that several
  read-only calls per generation would have saved;
  `scripts/probes/probe_example_match.py` scores a throwaway BM25 matcher
  on the 52 corpus prompts against the new skill sidecar
  `evaluation/corpus.v1.skills.json`. The corpus lock freezes the sidecar
  in a `sidecars` key, `check_lock` verifies the sidecars a lock names,
  `load_skills_sidecar` checks it against the corpus, and the corpus
  digest is unchanged. `relaymind.evaluation.environment` gains
  `application_revision()`, which each spike records. The results are in
  the 2026-10-23 entry of `docs/EVIDENCE.md`. No gateway behavior or
  default changes. After review (PR #147): the counterfactual
  `run.completed` carries every field section 8 requires and
  `answer_pass_applied` `draft`, the export documents every block
  reason, a local set is limited to the room left in its stage's tool
  limit, null arguments read as `{}`, the matcher picks only a unique
  best skill, all three scripts refuse an `--out` equal to the export,
  and a lock whose `sidecars` value is not an object is refused; no
  recorded figure changed.
- Correctness corpus and latency benchmark (#32, #33; RM25, spec 0.28
  sections 12.1 to 12.3 and 14): the frozen corpus
  `evaluation/corpus.v1.json` (40 English cases in the section 12.2
  classes and 12 best-effort Chinese simple cases, three new sandbox
  seeds, digest lock `evaluation/corpus.v1.lock.json`);
  `relaymind.evaluation` (corpus checks, grading from the trace,
  summaries with exact numerators and denominators, nearest-rank
  percentiles, the environment manifest); `scripts/run_corpus.py` (each
  case and mode in a new gateway from `relaymind serve --config`, with a
  `--regrade` option for stored traces); `scripts/latency_benchmark.py`
  (the compatible stream, a cold request, three warm-ups, ten runs per
  prompt in a seeded order); and `docs/RELEASE.md`. The sandbox now
  publishes its tool permission table (`DEMO_TOOL_PERMISSIONS`). The
  first runs on Apple Silicon macOS are in `docs/EVIDENCE.md`: local
  completion 4/12 on the English simple subset, no missed confirmation in
  156 runs, and p95 first model action 508 ms, so the 500 ms hypothesis
  is not met there. Those first runs covered macOS only; the Windows
  follow-up is recorded above (#110, AC15).
- Live acceptance harness and the first live runs of the M3 to M5
  scenarios (#88, #104; RM04, RM08 to RM25, spec sections 10, 12.1, 13,
  and 14.1): `scripts/live_acceptance.py` starts llama-server from the
  manifest and the gateway with `relaymind serve --config`, and runs
  AC02, AC05, AC06, AC07, AC09, AC10, AC11, AC12 (cancel and restart
  during a generation), `external_only`, `parallel_demo`, the AC08
  external swap, a demo UI session in live mode, and cache-off and
  cache-on runs of the AC01 prompts over HTTP. It records gateway checks
  and model findings apart; scenarios that need an external endpoint
  are recorded as not run without one. The macOS run with Z.ai `glm-4.6`
  is in `docs/EVIDENCE.md`: all 90 gateway checks held; 9 model checks
  failed and are recorded as findings. Deterministic coverage over model
  doubles is `tests/test_live_acceptance_script.py`;
  `tests/test_live_acceptance.py` is the `live` variant. No Windows
  machine was available. AC08 stays open: its external swap ran between
  two models of one provider.

- Live AC08 external swap to a second endpoint (#109; RM18, AC08, spec
  0.29): the live acceptance harness ran with Z.ai international
  (`https://api.z.ai/api/paas/v4`) and then Zhipu bigmodel.cn
  (`https://open.bigmodel.cn/api/paas/v4`), both `glm-4.6`, with only the
  `[external_model]` table changed. The swap passed and every gateway
  check of the run held; the run is in `docs/EVIDENCE.md`. AC08 is
  closed. Both endpoints are Zhipu's (international and mainland China
  platforms), so a second vendor's API is not covered.
- Second local model artifact and the live AC08 local swap (#90; RM18, AC08,
  spec sections 6 and 12.3): `models/qwen3-0.6b-q4_k_m.manifest.json`
  (Qwen3-0.6B Q4_K_M), generated from the artifact, passed the live
  smoke suite and the English live skill-and-tool runs on llama.cpp
  b11785. `RELAYMIND_LIVE_MANIFEST` selects the manifest the live suite
  runs against, so the swap from Q8_0 to Q4_K_M needs no code change;
  the live trace export names the manifest. The runs are in
  `docs/EVIDENCE.md`. `models/qwen3-0.6b-q4_0.manifest.json` (Q4_0) is
  recorded as not qualified: it failed the thinking-mode smoke check.
  The external half of AC08 stays covered by scripted endpoints only,
  so AC08 stays open.
- Chat disconnect cancels the run (#92; RM14, RM17, RM19, RM20, AC12;
  spec 0.24 section 7.4): when the client of `POST /v1/chat/completions`
  leaves before the response is sent, with stream true or false, the run
  the request owns is cancelled through the same path as the native
  cancel (adapter asked to cancel, in-flight tool operation settled, run
  `cancelled`). A native events disconnect still leaves the run alone.
  Tests: `tests/test_chat_disconnect_cancel.py`.
- Operator launcher (#87; RM12, RM17, RM18, RM19, RM21, RM23; spec 0.21
  section 14.1): `relaymind serve --config relaymind.toml` starts the
  gateway on macOS and Windows. The TOML file wires the local and
  external models, skill roots, sandbox, run store, chat and native
  settings, the degraded-mode policy, enabled workflows, and the UI data
  mode. Startup refuses a public bind address, a manifest hash mismatch,
  an invalid skill package, an undeclared data mode, an unknown key, a
  credential in the file, a non-loopback local model URL, an out-of-range
  port, a non-positive duration or limit, an unknown permission, an
  unreadable sandbox seed, and an unusable run store, with one message and
  exit status 2. A refused command-line argument is never echoed. Restart
  recovery runs once before the server accepts a run, and the token is
  printed once to standard output only. `relaymind.example.toml` is the
  template; the README, `docs/TESTING.md`, and `docs/ROADMAP.md` use the
  command. Tests use model doubles (`tests/test_launcher.py`); no live run
  is recorded.
- Specification 0.20 → 0.21: section 14.1 defines the operator launcher
  (#87; RM12, RM17, RM18, RM19, RM21, RM23): the `relaymind serve --config`
  command, the TOML tables, credentials from the environment only, the
  all-or-nothing startup order, restart recovery once before serving, and
  the explicit startup errors, including the value-range, permission, and
  loopback local model checks.
- Demo trace UI (#28; RM23, RM25, RM11, RM21; spec 0.20 sections 8 and 9):
  the gateway serves a static page at `GET /ui` (no token, no run data,
  a content security policy) that shows a run's original prompt, mode,
  models, skill, route, tool proposals and operations, failed
  operations, escalation reason, clarification, confirmation, final
  answer, and timing. `GET /ui/runs/{id}` renders a trace from the
  stored events only, with every value escaped. The operator declares
  the data mode (live, replay, or double); it has no default and is
  shown in every view. Latency is the first model action, never the
  completed answer, and the run's active and waiting time are separate
  fields. Confirmation, denial, clarification, and cancel go through the
  native resume and cancel endpoints. `POST /ui/comparisons` runs the
  same input in auto and then external_only, resetting the sandbox before
  each run and cancelling any run that waits for input; it never resets
  the sandbox while a run is active, and an arm still active after its
  cancel is reported and ends the comparison. The run.accepted
  event now carries `original_messages` (the caller's own text, redacted
  by field name only, kept with the run's seven-day retention). Tests use
  model doubles (`tests/test_demo_ui*.py`); the page script is not run by
  an automated browser test and no live run is recorded.
- Specification 0.19 → 0.20: section 8 adds `original_messages` to the
  run.accepted payload, and section 9 defines the demo UI (#28): the
  static shell, the declared data mode and its label, the trace fields,
  the model action latency rule, the clarification and confirmation
  controls, and the comparison on reset fixtures (RM23, RM25).
- Cancellation during generation and in-flight writes (#29; RM10, RM14,
  RM17, AC12; spec 0.19 section 7.4): a cancel of a running run now asks
  the adapter of the generation in flight, local or external, to cancel
  that generation ID while the generation is still running, before the
  run ends as cancelled. The run never waits for that request: it is
  bounded, and its failure is never the run's, also after a time limit
  stops a generation. A late result of a provider that ignores the cancel
  is dropped: it proposes, runs, and publishes nothing. The timing
  collector (RM25) no longer keeps a cancel that arrives after its
  generation ended. Deterministic tests (`tests/test_ac12_cancellation.py`)
  cover a cancel during local and external generation (including the real
  llama-server adapter over a mock transport), a provider that does not
  stop, a provider cancel that hangs or fails, a cancel during a write
  (recorded as `unknown_outcome`, never replayed, not rewritten by the
  late handler result), during a read, and after a write succeeded
  (outcome kept), and the native cancel endpoint. With restart recovery
  (#8) this closes the deterministic part of AC12; no live model is run.
- Specification 0.18 → 0.19: section 7.4 defines cancellation during
  generation and writes (RM10, RM14, RM17, AC12, #29): the order of
  stopping dispatch, the best-effort adapter cancel, the dropped late
  result, how an in-flight tool operation is recorded, and that the
  cancelled run retains every outcome and is never replayed.
- Idempotency keys (#26; RM17, RM21, AC11; spec 0.18 section 7.4):
  `POST /relay/v1/runs` and `POST /v1/chat/completions` accept an
  `Idempotency-Key` (1 to 255 printable ASCII characters, sent once; else
  400 `invalid_request`) instead of refusing it. The key is stored with
  the run it creates, in the same store transaction (new
  `idempotency_keys` table, schema version 3), scoped to the caller and
  bound to a SHA-256 of the canonical request (endpoint, decoded body,
  and for chat the execution mode header). A duplicate never reaches the
  orchestrator, so it makes no model call and runs no tool. A native
  duplicate returns 202 with the existing run reference and the header
  `Idempotent-Replayed: true`, even while the run is active or the
  execution slot is taken. A chat duplicate of a completed run returns
  the stored response (answer or client-owned tool calls), as the stored
  `answer.delta` chunks when streaming; a duplicate of an unfinished run
  is 409 `idempotency_conflict` with its run ID, and one of a failed or
  cancelled run is 409 `run_terminal`. A changed payload is 409
  `idempotency_conflict`; a request that loses a race for the key after
  the lookup is answered the same way. A duplicate first ends an expired
  wait, so it never answers from a run waiting past its limit. A request
  refused before a run exists stores no key, and a request without a key
  is not hashed. Keys leave with their run at the seven-day retention
  limit or on deletion; a key whose run is past that limit is unknown
  even before a purge. Client-owned `function.arguments` are sent with
  sorted keys, as the store keeps them, so a replay sends the same text.
  `tests/test_idempotency.py` covers it with model doubles;
  the old tests that expected the header to be refused are removed.
- Native run API (#25; RM21, AC11; spec 0.17 section 7.4):
  `POST /relay/v1/runs` accepts messages and an optional
  `execution_mode` and answers 202 with `run_id`, `state`,
  `state_version`, and `events_url`; the run continues in a background
  task (`LocalOrchestrator.start`), so it does not depend on the request
  that created it. `GET /relay/v1/runs/{id}` shows state, version,
  output, error and reason codes, the pending question or approval,
  metrics, and the requested and effective modes; the confirmation token
  is shown once, to the first reader, and is never stored or traced.
  `GET /relay/v1/runs/{id}/events` is SSE with the stored `seq` as the
  event ID: a reconnect with `Last-Event-ID` replays later retained
  events from the store with no model call, tool run, or write, and a
  live stream follows the run across waits until its terminal event.
  `POST .../resume` (input, or approval ID, token, and `approve`) and
  `POST .../cancel` take `expected_version`; a stale version is 409,
  a body that does not match the waiting state is 400
  `invalid_resume_input`, and a bad token is 403 `approval_invalid`.
  Cancel ends a waiting run at once (reason `cancel_requested`, pending
  approval voided in the same transaction) and asks a running run to
  stop; a run owned by a chat request is 409 `invalid_state`. Every
  `run.cancelled` names its reason: `user_denied` for a denied
  confirmation, otherwise `cancel_requested`, also when the run task is
  cancelled with no recorded reason (a chat disconnect or the event
  loop); the published schema and the write-time validator require it
  (RM22, spec 0.17 section 8). `/healthz`
  and `/readyz` report liveness and, for each permitted mode, whether
  its models are available and warm. The execution modes share the
  chat endpoint's operator policy (`ChatSettings.allowed_modes`).
  `parallel_demo` now fails explicitly with `workflow_unavailable`
  (reason `parallel_workflow`, 503 on chat) when no workflow is enabled,
  no external model is configured or its health check says it is not
  available (checked before routing), or the run routes to the workflow,
  which is not implemented until #30; auto and `local_only` keep their
  rules. `Idempotency-Key` was refused until #26 (now implemented, see
  above), and cancel during an in-flight write is #29. `tests/test_native_run_api.py` covers it
  with model doubles, including one loopback-socket test of a stream
  that stays open across a wait; no live model is run.
- Client-owned tools alias (#24; RM08, RM19, AC10; spec 0.16 section 7.2):
  `relaymind-client-tools` now has a backend (`ClientToolsChat`,
  `relaymind.orchestrator.client_loop`). Each request is its own run with
  no routing generation: the gateway records its own decision (local, or
  external in `external_only`). The model is offered only the functions
  the caller supplied, narrowed by `tool_choice` (`none` offers none, a
  named function offers only that one), and the gateway installs and
  executes no tool: the trace has `tool.proposed` for the exposed call and
  never `tool.started` or `tool.finished`. A generation is admissible as
  non-blank text, or as a call to an offered function whose arguments pass
  the supplied schema (the strict RM09 check); `required` and a named
  function need a call and `none` needs text. Invalid output gets one
  repair generation, then an escalation. One call is exposed per response,
  and a call ID is kept unless it was used earlier in the conversation.
  The reply is buffered, so external fallback happens only before anything
  is exposed, `local_only` returns 422 `needs_external`, and `external_only`
  skips the local model. A function whose `parameters` is not a valid JSON
  Schema, or has a `$ref` that does not resolve inside it, is
  `invalid_request`; the gateway never retrieves a remote schema.
  Invalid output with no room left for the repair stops as
  `invalid_output`, the repair exchange is measured against the context
  limit before the next generation, `max_completion_tokens` bounds every
  generation of the run, and an external stage that stops on invalid
  output records `invalid_output`. `RunOutcome.tool_calls` carries the
  calls.
  `tests/test_client_owned_tools.py` covers the round trip (in process and
  with the OpenAI SDK over a loopback socket), the no-execution rule, the
  modes, and the ownership-mixing rejections on both aliases. This backend
  buffers its generation, so it does not return a `LiveAnswer`; no
  production backend forwards a live provider stream yet (spec section
  7.3 now says so). No live model is run.
- Streaming contract (#23; RM20, AC09; spec 0.15 section 7.3): the
  stream of the managed alias is the replay of the run's `answer.delta`
  events, so content is append-only and nothing but framing is added. A
  backend can return a `LiveAnswer` (`relaymind.api.chat`) that forwards
  provider text as it arrives. The role chunk waits for the first answer
  text, so a provider failure before it, or in a request without stream
  (which collects the whole live answer), is a normal 503
  `external_failed` response. If the provider fails after the first
  chunk, the stream asks the backend to record `run.failed`, sends one
  `error` event with fixed text (`external_failed`, the run ID, no
  provider text), and closes the connection with no finish chunk and no
  `[DONE]`; no replacement answer is sent, and the server does not log
  that expected abort as a crash. A client disconnect closes the
  provider stream, which requests cancellation. A result cannot carry
  both client tool calls and a live answer. `tests/test_streaming_contract.py`
  covers the framing of both aliases and both terminal paths (`stop`,
  `tool_calls`), append-only content, the final generation with tools
  disabled, the mid-stream failure in process and over a loopback socket
  with the OpenAI SDK, and an unmet evidence predicate that cannot
  finalize. The production client-owned backend that returns a
  `LiveAnswer` and records `run.failed` is issue #24.
- OpenAI-compatible endpoints (#22; RM19, AC09, AC16 request part; spec
  0.14 section 7.1): `GET /v1/models` and `POST /v1/chat/completions` on a
  FastAPI app (`relaymind.api.app`) over the orchestrator. The request
  parser (`relaymind.api.chat_request`) accepts the section 7.1 subset
  and rejects every other behavior-changing field with a named
  `unsupported_parameter` error, validates message ordering and tool-call
  references, and keeps tool ownership apart: the `relaymind` alias
  refuses client tools, and `parallel_tool_calls` true is refused. The
  response carries a stable `chatcmpl-<run id>` ID, the virtual alias, and
  `X-RelayMind-Run-ID`; the stream is framing plus the run's
  `answer.delta` texts, then `[DONE]`. The optional
  `X-RelayMind-Execution-Mode` header is checked against the alias and an
  operator policy (`ChatSettings`): 400 `invalid_execution_mode`, 403
  `execution_mode_forbidden`, never downgraded. `max_completion_tokens`,
  `temperature`, and `top_p` apply to the answer generations only
  (`LocalOrchestrator.run(final_options=...)`); an external generation
  offered tools takes the sampling but keeps its own token budget, so the
  budget never cuts off a tool call. `max_completion_tokens` is bounded by
  the operator limit `ChatSettings.max_completion_tokens` (default
  32,768). A run that fails as run state (for example `missing_input`) is
  409 `run_terminal`, not a 500. The gateway binds only to loopback
  (`relaymind.api.server`; `[::1]` is bound as `::1`) and generates its
  own application token at startup (`app.state.application_token`); no
  caller supplies one. An unexpected failure is logged with its type and
  frames, never its text. `fastapi` and
  `uvicorn` join the runtime dependencies and `openai` the dev ones.
  The `relaymind-client-tools` alias is served only when a client-owned
  backend is configured (#24), and `Idempotency-Key` was refused until #26.
  `tests/test_chat_request.py`, `tests/test_chat_endpoint.py`,
  `tests/test_final_options.py`, `tests/test_api_binding.py`, and
  `tests/test_openai_sdk.py` (the official SDK, in process and over a
  loopback socket) cover it. The requested answer options are not yet
  recorded in `run.accepted`, which needs a trace schema change.
- AC08 model substitution test for the orchestrator and skill packages
  (#49; RM18, AC08, spec section 12.3): one managed scenario (local
  skill and tools, local budget stop, handoff) runs through the real
  orchestrator and adapters under two configurations, a second local
  endpoint and model and a second external endpoint and model, with one
  skill root and no code change. The traces name their own models and
  are equal in routing, tool proposals, results, and handoff content.
  `build_gateway` in the test helpers accepts configured models over
  real adapters. No live variant: only one local artifact has a
  manifest, which `docs/TESTING.md` records, so AC08 stays open until a
  second local artifact is qualified.
- Failure behavior and error codes (#21; RM04, AC07, AC16 error part; spec
  0.13 section 11): `relaymind.api.errors` holds the error contract, an
  `ApiError` with a stable code, message, and run reference, one status
  per code (400, 401, 403, 404, 409, 422, 429, 500, 503, 504), and the
  mapping of orchestrator exceptions (`error_from_exception`) and failed
  runs (`error_from_outcome`: `external_failed` is 503, or 504 when a
  time limit stopped it; `context_limit_exceeded` is 400). An exception
  raised by a run's own work is 500 `internal_error` whatever its type,
  as its `run.failed` event says. Internal errors carry a fixed message
  and no credentials.
  `relaymind.api.auth.require_bearer_token` gives 401 `unauthenticated`.
  `RunOutcome.budget` names the limit that stopped a failed run, as the
  `budget` field of its `run.failed` event does.
  Degraded mode is an operator policy (`degraded_mode`, off by default):
  an unavailable local model hands off to the external model, with the
  evidence so far, when external dispatch is permitted. The trace records
  it as `route.selected` with mechanism `degraded_mode` (when the stage
  had no routing decision, also after a clarification) and
  `degraded_mode` and `local_failure` (scrubbed of known secrets) in
  `handoff.started`.
  `tests/test_error_contract.py`, `tests/test_ac07_failure_behavior.py`,
  and `tests/test_degraded_mode.py` cover every status, the bounded
  failure after an exhausted local budget, and degraded mode.
- Tool-output injection defenses and secret redaction (#20; RM08, RM12,
  AC06; spec 0.12 section 8): `Redactor.redact_with_repeats` also replaces
  a value found under a secret field (a string or a number) wherever the
  same whole token is repeated, in one pass. The executor scrubs those
  values, and the ones the run already knows, from the result data and
  error message before the first write; the envelope fields are left
  whole. Model-proposed tool arguments are scrubbed of the run's known
  secrets in `tool.proposed`, `model.finished`, approvals, and tool
  operations. A failure while recording a finished handler still
  finishes the pending operation.
  `tests/test_ac06_tool_output_injection.py` plays an injected log entry
  with obedient model doubles: the unallowed `delete_demo_database` call
  is rejected, caller permissions, the skill allowlist, and the offered
  tool schemas stay unchanged, the injection reaches the model only as a
  tool message, and the secret is absent from events, tool operations,
  model requests, and the SQLite store read back from disk, also when
  the model copies it into a tool argument.
- Restart recovery (#8; RM10, RM17, AC12 restart part; spec 0.11
  sections 5 and 8): `LocalOrchestrator.recover_after_restart` (and
  `relaymind.orchestrator.recovery.recover_interrupted_runs`) fails
  every unfinished run with `interrupted` and a `run.failed` event that
  names the state it was in, rebuilds `model_calls` from the trace, and
  releases the execution slot. A pending write is finished as
  `unknown_outcome` and a pending read as `failed` (error code
  `tool_interrupted`, a `tool.finished` event marked `recovered`);
  no handler runs, nothing is retried, and a later proposal of the
  same call replays the recorded outcome. Pending approvals expire at
  recovery time (a void approval is not counted). Each run is read again
  and recovered in one store transaction, one failing run does not stop
  the others (a `RecoveryError` carries the report of the recovered
  ones), terminal runs are untouched, and a second recovery changes
  nothing. Recovery uses the orchestrator's executor and clock, and is
  refused once the orchestrator has accepted a run. No model is called
  and no model loop is resumed. The startup call is part of the API work
  (#25); the cancellation half of AC12 stays in #29. Deterministic
  coverage is `tests/test_restart_recovery.py`.
- Confirmation flow for mutating demo actions (#19; RM11, RM10, RM17,
  AC05; spec 0.10 sections 4.3, 5, 7.4, 8): an admitted
  `delete_demo_database` proposal no longer stops the run. The run
  suspends in `waiting_confirmation` (from `local_running` or
  `external_running`), records `confirmation.required`, and returns a
  random single-use token to the caller in `RunOutcome.confirmation`;
  the store keeps only its hash and the token is never traced.
  `LocalOrchestrator.resume_confirmation` takes the approval ID, the
  token, and `approve`. The approval binds the run, tool, canonical
  arguments, target (`database_id`, declared by the tool as
  `confirmation_target`), and run state version; the arguments carry the
  fixture revision, which the handler still compares atomically. A
  matching approval opens a new stage like a clarification resume and
  executes the held call once, and its `tool.started` event names the
  approval. A denial cancels the run with `user_denied`; an approval
  older than five minutes fails it with `approval_expired` (also swept
  by `expire_waiting`); a wrong token or approval ID is 403
  `approval_invalid`, a malformed body or a text resume is 400
  `invalid_resume_input`, and a busy execution slot is 429 with the
  approval kept. A call that already has a recorded operation is a
  replay and asks for nothing. A mutating tool without a confirmation
  target is rejected back to the model and never runs; the target must
  be a required input field. The approval ID and token are checked
  before expiry and the slot, the approval is consumed in one store
  transaction with the resume or cancel (`RunStore.transaction`), a
  secret target is redacted, and secret argument values are bound by an
  in-memory digest. An approval whose run moved on is no longer
  pending. A textual yes in
  chat is not an approval. Deterministic coverage is
  `tests/test_ac05_confirmation.py`; no live model was run.

- Handoff envelope and escalation to the external model (#18; RM13,
  RM14, AC02; spec 0.9 sections 4.4, 8, 11): the HandoffEnvelope is
  published as `relaymind/orchestrator/schemas/handoff_envelope.v1.json`
  and validated at the boundary (`relaymind.orchestrator.handoff`). It is
  built from the run store's tool operations and keeps the original
  messages, the skill version, completed operation IDs with their
  structured results, pending work, permitted tools, remaining budgets,
  and language; rejected local proposals and draft answers travel only
  as `local_proposals`, each labeled a proposal. An escalation with
  external dispatch permitted records `handoff.started`, moves the run
  to `external_running`, and revokes the local actor's dispatch
  authority; the external model proposes tools through the same
  validation, identifier provenance check, and executor, and a repeated
  completed operation gets its recorded result and runs no handler. One
  actor owns the final answer. The whole external request is measured
  against the external context limit (the adapter's tokenizer, or the
  UTF-8 byte length as an upper bound) before dispatch and before every
  external generation; over the limit the run fails with
  `context_limit_exceeded` and no external call. An operation with no
  known outcome blocks handoff, and so does a stop the external stage
  could not resolve either (a mutation that needs confirmation, or a
  skill with no permitted tool): those end as `needs_external` with
  `capability_missing` and no external call. An admitted parallel route
  (the workflow is not wired yet) and an unavailable local model (no
  operator policy permits degraded mode yet) do not hand off either. An
  `external_failed` run keeps an RM04 reason code. A timed-out generation is
  traced with the limit that bounded it, never as local time. New budgets `external_generations` (6)
  and `external_seconds` (30) are recorded in each run. External failures
  end as `external_failed`; without a configured external model, in
  `local_only`, or with no run budget left, the run still ends as
  `needs_external`. `handoff.started` now requires `reason_code`,
  `model`, and `operation_ids`. Deterministic coverage is
  `tests/test_ac02_handoff.py`; no live external model was run, and
  external skill selection when no skill was chosen (`external_only`)
  hands over the catalog and tools but does not load a skill body.
- Live AC01 record on the pinned llama.cpp b11785 runtime in
  `docs/EVIDENCE.md` (raw export
  `docs/evidence/2026-10-21-m2-local-trace-b11785-apple-m1.json`; RM01,
  RM16, RM18, RM22): 3 of 4 check-service-health runs completed locally
  with zero external calls, both English prompts among them; the
  Chinese staging-api answer again translated the status word.
  `docs/ROADMAP.md` records M1 (#58, #63) and M2 (#27, #67, #69) as
  closed, with the items carried into M3 to M5.
- Native x86-64 Windows M1 qualification record in `docs/EVIDENCE.md`
  (#46; RM18, spec sections 3, 6, 14): three live smoke tests passed
  with the pinned model and runtime commit on the contributor's Windows
  machine. Records the CPU build, app-local CRT setup, proxy workaround,
  and PowerShell quoting; `docs/TESTING.md` links the reproduction steps.
  M1 is now recorded as complete on both target platforms.
- Model adapter contract (RM18): capabilities, generate, cancel, and
  health with normalized messages and events (`relaymind.adapters`).
- llama.cpp adapter for Qwen3-0.6B over a llama-server endpoint, with
  non-thinking mode configured through chat-template settings and
  exercised by an opt-in live smoke test (`pytest -m live`, spec
  section 6).
- External Chat Completions adapter for an operator-configured
  OpenAI-compatible endpoint, sharing the stream and request handling
  with the llama.cpp adapter (`relaymind.adapters.chat_completions`,
  RM18).
- Model artifact manifest machinery: GGUF metadata hashing,
  `load_manifest`/`verify_artifact`, the manifest generator script, and
  the hash-pinned Qwen3-0.6B Q8_0 manifest (`models/`). The model is a
  candidate; it becomes the default only after the bilingual tool tests
  (spec section 6).
- Launch settings are manifest data (RM18, spec section 6): the live
  smoke test verifies the artifact against its manifest and derives the
  llama-server arguments and adapter config from `launch_settings`
  (`launch_settings_argv`, `launch_adapter_settings`). Manifests with
  unusable settings, or settings that would load other weights or
  replace the pinned chat template, are rejected when built or loaded.
- Timing and statistics export for adapter comparisons (RM25, spec
  section 12.1): `relaymind.stats` records per-generation metrics around
  any adapter, and `scripts/compare_adapters.py` runs the same prompts
  against several configurations and writes one JSON export each. This
  is engineering instrumentation, not benchmark evidence (spec 12.3).
- AC08 model substitution test at the adapter layer (RM18): the same
  caller code drives a swapped local model, a swapped external endpoint,
  and the local/external boundary with the same normalized outcome. The
  orchestrator and skill half of AC08 remains open.
- Manual testing guide for the M1 deliverable on macOS and Windows
  (`docs/TESTING.md`).
- Live evidence record (`docs/EVIDENCE.md`, raw exports in
  `docs/evidence/`): the M1 live smoke run and a timing probe on Apple
  Silicon macOS, with machine, runtime build, manifest, exact command,
  and result. Single-pass observations, not benchmark results (spec
  section 12.3).
- Reproducible timing probe (`scripts/probe_timing.py` with probe
  definitions in `scripts/probes/`): launches llama-server from the
  manifest, records cold start, warm-ups of both prompt kinds, and one
  measured pass, and writes an export whose `run` block names the
  command, prompts, system prompt, tool schema, and each generation's
  outcome (RM16, RM25). `export_stats` accepts that block as `run_info`.
- RouteDecision schema and boundary validator (spec section 8, RM02;
  part of #14): the versioned JSON Schema is published as
  `relaymind/orchestrator/schemas/route_decision.v1.json`, and
  `parse_route_decision` / `parse_route_decision_json` reject unknown
  or missing fields, invalid values, skills outside the advertised
  catalog, prose around the JSON, duplicate keys, non-finite numbers,
  and anything else the decoder refuses. The spec 0.6 cross-field rules
  (#60) are enforced: a clarify route carries a non-blank clarification
  and every other route none; with a `RoutingContext` (managed or
  client-owned catalog, effective execution mode, operator-enabled
  parallel workflows) managed local needs a skill, parallel needs an
  enabled workflow and an admitting mode, and the client-owned alias
  has no skills and no parallel. The reason code `execution_mode` is
  added. The schema mirrors the clarification rule with if/then and
  requires non-blank text, so it rejects what the validator rejects.
- Local tokenizer on the llama.cpp adapter (#47, spec section 4.1):
  `LlamaCppAdapter.count_tokens` counts a normalized message list and
  its tools through llama-server `/apply-template` and `/tokenize`, and
  `capabilities()` now reports `tokenizer_available=True`. A runtime
  failure raises `TokenCountError` instead of estimating. The external
  adapter still reports no tokenizer and refuses to count. A live test
  checks the count against the runtime's prompt tokens in English and
  Chinese.
- Routing probe spike (#48): `scripts/probes/probe_routing.py` with
  `scripts/probes/m2_routing.json`, its live result in
  `docs/EVIDENCE.md`, and the routing mechanism decision for #14
  (native function calling) in `docs/ROADMAP.md`. The probe applies
  the 2,048-token router cap (an over-cap row is bypassed and marked),
  starts its clock before the token count, sends and records
  llama-server's `cache_prompt` setting, and records adapter errors in
  their own field. The recorded run was re-scored under spec 0.6 and
  corrected in `docs/EVIDENCE.md` (grammar 7 of 12 valid, not 12 of
  12); its limits (tokenizer time excluded, prompt cache on and
  unmeasured) are stated there.
- Run state machine (#6, spec section 5, RM17):
  `relaymind.orchestrator.run_state` with the ten section 5 states and
  their allowed transitions. Each transition is a compare-and-set on the
  run state version, so stale transitions, resumes, and cancels get 409
  and leave the run unchanged; a stale version is reported before a
  terminal state. A resume starts the resuming store's run clock
  (RM25). Terminal runs never change or restart; a
  retry is a new run linked to the previous one. One execution slot
  (section 11) is held in the active states, released by waiting and
  terminal states, and reacquired on resume or refused with 429. The
  slot is a flag on the run row, set in the same SQLite transaction as
  the state change, so every process on one store file shares it. A
  transition into completed, failed, or cancelled carries its
  run.completed, run.failed, or run.cancelled event (RM22), and a
  failed run's event names the run's error code. A waiting run
  continues only through resume, to the state that suspended it, or
  fails or is cancelled; deleting or purging a run frees a slot it held.
  `create(accepted=...)` writes run.accepted with the run in one
  transaction, so a run can fail or be cancelled straight from
  accepted. `cancel()` without an event records a gateway run.cancelled
  with the reason, if any.
- SQLite run store (#7, spec section 5): `relaymind.store.RunStore`
  with runs, events, tool_operations, approvals, and artifact_refs
  under versioned migrations, split into statements with
  `sqlite3.complete_statement`. Event sequence numbers increase per
  run. A tool operation is recorded as pending with its canonical
  arguments and their SHA-256 before its side effect, then finished
  once as succeeded, failed, or unknown_outcome; a CHECK constraint
  admits only those statuses (RM10). Approval tokens are kept only as
  SHA-256 hashes; an approval is issued for the run's current state
  version, expires after the configured TTL (five minutes, RM11), and
  is consumed once in one atomic update that matches its run, tool,
  canonical arguments, target, and the run's current version. A run's
  pending approval can be read without its token hash (RM21). Store
  times must be timezone-aware UTC. A failed commit rolls back, and a
  failed rollback reopens the connection. Runs older than the seven-day
  default retention are purged or deleted explicitly with everything
  they own. `tool_operations(run_id)` and `artifact_refs(run_id)` list a
  run's operations and artifact references in record order, for the
  section 8 HandoffEnvelope (RM13).
- TraceEvent schema (spec section 8, RM22; part of #27): published as
  `relaymind/store/schemas/trace_event.v1.json` and validated before
  every write; tests check that the validator and the schema (through
  jsonschema) accept and reject the same events. It covers the RM22
  event types; tool events must name their operation or proposal ID and
  originating model, model events their call and model, and
  run.completed the final-answer model (RM16), and skill.loaded its
  name, version, and content hash (RM06). clarification.answered (spec
  0.6 section 8) needs a non-blank redacted `input`, a non-blank
  `missing`, and a non-negative integer `waited_ms`. Text fields must contain
  a non-space character, and an ID that is present must be non-blank.
  run.accepted is the first event (seq 1) and carries the section 8
  payload: requested and effective execution mode, model_alias
  (`relaymind`, `relaymind-client-tools`, or null for a native run;
  spec 0.6 section 8), budgets, skill_roots, and skills_offered (any
  sequence, stored as lists).
  The trace event API is exported from `relaymind.store`. Timestamps are UTC and elapsed_ms
  comes from a monotonic clock (RM25); a store joining a run continues
  from its last persisted elapsed_ms, so elapsed_ms never goes back.
- Tool registry (#10, RM08, RM09): `relaymind.tools.ToolRegistry`.
  Registration requires a name, description, input and output JSON
  Schemas, a timeout of at most two seconds (section 11), a side-effect
  class, a handler, and an authorization rule; input objects must use
  additionalProperties=false, and output schemas must be object
  schemas. The registered declaration holds deep, read-only copies of
  both schemas, so they cannot drift from the schema hash. Availability is the intersection of the
  skill allowlist, operator policy, and caller permissions, and a name
  that is not registered never gets a handler. `invoke` validates the
  name and the decoded argument object before the handler runs and
  validates the returned data; a handler error or bad output is a
  failed ToolResult, except that a mutating handler that raises an
  unexpected error or returns bad output gives unknown_outcome, since
  its side effect may have happened (RM10). Integers are strict: a float such as 60.0 or a
  boolean where a schema declares an integer, or an enum or const of
  integers, is rejected as invalid_arguments. Rejection messages name
  the JSON path and schema keyword, never the rejected value (RM12), and
  a tool name that is not a string is unknown_tool. The schema hash
  covers the name, description, both schemas, timeout, side-effect
  class, and required permission, in the canonical JSON form
  (`relaymind.canonical`) that the run store also uses for approval
  binding; that form refuses NaN and infinities. `invoke` requires a
  non-blank operation ID and never reports a negative duration. The
  ToolResult envelope (section 8) is published as
  `relaymind/tools/schemas/tool_result.v1.json`. Operation IDs, replay,
  and timeout enforcement stay with the executor (#11).
- Sandbox demo data service and the seven demo tools (#12, RM23,
  section 9): `relaymind.tools.sandbox` with schemas published under
  `relaymind/tools/schemas/demo/`. The seed fixture is
  `tests/fixtures/sandbox/baseline.json`, checked at load by running
  the real tool methods on it; the service works on a disposable copy in
  its work directory, which a new service keeps and only an explicit
  reset replaces; a reset reads and checks the seed again, and writes go
  through a unique temp file. Every output carries the scenario of the
  working copy and the observation time, an `unavailable` health status
  is a successful observation, an unreachable tool is a failed
  ToolResult, and recent logs come newest first. The fixture has one
  revision that every listed database reports and every delete and
  reset bumps; `delete_demo_database` compares `expected_revision` with
  it under a cross-process file lock (fcntl on POSIX, msvcrt on
  Windows) before it looks up the ID, returns `revision_conflict`
  without mutation, and returns
  the revision it stored. Each identifier input (`service`,
  `database_id`) carries an English `title` and an
  `x-relaymind-label` with `en` and `zh` text, so a clarification can
  name the missing input in the run's language.
- Skill registry (#9, RM06, RM07, spec 0.4 section 4.2):
  `relaymind.skills.SkillRegistry` loads SKILL.md packages with a
  `skill.manifest.json` manifest (schema
  `relaymind/skills/schemas/skill_manifest.v1.json`) from
  operator-configured local roots, `skills/` by default (a configured
  list replaces it), one level deep and in order. Relative roots, the
  default among them, resolve against the working directory, the
  checkout root when the gateway starts there. Every subdirectory of a
  root is a candidate package. Missing or malformed package files,
  duplicate names across roots, remote roots, unregistered tools,
  predicates, or workflows, package files or directories that resolve
  outside their package or root, and resource paths that are not
  normalized or name one file twice are startup errors. The content hash covers the body, manifest, declared
  resources, and the allowed tools' schema hashes. Loads emit
  `skill.loaded` with name, version, and hash; a pinned version or hash
  that is not held fails without substitution; resource reads are
  limited to declared package files. `offered_snapshot` gives the
  `skill_roots` and `skills_offered` fields of run.accepted;
  `skill_roots` are the configured root strings as given, not resolved
  host paths.
- Completion predicates (`relaymind.skills.predicates`) as registered
  gateway validators: the health check reads the status stated for the
  returned service, the checklist needs its draft label, each incident
  count must sit next to its own label, diagnostic section labels count
  only where they start a line, the deployment report must name the
  returned service, and with no recorded deployment any deployment-like
  ID (dep, deploy, or deployment followed by a separator and a
  digit-bearing token, or by digits) fails. Service and database IDs, lowercase by
  schema, match in any case at ID boundaries ("Staging-api is
  healthy"); a quoted deployment ID must stay exact. The fixed parallel workflow ID
  (`relaymind.orchestrator.workflows`, RM15) is the known workflow;
  operator enablement is checked at routing (spec 0.6 section 4.4).
- Six demo skill packages under `skills/` (part of #13, section 9) with
  paired English and Chinese examples that are checked against the
  sandbox outputs. investigate-performance and
  health-and-diagnostic-checklist are not local-eligible, because their
  final answer is external (section 9). The Chinese examples are not
  natively reviewed (best-effort since spec 0.7).
- Runtime dependencies `jsonschema` and `pyyaml` for schema validation
  and SKILL.md frontmatter.
- Tool executor (#11, RM10, RM14; spec section 11):
  `relaymind.tools.executor.ToolExecutor` is the only owner of
  operation IDs and replay. `validate` checks a call once (RM09) and
  returns a `ValidatedCall`; `execute` runs only such a call. The
  logical operation ID comes from the run, the tool name, the canonical
  arguments, and the attempt, and is stored as `pending` before the
  handler runs. Replaying a finished operation returns the recorded
  result without running it; a pending one is in flight
  (`OperationInFlightError`), never a replay. Handlers run on a daemon
  thread, so the event loop is not blocked, bounded by the tool timeout
  or the caller's remaining active budget, whichever is shorter. A
  timed-out or interrupted mutating tool is `unknown_outcome` and is
  never retried automatically; a read-only one is `failed`. Every
  pending record is finished, and a worker error is recorded as
  `tool_error`, not as a timeout. A read-only call gets one retry after
  a transient failure (`tool_unavailable` or `tool_timeout`), as a new
  operation. Arguments and results are redacted of configured secret
  fields before they are stored (RM12). `ToolRegistry.validate`,
  `ToolRegistry.run`, `ToolRegistry.tool_schemas`, and
  `ToolResult.from_json_object` are added; `invoke` is validate plus
  run.
- Secret-field redaction (RM12, section 8): `relaymind.redaction.Redactor`
  replaces the values of operator-configured secret fields (a default
  set of credential key names) at any depth, and `redact_text` scrubs
  those values from model text, before anything is persisted.
- Router (#14, RM01, RM02, RM05; spec 0.6 sections 4.1, 4.2, 4.4, 5, 7.4,
  8): `relaymind.orchestrator.router` uses native function calling, with
  one function per advertised skill (at most eight) plus
  `request_clarification`. The gateway builds the RouteDecision and
  validates it with a `RoutingContext`. Route and reason code come from
  the skill registry and the run: a skill with a parallel-workflow ID
  routes parallel only when operator configuration enables the workflow
  and the effective mode admits it (`parallel_demo`, or `auto` with
  `parallel_in_auto`); otherwise it routes external with
  `parallel_workflow`. `external_only` gets the gateway's own decision
  (external, skill null, `execution_mode`) without a routing generation.
  The language comes from the latest user turn with words in it, so an
  identifier-only reply, or a single bare word such as `auth`, keeps it. A selected skill whose tools all need
  an enumerated identifier gets a clarify decision with `missing_input`,
  naming the skill and the missing argument, when no user turn names one;
  the question's label is the schema's `x-relaymind-label` in the run's
  language (`请问是哪个服务？`), else its title, taken from any allowed
  tool's schema. Router input over the
  2,048 local-token cap takes the bypass decision (`context_limit`)
  without a routing generation. Invalid routing output never reaches a
  tool. RM05 provenance (user turns and tool results only) lives in
  `relaymind.orchestrator.identifiers`, shared with the loop; a value
  joined to a longer token by `.`, `:`, or `/` is another resource.
- Bounded local generation loop (#15, RM03, RM04, RM16; spec sections
  7.3, 8, 11): `relaymind.orchestrator.local_loop.LocalOrchestrator` with
  `LocalBudgets` and `BudgetMeter` (`relaymind.orchestrator.budgets`).
  Every budget is a finite, configurable number. `run.accepted` carries
  the section 8 payload: requested and effective execution mode, model
  alias, budgets, skill roots, and skills offered, and is written with
  the run in one transaction, so an invalid payload creates no run; the
  selected skill is loaded at exactly that snapshot (RM07). Every generation is charged by
  its purpose in one place, and only real tool executions are charged.
  Exceeding a budget, unrepaired output, a missing capability, an
  oversized input, a failed tool outcome (`tool_failure`), or a
  non-local route ends the run as failed with error code
  `needs_external` and an RM04 reason code, because external dispatch
  is not wired yet (#18). Any other exception fails the run with
  `internal_error` (a cancelled run is cancelled), which releases the
  execution slot. Tool proposals are checked for identifier-like
  arguments, enum or pattern, that no user turn or tool result named.
  Every call of a multi-call turn is recorded as its own `tool.proposed`
  event. A repair is offered only while both the repair and the local
  generation limits have room. A pending tool operation is finished on
  every exit, even before its handler runs, and free-text redaction
  replaces secret values by whole token only. A
  mutating tool is never executed by this loop: the confirmation flow
  (#19, RM11) is not wired yet, so such a proposal ends the run as
  `needs_external` with `capability_missing` and nothing is mutated. A
  finalize proposal must meet the skill's completion predicate. The
  final generation then runs with tools disabled, and its answer is
  checked again before the `answer.delta` events are appended in one
  batch. Every model call is labelled with its adapter's declared
  locality (`ModelAdapter.locality`: `local` for `LlamaCppAdapter`,
  `external` for `ChatCompletionsAdapter`), and the trace counts the
  external ones. One `TraceClock` serves the store, the tools, and the
  loop.
- AC01 with model doubles (`tests/test_ac01_local_health.py`) for both
  demo services in English and Chinese, with zero external calls
  measured on a recording external model double, and a live AC01 test
  (`tests/test_local_loop_live.py`, `pytest -m live`). The first
  recorded run is in `docs/EVIDENCE.md`: 3 of 4 completed locally. A
  rerun on the rebased stack, with the stricter #55 completion
  predicates, is recorded next to it: 2 of 4 completed, with the same
  model output. `ScriptedAdapter` (tests/helpers.py) now also scripts
  one turn per generation, and `of_type` is a shared test helper.
- Clarification and resume (#16, RM05, RM17, AC03; spec 0.6 sections 5,
  8, 11): a request that omits the target service waits in
  `waiting_input`. Its `clarification.required` event carries the
  question in the run's language (the schema's Chinese label for zh),
  the language, the reason code, `skill_id`, a non-blank `missing`
  (`skill_selection` for a model-issued clarification), and
  `waiting_seconds`. `LocalOrchestrator.resume` takes explicit input
  text. A stale version is 409 and a terminal run never restarts.
  Missing or blank text is 400 `invalid_resume_input`, and the run keeps
  waiting with its version unchanged, even after its wait expired: the
  body is checked before the waiting limit (sections 7.4, 11). A busy slot is 429, and the run keeps waiting. A resume
  records `clarification.answered` (redacted input, `missing`,
  `waited_ms`). It appends the question as an assistant turn and the
  input as a user turn, and opens a new local stage with the full
  per-stage budgets. When the clarify named a skill, the gateway checks
  the input without a routing generation (`Router.resume_decision`).
  When `skill_id` was null, the run routes again. The run keeps the
  language of its first RouteDecision. `LocalBudgets` gains the
  entire-run limits (`run_active_seconds` 60, `run_generations` 8,
  `run_tool_executions` 6), which bound all stages, as well as
  `waiting_seconds` (300) and `clarification_rounds` (2). A third needed
  clarification fails the run with `missing_input`, and an expired wait
  fails it with `input_expired`; its wait counts only toward the
  `waiting_ms` of `run.failed` (section 12.1). `expire_waiting` expires
  every other wait when one of them raises; the run that raised keeps its
  wait unchanged for the next sweep, and a run cancelled during its own
  expiry is dropped without an error (RM17). `run.completed` and `run.failed` report
  `active_ms` and `waiting_ms` separately (section 12.1). A time stop
  names the tighter budget (`active_seconds` or `run_active_seconds`).
  The `clarification.required` payload rules are added to the
  TraceEvent validator and schema: a non-blank question, language,
  reason code, and `missing`, a `skill_id` that is non-blank or null,
  and a positive, finite `waiting_seconds` (RM03). When `run.completed`
  or `run.failed` carry them, `model_calls` must be an array of objects
  and `external_calls`, `active_ms`, and `waiting_ms` non-negative
  integers (section 8) (`clarification.answered` is validated since the
  TraceEvent schema entry above).
- Invalid proposals (#17, RM02, RM04, RM08, RM09, AC04): the AC04 test
  (`tests/test_ac04_invalid_proposals.py`) covers an unknown tool, a bad
  enum value, an undeclared field, a missing field, a wrong type, and a
  tool outside the permitted set. None executes, exactly one repair is
  counted within the generation limit, and an unrepaired proposal stops
  with `invalid_output`. `Escalation` refuses a reason code outside the
  section 8 list. Inside a run, that refusal fails the run with
  `internal_error` and frees the slot.
- A third live AC01 run, on the rebuilt stack, is recorded in
  `docs/EVIDENCE.md`: 3 of 4 completed locally. The model's text
  changed with the labelled tool schemas, so the case-insensitive
  service match was not exercised. M2 is not closed: see
  `docs/ROADMAP.md`.
- Project scaffold: repository layout, CI, and contributor guides.
- Specification v0.1 (`docs/SPECIFICATION.md`) as the single source of
  truth for the prototype.
- Architecture and roadmap documents mapping spec components to code
  layout and milestones.

### Changed

- Specification 0.21 -> 0.22: section 8 adds `answer_options` to the
  run.accepted payload (#91; RM19, RM22, RM25): the effective token
  budget, temperature, and top_p applied to the answer generations. A run
  without options records an empty object, and a run stored before this
  revision has no field, which the trace and the demo UI report as not
  recorded. Section 9 adds the options to the run trace.
- Specification 0.22 -> 0.23: section 5 states that the approval and the
  execution slot are one store transaction when two gateway processes
  share a store file (#93; RM10, RM11, RM17). A second process that takes
  the slot after the first check makes the resume 429 and keeps the
  approval pending; there is no store lock. A deterministic test covers it.
- Specification 0.23 -> 0.24: section 7.4 states that a compatible chat
  disconnect cancels the run that request owns, with stream true or false,
  by the same path as the native cancel (#92; RM14, RM17, RM19, RM20,
  AC12). A native events disconnect still leaves the run untouched.
- Specification 0.24 -> 0.25: section 4.4 specifies the bounded parallel
  workflow (#30; RM14, RM15, RM16, RM22, RM23, AC14). The "not implemented"
  cause of workflow_unavailable is removed; the workflow runs a local_health
  branch and an external_checklist branch, joins them, and hands final
  ownership to a new synthesis call. Section 8 adds the branch field and the
  workflow summary of terminal events, section 9 the workflow part of the
  trace, section 12.1 the extra call latency, and section 7.4 the readiness
  of parallel_demo. The workflow checks room for both external generations
  before its branches start and again at the join, measures the draft
  request before parallel_running, and passes the draft to the synthesis
  as a JSON-encoded data message. abandoned_calls lists only parallel
  branch calls; restart recovery keeps the branch of a settled operation and
  writes no workflow object; the trace shows each stage's duration from the
  terminal workflow object.
- Specification 0.25 -> 0.26: section 10 specifies the caching policy
  (#31; RM07, RM24, RM22, AC13). The skill parse cache is keyed by file
  content hash. The routing-result cache is set by the new launcher table
  `cache` (section 14.1), off by default and in the benchmark
  configuration, and is limited to local read-only skill choices with an
  exact key, a five-minute TTL, and 256 entries. A hit skips only the
  routing generation: authorization and tools run fresh. Skill or
  permission changes drop dependent entries. Section 8 defines the
  cache.hit payload (originating_version and originating_run_id), the
  routing_cache label of run.accepted, and the routing_cache route
  mechanism; statistics exports name the cache setting (section 12.1).
  The key holds the tool calls of assistant turns and a routing prompt
  version derived from the routing prompt and functions; expired entries
  are removed as expired before each lookup and store; an unusable hit is
  counted as a miss; and every route.selected of a cache-enabled run,
  gateway-made ones included, carries the hit or miss label.
- Specification 0.26 -> 0.27: section 9 requires an automated browser test of
  the demo UI page script (#89; RM11, RM12, RM21, RM23, RM25, AC03, AC05,
  AC12). The test runs the page against the app over model doubles and
  checks the native endpoint calls, the data mode label on every view, single
  escaping, and the shown browser timestamp. The browser is a development
  dependency installed in CI on all three platforms. The approve and deny
  controls are rendered disabled and the page enables them only once it
  holds the approval token, so an early click cannot send a resume without
  it. A replay-mode view is also run in the browser.
- Specification 0.27 -> 0.28: section 12.2 defines the evaluation
  (#32, #33; RM25, sections 12.1 to 12.3 and 14). The correctness corpus
  is `evaluation/corpus.v1.json` with its sandbox seeds, frozen by a
  SHA-256 digest in `evaluation/corpus.v1.lock.json`; each case names
  its class, language, fixture seed, permissions, expected route,
  acceptable tool calls, clarification answer, confirmation decision,
  expected outcomes, required and forbidden evidence, expected sandbox
  state, and rubric, and no case repeats a development example. The
  corpus runner starts a new gateway for every case and mode, so the
  three modes start from the same fixture snapshot, and the section
  defines task success, local completion, tool correctness, schema
  validity, escalation, missed mandatory confirmation, and provider
  calls over the trace. The latency runner measures the local path over
  the compatible stream: first transport event and first answer token
  on the client's clock, first model action and first useful content
  from the trace, nearest-rank p50 and p95, timeouts and errors kept,
  a cold request apart, and the runtime's prefix cache and sampling
  settings recorded. `docs/RELEASE.md` is the release record of
  section 14.
- Specification 0.29 -> 0.30: the demo UI trace view (section 9;
  RM22, RM23) reads a tool result stored by reference back from its
  artifact and marks it as stored by reference, with its size. When
  the artifact is no longer retained, the operation shows the status
  of its tool.finished event, the reference, and that the result is
  not retained, with no result data. Before this, a run whose tool
  result was over the inline limit could not be shown (KeyError).
- Specification 0.28 -> 0.29: AC08 defines a second compatible external
  endpoint (#109; RM18, section 12.3): it has its own base URL, and the
  launch file names its own credential variable (it may hold the same
  key when one vendor serves both base URLs); another model behind the
  same base URL is not one.
  For the external switch only the external_model table of the launch
  file changes. The live acceptance harness takes
  `--second-external-base-url`, `--second-external-model`, and
  `--second-external-context-limit`, reads the second credential from
  `RELAYMIND_SECOND_EXTERNAL_API_KEY` (the launch file names only the
  variable; a second base URL needs its own context limit), and records
  the swap as `ac08_external_swap` only when the two base URLs differ
  (ignoring case, a default port, and a final slash) and the second
  names its own credential variable; any other swap is recorded as
  `external_model_swap` (RM18) and does not count for AC08. The #88
  same-URL swap in `docs/EVIDENCE.md` is marked as not AC08.
- Documentation brought up to date after M3 and M4: the README status,
  features, layout, and a verified script that starts the gateway by
  hand (there is no launcher yet); the M3 and M4 status and carried
  forward items in the roadmap; the open gaps in `docs/TESTING.md` and
  `docs/EVIDENCE.md`; the API and UI notes in `docs/ARCHITECTURE.md`
  and `AGENTS.md`; and small fixes in `models/README.md` and
  `skills/README.md`. No behavior change.
- Specification 0.17 → 0.18: section 7.4 defines idempotency keys (RM17,
  RM21, AC11, #26): the key format, the caller scope, the request hash,
  that a key is stored with the run it creates, the replay of a native
  creation (202 for the existing run) and of a stored chat response
  (also as chunks), 409 `idempotency_conflict` for a changed payload or
  an unfinished chat duplicate, 409 `run_terminal` for a failed or
  cancelled one, and the key leaving with its run at the retention limit.
  Section 7.1 replaces the refusal of `Idempotency-Key` with its format
  check and the check order, and section 11 widens the 409 row.
- Specification 0.16 → 0.17: section 7.4 defines the native run bodies and
  views (RM21, AC11, #25): the creation checks and 202 body, the run view
  and the once-only confirmation token, the SSE stream (event ID is the
  stored `seq`, replay from `Last-Event-ID` reads the store only, the
  stream stays open across waits and ends after the terminal event), the
  resume check order, cancel semantics, and `/healthz` and `/readyz`
  readiness per permitted mode. Section 4.4 names the unimplemented
  workflow as a cause of `workflow_unavailable` and says the gateway
  checks it before routing. Section 8 adds the `cancel_requested` reason
  to `run.cancelled`.
- Requalify llama.cpp b11785 on Apple Silicon macOS/Metal and native
  x86-64 Windows/CPU (#58; RM18). Migrate the manifest to `--reasoning off`,
  derive the matching HTTP override, and explicitly separate reasoning
  from answer content. Non-thinking remains the default; operator-enabled
  thinking remains supported. Record both live runs separately from the
  historical runtime evidence and add a Windows runtime smoke workflow.
- Specification 0.14 → 0.15: section 7.3 defines the streaming contract
  (RM20, AC09, #23): the buffered stream of the managed alias as a replay of
  the run's `answer.delta` events, the live stream of a backend that forwards
  provider text, append-only content chunks, and the mid-stream failure
  behavior (run recorded as failed, one fixed `error` data event, the
  connection closed with no finish chunk and no `[DONE]`). A failure
  before any answer text, or of a request without stream, is a normal
  503 `external_failed` response, and a client disconnect closes the
  provider stream.
- Specification 0.13 → 0.14: section 7.1 defines request validation for the
  compatible chat endpoint (RM19, AC16, #22): the order of the checks, which
  fields and message shapes are supported, the named `unsupported_parameter`
  and `invalid_request` cases, message ordering and tool-call references,
  how sampling and token budgets are applied (an external generation
  offered tools keeps its own token budget) and the operator limit on the
  token budget, the rejection of `Idempotency-Key` until it is
  implemented, the response and stream shape, 409 `run_terminal` for a
  run that ends as run state, and the loopback bind and generated token
  rules.
- Specification 0.12 → 0.13: section 11 defines the error contract (RM04,
  AC07, AC16, #21): the error body with a stable code, message, and run
  reference, the status for every code, and the mapping of failed runs
  (needs_external 422, context_limit_exceeded 400, external_failed 503 or
  504 for an exhausted time limit, workflow_unavailable 503, internal_error
  500). It also defines degraded mode as an operator policy, off by
  default, with its trace record (`degraded_mode` and `local_failure` in
  `handoff.started`). Sections 4.4 and 8 follow.
- Specification 0.11 → 0.12: section 8 states that redaction also replaces a
  secret value (string or number) repeated in the data or error message of
  the same tool result, and in later tool results, model text, proposed
  tool arguments, and resume input of the run, and that
  injected text in tool output never changes tools, permissions, or
  approvals (RM08, RM11, RM12, AC06, #20).
- Specification 0.10 → 0.11: section 5 defines restart recovery (RM10,
  RM17, #8): when it runs, what it settles (pending writes as
  `unknown_outcome`, pending reads as `failed`, pending approvals
  expired), the `interrupted` failure and that a recovered run is never
  resumed. Section 8 adds `interrupted_state` and `settled_operations`
  to the `run.failed` payload.
- Specification 0.9 → 0.10: sections 4.3, 4.4, 5, 7.4, 8, 9, and 11 define
  the confirmation flow (RM11, #19): the `confirmation_target` a mutating
  tool declares, the single-use token and its hash, the binding, the
  `confirmation.required` and `run.cancelled` payloads, resume with
  `approval_id`, `token`, and `approve`, `approval_expired`,
  `user_denied`, and 403 `approval_invalid`. A mutating proposal no
  longer ends the run as `needs_external`, and the external stage uses
  the same flow.
- Specification 0.8 → 0.9: sections 4.4, 8, and 11 define the handoff
  (RM13, RM14, #18): how the HandoffEnvelope is built from the run
  store and sent as normalized messages, the context measurement and the
  `context_limit_exceeded` failure, what blocks a handoff (including a
  mutation that needs confirmation, a skill with no permitted tool, an
  admitted parallel route, and an unavailable local model),
  the external stage and its limits, the `handoff.started` payload, and
  the `external_failed` error code.
- Specification 0.7 → 0.8: section 6 uses the selected runtime's
  supported reasoning controls (RM18, #58). llama.cpp b11785 uses
  `--reasoning off/on` at launch and the supported HTTP template boolean
  for per-request overrides, with explicit `deepseek` reasoning extraction.
- Specification 0.6 → 0.7: Chinese support is best-effort; native
  Chinese review is no longer required (sections 9, 12.2). English
  examples, corpus scenarios, and live traces stay required; Chinese
  ones are kept where they exist and reported per language, but do not
  block acceptance (sections 1, 6, 12.3, AC01, AC15, section 14).
  Evidence must say when Chinese material is not natively reviewed
  (RM16). No native Chinese reviewer is available to the project.
- Specification 0.5 → 0.6: section 8 adds cross-field rules for
  RouteDecision (clarification, and skill_id by route, mode, and the
  enabled parallel workflow), names the `run.accepted` `model_alias`
  field, and defines the `clarification.required` payload and the new
  required `clarification.answered` event. Section 7.4 states that a
  native run takes no model alias and always uses managed execution,
  that `external_only` records a gateway-produced RouteDecision with
  reason code `execution_mode`, and that a resume body that does not
  match the waiting state is 400 `invalid_resume_input`. Section 4.4
  says the operator enables the parallel workflow and which execution
  modes admit it. RM04 scopes the explicit escalation proposal to the
  routing decision for the MVP. Sections 5 and 11 define clarification
  resume: continue with the chosen skill or route again, per-stage
  local budgets with a two-round clarification limit, the run language
  kept across resume, and the resume input and waiting time recorded
  as evidence. After review: RM04's explicit proposal is `route=external`
  with `complex_skill` only, the `parallel_workflow` external route is
  gateway-produced, and `route=parallel` where the workflow is not
  admitted is invalid output; section 8 constrains `reason_code` by
  route, says the gateway sets the language of every later decision,
  and defines the `run.completed` and `run.failed` payloads
  (`active_ms`, `waiting_ms`); the resume body field is `input`
  (section 7.4); `parallel_demo` with an unavailable workflow fails with
  `workflow_unavailable` (503 on the chat endpoint); section 12.1 sums
  all recorded waits; AC03 references RM22. Owners: the RouteDecision validator (#53; #14), the
  run.accepted validator (#54; #27), and the clarification flow
  (#61; #16).
- Specification 0.4 → 0.5: require proxy bypass for loopback llama.cpp
  inference and startup health polls, preserving normal proxy handling
  for non-loopback endpoints and the external Chat Completions adapter
  (#57; RM01, RM18; section 6).
- Roadmap after M1: M2 split into a routing probe plus vertical slice
  and a widening stage; required trace events (#27) moved from M4 to
  M2; restart recovery (#8) moved from M2 to M3; Windows qualification
  (#46) added to M1; the orchestrator half of AC08 (#49) tracked in M3.
  Milestones now close against `docs/EVIDENCE.md`. No behavior change
  and no spec revision.
- Specification 0.3 → 0.4: the compatible chat endpoint accepts an
  optional `X-RelayMind-Execution-Mode` request header carrying the
  native `execution_mode` values under the same operator policy, so a
  standard OpenAI client can force a local or external run without
  the native API. The model alias keeps selecting tool ownership only.
  Section 7.1 lists request headers in their own table and names the
  error codes for invalid, forbidden, and body-supplied modes
  (sections 7.1, 7.2, 7.4; issues #22, #24).
- Specification 0.4: `needs_external` on the chat endpoint is HTTP 422
  with an error object (sections 7.2, 11).
- Specification 0.4: section 4.2 states where skills come from: an
  operator-configured list of local roots, `["skills/"]` by default,
  where every subdirectory is a candidate package with `SKILL.md` and
  `skill.manifest.json`, unique names across roots, and strict load
  errors (issue #9).
- Specification 0.4: section 8 defines the `run.accepted` payload,
  which records the execution mode, model alias, budgets, skill roots,
  and offered skills for every run.
- Specification 0.2 → 0.3: the artifact manifest records the "runtime
  version and commit" (one composite field) instead of only a "runtime
  commit", matching the implemented `runtime_version` field.

- Specification 0.1 → 0.2: section 6 now allows an operator option to
  enable Qwen3 thinking mode through the same chat-template settings.
  Non-thinking stays the default and the latency-experiment
  configuration; reasoning output never appears as answer content, and
  results from the two modes are never mixed.
