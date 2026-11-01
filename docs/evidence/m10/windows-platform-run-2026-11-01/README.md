# Native Windows platform run — 1 November 2026

Issue #274; RM38, AC29, AC37; specification 0.66 sections 12.6.5–12.6.6.

Run on the owner's native AMD64 Windows PC with Python 3.12.10 and Node
24.22.1. `machine.json` records the OS build, CPU, RAM and hashed hostname.
The package uses the pinned OpenClaw 2026.10.7 host. No external inference
credentials or calls are required by these deterministic lifecycle checks.

The initial operations attempt is retained as `operations-initial.json`.
It recorded ten missed startup deadlines and baseline/workload timeouts.
The operations harness inherited Windows proxy settings for loopback HTTP.
A real-gateway regression test first reproduced the readiness timeout; the
harness now disables proxy inheritance for its local client.

The ordinary host capture records `linux_runner_required`. Skill use and
MCP use remain unobserved. The platform runner uses generated catalog and
companion fixtures: its `fixture` / `not_applicable` labels are preserved.
This run does not establish final AC29 or AC37 acceptance.

The qualification record is a pending draft. Review fields remain empty;
an organization member other than the owner must review the PR. Its
`positive` outcome is the schema's required platform-record category, not
a claim that the platform gate passed. Raw private work directories and
host logs are not published.

The clean rerun completed without harness errors: 115/120 useful requests,
service p95 about 31 ms, scheduled p95 about 63 ms, and 2/4 timely batches.
All ten startups missed the 1,000 ms deadline. Gateway-only memory increment
was 36,864 bytes; this does not establish the full memory acceptance gate.
`platform/` binds the initial attempt; `platform-rerun/` binds the clean rerun
and is the qualification draft's report.

Validation: the full six-worker suite recorded 8,328 passes, 42 skips,
18 failures and 16 setup errors. Installing Chromium and selecting Git Bash
resolved 32 affected cases. The other two exposed a Windows CRLF byte-offset
error in the retirement test fixture; using the actual file size fixed it.
The final affected-file run passed 535 tests with one skip. Lint and format
checks pass. The serial suite was restarted with six workers for throughput.
