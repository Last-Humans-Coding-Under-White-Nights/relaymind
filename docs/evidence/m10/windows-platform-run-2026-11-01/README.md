# Native Windows platform run — 1 November 2026

Issue #274; RM38, AC29, AC37; specification 0.66 sections 12.6.5–12.6.6.
Native AMD64 Windows, Python 3.12.10, Node 24.22.1, OpenClaw 2026.10.7.
`machine.json` records the machine identity with only a hashed hostname.

## Findings after review

- All 15 packaged lifecycle steps passed in the recapture.
- Operations: 118/120 useful requests; service p95 about 31 ms, scheduled
  p95 about 62 ms; 3/4 timely batches. All ten startups missed 1,000 ms.
- Memory: baseline PID 31856 measured 67,166,208 bytes; workload PID 3496
  peaked at 79,749,120 bytes, an increase of 12,582,912 bytes (12 MiB).
  These are interpreter PIDs reported over the owned startup pipe, not
  Windows venv redirectors. Only gateway memory is covered; the full memory
  gate remains unknown. The previous 36,864-byte figure is invalid and
  superseded, not a second valid measurement.
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
python -c "from pathlib import Path; from relaymind.evaluation.asset_qualification_record import load_record; load_record(Path('evidence/windows-platform-full-2026-11-01.json'))"
```

The committed qualification draft binds `platform.json`, the summary and the
archive identity. The full record inside the archive verifies the underlying
files offline. The base repository revision and exact harness digest are
recorded; harness snapshots include the review fixes. Review fields remain
empty. An organization member other than the owner must approve the PR.
The schema's `positive` platform-record category is not a passing gate.

## Tests

Tests first reproduced the wrong interpreter PID on both launch paths and
the default-model leak. Both PID tests now observe a 64 MiB allocation in the
sampled interpreter. Focused suite: 165 passed, 2 skipped. Lint and formatting
checks pass. Full-suite results are recorded in the PR description.
