# Benchmarking skill and tool recommendation selectors

[English setup](asset-selection.md) · [简体中文入口](asset-selection.zh-CN.md) ·
[Source-backed development tutorial](SOURCE_BENCHMARK.md) ·
[Privacy and recovery](asset-privacy-security.md)

For the actual 412-asset Matrix/English-skill/MCP development catalog and 128 frozen
Chinese, English and mixed cases, start with [SOURCE_BENCHMARK.md](SOURCE_BENCHMARK.md).
It provides tested acquisition/build commands, the same v2 team integration and
index readiness, builtin/external runs on one full catalog, and comparisons.
The formal/fixture protocol walkthrough below remains a separate profile.


The retrieval benchmark implements RM35/RM40, AC26/AC31 and the exports in
specification §12.6.2. It uses the production v2 settings parser,
`RecommendationService`, HTTP transport, coordinator and admission. A disposable
SQLite database and an explicitly injected locked metadata catalog supply the
fixture run. This provider supplies no host authority, tool execution or claim
that an imported asset is installed or available.

The checked-in example has one synthetic skill and one synthetic English case.
Its fixture rights, people and judgments are infrastructure data. It is not a
200-skill development corpus, a 300-asset acceptance corpus, real annotation,
source qualification, a multilingual result or native platform evidence.
The HTTP selector is the existing deterministic `example_lexical` v2 server;
its HTTP server uses the Python standard library and its selection boundary uses
the installed production validators. It never calls a model or executes tools.
No download or package installation occurs in the commands below.

Use a Python 3.12+ environment with this project's dependencies, from the repository
root. For a source-checkout demonstration, explicitly select this checkout's Python
package before running the commands. This avoids measuring an unrelated editable
installation in a shared environment:

```bash
export PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}"
```

## The skill/tool v2 boundary

A team supplies a loopback HTTP server accepting `POST /select`. Configure its
exact selector name/version and URL in the normal production configuration;
the benchmark uses that parser and transport. Both kinds use one request:
[SelectionRequestV2](../src/relaymind/selection/schemas/selection_request.v2.json)
and [SelectionResultV2](../src/relaymind/selection/schemas/selection_result.v2.json).
Offered records use [AssetRecordV1](../src/relaymind/store/schemas/asset_record.v1.json).
A server receives every eligible offered record, not the expected target.

For example, this is the recommendation table in the supplied full production
fixture configuration (keep its other required launcher/source settings):

```toml
[recommendations]
enabled = true
user_id = "fixture-user"
workspace_id = "fixture-workspace"
primary = "http"
name = "example_lexical"
version = "1"
url = "http://127.0.0.1:8913/select"
fallback = false
```

Change name/version to the exact identity your team server emits. Use
`fallback=false` for `primary_only` algorithm comparison; an explicitly separate
`service` track may enable the gateway floor under the shared production policy.

This executable example writes a complete request with one skill and one MCP
tool. These two records are synthetic protocol examples, separate from the
locked benchmark fixture. Hashes commit the exact synthetic originals;
`schema_hash` is the canonical digest of the tool's input schema. A candidate
must echo its offered `asset_id`, `kind`, `content_hash` and `source_version`.
Tool names and schemas are data: a recommendation never calls the tool.

```bash
python - <<'PYEXAMPLE'
import hashlib, json
from pathlib import Path
from relaymind.canonical import canonical_digest
from relaymind.selection.asset_contract import parse_asset_request

root = Path('/var/tmp/relaymind-asset-demo')
root.mkdir(parents=True, exist_ok=True)
schema = {'type': 'object', 'properties': {'city': {'type': 'string'}},
          'required': ['city'], 'additionalProperties': False}
def record(asset_id, kind, original, description, identity):
    return dict(schema_version=1, asset_id=asset_id, kind=kind,
        source_id='protocol-demo', source_locator='fixture:' + asset_id,
        source_version='1', captured_at='2026-10-30T00:00:00Z',
        content_hash=hashlib.sha256(original.encode()).hexdigest(),
        description=description, language_tags=['en'], dependencies=[],
        availability='unknown', stale=False, identity=identity)
request = dict(schema_version=2,
    recommendation_id='00000000-0000-4000-8000-000000000001',
    task_id='protocol-calendar', task_text='Inspect my calendar', conversation=[],
    scope={'user_id': 'fixture-user', 'workspace_id': 'fixture-workspace'},
    permissions=[], requested_kinds=['mcp_tool', 'skill'], selection_scope='discovery',
    catalog_revision=1, host_snapshot_revision=None, language_tags=['en'],
    code_switched=False, deadline_ms=200, candidate_limit=10,
    catalog=[record('demo:skill:calendar', 'skill', 'calendar skill original',
        'Inspect a calendar', {'package_id': 'calendar', 'package_version': '1'}),
        record('demo:tool:weather', 'mcp_tool', 'weather tool metadata original',
        'Read weather for a city', {'server_id': 'weather', 'tool_name': 'lookup',
        'exposed_name': 'weather.lookup', 'input_schema': schema,
        'schema_hash': canonical_digest(schema)})])
validated = parse_asset_request(request).to_json_object()
(root / 'request.json').write_text(json.dumps(validated, ensure_ascii=False), encoding="utf-8", newline="\n")
print(json.dumps(validated, ensure_ascii=False, indent=2))
PYEXAMPLE
```

With the example server running on port 8913 (startup command below), obtain and
validate its actual response:

```bash
curl --fail --silent --show-error http://127.0.0.1:8913/select \
  -H 'Content-Type: application/json' -H 'Accept: application/json' \
  --data-binary @/var/tmp/relaymind-asset-demo/request.json \
  > /var/tmp/relaymind-asset-demo/response.json
python - <<'PYEXAMPLE'
import json
from pathlib import Path
from relaymind.selection.asset_contract import parse_asset_result
result = parse_asset_result(json.loads(
    Path('/var/tmp/relaymind-asset-demo/response.json').read_text(encoding="utf-8"))).to_json_object()
assert result['selector'] == {'name': 'example_lexical', 'version': '1'}
assert result['outcome'] == 'ranked' and result['decision'] == 'choice_required'
assert result['candidates'][0]['asset_id'] == 'demo:skill:calendar'
print(json.dumps(result, ensure_ascii=False, indent=2))
PYEXAMPLE
```

The response includes the echoed recommendation/catalog/host revisions,
`selector`, `mechanism`, ordered candidates with scores and JSON-Pointer
`evidence_refs`, `index_revision`, actual server `elapsed_ms`, and nullable
`usage_claims`. Scores are algorithm scores, not calibrated probabilities.
The gateway separately verifies identity, offer membership, exact hashes and
current eligibility; successful JSON validation alone does not admit a result.

## Built-in, team A and team B on the same fixture

Use an environment containing the project's declared dependencies. `python`
below means that environment's Python 3.12+ interpreter. Start from the repository
root. The checked-in fixture records Unicode 16.0.0. The explicit authoring
step below binds a new disposable provisional fixture copy to the interpreter's
actual Unicode table (for example Python 3.12 uses 15.0.0). It changes that copy's
lock identity, never its original-byte hashes or genuine/frozen corpus bindings.
Run and compare never repair or rewrite an incompatible lock.

An installed-wheel verification omits that override and uses the installed
candidate; the runner refuses source-inventory mismatches. Building/installing
that candidate is an explicit development step, never something the benchmark
performs automatically.

Stage the fixture in one disposable directory. All three arms keep the same
lock, catalog, cases, selected split, seed, repetitions and budgets:

```bash
mkdir -p /var/tmp/relaymind-asset-demo
cp -R tests/fixtures/asset-benchmark/. /var/tmp/relaymind-asset-demo/
```

Create two team configs from the supplied HTTP config. Each has its own explicit
endpoint; selector names/versions must match the server's actual responses. Here
two instances of the existing example stand in for teams A/B. Replace those
servers and their configured identities with your algorithms for a real team
comparison; two copies of the example do not establish independent quality.

```bash
python - <<'PYEXAMPLE'
from pathlib import Path
root = Path('/var/tmp/relaymind-asset-demo')
for name, port in (('team-a', 8913), ('team-b', 8914)):
    config = (root / 'third-party.toml').read_text(encoding="utf-8").replace(
        'third-party-production.toml', name + '-production.toml')
    production = (root / 'third-party-production.toml').read_text(encoding="utf-8").replace(
        '127.0.0.1:8913/select', f'127.0.0.1:{port}/select')
    (root / (name + '.toml')).write_text(config, encoding="utf-8", newline="\n")
    (root / (name + '-production.toml')).write_text(production, encoding="utf-8", newline="\n")
PYEXAMPLE
```

Author settings/environment digests on the device that will run the benchmark.
This explicit helper refuses a genuine development or acceptance corpus. It
explicitly reauthors only the provisional fixture Unicode binding, expands
production parser defaults, commits the full implementation/schema
inventories and collects actual device/dependency facts. `run` never rewrites
settings or increases budgets. Refresh after any implementation or schema edit.

```bash
python - <<'PY'
from pathlib import Path
from relaymind.evaluation.asset_benchmark import refresh_fixture_benchmark_config
root = Path('/var/tmp/relaymind-asset-demo')
for name in ('builtin', 'team-a', 'team-b'):
    refresh_fixture_benchmark_config(root / (name + '.toml'), reauthor_fixture_unicode=True)
PY
```

Start each HTTP example in a separate terminal:

```bash
python scripts/example_asset_selector.py --port 8913  # team A
# In another terminal:
python scripts/example_asset_selector.py --port 8914  # team B
```

Run all three arms. No arm has a lexical fallback or adaptation; the HTTP arm
receives the full same ordered catalog and task/permission envelope. No labels,
private skill bodies, target shortlist or hidden rerank hints enter that offer.

