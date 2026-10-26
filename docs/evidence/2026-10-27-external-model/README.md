# External model comparison package

Raw evidence for the 2026-10-27 external model entry in
[`docs/EVIDENCE.md`](../../EVIDENCE.md).

| Archive | Status | Arm |
|---|---|---|
| `glm-5.3-flash/normal-local_first-router-buffered-corpus.tar.zst` | valid | glm-5.3-flash, default reasoning, revision `741a191` |
| `glm-5.3-flash/normal-local_first-router-buffered-latency.tar.zst` | invalid: the host slept with the lid closed from 10:38:57Z | same |
| `glm-5.3-flash/normal-local_first-router-buffered-latency-rerun.tar.zst` | valid | same |
| `glm-5.3-flash-low/normal-local_first-router-buffered-corpus.tar.zst` | valid | glm-5.3-flash, `reasoning_effort` low, revision `8f51ba1` |
| `glm-5.3-flash-low/normal-local_first-router-buffered-latency.tar.zst` | valid | same |

The glm-4.6 arm is the M8 cell pair `placement-normal-local_first-router-buffered-*`
in [`../2026-10-26-phase2/`](../2026-10-26-phase2/).

Each archive holds `export.json`, `checkpoint.json`, `export.md`, `runner.log`,
and every run store (with its WAL and SHM files) and sandbox state of the
attempt. `manifest.json` gives every archive's and member's SHA-256; packing
checked each member against the original after a full decompression.

```bash
zstd -d --long=27 -c <archive> | tar -x -C /some/empty/dir
```

Recorded paths stay as written at measurement time. No provider credential
appears in any file.
