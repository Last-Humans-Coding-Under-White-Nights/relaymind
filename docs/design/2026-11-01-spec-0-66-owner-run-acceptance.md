# Spec 0.66: owner-run acceptance profile

Date: 1 November 2026. Status: non-normative rationale. The specification
(section 12.6.6) is authoritative; nothing here is a measured result.

## Purpose

#251 lets #255, #259, #260 and #261 be completed by the project owner, without
customers, outside annotators or extra hardware, using the owner's MacBook
(Apple M1, 8 GiB, macOS), Windows PC (native x86-64), Ubuntu VPS and the GitHub
org team for review. RM16, RM38–RM44; AC29–AC37. Metrics, denominators,
thresholds and budgets are unchanged; only who supplies people, labels,
machines and reviews changes, and every report says so.

## Decisions and rationale

- **Operator, not customer.** The owner runs every trial as operator. Wording
  that named a customer as the trial runner is replaced; historical feedback
  notes stay as written.
- **Labelling `owner_machine`.** Two raters, the owner and one machine
  annotator, is the common LLM-assisted annotation pattern with human
  adjudication. The machine rater is reproducible only when pinned: dated model
  ID, prompt template digest, temperature 0. Isolation from tuning results and
  arm outputs, and blindness of each rater to the other, keep the two judgments
  separate. Cohen's kappa (chance-corrected) is reported beside percent
  agreement, both before adjudication, because raw agreement alone overstates
  reliability on skewed classes. No kappa threshold is added: the spec already
  requires recorded agreement, not a pass level. The owner adjudicates with a
  recorded reason. The owner is also the tuner, so the label is
  `owner_machine`, never `independent`.
- **Machine-drafted prompts (`machine_drafted_owner_approved`).** The owner
  chose model drafting from the catalog with owner approval (#260). Pinning the
  drafter (model ID, template digest, temperature 0) and keeping the request and
  response bytes makes the cohort reproducible and auditable. Drafting each
  prompt directly in its lane avoids translation artefacts. A distinct label
  keeps the reports honest: they never say original, native or human-authored.
  Known risk: drafted prompts can be easier than real requests and can share
  the drafter's wording habits, so results are a lower bound on difficulty, not
  a claim about real users.
- **Two machine annotators (`machine_dual_owner_approved`).** With no human
  labeller, two blind machine raters plus a third adjudication pass follow the
  usual LLM-as-judge practice: temperature 0, pinned IDs and template digests,
  and raters blind to each other and to outputs. Different model families
  reduce correlated errors; when the provider offers one family, two
  independently written templates are the fallback and the record says
  `same_family`, because same-family raters share biases and kappa overstates
  their independence. Cohen's kappa is still reported before adjudication. The
  owner's PR approval of the frozen labels is the human check. None of the
  raters is a tuner, and the label is never `independent`.
- **Author competence by profile.** For `owner_machine` with `original`
  prompts, section 12.6.1 competence rules are unchanged: a lane without a
  competent author stays uncovered. Under `machine_drafted_owner_approved`,
  the recorded drafter replaces that competence rule for cohort counts, as
  [section 12.6.6](../SPECIFICATION.md#1266-owner-run-acceptance-profile-spec-066-rm16rm38rm44-ac29ac37)
  specifies. Translation does not satisfy either profile's originality rule.
- **Qualification record.** One closed, versioned JSON schema covers positive
  and negative records. Byte SHA-256 binding (not canonical JSON) makes the
  record tamper-evident for any file type, including media manifests and
  captures. Approval by a PR review from at least one org member other than the
  owner is the two-person rule; GitHub's review history is the audit trail.
  The PR number and reviewer logins are added after merge because they do not
  exist before it; a pending record lifts nothing. Records are immutable after
  approval; a correction is a new record ID.
- **Machine identity.** OS build, CPU model and RAM are what the platform gate
  depends on; the hostname is published only as a SHA-256 to avoid leaking the
  owner's machine name. The VPS is a functional host and stays
  `platform_not_target`, as section 12.6 already excluded Linux.
- **Negative qualification.** Proving an absence on a pinned version
  (captured hook, plugin SDK and gateway surface plus the attempted routes) is
  a valid, reviewable outcome. The operation stays disabled everywhere, is
  listed as a known host limitation, and M10 no longer waits for a host
  capability that does not exist. A new host version reopens it.
- **Acceptance catalog.** Public open-source MCP servers installed locally
  (for example the `modelcontextprotocol/servers` reference servers) give
  genuine, licensed, reproducible tool listings. Freezing the original
  `tools/list` capture bytes plus the canonical schema hash meets the existing
  originality and digest rules without a vendor account.
- **Preregistration.** A git commit SHA with its committer time, earlier than
  the first dispatch, is a public, tamper-evident timestamp the team can check.
- **Per-lane minimum: 20 of the 100 paired tasks per lane.** An even split of
  100 over five lanes. It stops one lane from being left out, while the gates
  stay on the complete cohort: at n=20 a 95% Wilson interval is roughly ±0.2,
  too wide for a separate per-lane pass, so per-lane results are reported as
  tradeoffs with intervals.
- **Blind success judging.** A preregistered rubric, transcripts with the arm
  identity removed, an order derived from a seeded hash and a sealed
  blind-ID-to-arm map committed before judging are the standard controls
  against judge bias, including the owner's.
- **Usage accounting.** Disjoint components (input, cached input, output,
  reasoning) prevent double counting across providers that nest cached or
  reasoning tokens. The native arm uses provider-reported usage as persisted by
  the pinned host, or the provider's usage export bound by request ID. Anything
  that cannot be bound stays unknown and cannot pass.
- **Episode corrections through the native feedback RPC.** This exercises the
  same host authority path a person uses; direct gateway HTTP would skip it.
- **Choice beyond rank 3.** People pick any offered candidate. Returning the
  chosen offered record in the receipt removes rank ambiguity for later use.
- **Durable MCP ledger.** An owner-only file in the operator's host state
  directory, bounded by the existing `retention_days` and erased with its
  choice on reset, matches the existing private retention and erasure rules
  without changing the fail-closed behavior.

## Implemented in this revision

The verifier support: the record schema and check
(`relaymind.evaluation.asset_qualification_record`), `--qualification-record`
on the `grade-*` commands, `--labelling owner_machine` on the label commands,
the `owner_machine.v1` agreement method and Cohen's kappa recomputation, and
`known_host_limitations` for negative records. The later 0.66 addition adds
`--labelling machine_dual_owner_approved` (`machine_dual.v1`), the
`machine_drafted_owner_approved` case authorship, and the record's
`prompt_authorship`, `drafter` and machine annotators shape with their checks. The rank 4–10 choice receipt,
the ledger location and retention, blind judging and native-RPC episode
corrections are specification decisions for the run issues to implement.
