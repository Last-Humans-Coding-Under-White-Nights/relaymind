# Token relationship of the owner's external provider (evidence `usage-relationship`)

Spec 0.66 section 12.6.6, usage accounting. The external endpoint is
OpenAI-compatible (`openai-completions`). Its `usage` reports prompt tokens
including cached prompt tokens, and completion tokens including reasoning
tokens. The attempts therefore use
`token_relationship: cached_within_input_reasoning_within_output`.

Observed on this machine (smoke turns of 1 November 2026, before the freeze):

- The pinned OpenClaw 2026.10.7 host persists, for every assistant message,
  `usage.input` (prompt minus cached), `usage.cacheRead`, `usage.output`,
  `usage.reasoningTokens`, `usage.totalTokens` and `contextUsage.promptTokens`.
  In every native message `input + cacheRead == contextUsage.promptTokens` and
  `totalTokens == promptTokens + output`, and `reasoningTokens <= output`
  (for example 6836 + 896 = 7732 and 7732 + 139 = 7871, reasoning 55 of 139).
  The native attempt's `input_tokens` is that `promptTokens`.
- The RelayMind gateway records the provider's own counts in TraceEvent
  `model.finished.usage`; in every call `cached_tokens <= input_tokens` and
  `reasoning_tokens <= output_tokens` (for example 9728 of 10446 cached).

This is the operator's reading of these records, not a provider statement.
