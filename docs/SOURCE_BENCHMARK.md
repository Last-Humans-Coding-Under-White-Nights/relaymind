# Source-grounded skill and tool benchmark

[English setup](asset-selection.md) · [简体中文入口](asset-selection.zh-CN.md) ·
[Privacy and recovery](asset-privacy-security.md)

This development benchmark builds its data from actual public originals. The
current pinned catalog contains **412 assets: 87 Matrix skills, 194 SkillsBench
skills and 131 GitHub MCP tools**, with 61 explicit exclusions. It is ready for
local selector development; a finished independently annotated dataset is not an
input prerequisite. Matrix supplies examples and benchmark sources. Users can
configure any supported local skills or metadata imports in their own libraries.

The 128 preregistered cases cover 54 capability requests and 58 distinct target
assets, plus ambiguous and unsupported requests. Chinese, English and mixed
queries use the same complete catalog. Original bodies and description languages
are recorded separately. Chinese-to-original-English retrieval is included,
without inserting shared Latin asset names into the Chinese request. For example:

> 检查鸿蒙应用源代码中可能导致对象无法释放的路径，并给出内存泄漏的修复建议。

Its reference is the original English `hmos-memleak-analysis` skill captured from
Matrix. Nothing is translated and relabeled as an original. Other cases cover
English-to-English, Chinese-to-Chinese, English-to-Chinese, code-switching, genuine
MCP tools and close functional alternatives. There are no captured Chinese or
mixed-description MCP tools in this version; missing combinations are reported.
A catalog of 412 is not a claim of 412 independently tested target capabilities.

