# Owner platform runs: MacBook and Ubuntu VPS — 1 November 2026

Issue #261 (the Windows PC part is #274); RM38, RM39, RM44; AC29, AC37;
specification 0.66 sections 12.6.5 and 12.6.6. Run commit
`2f4ddecc3da0efd632df89ed0d63f3cc1d72c00f`. The owner ran both machines as the
operator. Every report keeps its `fixture` label and its `not_applicable`
platform gate: the catalog and companion inputs are generated fixtures, so
nothing here is a measured acceptance result.

| Directory | Machine role | Identity (`machine.json`) |
| --- | --- | --- |
| `macbook/` | `target_macos_m1_8gib` | macOS 27.0.1 26A434, Apple M1, 8,589,934,592 bytes RAM, arm64 |
| `vps/` | `linux_host` (never a target platform) | Ubuntu 26.04.1 LTS 7.0.0-38-generic, Intel Xeon (Skylake, IBRS), 11,998,482,432 bytes RAM, x86_64 |

Hostnames are recorded only as SHA-256. Python 3.12.0 on both machines,
Node 24.22.1 (the pinned host's lower engine bound) for the packaged lifecycle.

## MacBook (`macbook/`)

**AC29 operations** (`operations.json`, generated 300-skill catalog). The run
started only after the 1-minute load average stayed below 4.0 for 30 seconds
(`load.json`: 2.24–3.10 during the run on 8 logical CPUs). The owner's usual
applications stayed open.

- Startups: **0/10 useful.** All ten hit the 1,000 ms deadline
  (`deadline_exceeded`) before a first lexical answer. A separate diagnostic of
  five more launches (`startup-diagnostic.json`, not graded) took 1,197–1,287 ms
  to report ready and 1,221–1,309 ms to the first answered request.
- Workload: 120/120 slots useful, none failed; service p95 46.6 ms, scheduled
  p95 74.5 ms (bound 300 ms).
- Import batches: **3/4 visible** within 1,000 ms (438, 365 and 401 ms). The
  30-second batch ended `batch_timeout`; its cause was not isolated.
- Memory (`ps` RSS, gateway process only): baseline 72,417,280 bytes, workload
  peak 80,789,504 bytes, increment 8,372,224 bytes. The grader keeps the
  memory increment unknown (only the gateway is covered). The startup memory
  samples are unknown because each launch ended at its deadline before a sample.
  On macOS, RSS does not count compressed pages, so it can understate memory on
  an 8 GiB machine.
- No harness errors. `verify` recomputes the same summary.

**Memory PID check.** The reviewer of #274 found that on Windows the kit can
sample a launcher process instead of the gateway. On this Mac the kit starts the
gateway directly (`.venv/bin/python` is a symlink, no launcher). A probe before
the run compared the sampled PID with the process listening on the gateway port:
for the startup, controlled-workload and baseline gateways they were the same
PID, with no child processes. During the run, `pid-watch.log` recorded every
child of the harness each second: the baseline and workload gateways were each
the listening process with no children (startup launches ended before they
listened). The kit's sampled PID is the gateway on macOS, so no code change was
made here.

**Packaged lifecycle** (`install-check.json`, exit 0 in `install-check.exit`):
all 15 steps through the pinned OpenClaw 2026.10.7 CLI passed (install,
configure, enable, inspect, token, discovery, text and tool exchanges, disable,
uninstall, and removal of the config and the credential). Release artifact
`relaymind-openclaw-provider-0.1.0.tgz`, SHA-256 `85512a7b…f67b6e2`
(`release.json`, `SHA256SUMS`), the same bytes as on the VPS and the #274
Windows run. A first lifecycle run with Node 26.13.0 also passed, but its
tarball was `6bd1d8e6…51b3b4`: identical extracted contents, different gzip
bytes. The reproducible-tarball claim holds per Node version, not across them.

**Ordinary actions** (`ordinary.json`): `linux_runner_required`. The
person-ingress harness runs only on Linux x86-64.

**Platform report** (`platform/platform.json`): install, configure and uninstall
passed. Recommend, choose, feedback, delete and reset are `not_run`
(`ordinary_harness_linux_only`). Skill use, MCP use and restart are `not_run`.
The operations report is bound as `operations_evidence`.

## Ubuntu VPS (`vps/`)

The code was copied as a git bundle of the run commit into a neutral work
directory and removed afterwards. The VPS has no GitHub credentials.

- Packaged lifecycle: all 15 steps passed (`install-check.json`, exit 0).
- **Ordinary actions harness: passed** (`ordinary.json`): recommend, choose,
  feedback, delete and reset through the installed host with disposable local
  identity-provider accounts. These are fixture accounts, not a human
  qualification, and the capture's own `production_adapter_qualified` check
  stays false. A first attempt from a `git archive` copy failed in
  `configuration_validation` because the harness reads `git rev-parse HEAD`. The
  git-bundle checkout fixed that; the failed attempt is not kept.
- Platform report: install, configure, recommend, choose, feedback, delete,
  reset and uninstall passed. Skill use, MCP use and restart are `not_run`. The
  grader adds `platform_not_target`, as section 12.6.6 requires for
  `linux_host`.

## Verify

`verify.txt` in each directory is the output of
`python scripts/asset_platform_run.py verify <dir>` on that machine. Both
directories were verified again from these committed copies, with the same
output. The companion bundle's `originals/` (a copy of the commit's sources) is
left out. To verify again, restore it from the run commit:

