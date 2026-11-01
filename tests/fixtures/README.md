# Test fixtures

Disposable sandbox data for the demo tool service and the test suite.
Nothing here is a production resource, and tests must never touch one.

- `sandbox/baseline.json`: the seed for `relaymind.tools.sandbox`
  (spec section 9). Scenario `baseline` for the demo services
  `staging-api` (healthy, one deployment) and `payments-api`
  (unavailable, no deployment): health, latest deployment, incident
  counts per window, metrics samples, log entries, two disposable demo
  databases, and the fixture `revision` they all report.
  `unreachable_tools` lists tools that simulate a failed tool contact.
  All values are synthetic.

`SandboxDataService` validates the seed against the published tool
output schemas, then copies it into its own working directory. Tests
give each service a fresh `tmp_path`, so state resets between tests, and
`delete_demo_database` only ever writes that working copy.

`openclaw-bundled-skills/` holds verbatim copies of five SKILL.md files
bundled with OpenClaw 2026.10.7 (`skills/` in the `openclaw` npm package),
with that package's MIT `LICENSE`. They test ordinary local discovery of
real host skills (spec section 4.6.1, RM34/AC25); their SHA-256 values are
pinned in `tests/test_asset_sources.py`.