```bash
python scripts/asset_selection_benchmark.py run \
  --config /var/tmp/relaymind-asset-demo/builtin.toml \
  --output /var/tmp/relaymind-asset-demo/builtin.json
python scripts/asset_selection_benchmark.py run \
  --config /var/tmp/relaymind-asset-demo/team-a.toml \
  --output /var/tmp/relaymind-asset-demo/team-a.json
python scripts/asset_selection_benchmark.py run \
  --config /var/tmp/relaymind-asset-demo/team-b.toml \
  --output /var/tmp/relaymind-asset-demo/team-b.json
```

Each report has a companion `<report>.inputs.json`, an explicit
`<report>.resolver.json` and a private `<report>.private/` directory. Private
source proof, restricted documents/evidence and exact implementation source
bytes stay under that private directory. Public exports contain commitments and
null locators for restricted inputs. Preserve the private bundle within the
custodian's authorized storage; possession of a hash never grants source access.
Do not publish source-proof files or genuine held-out labels.

The arms may have the same input ID and different committed hashes.
Combine resolver entries by their complete `(id, hash_kind, hash)` identity:

```bash
python - <<'PY'
import json
from pathlib import Path
root = Path('/var/tmp/relaymind-asset-demo')
entries = {}
for name in ('builtin', 'team-a', 'team-b'):
    value = json.loads((root / (name + '.json.resolver.json')).read_text(encoding="utf-8"))
    for entry in value['entries']:
        key = (entry['id'], entry['hash_kind'], entry['hash'])
        if key in entries and entries[key]['bytes'] != entry['bytes']:
            raise ValueError('conflicting resolver byte commitment')
        entries[key] = entry
(root / 'resolver.json').write_text(json.dumps({
    'schema_version': 1, 'entries': [entries[k] for k in sorted(entries)]
}), encoding="utf-8", newline="\n")
PY
python scripts/asset_selection_benchmark.py compare \
  /var/tmp/relaymind-asset-demo/builtin.json \
  /var/tmp/relaymind-asset-demo/team-a.json \
  --resolver /var/tmp/relaymind-asset-demo/resolver.json \
  --quality-only \
  --output /var/tmp/relaymind-asset-demo/comparison.json \
  --markdown /var/tmp/relaymind-asset-demo/comparison.md
python scripts/asset_selection_benchmark.py compare \
  /var/tmp/relaymind-asset-demo/team-a.json \
  /var/tmp/relaymind-asset-demo/team-b.json \
  --resolver /var/tmp/relaymind-asset-demo/resolver.json --quality-only \
  --output /var/tmp/relaymind-asset-demo/teams.json \
  --markdown /var/tmp/relaymind-asset-demo/teams.md
```

`--quality-only` explicitly allows different machine facts while retaining
shared Python/dependencies/runtime/protocol equality. It labels every latency,
resource and cost value **not comparable** and makes no superiority statement.
The example's GPU observation can remain unknown, so ordinary performance
comparison correctly refuses it. This fixture comparison demonstrates protocol
replacement only; its quality gates cannot establish real acceptance.

Removing `--resolver` exercises the missing-evidence refusal. A structurally valid
incompatible comparison writes JSON and Markdown with fixed field reasons and
returns exit code 2. Malformed/non-document input produces a fixed diagnostic and
exit code 2. Compare never searches the current directory or checkout, follows a
URL, installs a dependency, or accepts saved grades instead of inputs.
`--inputs-a PATH` and `--inputs-b PATH` explicitly relocate each committed
companion manifest; their digest and exact raw byte count still have to match.

## Reading the result

Warm-ups and every measured slot remain in the report, including failed,
cancelled and unstarted work. SIGINT/SIGTERM asks the runner to retain the active
slot and mark every remaining slot `not_run`; it never silently reruns a subset.
Only measured repetition zero supplies independent-case quality denominators and
Wilson intervals. Later repetitions supply workload, stability, latency and cost
evidence. Warm-ups contribute to neither measured totals nor quality counts.

Primary and service summaries are distinct. A successful floor cannot repair a
primary miss. Invalid/stale raw selector results remain in attempts and never
become an admitted result. An admitted `no_match` is terminal, and a valid initial
empty catalog retains revision zero with no selector attempt. An unavailable
predispatch catalog records no invented call or zero cost.

Costs preserve observed and reported provenance separately. Missing usage is
null, complete totals stay null when any owned component is unknown, and known
subtotals remain visible as partial. Inclusive attempt costs never add embedding
work a second time. CPU/peak observations are not inferred from wall-clock time.
Retrieval costs do not establish whole-task cost or a cost superiority claim.

Different selector/model/index identities are intended experiment variables.
Changing corpus locks, cases, budget, scope/permissions, source policy,
eligibility, seed/order, repetitions/warm-ups, fallback/adaptation, shared runner
or protocol is incompatible. The comparator resolves and revalidates original
inputs, source proof and code inventories, then recomputes grades and summaries.
Markdown consumes the self-contained comparison JSON alone.

For genuine development/acceptance work, supply the locked original corpus and
actual independent custodian/source policy review. The fixture authoring helper
and metadata injection never provide those reviews, rights, hardware or host
qualifications. A held-out/source proof that cannot be verified remains blocked.
Real hybrid/adaptation activation and whole-task/platform gates retain their
separate normative qualification requirements.

For library callers, `SourcePolicyReview` is a trusted custodian assertion scoped
to the exact effective-config digest and original source-proof SHA-256, with
resolved review records and the named reviewer. It is never parsed from report
JSON or enabled by a CLI flag. `load_asset_benchmark_report(...,
source_policy_review=review)` can consume that assertion after actual review;
unreviewed genuine source-policy evidence is refused even in quality-only mode.
Fixture protocol comparisons remain visibly fixture and supply no such review.
Related releases must be staged under the selected lock's root at their exact
bounded declared paths; the runner follows only that explicit closure and the
shared corpus loader validates every link. External/private captures stay under
the custodian's separate access control and need an explicit permitted staging
or resolver binding.

Each selected split must use one common locked requested-kind envelope; the
runner derives it from every included case and refuses heterogeneous envelopes
before dispatch. Single-kind diagnostics preserve that exact envelope and cannot
claim the full two-kind discovery gate. Author separate diagnostic splits before
freeze rather than selecting a favorable subset at run time. Summary catalog
size is the actual eligible offered size; all acceptable labels remain in their
original quality denominators even when an asset is excluded by policy.

Offline comparison verifies the complete Python/schema inventory against the
actually imported benchmark package, together with pinned, reviewed entry-point,
project-configuration and specification identities. Supplied source bytes are
still resolved only through the committed inputs/resolver. The running package's
own resources establish implementation identity; they are never a fallback
location for missing input evidence. An omitted member or an unknown generator
is incompatible, even when both arms supply matching rehashed claims.

Lexical/HTTP gateway primaries do not use a local selector embedding runtime.
Pinned hybrid and disabled adaptation preimages use the same production semantic
validators offline; the comparator never opens their embedded paths, loads a
model or imports a native runtime. Every supplied preimage must belong to the
exact configuration closure. Dangling references, unsupported schemas and
inconsistent runtime/artifact bindings are refused in both comparison modes.
A valid hash or a JSON version number does not activate a runtime or an artifact.

Expected production admission failures produce fixed incompatible JSON/Markdown
artifacts. Usable attempts must retain the actual coordinator mechanism, reported
metadata, raw usage claim, terminal producer and admission revision. Rejected raw
results remain visible. Duplicate cost provenance cannot stand in for another
attempt's missing observation; complete coverage is established per owned
attempt, with observed and reported components kept separate.

Summary/paired records use the published dimensions tuple order, with numeric
catalog size and stale ordered null, false, true. Named ID sets and permissions
are UTF-8 sorted before hashing; prompt/candidate/attempt order is preserved.


## Decisions, languages and reference profiles

A low-confidence supported shortlist is `outcome=ranked` with
`decision=choice_required`: show the candidates and let the user choose,
none-of-these or cancel. `confident` is the selector's recorded decision, never
execution authority. `no_match` is a valid abstention with no candidates and is
preserved without fallback. `not_ready` and `unavailable` have null decisions
and no candidates; they are failed primary attempts, eligible for the configured
gateway floor under the remaining deadline. The current deterministic lexical
example always asks for choice when it finds overlap; it never claims calibrated
confidence. Benchmarking the primary separately prevents a successful gateway
floor from concealing a failed or unready team algorithm.

The formal report's `summaries[].dimensions` include language lane, kind,
availability, stale state, scope, offered catalog size, primary/service retrieval
mode and hard subset. Read `hit3`, confident precision/coverage, false confidence,
ambiguous choice and negative abstention with their actual denominators and
intervals. Formal lanes include en→en, zh-Hans→zh-Hans, zh-Hans→en,
en→zh-Hans, code-switched and the separate zh-Hant diagnostic. A one-English-case
fixture supplies no evidence for the absent lanes, tools or larger catalogs.

For larger mixed skill/tool catalogs and source-grounded language diagnostics,
author one shared development catalog and its closed `AssetDiagnosticV1` cases
under specification §12.6.3. Preserve original languages, source/version/hash,
reference method and acceptable targets before comparing algorithms; include
available language-pair/kind cohorts and explicit missing coverage. This internal
profile uses author-defined source-backed references, diagnostic top-1/Hit@3,
abstention and false confidence, without inventing human annotators or formal
confidence intervals. The working [source workflow](SOURCE_BENCHMARK.md) provides
acquire/build/run/compare/choose; its comparison output is internal JSON only.
The formal `run` command above expects the §12.6.1 four-document lock, not a
development diagnostic file; formal compare writes JSON and Markdown. Matrix is
one source, not a fixed required count or an installed
library allowlist. Original English assets and actual upstream MCP definitions
may supplement it; translated replacements do not become original English assets.

The independently qualified profile uses the frozen catalog/cases/labels/lock
closure, actual independent judgments and unchanged acceptance minima. Neither
fixture execution nor author references qualify those gates. All teams must use
the same catalog/cases and policy envelope within the chosen profile; preparing a
target-only catalog or changing cases for each algorithm defeats replacement
comparison.