```bash
for m in macbook vps; do
  d=docs/evidence/m10/owner-platform-runs-2026-11-01/$m/platform/bundle/originals
  mkdir -p "$d"
  git archive 2f4ddecc3da0efd632df89ed0d63f3cc1d72c00f src/relaymind pyproject.toml \
    scripts/asset_whole_task.py docs/SPECIFICATION.md | tar -x -C "$d"
done
python scripts/asset_platform_run.py verify docs/evidence/m10/owner-platform-runs-2026-11-01/macbook
python scripts/asset_platform_run.py verify docs/evidence/m10/owner-platform-runs-2026-11-01/vps
```

## Redaction

Before any digest was computed, the work-directory paths in `operations.json`
(`source_locator`) and the VPS `ordinary.json` were replaced by `<work>`, and the
host model name leaking from the pinned host's system prompt
(`default_model=…`) in both `install-check.json` files was replaced by
`default_model=<redacted>`. The platform reports and bundles were built from the
redacted files. No home path, user name or hostname remains.

## Qualification records

`owner-platform-run-macbook-2026-11-01.json` and
`owner-platform-run-vps-2026-11-01.json` are pending `QualificationRecordV1`
drafts (scope `platform`, profile `owner_run`, labelling `not_applicable`). Each
binds its machine's `platform/platform.json` as the report, `machine.json`, and
every other committed file of that machine plus this README, by SHA-256. The
review fields stay empty until an organization member other than the owner
approves the pull request. `outcome: positive` is the schema's category for a
platform record. It does not mean a platform gate passed, and a pending record
lifts nothing.

## Acceptance catalog

The 100 genuine MCP tools of section 12.6.6 are now captured and frozen in
`evaluation/assets/acceptance-mcp-v1/`. These runs still used the generated
300-skill catalog. The complete 300-asset acceptance catalog (100 original
Chinese Matrix skills, 100 original English skills, these 100 tools, at least
25 reviewed equivalent pairs and the rights evidence) is not built yet.

## What remains open

- AC29 on the target MacBook: a run over the committed 300-asset acceptance
  catalog, the 1,000 ms startup bound (missed by about 200–300 ms here), the
  batch bound (one of four missed), and full memory coverage (workers, devices,
  embedding/inference contention). These need reports that are not fixture.
- AC37: an interactive allow/deny by a person in the host and a restart step.
  The lifecycle record has no restart step, and approvals were not exercised.
- Ordinary actions on the target MacBook: the person-ingress harness is
  Linux-only.
- Skill use and MCP use stay disabled (spec 7.6.1/7.6.2).
- Review and approval of both records by an organization member.
