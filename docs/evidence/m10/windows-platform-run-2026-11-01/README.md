# Native Windows platform run — 1 November 2026

Issue #274; RM38, AC29, AC37; specification 0.66 sections 12.6.5–12.6.6.
Native AMD64 Windows, Python 3.12.10, Node 24.22.1, OpenClaw 2026.10.7.
`machine.json` records the machine identity with only a hashed hostname.

## Known defects in the raw captured files

`machine.json`, `platform.json` and `verify.txt` are raw captured output,
bound by digest in the release archive, and are kept byte for byte:

- `machine.json` has no `os_version`, and its `cpu_model` carries the 14
  trailing spaces Windows pads the CPU name with. The committed review record
  therefore cannot copy it verbatim: it takes `os_version` and the trimmed CPU
  name from `platform.json`.
- `platform.json` declares `input_manifest.path` as `inputs.json`, which does
  not exist beside it; see "Evidence and reproduction" below.
- `verify.txt` starts with a UTF-8 byte order mark (`EF BB BF`), written by
  the Windows PowerShell capture. Read it as `utf-8-sig`. This README and the
  other files authored here have no byte order mark.

## Findings after review

- All 15 install-check lifecycle steps passed in the recapture. The separate
  11-step platform report has three passed steps and eight `not_run` steps.
- Operations: 118/120 useful requests; service p95 about 31 ms, scheduled
  p95 about 62 ms; 3/4 timely batches. All ten startup launches failed the
  1,000 ms gate before a first useful answer (`deadline_exceeded`, no
  request attempted); no startup duration was measured, so elapsed time is
  unknown, not a latency above 1,000 ms.
- Memory: baseline PID 31856 measured 67,166,208 bytes; workload PID 3496
  peaked at 79,749,120 bytes, an increase of 12,582,912 bytes (12 MiB).
  These are interpreter PIDs reported over the owned startup pipe, not
  Windows venv redirectors. Only gateway memory is covered; the full memory
  gate remains unknown. The previous 36,864-byte figure is invalid and
  superseded, not a second valid measurement.
- Startup memory has no samples: all ten `startup_bytes` entries are null.
  The archived operations report still lists `startup` among the memory
  phases and says `complete: true`, and `operations-summary.json` repeats
  the phase list, because the harness used for this capture set both
  unconditionally. Both files are digest-bound and kept as captured; read
  the memory observation as `complete: false`, with only `rebuild`, `import`
  and `query` phases. The summary's `incremental_bytes` and `passes` remain
  null; its phase list does not prove startup sampling. The current harness
  reports such a capture as incomplete.
- The operations recapture has no harness errors. The earlier proxy-related
  attempt and its timing results are superseded by this clean capture.
- `recommend`, `choose`, `feedback`, `delete` and `reset` are `not_run`
  (`ordinary_harness_linux_only`). `skill_use`, `mcp_use` and `restart` also
  remain `not_run`. Generated catalog/companion inputs remain `fixture` and the
  platform gate remains `not_applicable`; final acceptance is not claimed.
- The captured lifecycle redacts the host's private `default_model` as
  `<default-model>`. The sanitizer now also covers colon and spaced-equals
  assignments, double/single/backtick-quoted values, JSON string forms,
  single-quoted keys (Python, JS and YAML forms) and values containing
  escaped quotes, of both `default_model` and `defaultModel`.

## Evidence and reproduction

The repository keeps the platform report, operations summary and pending
qualification record. The historical capture bundle is the release
asset linked in `archive.json`: `relaymind-windows-platform-2026-11-01.zip`.
Its exact length and SHA-256 are recorded there and in `SHA256SUMS`.
The plugin tarball checksum is unchanged because the plugin did not change.

Download the archive from the release, check its SHA-256, and extract it into
an empty directory. The archive contains the full operations/lifecycle
reports, companion inputs, grader output, exact harness source snapshots,
and the capture file `windows-platform-full-2026-11-01.json`, which binds
every file by digest but is not a loadable qualification record.
Use a separate clean checkout at the exact reproduction commit
`8d406e943d76d4d67c2adc8bf7ae9a124ccae270`, with Python 3.12 and development
dependencies installed for that checkout. Place the downloaded zip in its
root, then run:

