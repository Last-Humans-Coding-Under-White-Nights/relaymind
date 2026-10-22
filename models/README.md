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
| `qwen3-0.6b-q8_0.manifest.json` | Qwen3-0.6B Q8_0 | Candidate. Becomes the default only after the English tool tests; Chinese results are reported but do not block the choice (spec section 6). |

This is the only qualified local artifact. AC08 asks for a swap to a
second qualified local model, so it stays open until a second manifest
is added here and qualified ([`docs/ROADMAP.md`](../docs/ROADMAP.md)).

## Runtime and reasoning controls

The manifest selects llama.cpp **b11785**, commit
`8baabd53b77d1c48c6ef8d3b620436292f3f6231`, with banner
`0.7.0-dev (build 11785, commit 8baabd53b)`. Download the matching
[release](https://github.com/ggml-org/llama.cpp/releases/tag/b11785):

| Platform | Archive | SHA-256 |
|----------|---------|---------|
| Apple Silicon macOS | `llama-b11785-bin-macos-arm64.tar.gz` | `55729a3b847d85c340b9083a936a3bbaa1c0ebfdfd0474b16e7bbd56485b28dc` |
| Native x86-64 Windows, CPU | `llama-b11785-bin-win-cpu-x64.zip` | `c59e0d3e31e096b728b94e943d3ca60df42912c34cba33489d520daab775c6fd` |

Extract the entire archive (the executable needs its accompanying libraries)
and put that directory first on PATH. `llama-server --version` must match
the manifest; the live smoke fixture rejects another build before loading
the model. On Windows, use a current x64 Visual C++ Redistributable.

Non-thinking launch settings are `--ctx-size 8192 --jinja --reasoning off
--reasoning-format deepseek`. `launch_adapter_settings` translates `off`
to the supported HTTP `chat_template_kwargs.enable_thinking=false`; `on`
translates to `true`. Other template kwargs are preserved. A manifest
that sets the deprecated `enable_thinking` CLI kwarg in launch settings is
rejected, with or without `reasoning`.

To enable thinking on the same server, configure `LlamaCppConfig` with
`chat_template_kwargs={"enable_thinking": True}`. This HTTP boolean remains
supported in b11785: only its command-line use is deprecated. Setting
`reasoning_effort` to a non-empty value alone does **not** enable thinking
after `--reasoning off`. Every adapter request selects
`reasoning_format="deepseek"`, keeping thoughts in `reasoning_content`,
which the adapter never emits as answer text.

These choices follow the selected build's
[server documentation](https://github.com/ggml-org/llama.cpp/blob/8baabd53b77d1c48c6ef8d3b620436292f3f6231/tools/server/README.md)
and [request parser](https://github.com/ggml-org/llama.cpp/blob/8baabd53b77d1c48c6ef8d3b620436292f3f6231/tools/server/server-common.cpp).
See [`docs/EVIDENCE.md`](../docs/EVIDENCE.md) for qualification of this
runtime. Earlier entries describe their recorded runtime only.
