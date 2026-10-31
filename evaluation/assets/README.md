# M10 retrieval releases (RM40/AC31, specification §12.6.1)

The offline loader, four schemas, judgment/agreement checks, split ledger and
same-source export reproduction are implemented. Genuine corpus acquisition,
authorship, annotation and externally reviewed qualification remain outstanding.
No development or acceptance release is distributed here. Synthetic inputs live
only in `tests/fixtures/asset-corpus/` and remain fixtures regardless of their size.

The current qualified input count is **0/100 original Chinese Matrix skills,
0/100 original English skills, 0/100 genuine MCP tools and 0/25 reviewed
function-equivalent pairs**. There are no qualified authored cases, independent
human judgments or custodian review records in this repository. Each required
lane still needs 200 independent original positives, 50 negatives and 30 ambiguous
cases; the two cross-language lanes also need their independently reviewed hard
subsets. Fixture person IDs and sample rights assertions supply none of this
missing evidence. No retrieval, operational, platform or whole-task gate passes
because this infrastructure is available.

## Supply a release

Use the exact four closed formats in `src/relaymind/evaluation/schemas/`:

- `AssetCatalogSetV1`: product `AssetRecordV1` records, originals, rights,
  provenance, opaque people, equivalent pairs and exclusions.
- `AssetCasesV1`: prompts, conversation, authors, families and splits; no labels.
- `AssetLabelsV1`: two immutable raw judgments and one final adjudication per
  case, including excluded cases, plus recomputable pre-adjudication agreement.
- `AssetLockV1`: canonical document hashes, exact original/evidence byte hashes,
  related release locks, Unicode version and derivation identity.

Prepare these inputs with the source owner and independent custodian:

1. Supply exact, bounded originals from one permitted local source export.
   Keep source identity, source revision, capture time, namespace and original
   languages. One archive or translated clone never supplies multiple genuine
   independent originals. An MCP capture can contain several distinct real tools;
   repeated namespace/tool identity is rejected.
2. Supply acquisition, evaluation and retention permission evidence for every
   admitted original. Preserve each applicable license and NOTICE, attribution
   and permission restriction. Record unknown/prohibited rights honestly and
   retain denied, oversized, unsupported, duplicate and language exclusions in
   the locked catalog. Redistribution can remain prohibited for a permitted
   private local export; reproduction does not publish it or grant permission.
3. Have the custodian review the actual people, native Chinese/proficient English
   competence, source provenance, equivalent function, independent blind double
   annotation and final adjudication. Raw answers are retained separately;
   adjudication never overwrites a raw row. Report agreement without inventing a
   pass threshold. Record task/paraphrase and labeled target families for tuning
   as well as held-out work.
4. Keep held-out prompts and judgments outside tuner/selector-accessible runtime
   environments until settings and artifacts freeze. Public hashes and a JSON
   assertion are not access controls. Record actual custody/access and split
   review evidence, with training overlap explicitly unknown when unverifiable.
5. Freeze the exact release and all related locks. Changed content, rights,
   annotations or splits require a new release. Acceptance retains the exact
   200 development originals and adds the 100 genuine tools. Restricted evidence
   can have a null public path but still needs its locked bytes supplied offline
   to an authorized custodian before qualification.

Every document/evidence path is relative and stays inside its explicit root.
Symlinks, traversal, nonordinary files, oversize files and file growth are refused.
JSON has strict integers, finite numbers, no duplicate/unknown keys, bounded UTF-8
strings and the published version. Originals/evidence use SHA-256 of exact bytes;
structured documents use `relaymind.canonical.canonical_digest`. Formatting a
structured document differently need not change its digest; editing an original
always does. Historical evaluation hash conventions remain unchanged.

## Verify and assess independently

```python
from pathlib import Path
from relaymind.evaluation.asset_corpus import load_asset_release, assess_asset_release

release = load_asset_release(
    Path("/private/export/lock.json"),
    evidence_root=Path("/private/export"),
    related_roots={"development": Path("/private/development")},
)
assessment = assess_asset_release(release)
```

`related_roots` maps each declared related-lock alias ID to one explicitly
allowed root. A reference's required `path` selects its actual lock below that
root. Catalog/case/label paths resolve relative to their owning lock, with the
whole path walk fenced inside the allowed root. Aliases need not equal a
`dataset_id`. Unknown aliases, traversal, symlink ancestors, wrong paths and wrong
digests fail; there is no alternate-base search or fallback to `lock.json`.
Evidence paths are relative to the explicit common `evidence_root`.
An optional `EvidenceResolver.resolve(evidence_id)` returns an offline local path
inside the evidence root or an explicitly supplied related root. It never fetches
or executes a URL. Missing restricted bytes remain visibly unavailable; altered
or missing public bytes fail structural verification.

Object arrays follow the normative composite key ordering and reject duplicates.
Nullable exclusion `original_id` sorts before nonempty UTF-8 IDs. Loaders reject
unsorted records rather than silently mutate their committed hash. Agreement
hashes retain the stored Jaccard scalar; recomputation checks it within the
published tolerance without replacing its committed value.

Each linked release must commit its referenced evidence locally or through its
own descendants. An ancestor or sibling cannot supply an otherwise uncommitted
reference.