```powershell
git switch --detach 8d406e943d76d4d67c2adc8bf7ae9a124ccae270
$env:PYTHONPATH = "src"
Get-FileHash relaymind-windows-platform-2026-11-01.zip -Algorithm SHA256
Expand-Archive relaymind-windows-platform-2026-11-01.zip -DestinationPath evidence
python scripts/asset_platform_run.py verify evidence
python -c "import hashlib, json, pathlib; d = pathlib.Path('evidence'); r = json.loads((d / 'windows-platform-full-2026-11-01.json').read_text()); print(all(hashlib.sha256((d / s['path']).read_bytes()).hexdigest() == s['sha256'] for s in r['subjects']))"
```

This reproduces the archived `verify.txt` result only with the captured
harness tree. The committed record's `repository_commit` is the run head
`52492ee4e16035d997cbe15fa05d928ebedce3e4`, whose clean tree fails verification
with `unsupported_generator`; it lacks the captured dirty-tree harness
changes. PR head `05957386` fails with `inventory_membership` after later
source changes. Offline checks with Python 3.12 reproduced both failures and
the exit-0 result at `8d406e94`. Python 3.13 uses a different Unicode data
version and cannot reproduce these Python 3.12 companion inputs.

The report's declared `input_manifest.path` is also defective: `inputs.json`
does not exist beside either committed or archived `platform.json`. The
actual archived manifest is `platform/bundle/inputs.json` (14,143 bytes,
canonical digest `c9fa3c38ec05282735d6b5e01580ce63837e53e009ff82ba654a15a8933397cd`).
The verifier supplies it explicitly via `--inputs`; its pass does not validate
the report-relative locator required by spec section 12.6.2. Direct resolution
of that locator fails. The digest-bound report and archive remain unchanged;
use the archived companion bundle explicitly for this historical check.

The committed qualification draft binds `platform.json`, the summary and the
archive identity, and loads with the current record schema. The capture file
inside the archive binds the underlying files by digest, as the last command
checks, but it is kept as captured: its `machine` block has no `os_version`,
carries the CPU name with the trailing spaces Windows pads it with, and its
`adapter_revision` is a branch name, so `load_record` rejects it. A fresh
extraction does not pass the qualification loader; only the committed draft
loads. The committed record takes `os_version` and the
trimmed CPU name from `platform.json`; the raw `machine.json` is unchanged.

The run was made from a dirty working tree. The lifecycle evidence records
`relaymind_revision.head` 52492ee4e16035d997cbe15fa05d928ebedce3e4 with
`dirty: true`; that commit sits on 4c1ce6e4d3f456f1a08955fd781f5adede9e1ce8,
which the archived full record names as `repository_commit`. The committed
record names the run head 52492ee instead; the record schema has no field for
the dirty state. Its `harness_sha256` (d056dd09…) is the archived snapshot of
`src/relaymind/evaluation/asset_operations_run.py`, which differs from that
file at both 4c1ce6e and 52492ee. The uncommitted harness changes were
committed afterwards as 8d406e943d76d4d67c2adc8bf7ae9a124ccae270, whose three
harness files match the archived snapshots byte for byte. The archived full
record names the branch `m10-control-ui-l10n` as `adapter_revision`; a branch
moves and identifies no revision, and the native adapter revision of the
measured tarball could not be recomputed, so the committed record leaves
`adapter_revision` null. The measured adapter is the tarball with SHA-256
85512a7bba21273180c63d03c07ecd0db5a976515cab97392ffc3f497f67b6e2 in
`release.json` and `SHA256SUMS`. Review fields remain
empty. An organization member other than the owner must approve the PR.
The schema's `positive` platform-record category is not a passing gate.

## Tests

The earlier review tests first reproduced the wrong interpreter PID on both
launch paths and the default-model leak. Both PID tests observe a 64 MiB
allocation in the sampled interpreter. That review reported 165 passed and
2 skipped in its focused suite; its full-suite results are in the PR
description. Follow-up sanitizer tests first reproduced 44 failures across
plain-text and JSON-encoded assignments, then passed after the fix; the
existing non-string JSON `null` case stays unchanged.