## Index readiness and setup cost

Three facts are distinct: gateway lexical readiness, the team's index readiness,
and the outcome of an actual query. A successful fallback proves only the final
service result. The existing example rebuilds its in-memory lexical index inside
each POST, so its observed query duration includes that build. Without its explicit indexed-mode option the example exposes no preparation
endpoint. Do not call a healthy socket or an absent build observation a ready
index or a zero-cost build.

The optional companion runs explicitly before query warm-ups. Existing stateless
configurations omit `[preparation]` and send no lifecycle traffic; their report
says `not_applicable` with null setup time. Indexed configurations bind selector
identity, exact ordered offered catalog digest/revision, scope, permissions and
requested kinds. A different scope or stale index cannot reuse that readiness.

Start two indexed example servers in separate terminals:

```bash
python scripts/example_asset_selector.py --port 8923 --preparation-path /index
# In another terminal:
python scripts/example_asset_selector.py --port 8924 --preparation-path /index
```

Using the staged files from above, create explicit cold/reuse configs for both
teams. Production query settings remain ordinary v2; `[preparation]` belongs to
the benchmark config only. Both teams get exactly the same catalog/cases and
preparation budgets, with endpoint/algorithm as the intended variable.

```bash
python - <<'PYINDEX'
from pathlib import Path
from relaymind.evaluation.asset_benchmark import refresh_fixture_benchmark_config
root = Path('/var/tmp/relaymind-asset-demo')
for team, old_port, port in (('team-a', 8913, 8923), ('team-b', 8914, 8924)):
    for profile in ('cold', 'reuse'):
        name = team + '-' + profile
        production = (root / (team + '-production.toml')).read_text(encoding="utf-8").replace(
            f'127.0.0.1:{old_port}/select', f'127.0.0.1:{port}/select')
        (root / (name + '-production.toml')).write_text(production, encoding="utf-8", newline="\n")
        config = (root / (team + '.toml')).read_text(encoding="utf-8").replace(
            team + '-production.toml', name + '-production.toml')
        config += ('\n[preparation]\nmode = "http"\n'
            + f'url = "http://127.0.0.1:{port}/index"\nprofile = "{profile}"\n'
            + 'total_ms = 60000\nrequest_ms = 2000\npoll_ms = 250\nmax_polls = 240\n')
        (root / (name + '.toml')).write_text(config, encoding="utf-8", newline="\n")
        refresh_fixture_benchmark_config(root / (name + '.toml'), reauthor_fixture_unicode=True)
PYINDEX
python scripts/asset_selection_benchmark.py prepare \
  --config /var/tmp/relaymind-asset-demo/team-a-cold.toml \
  --output /var/tmp/relaymind-asset-demo/team-a-preparation.json
python scripts/asset_selection_benchmark.py run \
  --config /var/tmp/relaymind-asset-demo/team-a-cold.toml \
  --output /var/tmp/relaymind-asset-demo/team-a-cold.json
python scripts/asset_selection_benchmark.py run \
  --config /var/tmp/relaymind-asset-demo/team-b-cold.toml \
  --output /var/tmp/relaymind-asset-demo/team-b-cold.json
python scripts/asset_selection_benchmark.py run \
  --config /var/tmp/relaymind-asset-demo/team-a-reuse.toml \
  --output /var/tmp/relaymind-asset-demo/team-a-reuse.json
python scripts/asset_selection_benchmark.py run \
  --config /var/tmp/relaymind-asset-demo/team-b-reuse.toml \
  --output /var/tmp/relaymind-asset-demo/team-b-reuse.json
```

`prepare` writes the bounded preparation transcript without querying a case.
`run` independently prepares once before its warm-ups; running a standalone cold
preparation followed by a cold run deliberately performs two fresh preparations.
The later reuse run may reuse the exact compatible ready index and records the
server's actual `reused` value. Cold does not delete another caller's active
index. Reuse does not claim that the original build cost was zero or amortized.

Merge the new resolver entries by the same complete identity triple used above,
then compare cold with cold and reuse with reuse:

```bash
python - <<'PYINDEX'
import json
from pathlib import Path
root = Path('/var/tmp/relaymind-asset-demo')
entries = {}
for name in ('builtin', 'team-a', 'team-b', 'team-a-cold', 'team-b-cold',
             'team-a-reuse', 'team-b-reuse'):
    for entry in json.loads((root / (name + '.json.resolver.json')).read_text(encoding="utf-8"))['entries']:
        key = (entry['id'], entry['hash_kind'], entry['hash'])
        if key in entries and entries[key]['bytes'] != entry['bytes']:
            raise ValueError('conflicting resolver byte commitment')
        entries[key] = entry
(root / 'resolver.json').write_text(json.dumps({
    'schema_version': 1, 'entries': [entries[k] for k in sorted(entries)]}), encoding="utf-8", newline="\n")
PYINDEX
python scripts/asset_selection_benchmark.py compare \
  /var/tmp/relaymind-asset-demo/team-a-cold.json \
  /var/tmp/relaymind-asset-demo/team-b-cold.json \
  --resolver /var/tmp/relaymind-asset-demo/resolver.json --quality-only \
  --output /var/tmp/relaymind-asset-demo/cold-comparison.json \
  --markdown /var/tmp/relaymind-asset-demo/cold-comparison.md
python scripts/asset_selection_benchmark.py compare \
  /var/tmp/relaymind-asset-demo/team-a-reuse.json \
  /var/tmp/relaymind-asset-demo/team-b-reuse.json \
  --resolver /var/tmp/relaymind-asset-demo/resolver.json --quality-only \
  --output /var/tmp/relaymind-asset-demo/reuse-comparison.json \
  --markdown /var/tmp/relaymind-asset-demo/reuse-comparison.md
```

Comparison JSON/Markdown keeps setup state/profile/reuse, observed preparation
wall time, cleanup and server-reported cumulative costs separate from query
latency/cost and primary/floor outcomes. Unknown CPU/token/memory costs stay null.
Poll claims are cumulative, so never sum them. Reported server build time is not
client-observed wall time. A failed/unready/stale preparation retains its
transcript and every planned query slot as `not_run`, with a nonzero exit. The
example supports bounded status/cancellation; closing an HTTP connection alone is
not proof that a build was cancelled.

Cold-versus-reuse comparison is incompatible in both modes; inspect those reports
separately to understand startup. Stateless-versus-indexed comparisons are allowed
with explicit `not_applicable` versus observed setup, never missing-as-zero.
`--quality-only` still makes latency/resource/cost values not comparable. These
synthetic examples validate protocol replacement, not multilingual usefulness or
independent qualification. The v2 query/result wire remains unchanged.


## Local native preparation and runtime evidence

