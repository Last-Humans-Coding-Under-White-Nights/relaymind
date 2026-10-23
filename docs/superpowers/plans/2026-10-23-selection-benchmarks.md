# Selection benchmarks (#143, #144)

Implements RM02, RM08, RM09, RM25, RM28 and specification section 12.4.
The approved specification supplies the design. Keep benchmark evidence separate
from the ops corpus; never execute dataset tools or commit unlicensed ToolRet rows.

1. Add failing synthetic tests for immutable locks, set metrics, seeded catalogs,
   source derivation, tool normalization, and proposal validation.
2. Implement SkillsBench derivation with source attribution and leave-one-task-out
   examples; freeze the pinned public source and its derived cases.
3. Implement optional parquet derivation, stratified sampling, and ToolRet row locks.
4. Run built-in and HTTP selectors through RM28; export case-level failures,
   timings, diagnostics, and grouped scores. Add live full/narrowed proposals using
   the existing skill-stage prompt and adapters without invoking a tool handler.
5. Verify the synthetic suite and available real data; report live-service gaps
   explicitly. Run pytest and both Ruff checks before committing.
