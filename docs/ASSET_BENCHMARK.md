# Benchmarking skill and tool recommendation selectors

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

Current lexical/HTTP gateway primaries own no local runtime configuration.
Non-null runtime claims, dangling effective-config references, unused config
preimages and unsupported config schemas are refused in both comparison modes.
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
confidence intervals. Its builder/runner is separate work: the formal `run`
command above currently expects the §12.6.1 four-document lock, not a development
diagnostic file. Matrix is one source, not a fixed required count or an installed
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
