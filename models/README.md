# Model artifacts

Pinned model artifact manifests (RM18, spec section 6). The GGUF files
themselves are not checked in; download them from the manifest's
`source_url` at `source_revision` and verify with
`relaymind.adapters.manifest.verify_artifact` before use. A hash
mismatch is an explicit failure, never a silent substitution.

Regenerate a manifest with `scripts/gen_model_manifest.py` (see
`scripts/README.md`).

| Manifest | Model | Status |
|----------|-------|--------|
| `qwen3-0.6b-q8_0.manifest.json` | Qwen3-0.6B Q8_0 | Candidate. Becomes the default only after the bilingual tool tests (spec section 6). |