The returned documents, evidence findings, agreement, split counts and Latin-token
intersections are immutable. Hard-subset diagnostics compare the task and all
conversation text against every acceptable target. Tool-schema search text uses property names and `description` strings; JSON
Schema keywords, `type` values, enum/default values and conversation roles do not
create token overlap.
Negative and ambiguous cases remain in hard-subset precision cohorts. The loader checks Unicode version,
normalized prompts, family exposure and derivation links across the lock closure.
All committed derivation edges remain in the lineage graph across releases; a
later release cannot overwrite an older edge to hide a cycle. Independent-case
counts use normalized tasks plus the ordered conversation, including roles. Later
identical inputs are listed as repetitions, excluded from lane/hard-subset minima
and keep nonfixture qualification unverified; their raw judgments and agreement
rows remain intact. Different contextual inputs remain distinct. Semantic
near-duplicate judgments and actual human/legal authenticity still need external
review.

By default a nonfixture release is `unverified`, even if JSON declares it frozen.
A trusted offline caller can supply `assess_asset_release(release,
custodian_review=CustodianReview(...))` only after an actual custodian review.
That frozen object binds the exact lock digest, an independent declared custodian,
verified review-record evidence IDs and per-evidence reviewed domains (`rights`,
`source_provenance`, `language`, `authorship`, `independence`, `adjudication`,
`split`, `access_control`, `derivation`). It is never loaded from corpus JSON,
environment variables or configuration. Missing domain coverage, wrong lock,
unavailable bytes, unknown/prohibited rights or incomplete cohorts remain
unverified. A fixture always stays `fixture`. Qualification records a caller's
external review assertion; the program cannot authenticate real humans or legal
rights from hashes.

## Reproduce a supplied export offline

```bash
python scripts/benchmarks/derive_matrix.py \
  --input /private/supplied-export \
  --lock /private/supplied-export/lock.json \
  --output /private/new-export
```

The input is an already staged, self-contained four-document release, with every
exact original and metadata item supplied locally. This command does no network
acquisition, remote refresh, archive extraction or source execution. This minimal reproduction command refuses exports that declare related locks
or need restricted-path resolution: its three-argument interface has no resolver
or related-root mapping. It never discovers related exports or rewrites a lock
to remove those dependencies.

It validates the input, requires recorded acquisition/evaluation/retention
permission, copies only the locked documents/evidence into a new private directory
and validates the copied closure again. It preserves original bytes, metadata,
digests and predeclared exclusions. It never rewrites a frozen release to remove
an invalid asset. Invalid/tampered/unavailable/rights-blocked input fails without
an output; an existing destination is refused without overwriting user content.
The JSON console summary reports reproduction, exclusions and the honest
qualification status. Reproducing a fixture cannot qualify a real baseline.

The benchmark run/compare protocol, live source refresh and real acceptance
measurements belong to later issues. This loader does not execute or recommend
an asset, alter product rights or expose private labels to a selector.


## Assess the 200-to-300 acceptance extension (#199)

```python
from relaymind.evaluation.asset_corpus import assess_acceptance_release

assessment = assess_acceptance_release(acceptance_release, development_release)
```

Both arguments come from the same bounded loader. The acceptance lock must
commit the development lock in its descendant closure. Assessment compares all
200 exact original asset/source/package identities, versions and original hashes;
missing, changed and unexpected IDs remain visible. Acceptance requires exactly
100 original Chinese skills, 100 original English skills and 100 genuine MCP
entries, with at least 25 reviewed equivalent-function pairs. Identical exposed
names on different actual servers remain separate identities. Names, URLs, a
`genuine_mcp` assertion or a frozen JSON field cannot authenticate a real source.
ToolRet/RapidAPI exports do not become genuine MCP captures through a flag.

The same assessor checks every required lane's 200/50/30 independent-case minima,
original authorship, full-catalog alternatives, and the half no-shared-Latin
cross-language requirement. Translated/generated/diagnostic prompts and later
iterations of an identical task/conversation cannot repair a missing independent
case. `classify_latin_overlap` exposes immutable per-target intersections under
the lock's pinned Unicode database, including combining, mixed-script and
underscore boundaries. All negative and ambiguous cases stay in hard precision.

A fixture is permanently `fixture`, even with 300 synthetic records, complete
synthetic cohort counts and a test custodian assertion. Real qualification still
requires actual source/legal/native-author/independent-judge/access review.

Keep real held-out originals, prompts and raw/final judgments in an independent
custodian directory with actual OS access restrictions, outside tuner checkouts
and selector runtime roots. Public hashes and an opaque person ID supply no
access separation. Give the trusted offline assessor only the explicit roots it
needs; restricted evidence can remain null-path in public metadata. Verify
real native Chinese/proficient English authors and two independent blind judges,
retain both original judgments and adjudication, and preserve exclusions before
measurement. Later episode reports reference these locked case/family identities;
no speculative episode fields are added to the four corpus documents.

## Reproduce pinned supplied MCP inputs offline

```bash
python scripts/benchmarks/derive_mcp_tools.py \
  --input /private/supplied-mcp-export \
  --lock /private/supplied-mcp-export/lock.json \
  --output /private/new-mcp-export
```

This uses the same exact-byte reproduction engine as `derive_matrix.py`, the same
four documents and single-lock/resolution limits. At least one pinned MCP record
is required. Full mixed catalogs are preserved intact, together with server,
original tool, exposed name, schema hash, metadata, versions, originals and locked
exclusions. It neither launches a server nor fetches/executes a locator or extracts
an archive. Missing bytes, bad schema/original hashes, unknown/prohibited required
rights and existing destinations block output. The result retains fixture or
unverified qualification. It never publishes content or upgrades source rights.

No genuine 200/300 source freeze, native authorship, human annotation or real
acceptance result is shipped by this implementation. The explicit missing input
counts at the top remain unchanged.
