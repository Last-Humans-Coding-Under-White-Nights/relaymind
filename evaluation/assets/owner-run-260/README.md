# Owner-run labelled cohort for #260 (spec 0.66 section 12.6.6)

RM40, RM42, RM44; AC31, AC33, AC36. Frozen inputs for the owner-run adaptation
episodes and paired host tasks. Prompt authorship is
`machine_drafted_owner_approved` and labelling `machine_dual_owner_approved`:
one pinned drafting model wrote every prompt and two machine annotators with a
machine adjudication pass labelled them. No human annotator took part. The
owner approves the frozen cases and labels by reviewing the pull request that
commits them. Nothing here is independent or human-labelled, and nothing here
is a measured pass.

See [PROTOCOL.md](PROTOCOL.md) for the procedure and
[docs/evidence/m10/owner-run-260/](../../../docs/evidence/m10/owner-run-260/)
for the runs, grades and the pending qualification record.

## Files

| File | Content |
| --- | --- |
| `cases.json` | AssetCasesV1: 600 held-out cases (100 host-task prompts, 20 per required lane; 500 episode prompts: three corrections and two paraphrases for each of 100 intents) |
| `labels.json` | AssetLabelsV1 (`machine_dual.v1`): two raw judgments per case, one adjudication per case, pre-adjudication agreement |
| `lock.json` | AssetLockV1, `frozen`, purpose `development` |
| `cohort-plan.json` | The seeded plan: targets, lanes, controls and reset probes (derivation evidence) |
| `drafting-log.json`, `labelling-log.json` | Pinned model, template SHA-256, sampling, and per call the request digest and the original response file |
| `logs/` | Original model responses. Requests are kept by digest only: they contain catalog excerpts whose redistribution rights are unknown |
| `prompts/` | The drafter, annotator A, annotator B and adjudicator templates |
| `episodes.json`, `episode-freeze.json`, `episode-configs/` | AssetEpisodesV1, EpisodeFreezeV1 (100 episodes) and the off/on EffectiveConfigV1 and adaptation configuration they bind |
| `host-tasks.json`, `host-freeze.json`, `configs/`, `rubrics/` | AssetHostTasksV1 (100 acceptance tasks), HostFreezeV1, the configuration documents (secrets omitted) and the success rubrics |
| `preregistration-*.json` | Outputs of `preregister-host` and `preregister-episodes` |
| `harness.py`, `run_episodes.py`, `run_host.py` | The cohort harness and the two run drivers |

## Rebuild the private catalog

The catalog and its originals are not checked in (unknown redistribution
rights). Rebuild them offline and check the lock:

```bash
python scripts/source_asset_benchmark.py acquire \
  --recipe evaluation/assets/matrix-development/sources.v2.json --cache CACHE
# The checked-in recipe has one Matrix row with a null revision
# (matrix:6a577dc2450ba62d80017fb7), which the current builder rejects as
# invalid_source_recipe. Drop that row into a private copy of the recipe:
python - <<'EOF'
import json
r = json.load(open("evaluation/assets/matrix-development/sources.v2.json"))
r["assets"] = [a for a in r["assets"] if a["revision"] is not None]
json.dump(r, open("RECIPE", "w"), ensure_ascii=False)
EOF
python scripts/source_asset_benchmark.py build --recipe RECIPE --cache CACHE --output BUILT
python evaluation/assets/owner-run-260/harness.py catalog --built BUILT --root ROOT
```

Then copy `cases.json`, `labels.json`, `lock.json`, `cohort-plan.json`,
`drafting-log.json`, `PROTOCOL.md` and `prompts/` into `ROOT`; the lock's
catalog digest must match.

## Known limits

- The catalog is the 412-asset development catalog, not the 300-asset
  acceptance catalog, so the corpus stays `development` and every graded
  report stays below `qualified`.
- Both annotators and the adjudicator use the same model family (the provider
  offers only one); they use independently written templates.
- Class kappa is low because almost every drafted prompt is positive (high
  prevalence makes chance agreement high); the acceptable-set agreement and
  the Jaccard sum are the informative numbers. No negatives or ambiguous
  prompts were drafted.

## Preregistration history

The first preregistration commit (`00276311de0594159bc61877bd736ac03d46c734`)
named the corpus by the lock's own `digest` field instead of the canonical
digest of the whole lock document, which the section 12.6.5 grader resolves
(`corpus_unresolved`). It is superseded by the next commit, which changes only
`corpus_lock_digest`/`dataset_lock_digest` (and so the freeze and input
digests). Cases, labels, the lock and all configurations are unchanged. Runs
made under the first preregistration (all 100 episodes and two host rows,
`h001` native and provider_only) are kept privately as a pilot and are not
part of any graded report; every graded run starts after the second commit.
