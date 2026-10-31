# Public GitHub skill-source acquisition

RM34/RM38/RM43; AC25/AC29/AC34. This is actual read-only acquisition evidence
for the proposed installed profile `github.relaymind-skills.v1`. It does not
activate a production source or establish a periodic product-refresh pass.

On 30 October 2026 the bounded probe made 17 serial unauthenticated HTTPS GETs
against the public GitHub repository
[`Last-Humans-Coding-Under-White-Nights/relaymind`](https://github.com/Last-Humans-Coding-Under-White-Nights/relaymind).
It captured six original SKILL.md files, their six optional manifests and the
root [MIT license notice](LICENSE), receiving 28,264 bytes in 4,167.79 ms.
Every captured file's declared length and Git blob SHA-1 were independently
verified, separately from its original-byte SHA-256. Existing ordinary-source
validation parsed all six packages; nothing was executed or installed.

[Observations](observations.json) record exact public endpoint/status/UTC,
response lengths/digests, immutable object identities, original lengths/hashes
and applicable permission evidence. Original raw API replies and skill bytes
remain outside Git in the capture directory; the committed report contains
no commit author/contact payloads, HTTP error bodies, credentials or private
user data. The license notice is preserved in full.

| Check | Actual observation |
| --- | --- |
| Protocol version | `X-GitHub-Api-Version: 2022-11-28` |
| Immutable commit | `849c1be66ad9caed985aa0a9e55d439d3bd3a17b` |
| Root tree | `9c387e752521c1173572261339fd7f29bb004192` |
| Selected `skills` tree | `576f891f61d333007447e15adce75bf84aba4574` |
| Completeness | Both relevant exports explicitly `truncated=false`; six selected immediate-child packages |
| Rights layout | Root MIT LICENSE only; no nested selected-scope license/NOTICE overrides |
| Rights bytes | 1,079 bytes; SHA-256 `a0df745636ded7b39d42761f4e5000fb0718c04e98be6b004bf00efdcd79509a` |
| Ref polling | Two actual ref observations separated by 704.62 seconds; observed `X-Poll-Interval: 300` |
| Ref movement | Not observed; both reads returned the same immutable commit |
| Product periodic refresh | Not run at this checkpoint; probe reads are not two scheduled product jobs |
| Product add/update/remove/outage/cancel/restart | Not run by this acquisition probe; deterministic server coverage belongs to implementation |
| Full acceptance | Not established: six demo skills are not a 300-asset corpus or platform/resource workload |

Run the developer probe with an output directory outside Git:

```bash
PYTHONPATH=src python scripts/probes/qualify_github_skill_source.py \
  --output /path/to/new-capture \
  --prior-capture /path/to/earlier-planning-capture
```

The optional earlier planning capture supplies verified prior ref bytes and
actual capture timing. It is evidence, not an activation input. Respect the
observed minimum before another poll. The probe accepts no arbitrary repository
or host and sends no ambient credentials. It disables redirects and environment
HTTP proxy/netrc behavior. Unknown installed runtime profiles remain denied.

The authoritative GitHub documentation covers [refs](https://docs.github.com/en/rest/git/refs?apiVersion=2022-11-28),
[commit objects](https://docs.github.com/en/rest/git/commits?apiVersion=2022-11-28),
[tree exports](https://docs.github.com/en/rest/git/trees?apiVersion=2022-11-28),
and [polling/rate limits](https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api).
These establish protocol behavior, not arbitrary repositories' rights. The
newer API version displayed in current documentation examples was not silently
substituted for the actually tested version.

Matrix market refresh remains independently unresolved. Its historical first
100-row response advertised 4,917 total records; 97 archive hashes matched,
three captures were excluded, and the 87-skill development import is partial.
The earlier acquisition took about 63.13 seconds. Those observations neither
fit a complete market traversal within the existing 1,000-candidate/60-second
job bounds nor establish permitted inventory/removal semantics. Local user
libraries and explicit metadata imports remain supported separately.

## Actual installed-profile periodic product run

The [versioned product report](product-demo.v1.json) records two real scheduled
jobs on 30 October 2026, using the ordinary production launcher/lifecycle and
installed profile. It used a new disposable store, actual monotonic/UTC clocks
and an explicit 300-second remote interval (the source floor, recorded deviation
from default 3,600). There was no transport/qualification injection, manual
rescan, fake clock, upstream write, inference-provider change or budget increase.
The existing source/derived worker triggered both jobs.

| Observation | First job | Second scheduled job |
| --- | --- | --- |
| Start UTC | 23:50:48.128808 | 23:55:52.410752 |
| Actual duration | 4,277.99 ms | 3,813.74 ms |
| Received HTTP body bytes | 28,264 | 28,264 |
| Published assets / revision | 6 / 1 | 6 / 1 |
| Lexical readiness / API query | ready / 200 | ready / 200 |
| Recommendation decision | choice_required | choice_required |

Job starts were 304.281944 actual seconds apart; the run completed after
308.34 seconds. The source commit and catalog were unchanged, so live
add/update/remove is `not_observed`. Both source jobs have stored budget,
batch and publication events. All assets remain availability unknown; discovery
neither installed nor executed them. No source outage was manufactured.

This report is explicitly provisional and not acceptance-eligible. It is a
six-skill functional demonstration, not the 300-asset contention/resource or
supported-platform acceptance protocol. Actual gateway requests used the ASGI
API path; this is not a TCP listener/latency qualification. The existing unused
external-model configuration was preserved, and lexical retrieval made no
inference calls; retrieval cost is not whole-host-task cost.

Executed v1 source/schema/script preimages and its complete disposable store
are retained locally beside the original report. Current candidate refinements
add precise partial-byte cancellation accounting, literal/NFC credential-echo
scrubbing, safe unencodable-header refusal, remaining-byte admission,
foreground-gated header persistence, current-cadence restart scheduling and
effective cadence/profile job config-digest binding. Those later repairs have separate RED→GREEN tests and
are not retroactively attributed to the v1 process. In particular, v1's queued
config digest predates the expanded job configuration closure; its separate
original production-config SHA-256 and preserved config still record the actual
300-second setting. The versioned actual observations are not rewritten into
new runtime qualification or a larger-corpus pass.

Controlled loopback HTTP-server tests separately exercise add/update/remove,
source failure/staleness/recovery, slow headers/body, shutdown while downloading,
restart after commit and foreground work during network wait. Those cases use
fixture generations and trusted test transport injection; they are not live
public-source changes, public outages or host qualification.
