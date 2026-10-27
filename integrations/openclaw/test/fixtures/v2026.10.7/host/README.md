# Host captures

This folder is for captures made with `scripts/capture_host_requests.mjs`
from a real OpenClaw v2026.10.7 install; the commands are in
[`../../../../README.md`](../../../../README.md#host-capture-operator).
None is stored here yet.

The first real-host capture was made on macOS with the probe's own capture
server and is kept, sanitized, as evidence in
[`docs/evidence/2026-10-27-openclaw-mac-probe/`](../../../../../../docs/evidence/2026-10-27-openclaw-mac-probe/REPORT.md)
rather than copied here; `tests/test_openclaw_host_captures.py` reads it
from there.

A host capture holds the host's own system prompt and tool catalog, so
review it before committing it: replace any local path or name the
capture script did not already replace with `<home>`.