References are developer-authored from retained originals before selector runs.
They are provisional and not independent human judgments. Source-family tags
identify source records, not independently adjudicated semantic families. The
[formal independent qualification profile](../evaluation/assets/README.md#independent-qualification-profile)
remains available separately. These development reports are internal diagnostics,
not `AssetBenchmarkReportV1` or formal acceptance evidence.

## Acquire and reproduce the data

From the repository root, use Python 3.12+ with the project installed, or select
the current checkout explicitly:

```bash
export PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}"
```

The following concrete paths were used for the example run. Use another private
cache/output location on systems without `/var/tmp`; every CLI path is explicit.
Do not put unknown-license originals into a public repository.
If an output directory already exists, choose a new private root throughout;
`build` refuses to overwrite an earlier capture.

```bash
python scripts/source_asset_benchmark.py acquire \
  --recipe evaluation/assets/matrix-development/sources.v2.json \
  --cache /var/tmp/rm-matrix-benchmark-20261030/large-pinned-cache
python scripts/source_asset_benchmark.py build \
  --recipe evaluation/assets/matrix-development/sources.v2.json \
  --cache /var/tmp/rm-matrix-benchmark-20261030/large-pinned-cache \
  --output /var/tmp/rm-g1-source-guide/catalog-v2
```

`acquire` fetches exact HTTPS URLs and checks declared lengths and SHA-256. Each
job is bounded to 60 seconds and 100 MiB, including at most ten seconds per
exchange. Large initial collections may need several invocations: verified cache
hits are reused, completed receipts remain visible, and incomplete invocations
exit nonzero with per-file reasons. Resume the same command; inspect persistent
HTTP/hash failures instead of accepting changed bytes. The tested local cache
replay verified all 442 pinned files. A separate fresh-network smoke capture
verified the pinned 99,960-byte seed ZIP. The source inventory records exact
upstream revisions and original hashes; changing a source creates a new version.

The Matrix acquisition starts from the provided
[skill example](https://matrix.openharmony.cn/#/skillSquare/details?id=69e1dea2c4357076d28ecb48)
and the public
[installation guide](https://matrix.openharmony.cn/api/registry/skill/matrix_skill_guide.md).
The recipe pins versioned originals rather than relying on a moving latest
endpoint. English skill originals supplement Matrix from a pinned SkillsBench
revision; MCP originals come from pinned official GitHub MCP snapshots. Individual
license/NOTICE records and known retention restrictions are retained. Rights stay
unknown where permission has not been established. Restricted upstream originals
are excluded instead of copied into the benchmark output.

`build` is offline and requires a new output directory. It verifies the originals,
inspects only bounded selected root text from ZIPs in memory, preserves every
predeclared exclusion, deduplicates genuine originals and writes:

- `catalog.json`, plus hash-addressed originals and license/provenance evidence;
- three ordinary `AssetMetadataImportV1` files usable by the product's generic
  metadata-import configuration;
- `builtin-production.toml` and `http-production.toml` with the same source policy,
  full catalog and 500 ms service / 150 ms primary query budgets.

No acquired package, script or tool is executed or installed. The generated
external-model table contains an unused declaration required by the production
configuration parser; retrieval does not contact that endpoint. Originals and
long source metadata remain in the private output. The checked-in recipe, cases
and measurement summaries contain no redistributed skill bodies.

## Connect an external selector and declare readiness

A team implements the existing v2 `POST /select` protocol described in
[ASSET_BENCHMARK.md](ASSET_BENCHMARK.md#the-skilltool-v2-boundary), using the
published request/result schemas under `src/relaymind/selection/schemas/`.
Each query receives actual task/conversation, scope/permissions, ordered full
catalog, catalog revision and remaining deadline. Reference labels, original
whole bodies and private answers are absent. The gateway validates and admits
results. A selector proposes recommendations; it gains no execution authority.

For an algorithm that builds an index, expose the shared optional
[preparation lifecycle](ASSET_BENCHMARK.md#index-readiness-and-setup-cost).
The catalog-bound prepare/status response establishes `ready` and an
`index_revision`; queries must return that same revision. `not_ready` and failed
preparation stay visible. Setup time and its reported/unknown costs are separate
from query latency. Merely starting a server does not claim indexing finished.
Gateway lexical readiness, a listening server and catalog-bound index readiness
are three different facts. The configured preparation URL is one POST endpoint
with `operation=prepare|status|cancel`, not separate status/cancel routes. Only
prepare starts work; status/cancel retain the ID/binding/profile with null
catalog/budget. See the published
[request](../src/relaymind/selection/schemas/asset_preparation_request.v1.json) and
[response](../src/relaymind/selection/schemas/asset_preparation_response.v1.json) schemas.

Start the supported examples in separate terminals:

```bash
python scripts/example_asset_selector.py --port 8933
python scripts/example_asset_selector.py --port 8934 --preparation-path /index
```

The first builds its lexical index within each query. The second prepares it
explicitly and can reuse a matching server-owned index. Both implement the same
query boundary and use the same lexical algorithm; they are integration examples,
not a demonstrated multilingual semantic model. Replace a URL and the production
selector name/version for another team's actual implementation.

Create the ordinary production and preparation files from the built output:

```bash
python - <<'PY'
from pathlib import Path
root = Path('/var/tmp/rm-g1-source-guide/catalog-v2')
base = (root / 'http-production.toml').read_text(encoding="utf-8")
for name, port in [('stateless', 8933), ('indexed', 8934)]:
    (root / (name + '-production.toml')).write_text(
        base.replace(':8913/', ':' + str(port) + '/'), encoding="utf-8", newline="\n")
for profile in ('cold', 'reuse'):
    (root / (profile + '.toml')).write_text(
        '[preparation]\nmode="http"\nurl="http://127.0.0.1:8934/index"\n'
        + 'profile="' + profile + '"\ntotal_ms=60000\nrequest_ms=2000\n'
        + 'poll_ms=250\nmax_polls=240\n', encoding="utf-8", newline="\n")
PY
```

`--preparation-config` reads this same closed `[preparation]` table used by the
shared benchmark client. It is separate from production query settings. Omit it
for a stateless algorithm or the builtin selector: the report explicitly records
`not_applicable`, unknown setup duration and no lifecycle traffic. Indexed cases
must share requested kinds so one preparation binds exactly the offered catalog.

## Run identical cases and compare

Use these arguments for every arm; change only the declared selector/preparation
and output. Cases and catalog are frozen before any new selector measurements.
Run cold before reuse while the indexed server remains alive.

```bash
python scripts/source_asset_benchmark.py run \
  --catalog /var/tmp/rm-g1-source-guide/catalog-v2/catalog.json \
  --cases evaluation/assets/matrix-development/cases.v2.json \
  --evidence-root /var/tmp/rm-g1-source-guide/catalog-v2 \
  --production /var/tmp/rm-g1-source-guide/catalog-v2/builtin-production.toml \
  --track service --output /var/tmp/rm-g1-source-guide/builtin-service.json
python scripts/source_asset_benchmark.py run \
  --catalog /var/tmp/rm-g1-source-guide/catalog-v2/catalog.json \
  --cases evaluation/assets/matrix-development/cases.v2.json \
  --evidence-root /var/tmp/rm-g1-source-guide/catalog-v2 \
  --production /var/tmp/rm-g1-source-guide/catalog-v2/stateless-production.toml \
  --track service --output /var/tmp/rm-g1-source-guide/stateless-service.json
python scripts/source_asset_benchmark.py run \
  --catalog /var/tmp/rm-g1-source-guide/catalog-v2/catalog.json \
  --cases evaluation/assets/matrix-development/cases.v2.json \
  --evidence-root /var/tmp/rm-g1-source-guide/catalog-v2 \
  --production /var/tmp/rm-g1-source-guide/catalog-v2/indexed-production.toml \
  --preparation-config /var/tmp/rm-g1-source-guide/catalog-v2/cold.toml \
  --track service --output /var/tmp/rm-g1-source-guide/cold-service.json
python scripts/source_asset_benchmark.py run \
  --catalog /var/tmp/rm-g1-source-guide/catalog-v2/catalog.json \
  --cases evaluation/assets/matrix-development/cases.v2.json \
  --evidence-root /var/tmp/rm-g1-source-guide/catalog-v2 \
  --production /var/tmp/rm-g1-source-guide/catalog-v2/indexed-production.toml \
  --preparation-config /var/tmp/rm-g1-source-guide/catalog-v2/reuse.toml \
  --track service --output /var/tmp/rm-g1-source-guide/reuse-service.json
python scripts/source_asset_benchmark.py compare \
  --left /var/tmp/rm-g1-source-guide/builtin-service.json \
  --right /var/tmp/rm-g1-source-guide/cold-service.json \
  --output /var/tmp/rm-g1-source-guide/compare-builtin-cold-service.json
```

Repeat the four run commands with `--track primary_only` and corresponding
`*-primary_only.json` outputs to grade only the admitted primary result, without
crediting service fallback. Production budgets/fallback remain identical, and
all attempts are still retained. Compare reports from the same track. The tested
record contains eight attempted arms. Each report retains all
128 planned slots, actual production requests/results/attempts, failures,
cancellation, input/config/implementation digests, language/kind groups, hard
Latin-overlap diagnostics, latency and cost coverage. A service hit reached by
fallback never becomes a primary hit. Repetitions retain observations but never
multiply the number of reference cases.

Comparison recomputes commitments, result/attempt/choice relationships, grades,
subgroups and setup transcript bindings. Identical cases/catalog/policy/track are
required. When both arms prepare an HTTP index, their preparation profile and
all setup budgets must also match. Cold versus reuse is incompatible; a
stateless/indexed pair remains comparable with separate setup evidence. A blocked
or interrupted required preparation writes every planned slot and exits nonzero.
Internal diagnostics do not establish a formal confidence interval,
independent annotation or a hardware performance pass. Full local reports contain
source metadata and remain private; the compact checked-in `measurements.v2.json`
records their hashes, actual counts and summaries. Missing cost observations stay
unknown. The initial 47-asset/36-case measurements remain unchanged, including
real stateless HTTP timeouts and the Chinese-to-English memory-leak miss; the new
version does not rewrite those observations.

## Recorded development results

The frozen record contains eight attempted arms and six historical comparison
artifacts. The original source CLI incorrectly returned zero for the two blocked
reuse runs and accepted cold/reuse as compatible. The corrected CLI preserves
the blocked reports and returns nonzero. An
[offline reassessment](../evaluation/assets/matrix-development/comparison-reassessment.v2.json)
retains four compatible builtin-to-stateless/cold comparisons and rejects the two
cold-to-reuse comparisons. The eight original run reports, six historical
comparison artifacts and `measurements.v2.json` remain unchanged; their original
hashes and outcomes are retained. Reassessment ran no selectors or preparation
and changed no cases, references or budgets.

The same original implementation/protocol digests apply to every measured arm.
Observations under concurrent development load are diagnostic, not a hardware
performance claim.

| Arm | Service Hit3 | Primary-only Hit3 | Preparation | HTTP primary outcome |
| --- | --- | --- | --- | --- |
| Builtin lexical | 63/116 | 63/116 | not applicable | in process |
| Stateless HTTP lexical | 63/116 | 0/116 | not applicable | all 128 attempts per track timed out |
| Indexed HTTP cold | 63/116 | 0/116 | ready: 2522.35 ms / 2723.62 ms | all 128 attempts per track timed out |
| Indexed HTTP immediate reuse | 0/116 | 0/116 | timeout: 2003.54 ms / 2003.32 ms | all 128 slots per track not_run |

The service successes for stateless/cold HTTP came from the real lexical floor.
Successful index setup did not make these full-catalog HTTP queries fit 150 ms.
The immediate reuse preparation failures are preserved without retrying them into
success; their future query latency/costs remain unknown. Controlled integration
tests separately verify a genuinely ready cold/reused index on a small catalog.
These examples establish the team integration path and expose a performance
limitation; they do not establish multilingual semantic quality.

The builtin's 128 decisions were choice_required, with 66 supported top-three
shortlists, 48 top-one positives, zero confident decisions and zero recorded user
choices. The Chinese-to-original-English memory-leak case still missed. Cases and
acceptable targets were not tuned after these results. Per-language/kind counts,
latency/cost coverage, setup transcripts and report hashes are in
[measurements.v2.json](../evaluation/assets/matrix-development/measurements.v2.json).

## Let the user choose when confidence is low

`choice_required` shows the gateway's ranked shortlist; it does not make an
automatic confident commitment. Choice coverage and interaction frequency are
reported separately from confident precision. To inspect a real recorded shortlist:

```bash
python scripts/source_asset_benchmark.py choose \
  --report /var/tmp/rm-g1-source-guide/builtin-service.json \
  --case capability-01-zh > /var/tmp/rm-g1-source-guide/choice.json
```

The command displays up to three actual candidates, names/descriptions and unknown
availability on stderr. Enter an option number, or `0` to decline; Ctrl-C
interrupts. The command emits a new JSON receipt on stdout, saved above by
redirection. It does not persist the choice into the benchmark report, send
backend feedback or execute an action (`action_executed=false`). No choices are
supplied in the recorded benchmark runs; their user-choice counts remain zero.
An abstention, timeout or unavailable index never fabricates a shortlist.


## Native development tooling

A separately qualified hybrid uses the same source input and complete catalog,
production service and explicit local-cold preparation described in the
[formal tooling guide](ASSET_BENCHMARK.md#local-native-preparation-and-runtime-evidence).
Supply that frozen preparation table through the existing
`run --preparation-config <path>` option. This does not change the query budget,
add a qualification flag or supply a currently approved production artifact.
The registry is currently empty; the native installation and actual 412-asset/128-case
run remain separate from the existing lexical/HTTP examples above.

New internal reports retain exact semantic preimages, environment facts and the
runner/protocol inventories alongside their digests. Comparison verifies these
and the actual native query/setup observations without opening model paths or
loading weights. Missing configuration evidence or a changed runtime build is
incompatible. Native failure and gateway-floor success remain separate attempts;
setup costs stay separate from query costs. These source comparisons are
provisional quality diagnostics with raw timing/cost evidence, not a performance
superiority or formal acceptance verdict. Historical stateless reports remain
historical artifacts rather than acquiring invented runtime evidence.
