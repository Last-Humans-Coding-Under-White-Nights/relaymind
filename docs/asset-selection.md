# Recommend skills and tools

[简体中文](asset-selection.zh-CN.md) · [Source benchmark](SOURCE_BENCHMARK.md) ·
[Third-party protocol and formal benchmark](ASSET_BENCHMARK.md)

Recommendations discover and rank assets from your configured library. They do
not install packages, execute tools or approve mutations (RM34–RM44).

| I want to… | Start here |
| --- | --- |
| Bring my skills or MCP tools | [Local setup below](#start-with-your-own-library), [import format](TESTING.md#independent-asset-catalog-discovery-192-rm34ac25) |
| Build a replacement selector | [Full-offer v2 protocol](ASSET_BENCHMARK.md#the-skilltool-v2-boundary) |
| Prepare an external index | [Readiness below](#connect-a-team-selector), [complete lifecycle](ASSET_BENCHMARK.md#index-readiness-and-setup-cost) |
| Compare algorithms and languages | [Source workflow](SOURCE_BENCHMARK.md#run-identical-cases-and-compare), [formal profile](ASSET_BENCHMARK.md#built-in-team-a-and-team-b-on-the-same-fixture) |
| Choose a result | [Explicit choice below](#benchmark-and-choose) |
| Inspect storage or recover access | [Privacy and recovery](asset-privacy-security.md) |

## Choose the right interface

| | Managed selection v1 | Independent recommendations v2 |
| --- | --- | --- |
| Configuration | `[selection]` | `[recommendations]` and explicit asset sources |
| Example | `scripts/example_selector.py` | `scripts/example_asset_selector.py` |
| Purpose | Route a managed skill | Propose skills and MCP tools |
| Authority | Gateway rules still apply | No host execution or approval authority |

v2 is a separate contract, not a wire-compatible rename. Published schemas:
[request](../src/relaymind/selection/schemas/selection_request.v2.json),
[result](../src/relaymind/selection/schemas/selection_result.v2.json),
[asset record](../src/relaymind/store/schemas/asset_record.v1.json),
[prepare request](../src/relaymind/selection/schemas/asset_preparation_request.v1.json),
[prepare response](../src/relaymind/selection/schemas/asset_preparation_response.v1.json).

The flow is configured sources → committed lexical catalog → full-offer v2
request → gateway admission → shortlist/choice → separately qualified host use.
An indexed external selector is prepared explicitly before benchmark queries;
ordinary query dispatch does not probe or build its index.

## Start with your own library

From the checkout, install Python 3.12+ and the declared dependencies manually:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

On Windows activate `.venv\Scripts\Activate.ps1`; the commands below use a POSIX
shell and an explicit private directory. Choose equivalent local paths there.
Create `/var/tmp/relaymind-customer/ordinary-skills/greeting/SKILL.md`:

```markdown
---
name: Greeting Helper
description: Help write a short greeting in English or Chinese
metadata:
  version: "1.0"
  languages: [en, zh-CN]
  dependencies: []
---
Write a greeting in the requested language.
```

Save `/var/tmp/relaymind-customer/relaymind.toml`:

```toml
[server]
host = "127.0.0.1"
port = 8087
[store]
path = "/var/tmp/relaymind-customer/relaymind.sqlite3"
[external_model]
base_url = "http://127.0.0.1:1/v1"
model = "unused-by-retrieval"
context_limit = 4096
[recommendations]
enabled = true
user_id = "customer"
workspace_id = "local-library"
primary = "lexical"
total_ms = 500
primary_ms = 150
[[assets.sources]]
source_id = "my-skills"
kind = "local_directory"
path = "ordinary-skills"
```

The required model declaration is unused by retrieval; no local model, download
or external inference is needed for this recommendation-only example. Start from
the checkout: `relaymind serve --config /var/tmp/relaymind-customer/relaymind.toml`.
In another terminal, enter the printed application token without echoing it:

```bash
read -r -s -p 'Application token: ' RELAYMIND_TOKEN
printf '\n'
curl -sS http://127.0.0.1:8087/relay/v1/recommendations/readiness \
  -H "Authorization: Bearer $RELAYMIND_TOKEN"
curl -sS http://127.0.0.1:8087/relay/v1/recommendations \
  -H "Authorization: Bearer $RELAYMIND_TOKEN" -H 'Idempotency-Key: greeting-001' \
  -H 'Content-Type: application/json' \
  --data-binary '{"schema_version":1,"task_text":"Find a greeting skill","requested_kinds":["skill"],"selection_scope":"discovery"}'
```

Wait for readiness `state=ready, reason=usable_catalog`, not merely HTTP 200.
Read `displayed_candidates` and `result.attempts`; lexical matches require choice.
Reuse the key for the historical POST receipt; use a new key for a new task.
[GET polling/events and restart](TESTING.md#recommendation-json-lifecycle-194-rm36ac27)
use the returned recommendation ID. After restart use the new application token.

Local discovery reads a root SKILL.md or immediate child packages; it does not
execute instructions or dependencies. Names, paths, UTF-8/front matter and fixed
size caps are checked. [Explicit metadata imports](TESTING.md#independent-asset-catalog-discovery-192-rm34ac25)
also support your tools and other libraries, retaining original hashes, provenance
and source-owned removals. A source URL in metadata is not fetched automatically.
Local scans default to 30 seconds; failed refreshes retain stale last-good data.
Only an [installed qualified remote profile](TESTING.md#qualified-public-source-refresh-spec-059-rm34rm38rm43)
can refresh remotely. Discovery availability `unknown` does not mean installed.

## Connect a team selector

Start either example in a separate terminal, then use the
[generated production/preparation files](SOURCE_BENCHMARK.md#connect-an-external-selector-and-declare-readiness):

```bash
python scripts/example_asset_selector.py --port 8933
python scripts/example_asset_selector.py --port 8934 --preparation-path /index
```

Set the query URL and exact returned name/version in `[recommendations]`.
Both examples use `example_lexical/1`; bump identity when your implementation changes.
Every team gets the same complete ordered eligible offer, never target labels or
private whole skill bodies. The examples prove transport, not semantic quality.

Three facts differ: the gateway's lexical readiness, a listening HTTP server,
and the team's catalog-bound ready index. One configured preparation URL accepts
POST JSON `operation=prepare|status|cancel`, with the same ID/binding/profile.
Only prepare starts work; status/cancel have null catalog/budget. There are no
invented `/status` or `/cancel` routes. Binding includes catalog digest/revision,
scope, permissions, kinds and selector identity; queries must use its index revision.
A cancel acknowledgment is not proof of a ready index or completed native cleanup.

## Benchmark and choose

First follow [offline build/acquisition](SOURCE_BENCHMARK.md#acquire-and-reproduce-the-data)
and [config generation](SOURCE_BENCHMARK.md#connect-an-external-selector-and-declare-readiness).
Use a new private output root throughout if the example directory already exists.
The tutorial's complete builtin/stateless/indexed run recipes use these paths:

```bash
python scripts/source_asset_benchmark.py run \
  --catalog /var/tmp/rm-g1-source-guide/catalog-v2/catalog.json \
  --cases evaluation/assets/matrix-development/cases.v2.json \
  --evidence-root /var/tmp/rm-g1-source-guide/catalog-v2 \
  --production /var/tmp/rm-g1-source-guide/catalog-v2/builtin-production.toml \
  --track service --output /var/tmp/rm-g1-source-guide/builtin-service.json
python scripts/source_asset_benchmark.py compare \
  --left /var/tmp/rm-g1-source-guide/builtin-service.json \
  --right /var/tmp/rm-g1-source-guide/cold-service.json \
  --output /var/tmp/rm-g1-source-guide/comparison.json
python scripts/source_asset_benchmark.py choose \
  --report /var/tmp/rm-g1-source-guide/builtin-service.json \
  --case capability-01-zh > /var/tmp/rm-g1-source-guide/choice.json
```

Source compare writes internal **JSON only**. The separate
[formal workflow](ASSET_BENCHMARK.md#built-in-team-a-and-team-b-on-the-same-fixture)
uses locked inputs and `asset_selection_benchmark.py compare ... --output ... --markdown ...`.
Keep policy/track/cases/catalog equal; service fallback success is not primary success.
Cold/reuse are incompatible; failed required preparation preserves all slots as
not_run and exits nonzero. Unknown third-party costs stay unknown.

`choose` prints options to stderr; enter a number or `0` to decline, or Ctrl-C
to interrupt. Redirection saves a **new stdout receipt**, not a change to the run
report, backend feedback or host state. It says `action_executed=false`.
`ranked/choice_required` offers choice; confident never executes; no_match is a
terminal abstention. Failed/unready/timed-out primaries may use one configured
lexical floor within the remaining budget, with separate attempt evidence.

The [412-asset/128-case development profile](SOURCE_BENCHMARK.md#recorded-development-results)
includes skill/MCP, en→en, zh→zh, zh→en, en→zh and mixed queries, alternatives,
decoys, negative and ambiguous cases. Original and description languages are recorded separately;
missing MCP language combinations are explicit. The no-shared-Latin Chinese
memory-leak request still missed its original English target. No translation,
semantic-quality or independent-annotation pass follows from these diagnostics.
No external customer dataset or human panel is needed to start using the tooling.
Production semantic qualification and installed M10 host execution/approval/
correction walkthroughs are separate pending work; see [status](ROADMAP.md#m10--multilingual-asset-recommendations-spec-061).