The benchmark tooling supports a separately qualified configured hybrid through
the production service and its single index worker. The production artifact
registry includes an exact reviewed Linux CPU bundle with a fresh ordinary
launch record; see the model status below for its strict runtime/device scope. The lexical/HTTP examples above remain
usable without a native model. See [model status](../models/README.md#local-multilingual-embedding-development-candidate).

For a qualified hybrid, add this table to the benchmark TOML before freezing its
inputs (the production file separately supplies `[asset_hybrid]` manifest/config
paths). Local setup is cold only; omit a URL. HTTP polling settings retain their
fixed, inapplicable defaults:

```toml
[preparation]
mode = "local"
profile = "cold"
total_ms = 60000
request_ms = 2000
poll_ms = 250
max_polls = 240
```

Preparation occurs once before all query slots. The report preserves actual job
events, semantic readiness, decision time, owned cleanup and available model costs.
A partial usable index retains missing coverage and failed jobs; it is not full
coverage. Failed or cancelled required setup retains every planned slot as
`not_run`. A genuinely empty offer records `not_applicable`, its actual empty
binding/readiness, and no invented model call. Query budgets stay unchanged.

Current comparisons carry a closed runtime display for each arm. Verified native
use can compare with verified local-runtime non-use; unknown is incompatible.
Two native arms must share backend/version/build/device and execution controls;
model/artifact and semantic index choices remain the declared variables.
`--quality-only` relaxes machine fields alone and labels all raw setup, query,
resource and cost quantities not comparable. Its Markdown renderer uses only the
comparison object. Unknown CPU/memory and incomplete costs remain unknown;
setup inference is never added to query inference a second time. Historical
comparison objects retain their original closed shape and are not upgraded.

## Whole-host trials, adaptation episodes and platform exports (spec 0.60/0.64/0.65 section 12.6.5)

These offline tools serve #198 (early installed-skill trial), #202 (adaptation
episodes), #204 (host MCP-use rows), #205 (paired whole-host tasks) and #206
(platform lifecycle); RM16, RM38–RM44; AC29–AC37. They check frozen inputs,
plan runs deterministically, drive adaptation episodes through the real
recommendation lifecycle, and grade the exported reports. They never run a
host, grant an approval or execute a tool: for host trials the host executor is
supplied by the caller, and only the host executes tools. Every summary is
recomputed from the verified rows; a stored summary that differs is rejected.
A Simplified Chinese version of this section is in
[asset-selection.zh-CN.md](asset-selection.zh-CN.md#整任务试验适配回合与平台导出).

| Command | What it does |
| --- | --- |
| `python scripts/asset_whole_task.py plan-host --tasks T --freeze F --output P` | Verifies `AssetHostTasksV1` against its `HostFreezeV1` and writes the three-arm plan: tasks in frozen UTF-8 order, arms ordered by SHA-256 of `{seed,task_id,arm}`, with the preregistered `input_digest` and `freeze_digest` |
| `python scripts/asset_whole_task.py plan-episodes --episodes E --freeze F --output P` | Verifies `AssetEpisodesV1` against its `EpisodeFreezeV1` and writes `input_digest`, `config_digest` and, per episode, the chronological corrections, the paired probes in `(kind,case_id)` order with ordinals from one, and the reset probes |
| `python scripts/asset_whole_task.py grade-host --report R --inputs M [--resolver X] --output O` | Resolves the companion input manifest, then verifies and regrades an `AssetHostReportV1` |
| `python scripts/asset_whole_task.py grade-platform --report R --inputs M [--resolver X] --output O` | The same for an `AssetPlatformReportV1` (eleven lifecycle steps in fixed order) |
| `python scripts/asset_whole_task.py grade-adaptation --report R --inputs M [--resolver X] --output O` | Verifies and regrades an `AssetAdaptationReportV1`; probe labels come from the verified corpus in the manifest |

Exit codes: `0` the gate passed or is `not_applicable` (a fixture platform
report, or an adaptation report that is not qualified); `1` a known gate
failure; `2` the report or an input was rejected (a digest, identity, order,
binding or arithmetic contradiction); `3` blocked (a required document,
configuration, corpus document, inventory preimage or evidence is missing).
Blocked is never reported as a pass. A grading command's output file always
records `status`, `gate` and a fixed safe `reason`; `plan-host` and
`plan-episodes` write only the plan, and on rejection it writes nothing and prints the reason to standard
error. A graded result also lists `unverified`: for host and platform reports
`configuration_semantics` (the pinned native host configurations are bound by
digest but not parsed) and `qualified_evidence_review`; for adaptation reports
`qualified_evidence_review`, plus `adaptation_config_semantics` unless both
arms reference the frozen section 14.5.2 configuration, in which case it is
parsed and verified with the rest of their reference closure.

**Inputs.** The manifest is section 12.6.2's `InputManifestV1`, with document
kinds extended (only here) by `host_tasks`, `host_freeze`, `episodes` and
`episode_freeze`. It must hold exactly one `environment_lock`,
`runner_inventory` and `protocol_inventory`; the root input named by the
report's `input_digest`; its freeze; every configuration digest the freeze
names, as `referenced_config:<digest>` (strict JSON or TOML, 4 MiB); and the
frozen corpus as `lock:`, `catalog:`, `cases:` and `labels:` documents with
their evidence. The corpus is materialized only in a private temporary
directory and verified by the unchanged release verifier (related locks are
not yet supported and block). Host tasks then bind to their case's lane and
families and to the exact catalog asset bytes; episodes bind to existing cases,
and a correction target present in the catalog must match its exact bytes (a
missing-pool target is retained, never replaced). Report, root and freeze
shapes are checked before any reference is followed. Inventory preimages
resolve only through an explicit `--resolver`; the runner inventory is the
whole `src/relaymind` Python package plus `pyproject.toml` and
`scripts/asset_whole_task.py`, whose package members must equal the running
grader and whose other files must match reviewed bytes.

**Qualification assessment (spec 0.65 sections 12.6.2 and 12.6.5; #240).** A
graded result also records `qualification`: the report's `claimed` label, the
`assessed` tier (`fixture`, `provisional` or `qualified`; `qualified` stands
for `measured` on a platform report) and its fixed safe `reasons`. The tier is
assessed from the resolved inputs, never from the label: a fixture corpus
(`fixture_corpus`) or any report evidence with fixture provenance
(`fixture_evidence`) makes it `fixture`; an unqualified corpus release
(`corpus_unqualified`, from the section 12.6.1 assessment), artifact identities
of unknown provenance (`artifact_provenance_unknown`), a platform other than
Apple Silicon macOS or native x86-64 Windows (`platform_not_target`) and every
`unverified` entry make it `provisional`. A report labelled `provisional`,
`qualified` or `measured` over fixture inputs is rejected
(`qualification_contradiction`); no gate passes unless the tier is
`qualified`, and a known failure still fails. Without a reviewed record,
`qualified_evidence_review` is always listed; observed provenance labels
cannot change that.

**Owner-run qualification records (spec 0.66 section 12.6.6; #251).** The
`grade-*` commands accept `--qualification-record PATH`, a closed
`QualificationRecordV1` (`asset_qualification_record.v1.json`) committed in
`docs/evidence/m10/`. It binds by SHA-256 the report, freeze, tasks or
episodes, labels, captures and media it covers (paths relative to the record),
names the machine (`target_macos_m1_8gib`, `target_windows_x64` or
`linux_host`; OS build, the tooling's `os_version`, CPU, RAM and a hostname digest), the repository commit
and harness/adapter revisions, and the preregistration commit. It is approved
by a pull request review by at least one organization member other than the
owner; the PR number, reviewer logins and approval time are added after the
merge. Only an approved record that binds the report's exact bytes removes
`qualified_evidence_review`. It rejects (exit 2) on a changed byte, another
report or scope (`host_trial` only for an `early_trial` task document,
`host_acceptance` only for `acceptance`), a freeze/tasks/episodes/labels
subject that is not the resolved input, a platform field (OS, architecture,
`os_version`, CPU, RAM) that contradicts the report (a null report field leaves
`machine_identity_unverified`), or labelling, people or agreement that differ
from the frozen labels. Its `labelling` (`owner_machine` or
`independent`) is reported next to the tier. A fixture input or any other
unverified input still keeps the report below `qualified`. The label commands
take `--labelling owner_machine` for the owner plus one recorded machine
annotator (`owner_machine.v1`; the owner may also be the tuner, the machine
annotator holds the annotator role only, the owner adjudicates every case and
every disagreement needs a nonempty reason; only the owner custodian may
also tune). A platform record may carry the corpus review, and any freeze,
tasks or labels it binds must be the resolved inputs. Every Time field must be
a real UTC instant (`invalid_record_time`). Under spec 12.6.6 an approved
`negative` record with scope `host_operation` closes the `skill_loaded_use`
or `mcp_use` gate with the operation disabled; `known_host_limitations`
returns those records for the roadmap, docs and reports to list. No approved
negative record exists yet, so none is listed. The module is
`relaymind.evaluation.asset_qualification_record`.

**Native configuration validation (spec 0.65; #240).** Host, provider, sampling
and budget configuration documents stay unverified and cannot establish qualified
acceptance until the pinned native validation and applicable schema/extraction
evidence required by section 12.6.5 are supplied and verified.

**Reset evidence (spec 0.64).** The registry entry `reset:<episode_id>` holds,
as its original bytes, the closed canonical JSON object
`{schema_version:1,episode_id,baseline,after}` with the two reset semantic
arrays; non-canonical bytes or duplicate keys reject. The grader verifies each claimed reset with it;
a missing document leaves the reset uncredited and blocks unless a structural
contradiction or known gate failure is reported first.

**Adaptation episode driver.** `relaymind.evaluation.asset_episode_driver`
runs each frozen episode in-process against the actual recommendation service
over the locked corpus projection, with disposable stores per arm:
`locked_episode_arms(release, {"off": off, "on": on}, settings, work_dir,
adaptation_stage=...)` builds the arms and `run_adaptation_episodes(...)`
returns the report and its reset evidence. The on arm records the reset
baselines, applies the three corrections through the product feedback store
(each bound to its `feedback.recorded` event), runs the paired probes, resets
feedback through the real lifecycle and repeats the reset probes. Every
invocation has its own idempotency claim. Reset preimages come only from ready
results; a failed reset probe leaves the reset unknown. A refused correction
or an exception ends only that episode: completed work is kept, the rest stays
`not_run` with a fixed reason, and later episodes still run. An interruption
(the caller's interruption check or a cancelled task) is checked before every
probe and correction. It ends the whole run: the current episode is recorded as
`cancelled`, and every later planned episode is `not_run` with reason
`interrupted`. Its evaluation-only action registration is never person or host
evidence, so the driver refuses `qualified`. Any dispatched probe, ready or
failed, must carry its exact offer; and every supplied reset document must be
canonical JSON for a planned episode, whether or not a reset is claimed.

**What is checked.**

- Host trial: the three arms share provider, model, sampling and provider
  configuration, and only the `recommendations` arm has a recommendation
  configuration. Task policies, permissions and budgets digests, asset
  bindings and rubrics bind to the freeze and the corpus. Rows equal the plan,
  and `not_run` rows carry no work. Success needs a host session and execution
  evidence. Attempt ordinals are consecutive and attempt IDs unique
  report-wide; attempt times compare as exact instants at full fractional
  precision. Each frozen package's bytes must equal its registry evidence.
  Totals are normalized only from evidenced token relationships. A started row
  with no external attempt has unknown, not zero, cost. A failed, timed-out or
  cancelled row counts as failed but keeps its own verdict: a null verdict
  stays unknown. Unknown verdicts or costs block, and a known positive cost
  difference fails.
- Adaptation: off and on are complete effective configurations, identical in
  every field (selector kind, name, version and endpoint included) except
  adaptation enablement, the on arm bound to the frozen adaptation
  configuration. Each arm's referenced configuration closure must resolve
  (missing preimages block). Corpus originals must all be available. Three chronological corrections; held-out
  paraphrase/control probes in `(kind, case_id)` order with recomputed grades;
  each dispatched probe's request and offered-catalog digests recomputed from
  the verified corpus and arm configuration; full planned denominators. The
  0.90 paraphrase target and the 0.02 control-loss limit are compared exactly.
  At most 100,000 planned observations across all episodes.
- Platform: steps `install` … `uninstall` exactly once and in order. A passed
  step needs receipts, and the dedicated operations/approvals references must
  equal registry entries.

**Current limits (no acceptance is reachable yet).** No reviewed
qualified-evidence verifier exists for these exports, so a qualified
acceptance report is graded `blocked`, never `pass`. A measured platform
report likewise blocks until it is bound to the `asset_operations` workload
grader and the actual Apple Silicon macOS / native Windows review. Genuine
episodes, paired host tasks and platform runs need real independent corpora,
hosts and hardware; fixture runs establish no measured result.

## Episode and host-task run kit (#236, #237)

These commands of `scripts/asset_whole_task.py` cover the operator steps
before and during the real adaptation-episode (#236; RM42/AC33) and paired
host-task (#237; RM40–RM44; AC31–AC36) runs. They do not supply genuine
people, labels, hosts or corrections, and every run they write is `fixture` or
`provisional`: there is no `qualified` option. Grading stays with `grade-host`
and `grade-adaptation`, which are already the one-command final grade.

| Command | What it does |
| --- | --- |
| `label-template --catalog K --cases C --annotator A --output T` | One annotator's blind template (section 12.6.1): only each case's prompt (`lane`, `task_text`, `conversation`, `requested_kinds`, `language_tags`, `code_switched`), bound to the cases and catalog digests. No labels, families, splits, authors, other judgments or algorithm outputs. The annotator must be a declared `annotator` who is not a `tuner`. |
| `label-worksheet --catalog K --cases C --judgments T1 T2 … --output W` | Checks the returned templates (unchanged binding and prompts, every case judged, exactly two distinct annotators per case) and writes the adjudicator's worksheet: both raw judgments, per-answer agreement and the recomputed agreement groups |
| `label-freeze --catalog K --cases C --judgments T1 T2 … --worksheet W --output L` | Assembles `AssetLabelsV1` from the raw judgments and the completed adjudications. Raw judgments cannot change in the worksheet, agreement is recomputed, and the unchanged release validator checks roles, coverage and inclusion |
| `preregister-host --tasks T --freeze F --output P` | Verifies the task set against its freeze and records the input, freeze and plan digests, task and lane counts, and acceptance eligibility (`kind_not_acceptance`, `below_minimum_tasks`, `missing_required_lanes`). AC36 pairs the whole tasks with the five required lanes, so a set without at least one task in each is not eligible; the spec states no larger per-lane host-task count |
| `preregister-episodes --episodes E --freeze F [--lock L --evidence-root R] --output P` | The same for `AssetEpisodesV1`. With the corpus it also binds the episodes to it and counts probes outside the `held_out` split, probes with no included adjudication and correction targets missing from the pool. Section 12.6.5 requires held-out probe sets, so any probe outside `held_out` makes the set ineligible. Reasons include `below_minimum_episodes`, `corpus_not_acceptance`, `corpus_not_frozen`, `probes_not_held_out`, `unlabelled_probes` and, without the corpus, `corpus_unverified` |
| `run-host --tasks T --freeze F --journal J --inputs M --environment E [--artifacts A] [--executor-timeout S] --qualification fixture\|provisional --output R -- PROGRAM ARGS…` | Executes the frozen plan through your host executor program and writes the `AssetHostReportV1` (see below). Exit `0` when every planned row has an outcome, `3` after an interruption |
| `capture-costs --events V --provider P --attempt-prefix X --evidence-id I [--token-relationship R --relationship-evidence I2] [--first-ordinal N] --output O` | Turns RelayMind trace events (a JSON array of TraceEvent objects) into `HostAttempt` objects for the external model calls of the provider arms |

Other exits: `2` rejected input, with a fixed reason on standard error. This
includes an output path that cannot be written safely, and `--evidence-root`
given without `--lock`.

**Blinding is a process.** Give each annotator only their own template and the
catalog. Only the adjudicator sees the worksheet. Separate files make blind,
independent judgment possible; they do not prove it. Custodian review of the
people, their competence and independence is still required (section 12.6.1).

**Run journal.** `run-host` (and `run_adaptation_episodes(..., journal=PATH)`
in Python) keeps progress in a private JSON-lines journal. It is not evidence,
but it holds rows and reset evidence, so it is created owner-only (`0600`, in a
new `0700` directory) and set back to `0600` on resume. Its header binds the
preregistered input, freeze and plan digests and the digest of the run's
execution identity: environment, artifacts, input manifest and qualification
label (for episodes, the corpus release lock instead of artifacts). A journal cannot resume with changed inputs or under another identity
(`journal_binding`), so rows run on one machine or package are never reported
under another; start a new journal for a new identity. Each row is recorded as
`started` before work begins and `finished` with its outcome, flushed to disk
each time.

- A rerun restores finished rows and never executes them again.
- A row that started but was never recorded (a crash) is exported as
  `cancelled` with reason `interrupted_unrecorded`. It has no attempts and a
  null verdict. It is not rerun, because its consumption is unknown, so the
  cost coverage of that arm stays unknown and the gate blocks.
- After SIGINT/SIGTERM, rows that never started are `not_run` (`interrupted`)
  and a later run executes them. A second signal stops at once, even inside a
  hung executor: its process group is killed, no report is written (exit `3`),
  and the started row is exported as `cancelled` (`interrupted_unrecorded`) on
  the next run.
- A journal with no complete record (a crash while its header was written)
  holds no started row and is initialized again.
- Only one runner can hold a journal at a time (`journal_locked`). A killed
  runner leaves `J.lock` behind. Check that no runner is still alive, then
  remove the lock.

For episodes, a finished episode is restored in whatever status it ended,
together with its reset evidence. An episode that started but was never
recorded is `cancelled` with reason `interrupted_unrecorded`.

**Executor program.** `run-host` calls the program once per planned row. On
standard input it gets
`{format:"relaymind.host_executor_request.v1",ordinal,task,arm,arm_config}`.
On standard output it must write `{row,evidence}`: one `HostRow` and its
`EvidenceRef`s. Each row is checked before it is recorded: plan identity,
evidence closure and conflicts, report-wide attempt IDs and the row rules
above. The row was recorded as started before the program ran, so a `not_run`
row is invalid. A row that fails these checks, or a nonzero exit or unusable output, is
recorded as `failed` with reason `executor_invalid_row` or `executor_error`,
with no attempts and a null verdict. The host's output is never repaired.
Only the host executes tools and grants approvals.

`--executor-timeout S` is optional and off by default. It is a harness bound,
not a task budget: a program still running after `S` seconds is killed and its
row is `failed` (`executor_error`) with unknown consumption, never a host
`timeout`. Set it above the frozen task budgets. The program leads its own
process group, and the whole group is killed; a descendant that leaves the
group (for example with `setsid`) is not, and a descendant still holding
standard output after the program exits makes the output unusable. Standard output is read
in bounded chunks; a program that writes more than the 256 MiB report bound is
killed and its row fails with `executor_error`.

**External cost capture.** For the `provider_only` and `recommendations` arms,
the RelayMind gateway records each external model call
(`model.started`/`model.finished`, actor `external`). `capture-costs` makes
one attempt per call:

- The provider-reported counts are copied with `usage_provenance: reported`.
  A missing count stays null, never zero, and `total_tokens` stays null.
- Each call keeps its own outcome. `cancelled` stays `cancelled`; the
  gateway's own bounds (`deadline_exceeded`, `time_budget`) and
  `provider_timeout` are `timeout`; other errors are `failed`.
- `calls` is `1` only when dispatch is evidenced: a successful call, a
  provider error or response, or `provider_timeout`. A gateway-side stop
  (`cancelled`, `deadline_exceeded`, `time_budget`, `interrupted`) or a
  transport error (`provider_unreachable`, which also covers a read error after
  streaming began) counts only if a token or at least one known usage count
  arrived. An unfinished call, or one never sent (`missing_credentials`), has
  null calls.
- `--events` and `--artifacts` are read with the same bounded strict reader as
  every other input: a symlink, an oversized file, duplicate keys or
  non-finite numbers reject.
- The relationship between counts is `unknown` unless you name evidence for
  it with `--relationship-evidence`.
- A finish without an earlier start of the same call (`trace_order`), or a
  repeated start or finish of one call (`trace_duplicate_call`), rejects the
  trace: a repeated receipt is never a new attempt.

Local model calls are not attempts. The native arm calls its provider
directly, so its usage must come from the host itself: this kit has no capture
for it.

## Operations and platform run kit (#242)

`scripts/asset_operations_run.py` prepares the real-world AC29 and AC37 runs
(#241, #235, #234). It does not perform them. Every report it writes is labeled
`fixture`.

- `startup` generates a bilingual catalog of `--assets` skills (default 300,
  the AC29 size; the count must be positive). It then starts a real
  `relaymind serve` process ten times
  through the existing AC29 startup driver (`measure_startups`) and records each
  process's memory: `ps` resident set size on POSIX, `tasklist` working set on
  Windows. `memory_counter` names the counter used.
  - **Base config.** `--base-config` is your own TOML for the store, skills,
    sandbox and model tables. A missing file is refused before the catalog is
    generated or any gateway is launched. Every path in it must be absolute:
    the kit writes the generated config into the run directory, so it refuses a
    relative path rather than let its meaning change. The kit adds `[server]`,
    `[recommendations]` and the catalog source itself, and refuses a base config
    that already has them.
  - **Readiness and logs.** A startup counts only once recommendations report
    `ready`. Each run directory keeps the gateway's `stdout.log` (without the
    token) and `stderr.log`; both start clean on every launch, so reusing
    `--work` never mixes separate runs.
  - **Completeness.** `complete` is true only when all ten startups were useful
    and every memory sample is known.
  - **Reused work and harness errors.** `work/catalog` is removed and generated
    again, so it holds exactly `--assets` skills. A launch that raises before
    its gateway is ready (it exits, its token never arrives, no port) is a
    harness error, named `startup_<ordinal>_<reason>` in `harness_errors`: the
    report is still written and the command exits 2. A launch that the
    one-second deadline cancels is a slow startup (`deadline_exceeded`), a
    recorded result. A gateway that would fail only after that deadline cannot
    be told apart from a slow one at this point.
- `operations` runs the lexical portion of the AC29 protocol on one machine, as
  a fixture (#241). It is not the whole AC29 run: spec 0.65 section 12.6 says
  lexical-only evidence does not close the protocol. Not measured here are
  all-phase worker and device memory coverage (the report covers only the
  gateway process), embedding, rebuild and inference contention, separate
  download, scan-detection and OpenClaw startup times, the committed
  300-asset acceptance catalog and the interactive host approvals. It runs ten
  clean startups from the generated `--assets` catalog (default 300), each with
  a new empty store unless you give `--base-config`. A reused `--work` is
  cleared first of the kit's own subdirectories (`catalog`, `config`,
  `imports`, `startups`, `baseline`, `workload`); an operator's
  `--base-config` store is never touched. It samples a gateway with
  recommendations disabled as the memory baseline. It then runs the existing
  60-second driver (`run_workload`): 120 slots at 2 requests per second,
  concurrency one, a derived lexical rebuild at the start, and four 25-record
  batches at 0, 15, 30 and 45 seconds.
  - **Batches.** Each batch is an incremental `metadata_import` file
    (`operations-import`) with 25 bilingual records and their original bytes,
    written next to the 300-skill `local_directory` source. Five probes per
    batch ask for its records by unique tokens. The expected identities are
    fixed before the run.
  - **Hooks.** The rescan and rebuild hooks reach the running gateway through
    `relaymind.evaluation.asset_operations_child`. This is the unchanged
    launcher plus one JSON command per standard-input line, which calls the
    gateway's own `AssetCatalogService.rescan` / `rebuild`. Each reply carries
    the stored job outcome and event chain, and `job_id`, the job the request
    awaited: the catalog owner runs one job at a time and answers a request
    when its job is recorded finished, so that is the latest-finished job of
    that source that was not finished when the request began. A periodic or
    repair job that started after it is listed but never bound. When no job or
    more than one qualifies, `job_id` is null with `job_missing` or
    `job_ambiguous`, recorded as the harness error `rescan_job_missing`,
    `rebuild_job_ambiguous` and so on. It is evaluation-only: it opens no
    socket and adds no endpoint.
  - **Memory.** Resident memory of the workload gateway is sampled every 250 ms.
    The sampled PID is the one the gateway reports for itself at startup
    (`Gateway-Pid:`), not the launcher's: on a native Windows venv the launcher
    is a separate process. `memory_samples` records `baseline_pid` and
    `workload_pid`. Only the gateway process is covered. Lexical
    recommendations start no child process or embedding device here, so the
    grader's memory increment stays unknown; the raw gateway-only numbers are
    kept in `memory_samples`.
  - **Phases.** `memory.phases` names only what was sampled. The workload
    samples cover rebuild, import and query. Startup is listed, and its samples
    join the peak, only when all ten startup samples are known; a startup that
    missed its deadline has none. `memory.complete` also needs the baseline and
    known workload samples.
  - **Report.** It holds every sample, the runner's environment
    (`collect_benchmark_environment`: OS, architecture, CPU, RAM, Python) and
    CI variables, the spec bounds for reading, and the unchanged
    `summarize_operations` result. It is always `fixture` / `not_applicable`.
  - **Exit codes.** A slow startup, a late batch or a failed request is a
    recorded result (exit 0). Exit 2 means a harness error: a startup, baseline
    or workload gateway that does not start or exits before readiness (for
    example `startup_3_gateway_exited` or `workload_gateway_exited`), a
    control call without a reply, a job the reply could not bind, or
    unavailable job evidence. A control reply of `job_evidence_unavailable`
    (the gateway could not read its job store) is recorded as
    `rescan_job_evidence_unavailable` or `rebuild_job_evidence_unavailable`,
    never as a refused rescan or rebuild. The report, with every sample already collected,
    is still written in that case.
  - **Default periodic scan.** The 30-second local scan stays at its default.
    On a loaded machine it can delay an import batch past 1,000 ms; that is
    recorded as observed.
- `platform` turns an install-check record from
  `scripts/openclaw_install_check.py`, plus an optional ordinary installed-host
  capture and operations report, into an `AssetPlatformReportV1` bound to the
  companion input manifest given in `--inputs`:
  - The input digest is the verified host-task digest from that manifest. Each
    original must be registered in it as `install-check` (or
    `ordinary-capture`) with matching bytes, and its registered path is used.
  - The observed `--platform` document must be a JSON object and must agree with
    the manifest's environment lock.
  - Install, configure and uninstall come from the lifecycle steps. A failed or
    timed-out step fails the group, and steps that never ran stay `not_run`. The
    record's own `remove-credential` step already requires every
    `after_removal` leftover to be clean.
  - Recommend, choose, feedback, delete and reset pass only when the
    installed-host qualification's own `ordinary_pass` accepts the capture.
  - Skill use stays `not_run` (`skill_use_not_observed`): a selection receipt is
    not observed instruction use (#239). MCP use stays `not_run`
    (`mcp_use_disabled`; spec 7.6.2), and so does restart
    (`restart_not_observed`): the lifecycle record has no restart step.
  - A capture of the Linux-only person-ingress harness taken on another
    platform (`linux_runner_required`) leaves the five ordinary actions
    `not_run` with `ordinary_harness_linux_only`.
  - A recorded ordinary product failure (`ordinary_product_failure`: the
    capture failed inside a recommendation UI action after setup completed,
    with a value or browser-timeout error) makes that action's step `failed`
    with reason `ordinary_<action>_failed`. `correct` and `inspect` map to
    feedback. The other ordinary steps stay `not_run`.
  - An operations report registered as `operations-report` becomes the
    dedicated `operations_evidence`.

```bash
python scripts/asset_operations_run.py startup --base-config /abs/base.toml \
  --work /tmp/ops --output startup.json
python scripts/asset_operations_run.py operations --work /tmp/ops-run \
  --label "my machine" --output operations.json
python scripts/asset_operations_run.py platform --install-check install-check.json \
  --ordinary ordinary.json --operations operations.json --inputs inputs.json \
  --platform environment.json --output platform.json
```

All three commands create the output's parent directory. A configuration, input
or write failure prints the reason and exits with code 2 instead of a traceback.
That includes a manifest whose environment lock is not a JSON object
(`environment_lock_invalid`).

The AC29 and AC37 numbers that count come only from the Apple M1 8 GiB macOS
and native x86-64 Windows machines; those runs remain open in #241, #235 and
#234.

### Platform runs on GitHub-hosted runners (#241, #235)

The separate workflow `.github/workflows/platform-runs.yml` (`Platform runs`)
runs the kit on `macos-latest` (Apple Silicon, arm64), `windows-latest` (native
x86-64 Windows) and `ubuntu-latest`. It starts by hand (`workflow_dispatch`)
or on a pull request that changes a file the run depends on: `src/relaymind`,
its scripts, `integrations/openclaw`, the default skills, the test helpers and
fixtures the bundle and the ordinary harness load, `pyproject.toml` and the
specification. Other pull requests stay fast. Each platform runs:

1. `asset_operations_run.py operations`, the lexical AC29 fixture run above,
   first, while the runner is quiet. Its exit code goes to `operations.exit`
   and the step passes, so a harness error never skips the steps below; the
   last step, `Harness result`, fails the job when that code is not 0;
2. the release artifact (`npm run pack:release`) and the pinned host's
   install-to-uninstall lifecycle (`scripts/openclaw_install_check.py`). A failed
   lifecycle step is kept in the export and its exit code in
   `install-check.exit`. Only a missing or unreadable export fails this step.
   The CI workflow's `openclaw-plugin` job still fails on it;
3. `asset_platform_run.py ordinary`, the ordinary installed-host qualification
   (recommend, choose, feedback, delete, reset). Its person-ingress harness
   needs a Linux x86-64 runner. On macOS and Windows its truthful
   `linux_runner_required` capture is kept, and the step passes. A recorded
   product failure of one UI action also passes the step; it becomes a failed
   platform step. A setup failure, a closed browser or a missing capture fails
   it. An earlier capture at `--out` is removed before the attempt, so only
   this attempt's capture is graded;
4. `asset_platform_run.py platform`, which writes the runner's `environment.json`,
   a fixture companion bundle locked to it, and `platform.json`. The bundle is
   built from the test fixtures of `tests/test_asset_whole_task_inputs.py`, not
   genuine host tasks. The bundle's `runner_inventory` is the S3 grader's own,
   closed to `src/relaymind`, `pyproject.toml` and
   `scripts/asset_whole_task.py`. The other files the platform run executes or
   loads are bound by the registered evidence `platform-runner-files` (path,
   SHA-256 and size of each): the workflow, `asset_operations_run.py`,
   `asset_platform_run.py`, `openclaw_install_check.py`, the ordinary
   installed-host harness with the action and person harnesses it is built on,
   and the test helpers the bundle and the harness import. The release tarball
   is bound by `release.json` and `SHA256SUMS`. Without an install-check record it writes
   `platform-not-built.json` (`install_check_missing`) instead, exits 2, and
   `verify` reports that record as a failure. An output directory that already
   holds a `bundle/` is refused (`platform_out_dir_exists`). Input and write
   errors, and an ordinary-step timeout (`ordinary_timeout`), exit 2 with the
   reason instead of a traceback;
5. `asset_platform_run.py verify`, which grades everything again with the
   existing graders. Each operations summary is recomputed by
   `summarize_operations` from the stored samples, and a report with any
   `harness_errors` fails. Each platform report is graded by `grade-platform`
   (it must be `not_applicable`), its `platform-runner-files` must equal this
   checkout's files, and its steps are derived again from the registered
   originals with this checkout's driver. A `platform.json` without its
   `bundle/` (`platform_bundle_missing`), or a bundle without its report
   (`platform_report_missing`), fails. The result also goes to the job
   summary.

The job fails only on a harness error; it never uses `continue-on-error`. A
slow runner, a late batch or a failed lifecycle step stays visible in the
reports and is never turned into a pass. Skill use and MCP use stay `not_run`
(spec 7.6.1/7.6.2: loaded use and MCP use remain disabled), and so does restart.

**What a runner report is.** Spec 0.65 section 12.6.5 says WSL, CI and Linux
do not satisfy the actual Apple Silicon macOS / named native x86-64 Windows
acceptance. Every report stays `fixture`, every platform gate `not_applicable`,
and #241 and #235 stay open. Two more limits apply. The catalog is the
generated development catalog, because the committed 300-asset acceptance
catalog (with 100 genuine MCP tools) does not exist yet. The interactive host
approvals are not exercised. A runner report is functional evidence about the
hosted platform only.

**Run and record.**

```bash
gh workflow run platform-runs.yml --ref <branch>
gh run watch <run-id>
gh run download <run-id> -D docs/evidence/<date>-asset-platform-runs-github-runners/<run-id>
python scripts/asset_platform_run.py verify \
  docs/evidence/<date>-asset-platform-runs-github-runners/<run-id>
```

`verify` prints one line per report and exits 1 when any report fails a grader,
records harness errors, is missing its other half, or none is found. Run it from a checkout at the run's commit
(`runner.variables.GITHUB_SHA` in `operations.json`; for a pull request this is
its merge commit). The inventory check compares the bundle with the running
grader.

**Committing evidence.** Each artifact is about 20 MB, mostly
`platform/bundle/originals/`, a copy of the commit's `src/relaymind`,
`pyproject.toml`, `scripts/asset_whole_task.py` and `docs/SPECIFICATION.md`.

- Commit everything else unchanged, in one dated directory per run: the
  reports, `install-check.exit`, `release.json`, `SHA256SUMS`, `verify.txt` and
  the rest of `platform/bundle/`. Leave out `originals/` and the gateway
  `*.log` files; the logs are uploaded only for diagnosis.
- To verify a committed copy again, restore the originals from the commit:
  `git archive <sha> src/relaymind pyproject.toml scripts/asset_whole_task.py docs/SPECIFICATION.md | tar -x -C <run>/platform/bundle/originals`.
- Add a short README with the run ID, the commit, the runner images
  (`runner.variables.ImageOS` / `ImageVersion`) and the `verify` output.
  Label it "GitHub-hosted runner functional evidence (fixture; not an
  acceptance platform)".

### Native loaded-skill observation tier (#239; RM39/AC30)

The ordinary installed-host harness at
`integrations/openclaw/test/host-ordinary-qualification/qualify.py` now includes
an opt-in evidence-only loaded-byte experiment (`--observe-loaded-use`, default off).
Run it with the same disposable Linux
person-ingress prerequisites and release tarball used by the ordinary host job:

```sh
PYTHONPATH=src:tests python integrations/openclaw/test/host-ordinary-qualification/qualify.py \
  --tarball /absolute/path/to/relaymind-provider.tgz \
  --work-dir /absolute/path/to/new-private-host \
  --observe-loaded-use \
  --out /absolute/path/to/ordinary-observation.json
```

The installed RelayMind provider sends native turns to a real loopback RelayMind
client-owned chat API. Its deterministic adapter proposes `skills_read` using
the selected skill's actually advertised name, then returns a fixed fixture
answer after receiving the native tool-result bytes. The gateway never executes
the read. The capture retains the native tool result, the receiving HTTP request,
its Idempotency-Key and X-RelayMind-Run-Id, the provider's independent response-attempt
records matched one-to-one by key and run ID, the browser `chat.send` acknowledgement
and terminal event, and the host session key, ID and lifecycle revision.
Each turn first obtains a native `sessions.messages.subscribe` receipt on that
browser socket and retains the receipt and socket ID; subscription, send, and terminal
evidence must share that socket. It uses a separate observer ID to preserve the UI's subscription. The
predicate recomputes SHA-256 from retained UTF-8 text and joins the native call
and receiving tool message by their call ID. Catalog text or a final marker alone
cannot pass. The retained selected SKILL.md text is capped at 256 KiB; each
request is read incrementally up to 1 MiB. Four requests plus one retained
overflow are allowed per turn, with a 24-record global ceiling. The observer
caps each serialized record at 1 MiB and its file at 8 MiB before appending.
Waits and generations are bounded. These are component retention limits, not
a size limit on the complete JSON export. Only an unterminated trailing event
line may be deferred, after one 50 ms re-read; malformed complete records are
fixed-reason harness failures.

Four isolated cases record a bridge-selected positive turn and native controls:
a changed library head after selection, an entry removed after selection, and
`sessions.reset`. Controls use supported native library/session RPCs on the same
authenticated browser connection; their direct activation is fixture setup, not
an additional qualified adapter path. The closed `observed` values distinguish
`loaded_pinned_revision`, `loaded_other_bytes`, `refused`, `not_loaded`, and
`control_refused`.
An unfinished capture uses `incomplete` and fails the observation predicate;
its already received tool/request evidence is retained. A tool-result request
without its asynchronous native hook stays `incomplete` after the bounded
catch-up wait; events are filtered by host run so a late prior event cannot
contaminate the next case. A fixed per-case failure reason preserves the
failure while later control turns still run. Controls retain the advertised
revision and can record a different syntactically valid revision as
`loaded_other_bytes`; only the positive proof requires the selected path.
A retained old pin is reported as observed even when the library head changed
or the entry was removed; it is never relabeled a successful rejection control.
On the pinned 2026.10.7 host `sessions.reset` requires `operator.admin`, which
the configured person policy does not grant (the native Control UI declares
that scope for its own reset call and otherwise uses `sessions.create`). Only
that exact native
`FORBIDDEN`/`MISSING_SCOPE` refusal, with the session unchanged, is recorded as
`control_refused`: no turn runs and any turn evidence fails the predicate. It is
a host outcome, not a rejection RelayMind performed (#255).

`ordinary_skill_loaded_use_pass` and the capture check
`ordinary_skill_loaded_use_observation` mean that the positive byte round trip
and control observations are complete. The capture always keeps
`qualification: unavailable`. This is not `skill_loaded_use` qualification,
autonomous model compliance, real-model capability, a platform acceptance
measurement, or full AC30 acceptance. The selection receipt still says
`instructions: not_observed`, and no production registry operation is enabled.
A later normative decision about stale/deleted pins and authority at use time
is still required. Already loaded conversation text cannot be unloaded by this
harness; each control uses a fresh session. This change supplies tooling and
unit/transport checks, not a newly measured native-host evidence record.

The ordinary command without `--observe-loaded-use` keeps its existing exit
criterion: native person composition, `ordinary_actions`, and `ordinary_skill`.
CI runs that qualification first, then a separate opt-in observation step with
a six-minute timeout and its own `openclaw-loaded-use-observation-ubuntu`
artifact. The second step exits nonzero for a harness/fixture failure (including
missing or inconsistent evidence), never because a complete truthful observation
is `refused`, `not_loaded`, or `loaded_other_bytes`, including on the positive
turn. No `continue-on-error` is used. The separate
`ordinary_skill_observation_complete` check validates the evidence without
requiring a positive loaded-byte outcome; `ordinary_skill_loaded_use_observation`
still requires the positive byte round trip. Neither check qualifies loaded use.

## Early installed-skill trial kit (#234)

The `trial-*` commands of `scripts/asset_whole_task.py` prepare the early
installed-skill trial, run by the owner as operator on the owner's Apple M1 8 GiB MacBook with
the packaged plugin (RM39, RM44; AC30, AC36; spec 0.65 section 12.6.5). They
wrap the existing frozen plan, conserving driver (`run_host_trial`) and
offline grader (`grade-host`) above; they add no other calculation. The
implementation is `relaymind.evaluation.asset_trial_kit`, so the runner
inventory already covers it. The trial itself still needs the owner's
machine session and frozen inputs; nothing here performs it or records a
result.

| Command | What it does |
| --- | --- |
| `trial-preflight --inputs M --tarball T --output P -- openclaw` | Checks the machine and the release, writes the record, and exits `0` only when every check passed, otherwise `3` |
| `trial-install --tarball T --work W --output I -- openclaw` | `plugins install npm-pack:T --force --accept-capabilities`, then `plugins inspect relaymind --runtime --json` |
| `trial-run --inputs M --observations O --bundle B --preflight P [--install I] [--tarball T] [--resolver X] --qualification fixture\|provisional [--interactive]` | Runs the frozen plan over row observations, bundles every file and grades the bundle |
| `trial --inputs M --tarball T --observations O --bundle B --work W [--resolver X] --qualification fixture\|provisional [--interactive] -- openclaw` | All of the above, in order |

**Preflight.** Seven checks, each `passed` or `failed`; an unknown value fails:

- `macos_arm64`, `apple_m1` and `memory_8gib`: the shared environment collector
  reports `Darwin`, `arm64`, CPU `Apple M1` and exactly 8 GiB of memory;
- `environment_lock`: every value in the manifest's `environment_lock` matches;
- `host_version`: `openclaw --version` names only `2026.10.7` (the plugin's
  `PINNED_OPENCLAW`), which the freeze's host and SDK versions also name; a
  suffixed version such as `2026.10.7-beta.2` or `2026.10.7.1` is another version;
- `release_checksum`: the tarball's SHA-256 is the one in `SHA256SUMS` and
  `release.json` beside it (`npm run pack:release`), and `release.json` pins
  that host;
- `frozen_package`: exactly one frozen package has the tarball's SHA-256 and
  size, and its version is the release version.

**Install.** The install changes the operator's actual host (the same command
as the operator guide), so it checks first and runs no host command that
changes anything when a check fails: a release that fails `release_checksum`
is rejected (`release_checksum`, exit `2`), and a host whose `--version` does
not name only the pinned version is blocked (`host_version`, exit `3`). Then
neither the work directory nor the record is written. Host standard output and standard error stay in
separate private logs (`install.log`, `install.stderr.log`, `inspect.log`,
`inspect.stderr.log`, each `0600`) under the work directory. The record keeps
only placeholders (`<openclaw>`, `<artifact>`), exit codes, SHA-256 digests
and closed inspect codes: `status` and `version` only when they are a plain
code or release version (otherwise `null`), `enabled`, and
`diagnostics_count`. Diagnostic text can carry paths, user names or tokens, so
it stays only in the private log, bound by `output_sha256`. Inspect passes
when the JSON on standard output (warnings on standard error are ignored)
shows the plugin `loaded` at the release version with no diagnostics. A missing or unreadable tarball fails `release_checksum` and
`frozen_package` in the preflight record; it does not stop the preflight.
Configuring each arm's frozen host configuration is the operator's step;
see the operator guide in `integrations/openclaw/README.md`.

**Row observations.** The kit writes `plan.json` to the bundle first. For each
planned ordinal it gives the task (`task_id`, `lane`, `prompt_case_id`), the
`arm`, the arm's `host_config_digest` and the observation file name
`row-NNNN.json`. The
operator runs that row on the host and saves
`{schema_version:1,ordinal,task_id,arm,row,evidence:[{id,file,provenance}]}`.
`row` is the section 12.6.5 `HostRow`, and each `file` is relative to the
observations directory. The rules:

- Every row is validated, and the bytes of every evidence file are hashed.
  The stated identity is not trusted.
- A file path that leaves the directory, an observation for another
  row/arm, or changed bytes under an already registered ID rejects (exit `2`).
- Evidence files keep the 10 MiB original-byte limit of the companion
  registry: a larger file rejects (`observation_evidence_too_large`), an absent
  one `observation_evidence_missing`.
- A row without an observation is exported `not_run` with reason
  `observation_missing`. With `--interactive` the kit asks first, and `skip`
  gives `operator_skipped`. If standard input closes, the kit stops asking:
  that row and every later row without a saved observation stay
  `observation_missing`. Ctrl-C (then Enter, at a prompt) exports the
  remaining rows `not_run` (`interrupted`).
- A provisional run refuses `fixture` evidence. Producing these observations
  from native session/run records is the operator's evidence obligation; the
  kit never infers a row from a gateway trace or a selection receipt.

**Bundle.** The bundle is a new directory, private to the operator
(directories `0700`, files `0600` on POSIX), because the frozen evidence is
`access: restricted`. It is built in a hidden staging directory beside it and
renamed into place only when complete; a rejected or blocked run leaves no
bundle. Output paths (`--bundle`, `--work`, `--output`) are resolved once, so
a path under a symlinked parent such as macOS `/tmp` is written to its real
directory. The bundle holds:

- copies of every document and evidence original in the frozen manifest, at
  the same relative paths;
- the collected evidence under `trial-evidence/<sha256>`;
- `preflight.json` and `install.json`, registered as `trial-preflight` and
  `trial-install`, and the tarball under its frozen package evidence ID;
- `plan.json`, `report.json` (an `AssetHostReportV1`), and a new companion
  `inputs.json` holding the frozen documents and the merged evidence registry;
- `grade.json`, from the unchanged `grade-host`;
- `SHA256SUMS` over every file, which `shasum -a 256 -c SHA256SUMS` can check.

The bundle grades again on its own with `grade-host --inputs B/inputs.json`.
`trial-run` may run on another machine: the report's environment is the one
recorded in `--preflight` (a preflight without one is rejected), never the
machine running the command. Manifest documents and evidence with a null path
are resolver-only: they stay in `inputs.json` as they are and are not copied.
A root document (tasks, freeze or environment lock) with a null path blocks
(`input_unresolved`).

**Qualification.** `--qualification provisional` checks the preflight and
install records before any bundle file is written. A `passed` label alone is
not accepted:

- each record must have exactly the closed shape the kit writes, otherwise it
  rejects (`preflight_record`, `install_record`; exit `2`);
- a well-formed failed preflight or install, or no install record, blocks
  (`preflight_failed`, `install_failed`; exit `3`);
- the target machine checks are recomputed from the recorded environment and
  the frozen lock, and the recorded host version must be the pinned one
  (`preflight_contradiction`); a passed install must agree with its own steps
  and inspection (`install_contradiction`);
- the recorded environment must be the report environment, the release must
  be exactly one frozen package with its evidence ID, the install archive must
  be that release, and `--tarball`, when given, those bytes
  (`preflight_binding`, `install_binding`, `package_binding`).

These checks bind the records to the frozen inputs; they cannot prove the
records were observed. The one-command `trial` keeps `preflight.json` and the
install logs in its private work directory even when the run is then blocked.
`fixture` runs anywhere. `qualified` is never produced. The tasks must have
kind `early_trial`; an acceptance task set is refused
(`early_trial_required`). An early trial grades `blocked` at best (exit `3`);
a provisional label over fixture inputs is a qualification contradiction and
rejects (exit `2`, #240). The provisional per-arm token and call totals in
`grade.json` are the baseline three-arm costs #234 asks for, as provisional
evidence only.

```bash
python scripts/asset_whole_task.py trial --inputs /abs/frozen/inputs.json \
  --tarball /abs/release/openclaw/relaymind-openclaw-provider-0.1.0.tgz \
  --observations /abs/trial/observations --bundle /abs/trial/bundle \
  --work /abs/trial/private-work --resolver /abs/frozen/resolver.json \
  --qualification provisional --interactive -- openclaw
```

**Still open in #234 (now #259):** the owner's machine session, the frozen trial
inputs (tasks, freeze, corpus, configurations and inventories), and genuine
row observations from native host records. Spec 0.66 section 12.6.6 names the
capture source (the host's persisted session record of provider-reported usage);
until it is captured, the kit takes observations as operator evidence. Native
host configuration validation (#240) remains open; the qualified-evidence
record and its check are in place (#251).

## Bilingual packaged walkthrough recorder (#238)

`integrations/openclaw/test/host-walkthrough/record.py` records the English and
Simplified Chinese walkthroughs on the installed host (RM39; AC30, AC35). It
reuses the ordinary installed-host qualification
(`host-ordinary-qualification/qualify.py` on the person-ingress harness)
without changing it: the same disposable Linux x86-64 host, the same packaged
tarball installed with the pinned CLI, the same signed-in person and the same
pass predicates. For each language it:

- opens every browser context with that locale (`en-US`, `zh-CN`) and records
  video;
- clicks the plugin's own Control UI labels for the language, read from
  `src/control-ui.ts`. An unknown label fails; it never falls back to English;
- takes a screenshot after every UI action and once each step has settled.

```bash
playwright install --with-deps --only-shell chromium
python integrations/openclaw/test/host-walkthrough/record.py record \
  --tarball /abs/relaymind-openclaw-provider-0.1.0.tgz \
  --work-root /abs/new-private-work --out-dir /abs/new-walkthrough \
  [--font-dir /abs/cjk-fonts]
```

The harness starts the browser with `HOME` inside the work directory, so it
sees only system fonts. On a host without a CJK font, the Chinese labels still
match but render as empty boxes. `--font-dir` copies the `.otf`, `.ttf` and
`.ttc` files of a directory once into the work root (`fonts/`), then installs
that copy into each isolated home's `$XDG_DATA_HOME/fonts` (Linux fontconfig)
as part of the install step, before the host is installed. The manifest lists
the names and digests of that copy under `fonts`, so they are the bytes every
language installed. Use a
work root whose path holds no user name, because the source locator and the
capture show it.

The output directory holds, per language, `capture.json` (the harness's
sanitized capture), `screens/`, `screens.json` and `video/`. `manifest.json`
lists every file as an evidence reference `{id,path,sha256,bytes,provenance}`,
the steps in order, the harness and recorder source digests, and `complete`.

| Step | Status rule |
| --- | --- |
| `open`, `recommend`, `choose`, `correct`, `inspect`, `delete`, `reset` | `recorded` when the harness's `ordinary_pass` accepts the capture and the step has a screenshot |
| `select_skill` (exact Skill Library revision for the next turn) | `recorded` when `ordinary_skill_pass` accepts the capture and the step has a screenshot |
| `skill_use` | always `blocked`: `skill_loaded_use_unqualified` (spec 7.6.1; #239) |
| `mcp_use` | always `blocked`: `mcp_use_unqualified` (spec 7.6.2; #204) |
| `mcp_approval` | always `blocked`: `host_mcp_approval_unqualified` (spec 7.6.2; AC35) |

If the predicate fails, the step named by the harness failure is `failed` with
the harness reason. Earlier steps with screenshots are `not_verified`, and
later ones are `not_run`. A screenshot alone never makes a step recorded. If a
language's child fails or times out (15 minutes), the recorder still writes
`manifest.json`. Each child runs in its own process session: on a timeout or
an interruption, and also when the child exits on its own (a crash included),
the recorder sends SIGTERM to whatever is left in the process group (so the
harness can stop its gateway and proxy), waits up to 15 seconds, then sends
SIGKILL, so no process left in that group keeps running. The manifest has
`complete: false`. It sets the language `reason` to
`child_failed` or `child_timeout`, and every outcome-dependent step to
`not_run` with `capture_missing`, or `capture_invalid` for a torn capture.
A repeated `--languages` value is refused before anything is created
(`languages_repeated`, exit `2`, no manifest).
Any partial files are still listed by digest, including every
`screens/*.png` that `screens.json` does not name (a killed child never saves
it). Such a screenshot belongs to no step. A torn `screens.json`, a listed file
that is missing or outside the language directory, and an unlisted
screenshot are named in the language's `media_problems` (`screens_invalid`,
`screenshot_missing`, `screenshot_path`, `screenshot_unlisted`, and
`capture_path`, `video_path` for unsafe capture or video files); any of them
makes the manifest incomplete. The command exits `0` only when
both languages are complete.

The tier is `functional_fixture`, with a deterministic gateway and disposable
fixture accounts. It is not an owner, human or live-model walkthrough and
not platform acceptance. Blocked steps are never attempted. Screenshots and
video can show fixture account names; keep them private until reviewed, and
never publish the work directory. On the owner's VPS (1 November 2026), the
pinned host's Control UI and the plugin's controls followed the `zh-CN` browser
locale. The recommendation detail block and the identity provider's sign-in
pages stayed in English, and the host's community banner stays visible because
the harness never dismisses it. An unknown label still fails the Chinese run
on that label rather than recording English screens.
The recorder is not part of CI. Its fixture tests
(`tests/test_openclaw_walkthrough.py`) run in the default `pytest`.
