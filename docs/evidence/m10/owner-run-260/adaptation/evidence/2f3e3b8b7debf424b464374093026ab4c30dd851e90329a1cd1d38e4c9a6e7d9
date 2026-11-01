# Owner-run cohort protocol for #260 (spec 0.66 section 12.6.6)

Profile: `machine_drafted_owner_approved` prompts and
`machine_dual_owner_approved` labels. No human annotator is used; the owner
approves the frozen cases and labels by pull request review.

- Catalog: the 412-asset development catalog built offline from
  `evaluation/assets/matrix-development/sources.v2.json` (one recipe row with a
  null revision dropped before the build, see README), with coarse source
  families (`source-family:matrix`, `source-family:skillsbench`,
  `source-family:github-mcp-server`). Purpose stays `development`.
- Plan: `harness.py plan` picks targets and lanes from a fixed seed: 100 host
  tasks (20 in each of the five required lanes) and 100 adaptation episodes.
  Every case is `held_out`.
- Drafting: one pinned model at temperature 0 writes every prompt from the
  catalog projection of its target and the lane, with the template in
  `prompts/drafter.txt`. It sees no labels, tuning results or arm outputs.
- Annotation: two machine annotators with two independently written
  templates (`prompts/annotator-a.txt`, `prompts/annotator-b.txt`), same model
  family (`same_family`: the provider offers one family), temperature 0. Each
  sees only its own blind label template and the catalog listing.
- Adjudication: the third pass. Cases where both raw judgments agree on all
  four answers are adopted by rule; every other case goes to the adjudicator
  model (`prompts/adjudicator.txt`), which writes a reason.
- Episode controls and reset probes are host prompts of other intents, picked
  by seed.
- Access: the owner is custodian and tuner and does not tune on these labels
  after the preregistration commit. Request and response logs are kept;
  provider credentials and endpoints never are.
