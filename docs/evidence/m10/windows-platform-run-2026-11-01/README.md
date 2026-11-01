# Native Windows platform run — 1 November 2026

Issue #274; RM38, AC29, AC37; specification 0.66 sections 12.6.5–12.6.6.
Native AMD64 Windows, Python 3.12.10, Node 24.22.1, OpenClaw 2026.10.7.
`machine.json` records the machine identity with only a hashed hostname.

## Findings after review

- All 15 packaged lifecycle steps passed in the recapture.
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
  the memory observation as not complete, without a startup phase. The current harness reports such a capture as incomplete.
- The operations recapture has no harness errors. The earlier proxy-related
  attempt and its timing results are superseded by this clean capture.
- Ordinary ingress requires Linux. Skill use, MCP use and restart remain
  `not_run`. Generated catalog/companion inputs remain `fixture` and the
  platform gate remains `not_applicable`; final acceptance is not claimed.
- The host's private `default_model` is replaced with `<default-model>` by
  the lifecycle sanitizer before exporting any plain or JSON text.

## Evidence and reproduction

The repository keeps the platform report, operations summary and pending
qualification record. The complete reproducibility bundle is the release
asset linked in `archive.json`: `relaymind-windows-platform-2026-11-01.zip`.
Its exact length and SHA-256 are recorded there and in `SHA256SUMS`.
The plugin tarball checksum is unchanged because the plugin did not change.

Download the archive from the release, check its SHA-256, and extract it into
an empty directory. The archive contains the full operations/lifecycle
reports, companion inputs, grader output, exact harness source snapshots,
and `windows-platform-full-2026-11-01.json`, which binds every file by digest.
From this checkout with development dependencies installed:

```powershell
Get-FileHash relaymind-windows-platform-2026-11-01.zip -Algorithm SHA256
Expand-Archive relaymind-windows-platform-2026-11-01.zip -DestinationPath evidence
python scripts/asset_platform_run.py verify evidence
python -c "import hashlib, json, pathlib; d = pathlib.Path('evidence'); r = json.loads((d / 'windows-platform-full-2026-11-01.json').read_text()); print(all(hashlib.sha256((d / s['path']).read_bytes()).hexdigest() == s['sha256'] for s in r['subjects']))"
```

The committed qualification draft binds `platform.json`, the summary and the
archive identity, and loads with the current record schema. The full record
inside the archive binds the underlying files by digest, as the last command
checks, but it is kept as captured: its `machine` block has no `os_version`
and carries the CPU name with the trailing spaces Windows pads it with, so
`load_record` rejects it. The committed record takes `os_version` and the
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
harness files match the archived snapshots byte for byte. Review fields remain
empty. An organization member other than the owner must approve the PR.
The schema's `positive` platform-record category is not a passing gate.

## Tests

Tests first reproduced the wrong interpreter PID on both launch paths and
the default-model leak. Both PID tests now observe a 64 MiB allocation in the
sampled interpreter. Focused suite: 165 passed, 2 skipped. Lint and formatting
checks pass. Full-suite results are recorded in the PR description.
