# Phase 2 M7 and M8 evidence package

Raw and derived evidence for the 2026-10-26 entry in
[`docs/EVIDENCE.md`](../../EVIDENCE.md) and the default decisions of
specification 0.45, section 14.4.6 (#134, #128, #125).

## Contents

| Path | What it is |
|---|---|
| `manifest.json` | SHA-256 and size of every file below and of every archive member |
| `m8/plan.json`, `m8/index.json` | The fixed M8 plan and the final index, exact bytes |
| `m8/prior-normal.json`, `m8/prior-constrained.json` | The frozen calibration priors the candidate cells used |
| `m8/controller-before-*.json` | The index snapshot recorded when the separate controller took over (spec 14.4.5) |
| `m7/plan.json`, `m7/index.json` | The M7 plan and final index |
| `<matrix>/<cell>/<attempt>.tar.zst` | One archive per physical attempt, including the interrupted `initial` attempt of `placement-constrained-expected_wait-example-router-buffered-corpus` |
| `analysis/m8-analysis.json`, `.md` | Offline M8 analysis, analyst revision `074c0dd` |
| `analysis/m7-analysis.json`, `.md` | Offline M7 analysis, same revision |

M7 C0 and C5 reuse M8 attempts (`calibration-normal-local_first-router-buffered-corpus`
and `streaming-none-local_first-example-buffered-corpus`), so they have no
archive under `m7/`.

Each archive holds, for its attempt: `export.json`, `checkpoint.json`,
`export.md`, `runner.log`, and from `checkpoint.work/` every `runs.sqlite3`
with its `-wal` and `-shm` companions and every `sandbox_state.json`. The
per-case gateway configuration and logs, llama-server logs and lock files of
the work directories are not packaged. No archive member was rewritten:
packing checked each member's bytes against the original after a full
decompression.

## Verify and extract

```bash
shasum -a 256 m8/index.json            # compare with manifest.json
zstd -d --long=27 -c m8/<cell>/initial.tar.zst | tar -x -C /some/empty/dir
```

`--long=27` is required for decompression. Members extract under
`<matrix>/<cell>/<attempt>/...`, the same relative layout as the measured
root.

## Paths and replay

Raw checkpoints, exports and the index record the original absolute paths
(`/private/tmp/relaymind-phase2-measurements-2026-10-25/...`,
`/private/tmp/relaymind-m7-completion`) as provenance. They are never
rewritten. Reading, digest checks and the grading and metric code work on
extracted copies. An unchanged replay of the matrix controllers or analyzers
additionally needs the recorded checkout, interpreter and absolute prior
locations; with the files relocated, a replay is a derived analysis and must
say so.

No provider credential appears in any file; the gateway configuration only
names the environment variable that held it.
