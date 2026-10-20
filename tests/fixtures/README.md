# Test fixtures

Disposable sandbox data for the demo tool service and the test suite.
Nothing here is a production resource, and tests must never touch one.

Planned contents (spec section 9):

- Fixture state for the demo services `staging-api` and `payments-api`:
  health status, deployments, incident counts, metrics samples, log
  entries.
- Disposable demo database fixtures for the confirmed-mutation flow
  (`list_demo_databases` / `delete_demo_database`), with revisions for
  atomic compare.
- Fixture scenario metadata carried in tool outputs.

Fixtures are added together with the tests that use them.
