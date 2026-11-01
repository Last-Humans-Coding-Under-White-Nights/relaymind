# Bilingual packaged walkthrough recorder (#238)

`record.py` records the English and Simplified Chinese walkthroughs of the
installed plugin's recommendation, choice, exact skill selection and
correction/privacy controls. It runs the ordinary installed-host qualification
(`../host-ordinary-qualification/qualify.py`) unchanged, once per language, with
locale-specific browser contexts, video and per-step screenshots, and writes
`manifest.json` with SHA-256 digests and evidence references.

Loaded skill use (#239), MCP use and host MCP approval (#204) are recorded as
`blocked` with fixed reasons and are never attempted. The tier is
`functional_fixture`, not a customer or human walkthrough.

Prerequisites, usage, step rules and limits:
[docs/ASSET_BENCHMARK.md](../../../../docs/ASSET_BENCHMARK.md#bilingual-packaged-walkthrough-recorder-238).
